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
Pull all the anomaly records for the provided job id and print
the timestamp, anomaly score and unusual score

The script is invoked with 1 positional argument -the id of the 
job to query the results of. Additional optional arguments
to specify the location of the Engine API. Run the script with 
'--help' to see the options.
 
'''

import argparse
import sys
import json
import logging
import time

from prelert.engineApiClient import EngineApiClient

# defaults
HOST = 'localhost'
PORT = 8080
BASE_URL = 'engine/v2'


def setupLogging():
    '''
        Log to console
    '''    
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')


def parseArguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="The Prelert Engine API host, defaults to "
        + HOST, default=HOST)
    parser.add_argument("--port", help="The Prelert Engine API port, defaults to "
        + str(PORT), default=PORT)
    parser.add_argument("--anomalyScore", help="Filter out buckets with an anomalyScore "  
        + "less than this", type=float, default=0.0)
    parser.add_argument("--normalizedProbability", help="Filter out buckets with an " 
        + "max normalized probablilty less than this", type=float, default=0.0)         
    parser.add_argument("jobid", help="The jobId to request results from", default="0")
    return parser.parse_args()   


def printHeader():
    print "Date,Anomaly Score,Normalized Probability"

def printRecords(records):
    for record in records:
        print "{0},{1},{2}".format(record['timestamp'], record['anomalyScore'], 
            record['normalizedProbability'])


def main():

    setupLogging()

    args = parseArguments()    
    job_id = args.jobid

    # Create the REST API client
    engine_client = EngineApiClient(args.host, BASE_URL, args.port)

    # Get all the records up to now
    logging.info("Get records for job " + job_id)

    skip = 0
    take = 200
    (http_status_code, response) = engine_client.getRecords(job_id, skip, take,
                            normalized_probability_filter_value=args.normalizedProbability, 
                            anomaly_score_filter_value=args.anomalyScore)        
    if http_status_code != 200:
        print (http_status_code, json.dumps(response))
        return

    hit_count = int(response['hitCount'])

    printHeader()
    printRecords(response['documents'])

    while (skip + take) < hit_count:
        skip += take

        (http_status_code, response) = engine_client.getRecords(job_id, skip, take,
                            normalized_probability_filter_value=args.normalizedProbability, 
                            anomaly_score_filter_value=args.anomalyScore)        

        if http_status_code != 200:
            print (http_status_code, json.dumps(response))
            return

        printRecords(response['documents'])


if __name__ == "__main__":
    main()    

