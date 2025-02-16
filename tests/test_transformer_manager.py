# Copyright (c) 2019, IRIS-HEP
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import base64
import zipfile

import pytest
import re
from servicex import TransformerManager

from tests.resource_test_base import ResourceTestBase


def _arg_value(args, param):
    return re.search(param + ' (\\S+)', args[0]).group(1)


def _env_value(env_list, env_name):
    return [x for x in env_list if x.name == env_name][0].value


class TestTransformerManager(ResourceTestBase):
    @pytest.fixture
    def mock_kubernetes(self, mocker):
        mock_kubernetes = mocker.MagicMock(name="mock_kubernetes")
        mocker.patch('servicex.transformer_manager.kubernetes', mock_kubernetes)
        mocker.patch('servicex.transformer_manager.client', mock_kubernetes.client)
        return mock_kubernetes

    def test_init_external_kubernetes(self, mock_kubernetes):
        TransformerManager('external-kubernetes')
        mock_kubernetes.config.load_kube_config.assert_called()

    def test_init_internal_kubernetes(self, mock_kubernetes):
        TransformerManager('internal-kubernetes')
        mock_kubernetes.config.load_incluster_config.assert_called()

    def test_init_invalid_config(self, mock_kubernetes):
        with pytest.raises(ValueError):
            TransformerManager('foo')
            mock_kubernetes.config.load_incluster_config.assert_not_called()
            mock_kubernetes.config.load_kube_config.assert_not_called()

    def test_launch_transformer_jobs(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_api = mocker.patch.object(kubernetes.client, 'AppsV1Api')

        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')
        cfg = {
            'TRANSFORMER_CPU_LIMIT': 4,
            'TRANSFORMER_CPU_SCALE_THRESHOLD': 30,
            'TRANSFORMER_MIN_REPLICAS': 3,
            'TRANSFORMER_MAX_REPLICAS': 17,
        }
        client = self._test_client(transformation_manager=transformer,
                                   extra_config=cfg)

        with client.application.app_context():
            transformer.launch_transformer_jobs(
                image='sslhep/servicex-transformer:pytest', request_id='1234', workers=17,
                chunk_size=5000, rabbitmq_uri='ampq://test.com', namespace='my-ns',
                result_destination='kafka', result_format='arrow', x509_secret='x509',
                generated_code_cm=None)
            called_deployment = mock_api.mock_calls[1][2]['body']
            assert called_deployment.spec.replicas == cfg['TRANSFORMER_MIN_REPLICAS']
            assert len(called_deployment.spec.template.spec.containers) == 1
            container = called_deployment.spec.template.spec.containers[0]
            assert container.image == 'sslhep/servicex-transformer:pytest'
            assert container.image_pull_policy == 'Always'
            args = container.args

            assert _arg_value(args, '--rabbit-uri') == 'ampq://test.com'
            assert _arg_value(args, '--chunks') == '5000'
            assert _arg_value(args, '--result-destination') == 'kafka'

            limits = container.resources.limits
            assert "cpu" in limits
            assert limits['cpu'] == 4

            assert mock_api.mock_calls[1][2]['namespace'] == 'my-ns'
            mock_autoscaling.create_namespaced_horizontal_pod_autoscaler.assert_called()
            autoscaling_spec = mock_autoscaling.mock_calls[0][2]['body'].spec
            assert autoscaling_spec.min_replicas == 3
            assert autoscaling_spec.max_replicas == 17
            assert autoscaling_spec.scale_target_ref.name == 'transformer-1234'
            assert autoscaling_spec.target_cpu_utilization_percentage == 30

    def test_launch_transformer_jobs_no_autoscaler(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_api = mocker.patch.object(kubernetes.client, 'AppsV1Api')

        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')
        cfg = {
            'TRANSFORMER_AUTOSCALE_ENABLED': False,
            'TRANSFORMER_CPU_LIMIT': 1,
            'TRANSFORMER_CPU_SCALE_THRESHOLD': 30
        }
        client = self._test_client(
            extra_config=cfg, transformation_manager=transformer
        )

        with client.application.app_context():
            transformer.launch_transformer_jobs(
                image='sslhep/servicex-transformer:pytest', request_id='1234', workers=17,
                chunk_size=5000, rabbitmq_uri='ampq://test.com', namespace='my-ns',
                result_destination='kafka', result_format='arrow', x509_secret='x509',
                generated_code_cm=None)
            called_deployment = mock_api.mock_calls[1][2]['body']
            assert called_deployment.spec.replicas == 17
            mock_autoscaling.create_namespaced_horizontal_pod_autoscaler.assert_not_called()

    def test_launch_transformer_with_hostpath(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_kubernetes = mocker.patch.object(kubernetes.client, 'AppsV1Api')

        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        additional_config = {
            'TRANSFORMER_LOCAL_PATH': '/tmp/foo',
            'TRANSFORMER_CPU_LIMIT': 1,
            'TRANSFORMER_CPU_SCALE_THRESHOLD': 30
        }

        transformer = TransformerManager('external-kubernetes')
        client = self._test_client(
            extra_config=additional_config, transformation_manager=transformer
        )

        with client.application.app_context():
            transformer.launch_transformer_jobs(
                image='sslhep/servicex-transformer:pytest', request_id='1234', workers=17,
                chunk_size=5000, rabbitmq_uri='ampq://test.com', namespace='my-ns',
                result_destination='kafka', result_format='arrow', x509_secret='x509',
                generated_code_cm=None)

            called_job = mock_kubernetes.mock_calls[1][2]['body']
            container = called_job.spec.template.spec.containers[0]
            assert container.volume_mounts[0].mount_path == '/etc/grid-security-ro'
            assert called_job.spec.template.spec.volumes[0].secret.secret_name == 'x509'

            assert container.volume_mounts[1].mount_path == '/data'
            assert called_job.spec.template.spec.volumes[1].host_path.path == '/tmp/foo'

    def test_launch_transformer_jobs_with_generated_code(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_kubernetes = mocker.patch.object(kubernetes.client, 'AppsV1Api')
        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')
        cfg = {
            'TRANSFORMER_CPU_LIMIT': 1,
            'TRANSFORMER_CPU_SCALE_THRESHOLD': 30
        }
        client = self._test_client(
            extra_config=cfg, transformation_manager=transformer
        )

        with client.application.app_context():
            transformer.launch_transformer_jobs(
                image='sslhep/servicex-transformer:pytest', request_id='1234', workers=17,
                chunk_size=5000, rabbitmq_uri='ampq://test.com', namespace='my-ns',
                result_destination='kafka',
                result_format='parquet', x509_secret='x509',
                generated_code_cm="my-config-map")
            called_job = mock_kubernetes.mock_calls[1][2]['body']
            container = called_job.spec.template.spec.containers[0]
            config_map_vol_mount = container.volume_mounts[1]
            assert config_map_vol_mount.name == 'generated-code'
            assert config_map_vol_mount.mount_path == '/generated'

            config_map_vol = called_job.spec.template.spec.volumes[1]
            assert config_map_vol.name == 'generated-code'
            assert config_map_vol.config_map.name == 'my-config-map'

    def test_launch_transformer_jobs_with_object_store(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_kubernetes = mocker.patch.object(kubernetes.client, 'AppsV1Api')
        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')
        my_config = {
            'OBJECT_STORE_ENABLED': True,
            'MINIO_URL_TRANSFORMER': 'rolling-snail-minio:9000',
            'MINIO_ACCESS_KEY': 'itsame',
            'MINIO_SECRET_KEY': 'shhh',
            'TRANSFORMER_CPU_LIMIT': 1,
            'TRANSFORMER_CPU_SCALE_THRESHOLD': 30
        }

        client = self._test_client(
            extra_config=my_config, transformation_manager=transformer
        )

        with client.application.app_context():
            transformer.launch_transformer_jobs(
                image='sslhep/servicex-transformer:pytest', request_id='1234', workers=17,
                chunk_size=5000, rabbitmq_uri='ampq://test.com', namespace='my-ns',
                result_destination='object-store',
                result_format='parquet', x509_secret='x509',
                generated_code_cm=None)
            called_job = mock_kubernetes.mock_calls[1][2]['body']
            container = called_job.spec.template.spec.containers[0]
            args = container.args
            assert _arg_value(args, '--result-destination') == 'object-store'
            assert _arg_value(args, '--result-format') == 'parquet'

            env = container.env
            assert _env_value(env, 'MINIO_URL') == 'rolling-snail-minio:9000'
            assert _env_value(env, 'MINIO_ACCESS_KEY') == 'itsame'
            assert _env_value(env, 'MINIO_SECRET_KEY') == 'shhh'

    def test_launch_transformer_jobs_with_kafka_broker(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_kubernetes = mocker.patch.object(kubernetes.client, 'AppsV1Api')

        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')

        cfg = {
            'TRANSFORMER_CPU_LIMIT': 1,
            'TRANSFORMER_CPU_SCALE_THRESHOLD': 30
        }
        client = self._test_client(
            extra_config=cfg, transformation_manager=transformer
        )

        with client.application.app_context():
            transformer.launch_transformer_jobs(
                image='sslhep/servicex-transformer:pytest', request_id='1234', workers=17,
                chunk_size=5000, rabbitmq_uri='ampq://test.com', namespace='my-ns',
                result_destination='kafka', result_format='arrow',
                kafka_broker='kafka.servicex.org', x509_secret='x509',
                generated_code_cm=None)
            called_job = mock_kubernetes.mock_calls[1][2]['body']
            container = called_job.spec.template.spec.containers[0]
            args = container.args
            assert _arg_value(args, '--result-destination') == 'kafka'
            assert _arg_value(args, '--brokerlist') == 'kafka.servicex.org'

    def test_shutdown_transformer_jobs(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')

        mock_api = mocker.MagicMock(kubernetes.client.AppsV1Api)
        mocker.patch.object(kubernetes.client, 'AppsV1Api',
                            return_value=mock_api)

        mock_core_api = mocker.MagicMock(kubernetes.client.CoreV1Api)
        mocker.patch.object(kubernetes.client, 'CoreV1Api',
                            return_value=mock_core_api)

        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')
        client = self._test_client(transformation_manager=transformer)

        with client.application.app_context():
            transformer.shutdown_transformer_job('1234', 'my-ns')
            mock_api.delete_namespaced_deployment.assert_called_with(name='transformer-1234',
                                                                     namespace='my-ns')
            mock_core_api.delete_namespaced_config_map.assert_called_with(
                name='1234-generated-source',
                namespace='my-ns'
            )
            mock_autoscaling.delete_namespaced_horizontal_pod_autoscaler.assert_called_with(
                name='transformer-1234',
                namespace='my-ns')

    def test_shutdown_transformer_jobs_no_autoscaler(self, mocker):
        import kubernetes

        mocker.patch.object(kubernetes.config, 'load_kube_config')

        mock_api = mocker.MagicMock(kubernetes.client.AppsV1Api)
        mocker.patch.object(kubernetes.client, 'AppsV1Api',
                            return_value=mock_api)

        mock_core_api = mocker.MagicMock(kubernetes.client.CoreV1Api)
        mocker.patch.object(kubernetes.client, 'CoreV1Api',
                            return_value=mock_core_api)

        mock_autoscaling = mocker.Mock()
        mocker.patch.object(kubernetes.client, 'AutoscalingV1Api', return_value=mock_autoscaling)

        transformer = TransformerManager('external-kubernetes')
        client = self._test_client(
            extra_config={'TRANSFORMER_AUTOSCALE_ENABLED': False},
            transformation_manager=transformer,
        )

        with client.application.app_context():
            transformer.shutdown_transformer_job('1234', 'my-ns')
            mock_api.delete_namespaced_deployment.assert_called_with(name='transformer-1234',
                                                                     namespace='my-ns')
            mock_core_api.delete_namespaced_config_map.assert_called_with(
                name='1234-generated-source',
                namespace='my-ns'
            )
            mock_autoscaling.delete_namespaced_horizontal_pod_autoscaler.assert_not_called()

    def test_create_configmap_from_zip(self, mocker):
        import kubernetes
        mocker.patch.object(kubernetes.config, 'load_kube_config')
        mock_api = mocker.MagicMock(kubernetes.client.CoreV1Api)
        mocker.patch.object(kubernetes.client, 'CoreV1Api',
                            return_value=mock_api)

        mock_create_namespaced_config_map = mocker.Mock()
        mock_api.create_namespaced_config_map = mock_create_namespaced_config_map

        transformer = TransformerManager('external-kubernetes')
        mock_zip = mocker.MagicMock(zipfile.ZipFile)
        mock_zip_ext = mocker.Mock()
        mock_zip_ext.filename = 'foo.sh'
        mock_zip.filelist = [mock_zip_ext]
        mock_open = mocker.Mock()
        mock_open.read = mocker.Mock(return_value=b'hi there')
        mock_zip.open = mocker.Mock(return_value=mock_open)

        transformer.create_configmap_from_zip(mock_zip, "my-request", "servicex")

        mock_create_namespaced_config_map.assert_called()
        calls = mock_create_namespaced_config_map.call_args
        "foo.sh" in calls[1]['body'].binary_data.keys()
        assert calls[1]['body'].binary_data['foo.sh'] == base64.\
            b64encode(b"hi there").\
            decode("ascii")
        assert calls[1]['namespace'] == 'servicex'
        assert calls[1]['body'].metadata.name == 'my-request-generated-source'

    def test_get_deployment_status(self, mocker, mock_kubernetes):
        mock_api = mock_kubernetes.client.AppsV1Api.return_value
        mock_deployment_list = mocker.MagicMock(name="mock_deployment_list")
        mock_api.list_namespaced_deployment.return_value = mock_deployment_list
        mock_deployment = mocker.MagicMock(name="mock_deployment")
        mock_deployment_list.items = [mock_deployment]

        transformer_manager = TransformerManager('external-kubernetes')
        client = self._test_client(
            extra_config={'TRANSFORMER_AUTOSCALE_ENABLED': False},
            transformation_manager=transformer_manager,
        )

        with client.application.app_context():
            status = transformer_manager.get_deployment_status("1234")
            assert status == mock_deployment.status

    def test_get_deployment_status_404(self, mocker, mock_kubernetes):
        mock_api = mock_kubernetes.client.AppsV1Api.return_value
        mock_deployment_list = mocker.MagicMock(name="mock_deployment_list")
        mock_api.list_namespaced_deployment.return_value = mock_deployment_list
        mock_deployment_list.items = []

        transformer_manager = TransformerManager('external-kubernetes')
        client = self._test_client(
            extra_config={'TRANSFORMER_AUTOSCALE_ENABLED': False},
            transformation_manager=transformer_manager,
        )

        with client.application.app_context():
            status = transformer_manager.get_deployment_status("1234")
            assert status is None
