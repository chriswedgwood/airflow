#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import contextlib
import os
from unittest import mock

import pytest
from botocore.exceptions import ClientError

from airflow.models import DAG, DagRun, TaskInstance
from airflow.operators.empty import EmptyOperator
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.amazon.aws.log.s3_task_handler import S3TaskHandler
from airflow.utils.session import create_session
from airflow.utils.state import State
from airflow.utils.timezone import datetime
from tests.test_utils.config import conf_vars

try:
    import boto3
    import moto
    from moto import mock_s3
except ImportError:
    mock_s3 = None


@pytest.fixture(autouse=True, scope="module")
def s3mock():
    with mock_s3():
        yield


@pytest.mark.skipif(mock_s3 is None, reason="Skipping test because moto.mock_s3 is not available")
class TestS3TaskHandler:
    @conf_vars({("logging", "remote_log_conn_id"): "aws_default"})
    @pytest.fixture(autouse=True)
    def setup(self, create_log_template, tmp_path_factory):
        self.remote_log_base = "s3://bucket/remote/log/location"
        self.remote_log_location = "s3://bucket/remote/log/location/1.log"
        self.remote_log_key = "remote/log/location/1.log"
        self.local_log_location = str(tmp_path_factory.mktemp("local-s3-log-location"))
        create_log_template("{try_number}.log")
        self.s3_task_handler = S3TaskHandler(self.local_log_location, self.remote_log_base)
        # Vivfy the hook now with the config override
        assert self.s3_task_handler.hook is not None

        date = datetime(2016, 1, 1)
        self.dag = DAG("dag_for_testing_s3_task_handler", start_date=date)
        task = EmptyOperator(task_id="task_for_testing_s3_log_handler", dag=self.dag)
        dag_run = DagRun(dag_id=self.dag.dag_id, execution_date=date, run_id="test", run_type="manual")
        with create_session() as session:
            session.add(dag_run)
            session.commit()
            session.refresh(dag_run)

        self.ti = TaskInstance(task=task, run_id=dag_run.run_id)
        self.ti.dag_run = dag_run
        self.ti.try_number = 1
        self.ti.state = State.RUNNING

        self.conn = boto3.client("s3")
        # We need to create the bucket since this is all in Moto's 'virtual'
        # AWS account
        moto.moto_api._internal.models.moto_api_backend.reset()
        self.conn.create_bucket(Bucket="bucket")

        yield

        self.dag.clear()

        with create_session() as session:
            session.query(DagRun).delete()

        if self.s3_task_handler.handler:
            with contextlib.suppress(Exception):
                os.remove(self.s3_task_handler.handler.baseFilename)

    def test_hook(self):
        assert isinstance(self.s3_task_handler.hook, S3Hook)
        assert self.s3_task_handler.hook.transfer_config.use_threads is False

    def test_log_exists(self):
        self.conn.put_object(Bucket="bucket", Key=self.remote_log_key, Body=b"")
        assert self.s3_task_handler.s3_log_exists(self.remote_log_location)

    def test_log_exists_none(self):
        assert not self.s3_task_handler.s3_log_exists(self.remote_log_location)

    def test_log_exists_raises(self):
        assert not self.s3_task_handler.s3_log_exists("s3://nonexistentbucket/foo")

    def test_log_exists_no_hook(self):
        with mock.patch("airflow.providers.amazon.aws.hooks.s3.S3Hook") as mock_hook:
            mock_hook.side_effect = Exception("Failed to connect")
            with pytest.raises(Exception):
                self.s3_task_handler.s3_log_exists(self.remote_log_location)

    def test_set_context_raw(self):
        self.ti.raw = True
        mock_open = mock.mock_open()
        with mock.patch("airflow.providers.amazon.aws.log.s3_task_handler.open", mock_open):
            self.s3_task_handler.set_context(self.ti)

        assert not self.s3_task_handler.upload_on_close
        mock_open.assert_not_called()

    def test_set_context_not_raw(self):
        mock_open = mock.mock_open()
        with mock.patch("airflow.providers.amazon.aws.log.s3_task_handler.open", mock_open):
            self.s3_task_handler.set_context(self.ti)

        assert self.s3_task_handler.upload_on_close
        mock_open.assert_called_once_with(os.path.join(self.local_log_location, "1.log"), "w")
        mock_open().write.assert_not_called()

    def test_read(self):
        self.conn.put_object(Bucket="bucket", Key=self.remote_log_key, Body=b"Log line\n")
        log, metadata = self.s3_task_handler.read(self.ti)
        assert (
            log[0][0][-1]
            == "*** Reading remote log from s3://bucket/remote/log/location/1.log.\nLog line\n\n"
        )
        assert metadata == [{"end_of_log": True}]

    def test_read_when_s3_log_missing(self):
        log, metadata = self.s3_task_handler.read(self.ti)

        assert 1 == len(log)
        assert len(log) == len(metadata)
        assert "*** Log file does not exist:" in log[0][0][-1]
        assert {"end_of_log": True} == metadata[0]

    def test_s3_read_when_log_missing(self):
        handler = self.s3_task_handler
        url = "s3://bucket/foo"
        with mock.patch.object(handler.log, "error") as mock_error:
            result = handler.s3_read(url, return_error=True)
            msg = (
                f"Could not read logs from {url} with error: An error occurred (404) when calling the "
                f"HeadObject operation: Not Found"
            )
            assert result == msg
            mock_error.assert_called_once_with(msg, exc_info=True)

    def test_read_raises_return_error(self):
        handler = self.s3_task_handler
        url = "s3://nonexistentbucket/foo"
        with mock.patch.object(handler.log, "error") as mock_error:
            result = handler.s3_read(url, return_error=True)
            msg = (
                f"Could not read logs from {url} with error: An error occurred (NoSuchBucket) when "
                f"calling the HeadObject operation: The specified bucket does not exist"
            )
            assert result == msg
            mock_error.assert_called_once_with(msg, exc_info=True)

    def test_write(self):
        with mock.patch.object(self.s3_task_handler.log, "error") as mock_error:
            self.s3_task_handler.s3_write("text", self.remote_log_location)
            # We shouldn't expect any error logs in the default working case.
            mock_error.assert_not_called()
        body = boto3.resource("s3").Object("bucket", self.remote_log_key).get()["Body"].read()

        assert body == b"text"

    def test_write_existing(self):
        self.conn.put_object(Bucket="bucket", Key=self.remote_log_key, Body=b"previous ")
        self.s3_task_handler.s3_write("text", self.remote_log_location)
        body = boto3.resource("s3").Object("bucket", self.remote_log_key).get()["Body"].read()

        assert body == b"previous \ntext"

    def test_write_raises(self):
        handler = self.s3_task_handler
        url = "s3://nonexistentbucket/foo"
        with mock.patch.object(handler.log, "error") as mock_error:
            handler.s3_write("text", url)
            mock_error.assert_called_once_with("Could not write logs to %s", url, exc_info=True)

    def test_close(self):
        self.s3_task_handler.set_context(self.ti)
        assert self.s3_task_handler.upload_on_close

        self.s3_task_handler.close()
        # Should not raise
        boto3.resource("s3").Object("bucket", self.remote_log_key).get()

    def test_close_no_upload(self):
        self.ti.raw = True
        self.s3_task_handler.set_context(self.ti)
        assert not self.s3_task_handler.upload_on_close
        self.s3_task_handler.close()

        with pytest.raises(ClientError):
            boto3.resource("s3").Object("bucket", self.remote_log_key).get()
