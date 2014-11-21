#!/usr/bin/env python
############################################################################
#                                                                          #
# Copyright 2014 Prelert Ltd                                               #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License");          #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#    http://www.apache.org/licenses/LICENSE-2.0                            #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
#                                                                          #
############################################################################

'''
Script to pull metric data from AWS CloudWatch and analyze it in
the Prelert Engine API. There are 2 modes of operation: historical
where stored metric data is extracted between 2 dates and a continous
realtime mode where the preceeding few minutes of data is queried in
a loop.

The path to a configuration file containing the AWS connection parameters
must be passed to the script the file should have the following propteries:

    region=REGION
    aws_access_key_id=YOUR_ACCESS_ID
    aws_secret_access_key=YOUR_SECRET_KEY

Where REGION is one of us-east-1, eu-west-1, etc

If the --start-date parameter is set then this will query historical data
from CloudWatch until --end-date or the current time if --end-date is not
set. Otherwise the script will run in an infinite loop pulling realtime
data, use Ctrl-C to quit the realtime mode as the script will catch
the interrupt and handle the exit gracefully.

To create a job with a specific ID use the --job-id argument. If the
job already exists data will be sent to that job otherwise a new job
with the ID is created. If no job ID is specified one will be automatically
generated by the API

Only EC2 metrics are monitored and only those belonging to an instance.
Aggregated metrics by instance type and AMI metrics are ignored.

Usage
    python cloudWatchMetrics.py awskey.conf

    python cloudWatchMetrics.py --job-id=cloudwatch --start-date=2014-10-01 awskey.conf
'''

import argparse
import ConfigParser
from datetime import datetime, timedelta, tzinfo
import json
import StringIO
import time

import boto.ec2
import boto.ec2.cloudwatch
from boto.exception import BotoServerError

from prelert.engineApiClient import EngineApiClient


# Prelert Engine API default connection prarams
API_HOST = 'localhost'
API_PORT = 8080
API_BASE_URL = 'engine/v1'

''' Interval between query new data from CloudWatch (seconds)'''
UPDATE_INTERVAL=300

''' Interval between data points that are being fetched from CloudWatch (seconds)'''
REPORTING_INTERVAL=60

''' In realtime mode run this many seconds behind realtime '''
DELAY=600

'''
CloudWatch imposes a limit to the number of data points a query can return.
The limit is currently 1440, allowing e.g. a daily query with a reporting interval
of one minute (a day has 1440 minutes).
'''
MAX_DATAPOINTS_PER_QUERY = 1440

'''
    Prelert Engine job configuration.
    Multiple detectors configured one for each metric by the instance id.
'''
JOB_CONFIG = '{ %s\
                "analysisConfig" : {\
                    "bucketSpan":' + str(UPDATE_INTERVAL) + ',\
                    "detectors" :[\
                        {"function":"mean", "fieldName":"DiskReadOps", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"DiskReadBytes", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"DiskWriteOps", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"DiskWriteBytes", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"NetworkIn", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"NetworkOut", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"CPUUtilization", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"StatusCheckFailed", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"StatusCheckFailed_Instance", "byFieldName":"instance"},\
                        {"function":"mean", "fieldName":"StatusCheckFailed_System", "byFieldName":"instance"}\
                    ]\
                },\
                "dataDescription" : {"format":"JSON","timeField":"timestamp","timeFormat":"yyyy-MM-dd\'T\'HH:mm:ssX"\
                }\
            }'



class MetricRecord:
    '''
    Simple holder class for the CloudWatch metrics.
    toJsonStr returns the metric in a format for the job
    configuration above.
    '''
    def __init__(self, timestamp, instance, metric_name, metric_value):
        self.timestamp = timestamp
        self.instance = instance
        self.metric_name = metric_name
        self.metric_value = metric_value

    def toJsonStr(self):
        result = '{"timestamp":"' + self.timestamp.isoformat() + \
            '", "instance":"' + self.instance + '", "metric_name":"' + \
            self.metric_name + '", "Average":' + str(self.metric_value) + '}'

        return result


class UTC(tzinfo):
    ''' UTC timezone class '''
    def utcoffset(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return timedelta(0)

def replaceTimezoneWithUtc(date):
    return date.replace(tzinfo=UTC())


def parseArguments():
    parser = argparse.ArgumentParser()

    parser.add_argument("config", help="The AWS connection parameters.")

    parser.add_argument("--api-host", help="The Prelert Engine API host, defaults to "
        + API_HOST, default=API_HOST, dest="api_host")
    parser.add_argument("--api-port", help="The Prelert Engine API port, defaults to "
        + str(API_PORT), default=API_PORT, dest="api_port")

    parser.add_argument("--job-id", help="Send data to this job. If not set a \
        new job will be created.", default=None, dest="job_id")

    parser.add_argument("--start-date", help="Request data from this date. If not \
        set then run in realtime mode. Dates must be in YYYY-MM-DD format",
        default=None, dest="start_date")
    parser.add_argument("--end-date", help="if --start-date is set then pull \
        and analyze only the metric data between those dates. \
        If --start-date is not set this argument has no meaning. \
        Dates must be in YYYY-MM-DD format",
        default=None, dest="end_date")

    return parser.parse_args()

def calculateIntervalBetweenQueries(reporting_interval):
    '''
    For querying historic data, we can improve the performance by
    minimising the number of queries we fire against CloudWatch.
    CloudWatch allows a query spanning a day given the reporting
    interval is a minute. Thus, we return the product of a the
    max number of data points and the reporting interval in seconds
    '''
    return timedelta(seconds = MAX_DATAPOINTS_PER_QUERY * reporting_interval)


def queryMetricRecords(metrics, start, end, reporting_interval):
    '''
        Return the metrics sorted by date.
        The Average statistic is always taken
    '''
    metric_records = []
    for m in metrics:
        if 'InstanceId' not in m.dimensions:
            continue
        instance = m.dimensions['InstanceId'][0]

        datapoints = m.query(start, end, 'Average', period=reporting_interval)
        for dp in datapoints:
            # annoyingly Boto does not return datetimes with a timezone
            utc_time = replaceTimezoneWithUtc(dp['Timestamp'])
            mr = MetricRecord(utc_time, instance, m.name, dp['Average'])
            metric_records.append(mr)


    metric_records.sort(key=lambda r : r.timestamp)
    return metric_records


def transposeMetrics(metrics):
    '''
    Convert a list of metrics of the form
    {time_1, instance, metric_A, Average},
    {time_1, instance, metric_B, Average},
    {time_1, instance, metric_C, Average}
    {time_2, instance, metric_A, Average}
    ...

    To a single record so that there is 1 record for each time period.
    {time_1, instance, metric_A, metric_B, metric_C}
    {time_2, instance, metric_A, metric_B, metric_C}

    The input list must be ordered by timestamp
    '''

    tranposed_metrics = []
    current_record = None;
    current_time = datetime.fromtimestamp(0, UTC());

    for metric in metrics:

        if current_time < metric.timestamp:
            current_time = metric.timestamp

            if current_record != None:
                tranposed_metrics.append(current_record)
            current_record = dict()
            current_record['timestamp'] = metric.timestamp.isoformat()
            current_record['instance'] = metric.instance

        current_record[metric.metric_name] = metric.metric_value

    return tranposed_metrics


def runHistorical(job_id, start_date, end_date, cloudwatch_conn, engine_client):
    '''
    Query and analyze the CloudWatch metrics from start_date to end_date.
    If end_date == None then run until the time now.
    '''

    end = start_date
    delta = calculateIntervalBetweenQueries(REPORTING_INTERVAL)

    while True:

        start = end
        end = start + delta
        if (end > end_date):
            end = end_date

        if start == end:
            break

        print "Querying metrics starting at time " + str(start.isoformat())

        try:
            metrics = cloudwatch_conn.list_metrics(namespace='AWS/EC2')
            metric_records = queryMetricRecords(metrics, start, end, reporting_interval = REPORTING_INTERVAL)

            tranposed_metrics = transposeMetrics(metric_records)

            data = ''
            for met in tranposed_metrics:
                json_str = json.dumps(met)
                data += json_str + '\n'

            (http_status, response) = engine_client.upload(job_id, data)
            if http_status != 202:
                print "Error uploading metric data to the Engine"
                print http_status, json.dumps(response)

        except BotoServerError as error:
            print "Error querying CloudWatch"
            print error


def runRealtime(job_id, cloudwatch_conn, engine_client):
    '''
    Query the previous 5 minutes of metric data every 5 minutes
    then upload to the Prelert Engine.

    This function runs in an infinite loop but will catch the
    keyboard interrupt (Ctrl C) and exit gracefully
    '''
    try:
        delay = timedelta(seconds=DELAY)
        end = datetime.utcnow() - delay - timedelta(seconds=UPDATE_INTERVAL)
        end = replaceTimezoneWithUtc(end)

        while True:

            start = end
            end = datetime.utcnow() - delay
            end = replaceTimezoneWithUtc(end)

            print "Querying metrics from " + str(start.isoformat())  + " to " + end.isoformat()

            try:
                metrics = cloudwatch_conn.list_metrics(namespace='AWS/EC2')
                metric_records = queryMetricRecords(metrics, start, end, reporting_interval = REPORTING_INTERVAL)
                tranposed_metrics = transposeMetrics(metric_records)

                data = ''
                for met in tranposed_metrics:
                    json_str = json.dumps(met)
                    print json_str
                    data += json_str + '\n'


                (http_status, response) = engine_client.upload(job_id, data)
                if http_status != 202:
                    print "Error uploading metric data to the Engine"
                    print http_status, json.dumps(response)

            except BotoServerError as error:
                print "Error querying CloudWatch"
                print error

            now = datetime.utcnow()
            now = replaceTimezoneWithUtc(now)
            duration = now - delay - end
            sleep_time = max(UPDATE_INTERVAL - duration.seconds, 0)
            print "sleeping for " + str(sleep_time) + " seconds"
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print "Interrupt caught... terminating real time queries"
        return


def createJob(job_id, client):
    '''
    Create the job. If job_id == None then create the job with
    a default Id else use job_id. If the job already exists
    return job_id and continue.

    Returns the created job_id or None if the job could not
    be created.
    '''

    # if no job id create a new job
    if job_id == None:
        # no job id in the config
        config = JOB_CONFIG % ''
        (http_status, response) = client.createJob(config)
        if http_status != 201:
            print "Error creating job"
            print response
            return None

        job_id = response['id']
        print "Created job with automatic ID " + job_id
    else:
        (http_status, response) = client.getJob(job_id)
        if http_status == 404:
            # no job id in the config
            config = JOB_CONFIG % ('"id" : "' + job_id + '",')
            (http_status, response) = client.createJob(config)
            if http_status != 201:
                print "Error creating job"
                print response
                return None

            job_id = response['id']
            print "Created job with ID " + job_id
        else:
            print "Using job with ID " + job_id

    return job_id


def main():
    args = parseArguments()

    # read the config file
    config = ConfigParser.RawConfigParser()
    try:
        # insert a section header into the config so
        # ConfigParser will read it without complaint
        with open(args.config, "r") as config_file:
            ini_str = '[root]\n' + config_file.read()
            ini_fp = StringIO.StringIO(ini_str)
            config.readfp(ini_fp)
    except IOError:
        print "Error opening file " + args.config
        return


    try:
        region = config.get('root', 'region')
        access_id = config.get('root', 'aws_access_key_id')
        secret_key = config.get('root', 'aws_secret_access_key')
    except ConfigParser.NoOptionError as e:
        print e
        return


    # AWS CloudWatch connection
    cloudwatch_conn = boto.ec2.cloudwatch.connect_to_region(region,
                 aws_access_key_id=access_id,
                 aws_secret_access_key=secret_key)

    if cloudwatch_conn == None:
        print "Error unknown region " + region
        return

    # The Prelert REST API client
    engine_client = EngineApiClient(args.api_host, API_BASE_URL, args.api_port)

    # If no job ID is supplied create a new job
    job_id = createJob(args.job_id, engine_client)
    if job_id == None:
        return

    # default start date is None meaning run realtime
    start_date = None
    if args.start_date != None:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
        start_date = replaceTimezoneWithUtc(start_date)

    if start_date == None:
        runRealtime(job_id, cloudwatch_conn, engine_client)
    else:
        # historical mode, check for an end date
        end_date = replaceTimezoneWithUtc(datetime.utcnow())
        if args.end_date != None:
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
            end_date = replaceTimezoneWithUtc(end_date)

        runHistorical(job_id, start_date, end_date, cloudwatch_conn, engine_client)


    print "Closing job..."
    engine_client.close(job_id)

if __name__ == "__main__":
    main()
