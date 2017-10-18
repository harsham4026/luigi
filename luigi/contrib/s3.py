# -*- coding: utf-8 -*-
#
# Copyright 2012-2015 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Implementation of Simple Storage Service support.
:py:class:`S3Target` is a subclass of the Target class to support S3 file
system operations. The `boto` library is required to use S3 targets.
"""

from __future__ import division

import datetime
import io
import itertools
import logging
import os
import os.path
import warnings

import botocore

from luigi import configuration, six
from luigi.format import get_default_format
from luigi.parameter import Parameter
from luigi.target import (AtomicLocalFile, FileAlreadyExists, FileSystem,
                          FileSystemException, FileSystemTarget,
                          MissingParentDirectory)
from luigi.task import ExternalTask

try:
    from urlparse import urlsplit
except ImportError:
    from urllib.parse import urlsplit

try:
    from ConfigParser import NoSectionError
except ImportError:
    from configparser import NoSectionError


logger = logging.getLogger('luigi-interface')


# two different ways of marking a directory
# with a suffix in S3
S3_DIRECTORY_MARKER_SUFFIX_0 = '_$folder$'
S3_DIRECTORY_MARKER_SUFFIX_1 = '/'


class InvalidDeleteException(FileSystemException):
    pass


class FileNotFoundException(FileSystemException):
    pass


class DeprecatedBotoClientException(Exception):
    pass


class _StreamingBodyAdaptor(io.IOBase):
    """
    Adapter class wrapping botocore's StreamingBody to make a file like iterable
    """

    def __init__(self, streaming_body):
        self.streaming_body = streaming_body

    def read(self, size):
        return self.streaming_body.read(size)

    def close(self):
        return self.streaming_body.close()


class S3Client(FileSystem):
    """
    boto3-powered S3 client.
    """

    _s3 = None

    def __init__(self, aws_access_key_id=None, aws_secret_access_key=None,
                 **kwargs):
        options = self._get_s3_config()
        options.update(kwargs)
        if aws_access_key_id:
            options['aws_access_key_id'] = aws_access_key_id
        if aws_secret_access_key:
            options['aws_secret_access_key'] = aws_secret_access_key

        self._options = options

    @property
    def s3(self):
        # only import boto3 when needed to allow top-lvl s3 module import
        import boto3

        options = dict(self._options)

        if self._s3:
            return self._s3

        aws_access_key_id = options.get('aws_access_key_id')
        aws_secret_access_key = options.get('aws_secret_access_key')

        # Removing key args would break backwards compability
        role_arn = options.get('aws_role_arn')
        role_session_name = options.get('aws_role_session_name')

        aws_session_token = None

        if role_arn and role_session_name:
            sts_client = boto3.client('sts')
            assumed_role = sts_client.assume_role(RoleArn=role_arn,
                                                  RoleSessionName=role_session_name)
            aws_secret_access_key = assumed_role['Credentials'].get(
                'SecretAccessKey')
            aws_access_key_id = assumed_role['Credentials'].get('AccessKeyId')
            aws_session_token = assumed_role['Credentials'].get('SessionToken')

        for key in ['aws_access_key_id', 'aws_secret_access_key',
                    'aws_role_session_name', 'aws_role_arn']:
            if key in options:
                options.pop(key)

        # At this stage, if no credentials provided, boto3 would handle their resolution for us
        # For finding out about the order in which it tries to find these credentials
        # please see here details http://boto3.readthedocs.io/en/latest/guide/configuration.html#configuring-credentials
        self._s3 = boto3.resource('s3',
                                  aws_access_key_id=aws_access_key_id,
                                  aws_secret_access_key=aws_secret_access_key,
                                  aws_session_token=aws_session_token,
                                  **options)
        return self._s3

    @s3.setter
    def s3(self, value):
        self._s3 = value

    def exists(self, path):
        """
        Does provided path exist on S3?
        """
        (bucket, key) = self._path_to_bucket_and_key(path)

        # root always exists
        if self._is_root(key):
            return True

        # file
        if self._exists(bucket, key):
            return True

        if self.isdir(path):
            return True

        logger.debug('Path %s does not exist', path)
        return False

    def remove(self, path, recursive=True):
        """
        Remove a file or directory from S3.
        """
        if not self.exists(path):
            logger.debug('Could not delete %s; path does not exist', path)
            return False

        (bucket, key) = self._path_to_bucket_and_key(path)
        s3_bucket = self.s3.Bucket(bucket)
        # root
        if self._is_root(key):
            raise InvalidDeleteException(
                'Cannot delete root of bucket at path %s' % path)

        # file
        if self._exists(bucket, key):
            self.s3.meta.client.delete_object(Bucket=bucket, Key=key)
            logger.debug('Deleting %s from bucket %s', key, bucket)
            return True

        if self.isdir(path) and not recursive:
            raise InvalidDeleteException(
                'Path %s is a directory. Must use recursive delete' % path)

        delete_key_list = []
        for obj in s3_bucket.objects.filter(Prefix=self._add_path_delimiter(key)):
            delete_key_list.append({'Key': obj.key})

        # delete the directory marker file if it exists
        if self._exists(bucket, key + S3_DIRECTORY_MARKER_SUFFIX_0):
            delete_key_list.append({'Key': key + S3_DIRECTORY_MARKER_SUFFIX_0})

        if len(delete_key_list) > 0:
            self.s3.meta.client.delete_objects(
                Bucket=bucket, Delete={'Objects': delete_key_list})
            return True

        return False

    def move(self, source_path, destination_path, **kwargs):
        """
        Rename/move an object from one S3 location to another.
        :param kwargs: Keyword arguments are passed to the boto3 function `copy`
        """
        self.copy(source_path, destination_path, **kwargs)
        self.remove(source_path)

    def get_key(self, path):
        """
        Returns the object summary at the path
        """
        (bucket, key) = self._path_to_bucket_and_key(path)

        if self._exists(bucket, key):
            return self.s3.ObjectSummary(bucket, key)

    def put(self, local_path, destination_s3_path, **kwargs):
        """
        Put an object stored locally to an S3 path.

        :param kwargs: Keyword arguments are passed to the boto function `put_object`
        """
        if 'encrypt_key' in kwargs:
            raise DeprecatedBotoClientException(
                'encrypt_key deprecated in boto3. Please refer to boto3 documentation for encryption details.')

        # put the file
        self.put_multipart(local_path, destination_s3_path, **kwargs)

    def put_string(self, content, destination_s3_path, **kwargs):
        """
        Put a string to an S3 path.
        :param kwargs: Keyword arguments are passed to the boto3 function `put_object`
        """
        if 'encrypt_key' in kwargs:
            raise DeprecatedBotoClientException(
                'encrypt_key deprecated in boto3. Please refer to boto3 documentation for encryption details.')
        (bucket, key) = self._path_to_bucket_and_key(destination_s3_path)

        # validate the bucket
        self._validate_bucket(bucket)

        # put the file
        self.s3.meta.client.put_object(
            Key=key, Bucket=bucket, Body=content, **kwargs)

    def put_multipart(self, local_path, destination_s3_path, part_size=8388608, **kwargs):
        """
        Put an object stored locally to an S3 path
        using S3 multi-part upload (for files > 8Mb).
        :param local_path: Path to source local file
        :param destination_s3_path: URL for target S3 location
        :param part_size: Part size in bytes. Default: 8388608 (8MB)
        :param kwargs: Keyword arguments are passed to the boto function `upload_fileobj` as ExtraArgs
        """
        if 'encrypt_key' in kwargs:
            raise DeprecatedBotoClientException(
                'encrypt_key deprecated in boto3. Please refer to boto3 documentation for encryption details.')

        import boto3
        # default part size for boto3 is 8Mb, changing it to fit part_size provided as a parameter
        transfer_config = boto3.s3.transfer.TransferConfig(
            multipart_chunksize=part_size)

        (bucket, key) = self._path_to_bucket_and_key(destination_s3_path)

        # validate the bucket
        self._validate_bucket(bucket)

        self.s3.meta.client.upload_fileobj(
            Fileobj=open(local_path, 'rb'), Bucket=bucket, Key=key, Config=transfer_config, ExtraArgs=kwargs)

    def copy(self, source_path, destination_path, threads=100, start_time=None, end_time=None, part_size=8388608, **kwargs):
        """
        Copy object(s) from one S3 location to another. Works for individual keys or entire directories.
        When files are larger than `part_size`, multipart uploading will be used.
        :param source_path: The `s3://` path of the directory or key to copy from
        :param destination_path: The `s3://` path of the directory or key to copy to
        :param threads: Optional argument to define the number of threads to use when copying (min: 3 threads)
        :param start_time: Optional argument to copy files with modified dates after start_time
        :param end_time: Optional argument to copy files with modified dates before end_time
        :param part_size: Part size in bytes
        :param kwargs: Keyword arguments are passed to the boto function `copy` as ExtraArgs
        :returns tuple (number_of_files_copied, total_size_copied_in_bytes)
        """

        start = datetime.datetime.now()

        (src_bucket, src_key) = self._path_to_bucket_and_key(source_path)
        (dst_bucket, dst_key) = self._path_to_bucket_and_key(destination_path)

        # don't allow threads to be less than 3
        threads = 3 if threads < 3 else threads
        import boto3

        transfer_config = boto3.s3.transfer.TransferConfig(
            max_concurrency=threads, multipart_chunksize=part_size)
        total_keys = 0

        if self.isdir(source_path):
            (bucket, key) = self._path_to_bucket_and_key(source_path)
            key_path = self._add_path_delimiter(key)
            key_path_len = len(key_path)
            src_prefix = self._add_path_delimiter(src_key)
            dst_prefix = self._add_path_delimiter(dst_key)
            total_size_bytes = 0
            for item in self.list(source_path, start_time=start_time, end_time=end_time, return_key=True):
                path = item.key[key_path_len:]
                # prevents copy attempt of empty key in folder
                if path != '' and path != '/':
                    total_keys += 1
                    total_size_bytes += item.size
                    copy_source = {
                        'Bucket': src_bucket,
                        'Key': src_prefix + path
                    }

                    self.s3.meta.client.copy(
                        copy_source, dst_bucket, dst_prefix + path, Config=transfer_config, ExtraArgs=kwargs)

            end = datetime.datetime.now()
            duration = end - start
            logger.info('%s : Complete : %s total keys copied in %s' %
                        (datetime.datetime.now(), total_keys, duration))

            return total_keys, total_size_bytes

        # If the file isn't a directory just perform a simple copy
        else:
            copy_source = {
                'Bucket': src_bucket,
                'Key': src_key
            }
            self.s3.meta.client.copy(
                copy_source, dst_bucket, dst_key, Config=transfer_config, ExtraArgs=kwargs)

    def get(self, s3_path, destination_local_path):
        """
        Get an object stored in S3 and write it to a local path.
        """
        (bucket, key) = self._path_to_bucket_and_key(s3_path)
        # download the file
        self.s3.meta.client.download_file(bucket, key, destination_local_path)

    def get_as_string(self, s3_path):
        """
        Get the contents of an object stored in S3 as a string.
        """
        (bucket, key) = self._path_to_bucket_and_key(s3_path)

        # get the content
        obj = self.s3.Object(bucket, key)
        contents = obj.get()['Body'].read().decode('utf-8')
        return contents

    def isdir(self, path):
        """
        Is the parameter S3 path a directory?
        """
        (bucket, key) = self._path_to_bucket_and_key(path)

        s3_bucket = self.s3.Bucket(bucket)

        # root is a directory
        if self._is_root(key):
            return True

        for suffix in (S3_DIRECTORY_MARKER_SUFFIX_0,
                       S3_DIRECTORY_MARKER_SUFFIX_1):
            try:
                self.s3.meta.client.get_object(
                    Bucket=bucket, Key=key + suffix)
            except botocore.exceptions.ClientError as e:
                if not e.response['Error']['Code'] in ['NoSuchKey', '404']:
                    raise
            else:
                return True

        # files with this prefix
        key_path = self._add_path_delimiter(key)
        s3_bucket_list_result = list(itertools.islice(
            s3_bucket.objects.filter(Prefix=key_path), 1))
        if s3_bucket_list_result:
            return True

        return False

    is_dir = isdir  # compatibility with old version.

    def mkdir(self, path, parents=True, raise_if_exists=False):
        if raise_if_exists and self.isdir(path):
            raise FileAlreadyExists()

        bucket, key = self._path_to_bucket_and_key(path)
        if self._is_root(key):
            # isdir raises if the bucket doesn't exist; nothing to do here.
            return

        path = self._add_path_delimiter(path)

        if not parents and not self.isdir(os.path.dirname(path)):
            raise MissingParentDirectory()

        return self.put_string("", path)

    def listdir(self, path, start_time=None, end_time=None, return_key=False):
        """
        Get an iterable with S3 folder contents.
        Iterable contains paths relative to queried path.
        :param start_time: Optional argument to list files with modified datetime after start_time
        :param end_time: Optional argument to list files with modified datetime before end_time
        :param return_key: Optional argument, when set to True will return boto3's ObjectSummary (instead of the filename)
        """
        (bucket, key) = self._path_to_bucket_and_key(path)

        # grab and validate the bucket
        s3_bucket = self.s3.Bucket(bucket)

        key_path = self._add_path_delimiter(key)
        key_path_len = len(key_path)
        for item in s3_bucket.objects.filter(Prefix=key_path):
            last_modified_date = item.last_modified
            if (
                # neither are defined, list all
                (not start_time and not end_time) or
                # start defined, after start
                (start_time and not end_time and start_time < last_modified_date) or
                # end defined, prior to end
                (not start_time and end_time and last_modified_date < end_time) or
                (start_time and end_time and start_time <
                 last_modified_date < end_time)  # both defined, between
            ):
                if return_key:
                    yield item
                else:
                    yield self._add_path_delimiter(path) + item.key[key_path_len:]

    def list(self, path, start_time=None, end_time=None, return_key=False):  # backwards compat
        key_path_len = len(self._add_path_delimiter(path))
        for item in self.listdir(path, start_time=start_time, end_time=end_time, return_key=return_key):
            if return_key:
                yield item
            else:
                yield item[key_path_len:]

    def _get_s3_config(self, key=None):
        defaults = dict(configuration.get_config().defaults())
        try:
            config = dict(configuration.get_config().items('s3'))
        except NoSectionError:
            return {}
        # So what ports etc can be read without us having to specify all dtypes
        for k, v in six.iteritems(config):
            try:
                config[k] = int(v)
            except ValueError:
                pass
        if key:
            return config.get(key)
        section_only = {k: v for k, v in config.items(
        ) if k not in defaults or v != defaults[k]}
        return section_only

    def _path_to_bucket_and_key(self, path):
        (scheme, netloc, path, query, fragment) = urlsplit(path)
        path_without_initial_slash = path[1:]
        return netloc, path_without_initial_slash

    def _is_root(self, key):
        return (len(key) == 0) or (key == '/')

    def _add_path_delimiter(self, key):
        return key if key[-1:] == '/' or key == '' else key + '/'

    def _validate_bucket(self, bucket_name):
        exists = True

        try:
            self.s3.meta.client.head_bucket(Bucket=bucket_name)
        except botocore.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code in ('404', 'NoSuchBucket'):
                exists = False
            else:
                raise
        return exists

    def _exists(self, bucket, key):
        s3_key = False
        try:
            self.s3.Object(bucket, key).load()
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] in ['NoSuchKey', '404']:
                s3_key = False
            else:
                raise
        else:
            s3_key = True
        if s3_key:
            return True


class AtomicS3File(AtomicLocalFile):
    """
    An S3 file that writes to a temp file and puts to S3 on close.

    :param kwargs: Keyword arguments are passed to the boto function `initiate_multipart_upload`
    """

    def __init__(self, path, s3_client, **kwargs):
        self.s3_client = s3_client
        super(AtomicS3File, self).__init__(path)
        self.s3_options = kwargs

    def move_to_final_destination(self):
        self.s3_client.put_multipart(
            self.tmp_path, self.path, **self.s3_options)


class ReadableS3File(object):
    def __init__(self, s3_key):
        self.s3_key = _StreamingBodyAdaptor(s3_key.get()['Body'])
        self.buffer = []
        self.closed = False
        self.finished = False

    def read(self, size=None):
        f = self.s3_key.read(size)
        return f

    def close(self):
        self.s3_key.close()
        self.closed = True

    def __del__(self):
        self.close()

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def __enter__(self):
        return self

    def _add_to_buffer(self, line):
        self.buffer.append(line)

    def _flush_buffer(self):
        output = b''.join(self.buffer)
        self.buffer = []
        return output

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return False

    def __iter__(self):
        key_iter = self.s3_key.__iter__()

        has_next = True
        while has_next:
            try:
                # grab the next chunk
                chunk = next(key_iter)

                # split on newlines, preserving the newline
                for line in chunk.splitlines(True):

                    if not line.endswith(os.linesep):
                        # no newline, so store in buffer
                        self._add_to_buffer(line)
                    else:
                        # newline found, send it out
                        if self.buffer:
                            self._add_to_buffer(line)
                            yield self._flush_buffer()
                        else:
                            yield line
            except StopIteration:
                # send out anything we have left in the buffer
                output = self._flush_buffer()
                if output:
                    yield output
                has_next = False
        self.close()


class S3Target(FileSystemTarget):
    """
    Target S3 file object

    :param kwargs: Keyword arguments are passed to the boto function `initiate_multipart_upload`
    """

    fs = None

    def __init__(self, path, format=None, client=None, **kwargs):
        super(S3Target, self).__init__(path)
        if format is None:
            format = get_default_format()

        self.path = path
        self.format = format
        self.fs = client or S3Client()
        self.s3_options = kwargs

    def open(self, mode='r'):
        if mode not in ('r', 'w'):
            raise ValueError("Unsupported open mode '%s'" % mode)

        if mode == 'r':
            s3_key = self.fs.get_key(self.path)
            if not s3_key:
                raise FileNotFoundException(
                    "Could not find file at %s" % self.path)

            fileobj = ReadableS3File(s3_key)
            return self.format.pipe_reader(fileobj)
        else:
            return self.format.pipe_writer(AtomicS3File(self.path, self.fs, **self.s3_options))


class S3FlagTarget(S3Target):
    """
    Defines a target directory with a flag-file (defaults to `_SUCCESS`) used
    to signify job success.

    This checks for two things:

    * the path exists (just like the S3Target)
    * the _SUCCESS file exists within the directory.

    Because Hadoop outputs into a directory and not a single file,
    the path is assumed to be a directory.

    This is meant to be a handy alternative to AtomicS3File.

    The AtomicFile approach can be burdensome for S3 since there are no directories, per se.

    If we have 1,000,000 output files, then we have to rename 1,000,000 objects.
    """

    fs = None

    def __init__(self, path, format=None, client=None, flag='_SUCCESS'):
        """
        Initializes a S3FlagTarget.

        :param path: the directory where the files are stored.
        :type path: str
        :param client:
        :type client:
        :param flag:
        :type flag: str
        """
        if format is None:
            format = get_default_format()

        if path[-1] != "/":
            raise ValueError("S3FlagTarget requires the path to be to a "
                             "directory.  It must end with a slash ( / ).")
        super(S3FlagTarget, self).__init__(path, format, client)
        self.flag = flag

    def exists(self):
        hadoopSemaphore = self.path + self.flag
        return self.fs.exists(hadoopSemaphore)


class S3EmrTarget(S3FlagTarget):
    """
    Deprecated. Use :py:class:`S3FlagTarget`
    """

    def __init__(self, *args, **kwargs):
        warnings.warn("S3EmrTarget is deprecated. Please use S3FlagTarget")
        super(S3EmrTarget, self).__init__(*args, **kwargs)


class S3PathTask(ExternalTask):
    """
    A external task that to require existence of a path in S3.
    """
    path = Parameter()

    def output(self):
        return S3Target(self.path)


class S3EmrTask(ExternalTask):
    """
    An external task that requires the existence of EMR output in S3.
    """
    path = Parameter()

    def output(self):
        return S3EmrTarget(self.path)


class S3FlagTask(ExternalTask):
    """
    An external task that requires the existence of EMR output in S3.
    """
    path = Parameter()
    flag = Parameter(default=None)

    def output(self):
        return S3FlagTarget(self.path, flag=self.flag)
