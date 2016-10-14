#
# Copyright 2016 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from afkak.client import KafkaClient as _KafkaClient
from afkak.producer import Producer as _kafkaProducer
from structlog import get_logger
from voltha.consulhelpers import get_endpoint_from_consul
from twisted.internet.defer import inlineCallbacks
from afkak.common import (
    PRODUCER_ACK_LOCAL_WRITE,
)


class KafkaProxy(object):
    """
    This is a singleton proxy kafka class to hide the kafka client details.
    """
    _kafka_instance = None

    def __init__(self, consul_endpoint='localhost:8500',
                 kafka_endpoint='localhost:9092',
                 ack_timeout=1000, max_req_attempts=10):

        # return an exception if the object already exist
        if KafkaProxy._kafka_instance:
            raise Exception('Singleton exist for :{}'.format(KafkaProxy))

        self.log = get_logger()

        self.log.info('KafkaProxy init with kafka endpoint:{}'.format(
            kafka_endpoint))

        self.ack_timeout = ack_timeout
        self.max_req_attempts = max_req_attempts
        self.consul_endpoint = consul_endpoint
        self.kafka_endpoint = kafka_endpoint
        self.kclient = None
        self.kproducer = None

        self._get_kafka_producer()

        KafkaProxy._kafka_instance = self

    def _get_kafka_producer(self):
        # PRODUCER_ACK_LOCAL_WRITE : server will wait till the data is written
        #  to a local log before sending response

        if self.kafka_endpoint.startswith('@'):
            try:
                _k_endpoint = get_endpoint_from_consul(self.consul_endpoint,
                                                       self.kafka_endpoint[1:])
                self.log.info(
                    'Found kafka service at {}'.format(_k_endpoint))

            except Exception as e:
                self.log.error('Failure to locate a kafka service from '
                               'consul {}:'.format(repr(e)))
                self.kproducer = None
                self.kclient = None
                return
        else:
            _k_endpoint = self.kafka_endpoint

        self.kclient = _KafkaClient(_k_endpoint)
        self.kproducer = _kafkaProducer(self.kclient,
                                        req_acks=PRODUCER_ACK_LOCAL_WRITE,
                                        ack_timeout=self.ack_timeout,
                                        max_req_attempts=self.max_req_attempts)

    @inlineCallbacks
    def send_message(self, topic, msg):
        assert topic is not None
        assert msg is not None

        # first check whether we have a kafka producer.  If there is none
        # then try to get one - this happens only when we try to lookup the
        # kafka service from consul
        if self.kproducer is None:
            self._get_kafka_producer()
            # Lets the next message request do the retry if still a failure
            if self.kproducer is None:
                self.log.error('No kafka producer available at {}'.format(
                    self.kafka_endpoint))
                return

        self.log.info('Sending message {} to kafka topic {}'.format(msg,
                                                                    topic))
        try:
            msg_list = [msg]
            yield self.kproducer.send_messages(topic, msgs=msg_list)
            self.log.info('Successfully sent message {} to kafka topic '
                          '{}'.format(msg, topic))
        except Exception as e:
            self.log.error('Failure to send message {} to kafka topic {}: '
                           '{}'.format(msg, topic, repr(e)))
            # set the kafka producer to None.  This is needed if the
            # kafka docker went down and comes back up with a different
            # port number.
            self.kproducer = None
            self.kclient = None


# Common method to get the singleton instance of the kafka proxy class
def get_kafka_proxy():
    return KafkaProxy._kafka_instance