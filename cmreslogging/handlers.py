""" Elasticsearch logging handler
"""

import logging
import datetime
from threading import Timer, Lock
from enum import Enum
from elasticsearch import helpers as eshelpers
from elasticsearch import Elasticsearch
import json

try:
    from requests_kerberos import HTTPKerberosAuth, DISABLED
    CMR_KERBEROS_SUPPORTED = True
except ImportError:
    CMR_KERBEROS_SUPPORTED = False

try:
    from requests_aws4auth import AWS4Auth
    AWS4AUTH_SUPPORTED = True
except ImportError:
    AWS4AUTH_SUPPORTED = False

from cmreslogging.serializers import CMRESSerializer


class CMRESHandler(logging.Handler):
    """ Elasticsearch log handler

    Allows to log to elasticsearch into json format.
    All LogRecord fields are serialised and inserted
    """

    class AuthType(Enum):
        """ Authentication types supported

        The handler supports
         - No authentication
         - Basic authentication
         - Kerberos or SSO authentication (on windows and linux)
         - ElasticSearch API Keys
        """
        NO_AUTH = 0
        BASIC_AUTH = 1
        KERBEROS_AUTH = 2
        AWS_SIGNED_AUTH = 3
        API_KEY = 4

    class IndexNameFrequency(Enum):
        """ Index type supported
        the handler supports
        - Daily indices
        - Weekly indices
        - Monthly indices
        - Year indices
        - Disabled/No rotation - use this for Datastreams
        """
        DAILY = 0
        WEEKLY = 1
        MONTHLY = 2
        YEARLY = 3
        DISABLE = 5

    # Defaults for the class
    __DEFAULT_ELASTICSEARCH_HOST = [{'host': 'localhost', 'port': 9200}]
    __DEFAULT_AUTH_USER = ''
    __DEFAULT_AUTH_PASSWD = ''
    __DEFAULT_AUTH_API_KEY = ''
    __DEFAULT_AWS_ACCESS_KEY = ''
    __DEFAULT_AWS_SECRET_KEY = ''
    __DEFAULT_AWS_REGION = ''
    __DEFAULT_USE_SSL = False
    __DEFAULT_VERIFY_SSL = True
    __DEFAULT_AUTH_TYPE = AuthType.NO_AUTH
    __DEFAULT_INDEX_FREQUENCY = IndexNameFrequency.DAILY
    __DEFAULT_BUFFER_SIZE = 1000
    __DEFAULT_FLUSH_FREQ_INSEC = 1
    __DEFAULT_ADDITIONAL_FIELDS = {}
    __DEFAULT_ES_INDEX_NAME = 'python_logger'
    __DEFAULT_ES_DOC_TYPE = 'python_log'
    __DEFAULT_RAISE_ON_EXCEPTION = False
    __DEFAULT_TIMESTAMP_FIELD_NAME = "timestamp"

    __LOGGING_FILTER_FIELDS = ['msecs',
                               'relativeCreated',
                               'levelno',
                               'created']

    __DEFAULT_INDEX_LIFECYCLE = json.loads('{"phases": {"hot": {"min_age": "0ms","actions": {"rollover": {"max_age": "30d","max_primary_shard_size": "50gb"}}},"delete": {"min_age": "60d","actions": {"delete": {"delete_searchable_snapshot": true}}}}}')
    __DEFAULT_INDEX_LC_COMPONENT = json.loads('{"settings":{"index.lifecycle.name":"pylog_handler_policy"}}')
    __DEFAULT_INDEX_MAPPING_COMPONENT = json.loads('{"settings": {"index": {"number_of_shards": "1","mapping": {"total_fields": {"limit": "2500"}}}}, "mappings": { "dynamic": true,"numeric_detection": false,"date_detection": true,"dynamic_date_formats": ["strict_date_optional_time","yyyy/MM/dd HH:mm:ss Z||yyyy/MM/dd Z"], "properties":{"exc_info":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"exc_text":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"filename":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"funcName":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"levelname":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"lineno":{"type":"long"},"message":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"module":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"msg":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"name":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"pathname":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"process":{"type":"long"},"processName":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"stack_info":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"thread":{"type":"long"},"threadName":{"type":"text","fields":{"keyword":{"type":"keyword","ignore_above":256}}},"timestamp":{"type":"date"}}}}')
    @staticmethod
    def _get_daily_index_name(es_index_name):
        """ Returns elasticearch index name
        :param: index_name the prefix to be used in the index
        :return: A string containing the elasticsearch indexname used which should include the date.
        """
        return "{0!s}-{1!s}".format(es_index_name, datetime.datetime.now().strftime('%Y.%m.%d'))

    @staticmethod
    def _get_weekly_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A string containing the elasticsearch indexname used which should include the date and specific week
        """
        current_date = datetime.datetime.now()
        start_of_the_week = current_date - datetime.timedelta(days=current_date.weekday())
        return "{0!s}-{1!s}".format(es_index_name, start_of_the_week.strftime('%Y.%m.%d'))

    @staticmethod
    def _get_monthly_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A string containing the elasticsearch indexname used which should include the date and specific moth
        """
        return "{0!s}-{1!s}".format(es_index_name, datetime.datetime.now().strftime('%Y.%m'))

    @staticmethod
    def _get_yearly_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A string containing the elasticsearch indexname used which should include the date and specific year
        """
        return "{0!s}-{1!s}".format(es_index_name, datetime.datetime.now().strftime('%Y'))

    @staticmethod
    def _get_base_index_name(es_index_name):
        """ Return elasticsearch index name
        :param: index_name the prefix to be used in the index
        :return: A string containing the elasticsearch indexname 
        """
        return es_index_name

    _INDEX_FREQUENCY_FUNCION_DICT = {
        IndexNameFrequency.DAILY: _get_daily_index_name,
        IndexNameFrequency.WEEKLY: _get_weekly_index_name,
        IndexNameFrequency.MONTHLY: _get_monthly_index_name,
        IndexNameFrequency.YEARLY: _get_yearly_index_name,
        IndexNameFrequency.DISABLE: _get_base_index_name
    }

    def __init__(self,
                 hosts=__DEFAULT_ELASTICSEARCH_HOST,
                 auth_details=(__DEFAULT_AUTH_USER, __DEFAULT_AUTH_PASSWD),
                 auth_apikey=__DEFAULT_AUTH_API_KEY,
                 aws_access_key=__DEFAULT_AWS_ACCESS_KEY,
                 aws_secret_key=__DEFAULT_AWS_SECRET_KEY,
                 aws_region=__DEFAULT_AWS_REGION,
                 auth_type=__DEFAULT_AUTH_TYPE,
                 use_ssl=__DEFAULT_USE_SSL,
                 verify_ssl=__DEFAULT_VERIFY_SSL,
                 buffer_size=__DEFAULT_BUFFER_SIZE,
                 flush_frequency_in_sec=__DEFAULT_FLUSH_FREQ_INSEC,
                 es_index_name=__DEFAULT_ES_INDEX_NAME,
                 index_name_frequency=__DEFAULT_INDEX_FREQUENCY,
                 es_doc_type=__DEFAULT_ES_DOC_TYPE,
                 es_additional_fields=__DEFAULT_ADDITIONAL_FIELDS,
                 raise_on_indexing_exceptions=__DEFAULT_RAISE_ON_EXCEPTION,
                 default_timestamp_field_name=__DEFAULT_TIMESTAMP_FIELD_NAME,
                 default_ilm_policy=__DEFAULT_INDEX_LIFECYCLE,
                 default_index_mapping=__DEFAULT_INDEX_MAPPING_COMPONENT
                 ):
        """Handler constructor

        :param hosts: The list of hosts that elasticsearch clients will connect. The list can be provided
                    in the format ```[{'host':'host1','port':9200}, {'host':'host2','port':9200}]``` to
                    make sure the client supports failover of one of the instertion nodes
        :param auth_details: When ```CMRESHandler.AuthType.BASIC_AUTH``` is used this argument must contain
                    a tuple of string with the user and password that will be used to authenticate against
                    the Elasticsearch servers, for example```('User','Password')
        :param auth_apikey: When ```CMSRESHandlet.AuthType.API_KEY``` is used this argument must contain
                    a string with the API Key used to authenticate against the Elasticsearch Servers
        :param aws_access_key: When ```CMRESHandler.AuthType.AWS_SIGNED_AUTH``` is used this argument must contain
                    the AWS key id of the  the AWS IAM user
        :param aws_secret_key: When ```CMRESHandler.AuthType.AWS_SIGNED_AUTH``` is used this argument must contain
                    the AWS secret key of the  the AWS IAM user
        :param aws_region: When ```CMRESHandler.AuthType.AWS_SIGNED_AUTH``` is used this argument must contain
                    the AWS region of the  the AWS Elasticsearch servers, for example```'us-east'
        :param auth_type: The authentication type to be used in the connection ```CMRESHandler.AuthType```
                    Currently, NO_AUTH, BASIC_AUTH, KERBEROS_AUTH, API_KEY are supported
        :param use_ssl: A boolean that defines if the communications should use SSL encrypted communication
        :param verify_ssl: A boolean that defines if the SSL certificates are validated or not
        :param buffer_size: An int, Once this size is reached on the internal buffer results are flushed into ES
        :param flush_frequency_in_sec: A float representing how often and when the buffer will be flushed, even
                    if the buffer_size has not been reached yet
        :param es_index_name: A string with the prefix of the elasticsearch index that will be created. Note a
                    date with YYYY.MM.dd, ```python_logger``` used by default
        :param index_name_frequency: Defines what the date used in the postfix of the name would be. available values
                    are selected from the IndexNameFrequency class (IndexNameFrequency.DAILY,
                    IndexNameFrequency.WEEKLY, IndexNameFrequency.MONTHLY, IndexNameFrequency.YEARLY, IndexNameFrequency.DISABLE).
                    DISABLED will create and use a datastream if the index does not exist. By default it uses daily indices.
        :param es_doc_type: A string with the name of the document type that will be used ```python_log``` used
                    by default
        :param es_additional_fields: A dictionary with all the additional fields that you would like to add
                    to the logs, such the application, environment, etc.
        :param raise_on_indexing_exceptions: A boolean, True only for debugging purposes to raise exceptions
                    caused when
        :param default_ilm_policy: A json string representing the default ILM Policy. Default is to keep data in the hot tier for
                    30 days and delete after 60 days
        :param default_index_mapping: A json string representing the field mappings for the index. Default is fairly generic,
                    and has dynamic mapping enabled.
        :return: A ready to be used CMRESHandler.
        """
        logging.Handler.__init__(self)

        self.hosts = hosts
        self.auth_details = auth_details
        self.auth_apikey = auth_apikey
        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.aws_region = aws_region
        self.auth_type = auth_type
        self.use_ssl = use_ssl
        self.verify_certs = verify_ssl
        self.buffer_size = buffer_size
        self.flush_frequency_in_sec = flush_frequency_in_sec
        self.es_index_name = es_index_name
        self.index_name_frequency = index_name_frequency
        self.es_doc_type = es_doc_type
        self.es_additional_fields = es_additional_fields.copy()
        self.raise_on_indexing_exceptions = raise_on_indexing_exceptions
        self.default_timestamp_field_name = default_timestamp_field_name
        self.default_ilm_policy = default_ilm_policy
        self.default_index_mapping = default_index_mapping

        self._client = None
        self._buffer = []
        self._buffer_lock = Lock()
        self._timer = None
        self._index_name_func = CMRESHandler._INDEX_FREQUENCY_FUNCION_DICT[self.index_name_frequency]
        self.serializer = CMRESSerializer()

    def __schedule_flush(self):
        if self._timer is None:
            self._timer = Timer(self.flush_frequency_in_sec, self.flush)
            self._timer.setDaemon(True)
            self._timer.start()

    def __get_es_client(self):
        if self.auth_type == CMRESHandler.AuthType.NO_AUTH:
            if self._client is None:
                self._client = Elasticsearch(hosts=self.hosts,
                                             use_ssl=self.use_ssl,
                                             verify_certs=self.verify_certs,
                                             serializer=self.serializer)
            return self._client

        if self.auth_type == CMRESHandler.AuthType.BASIC_AUTH:
            if self._client is None:
                return Elasticsearch(hosts=self.hosts,
                                     http_auth=self.auth_details,
                                     verify_certs=self.verify_certs,
                                     serializer=self.serializer)
            return self._client
        if self.auth_type == CMRESHandler.AuthType.API_KEY:
            if self._client is None:
                return Elasticsearch(hosts=self.hosts,
                                     api_key=self.auth_apikey,
                                     verify_certs=self.verify_certs,
                                     serializer=self.serializer)
            return self._client

        if self.auth_type == CMRESHandler.AuthType.KERBEROS_AUTH:
            if not CMR_KERBEROS_SUPPORTED:
                raise EnvironmentError("Kerberos module not available. Please install \"requests-kerberos\"")
            # For kerberos we return a new client each time to make sure the tokens are up to date
            return Elasticsearch(hosts=self.hosts,
                                 use_ssl=self.use_ssl,
                                 verify_certs=self.verify_certs,
                                 http_auth=HTTPKerberosAuth(mutual_authentication=DISABLED),
                                 serializer=self.serializer)

        if self.auth_type == CMRESHandler.AuthType.AWS_SIGNED_AUTH:
            if not AWS4AUTH_SUPPORTED:
                raise EnvironmentError("AWS4Auth not available. Please install \"requests-aws4auth\"")
            if self._client is None:
                awsauth = AWS4Auth(self.aws_access_key, self.aws_secret_key, self.aws_region, 'es')
                self._client = Elasticsearch(
                    hosts=self.hosts,
                    http_auth=awsauth,
                    use_ssl=self.use_ssl,
                    verify_certs=True,
                    serializer=self.serializer
                )
            return self._client

        raise ValueError("Authentication method not supported")

    def test_es_source(self):
        """ Returns True if the handler can ping the Elasticsearch servers

        Can be used to confirm the setup of a handler has been properly done and confirm
        that things like the authentication is working properly

        :return: A boolean, True if the connection against elasticserach host was successful
        """
        return self.__get_es_client().ping()

    @staticmethod
    def __get_es_datetime_str(timestamp):
        """ Returns elasticsearch utc formatted time for an epoch timestamp

        :param timestamp: epoch, including milliseconds
        :return: A string valid for elasticsearch time record
        """
        current_date = datetime.datetime.utcfromtimestamp(timestamp)
        return "{0!s}.{1:03d}Z".format(current_date.strftime('%Y-%m-%dT%H:%M:%S'), int(current_date.microsecond / 1000))

    def __create_datastream_or_index(self):
        es_client = self.__get_es_client()
        idx_name=self._index_name_func.__func__(self.es_index_name)
        # If index does not exist, explicitly create it as a data stream (ES7.X and higher)
        if es_client.indices.exists(index=idx_name) == False:
            if self.index_name_frequency == self.IndexNameFrequency.DISABLE:
                try:
                    es_client.ilm.put_lifecycle(
                        name="pylog_handler_policy",
                        policy=self.default_ilm_policy
                    )
                    es_client.cluster.put_component_template(
                        name="mappings@pylog",
                        template=self.default_index_mapping,
                    )
                    es_client.cluster.put_component_template(
                        name="lifecycle@pylog",
                        template=self.__DEFAULT_INDEX_LC_COMPONENT,
                    )
                    es_client.indices.put_index_template(
                        name="python_log_template",
                        index_patterns=[idx_name+"*"],
                        data_stream={},
                        composed_of=["mappings@pylog", "lifecycle@pylog"]
                    )
                    es_client.indices.create_data_stream(name=idx_name)
                except Exception as exception:
                    print("Error: Unable to create datastream\n Reason:",exception)
                    print("Falling back to a standard index")
                    es_client.indices.create(index=idx_name)
            else:
                es_client.indices.create(index=idx_name)

    def flush(self):
        """ Flushes the buffer into ES
        :return: None
        """
        if self._timer is not None and self._timer.is_alive():
            self._timer.cancel()
        self._timer = None
        self.__create_datastream_or_index()
        if self._buffer:
            try:
                with self._buffer_lock:
                    logs_buffer = self._buffer
                    self._buffer = []
                actions=[]
                for log_record in logs_buffer:
                    log_record["args"]=str(log_record["args"])
                    log_record["msg"] = str(log_record["msg"])
                    log_record["@timestamp"]=str(log_record["timestamp"])
                    actions.append({"_op_type":"create","_index": self._index_name_func.__func__(self.es_index_name), "_source": log_record})
                eshelpers.bulk(
                    client=self.__get_es_client(),
                    index=self._index_name_func.__func__(self.es_index_name),
                    actions=actions,
                )
            except eshelpers.BulkIndexError as e:
                print("Exception:", e)
                for err in e.errors:
                    print("Error:", err)
            except Exception as exception:
                if self.raise_on_indexing_exceptions:
                    print("Error: %s\n", exception)
                    raise exception

    def close(self):
        """ Flushes the buffer and release any outstanding resource

        :return: None
        """
        if self._timer is not None:
            self.flush()
        self._timer = None

    def emit(self, record):
        """ Emit overrides the abstract logging.Handler logRecord emit method

        Format and records the log

        :param record: A class of type ```logging.LogRecord```
        :return: None
        """
        self.format(record)

        rec = self.es_additional_fields.copy()
        for key, value in record.__dict__.items():
            if key not in CMRESHandler.__LOGGING_FILTER_FIELDS:
                if key == "args":
                    value = tuple(str(arg) for arg in value)
                rec[key] = "" if value is None else value
        rec[self.default_timestamp_field_name] = self.__get_es_datetime_str(record.created)
        with self._buffer_lock:
            self._buffer.append(rec)

        if len(self._buffer) >= self.buffer_size:
            self.flush()
        else:
            self.__schedule_flush()
