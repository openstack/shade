# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from keystoneauth1 import plugin as ksa_plugin

from distutils import version as du_version
import mock
import munch
import testtools

import os_client_config as occ
from os_client_config import cloud_config
import shade
from shade import exc
from shade.tests import fakes
from shade.tests.unit import base


class TestShadeOperator(base.RequestsMockTestCase):

    def setUp(self):
        super(TestShadeOperator, self).setUp()

    def test_operator_cloud(self):
        self.assertIsInstance(self.op_cloud, shade.OperatorCloud)

    @mock.patch.object(shade.OpenStackCloud, '_image_client')
    def test_get_image_name(self, mock_client):

        fake_image = munch.Munch(
            id='22',
            name='22 name',
            status='success')
        mock_client.get.return_value = [fake_image]
        self.assertEqual('22 name', self.op_cloud.get_image_name('22'))
        self.assertEqual('22 name', self.op_cloud.get_image_name('22 name'))

    @mock.patch.object(shade.OpenStackCloud, '_image_client')
    def test_get_image_id(self, mock_client):

        fake_image = munch.Munch(
            id='22',
            name='22 name',
            status='success')
        mock_client.get.return_value = [fake_image]
        self.assertEqual('22', self.op_cloud.get_image_id('22'))
        self.assertEqual('22', self.op_cloud.get_image_id('22 name'))

    @mock.patch.object(cloud_config.CloudConfig, 'get_endpoint')
    def test_get_session_endpoint_provided(self, fake_get_endpoint):
        fake_get_endpoint.return_value = 'http://fake.url'
        self.assertEqual(
            'http://fake.url', self.op_cloud.get_session_endpoint('image'))

    @mock.patch.object(cloud_config.CloudConfig, 'get_session')
    def test_get_session_endpoint_session(self, get_session_mock):
        session_mock = mock.Mock()
        session_mock.get_endpoint.return_value = 'http://fake.url'
        get_session_mock.return_value = session_mock
        self.assertEqual(
            'http://fake.url', self.op_cloud.get_session_endpoint('image'))

    @mock.patch.object(cloud_config.CloudConfig, 'get_session')
    def test_get_session_endpoint_exception(self, get_session_mock):
        class FakeException(Exception):
            pass

        def side_effect(*args, **kwargs):
            raise FakeException("No service")
        session_mock = mock.Mock()
        session_mock.get_endpoint.side_effect = side_effect
        get_session_mock.return_value = session_mock
        self.op_cloud.name = 'testcloud'
        self.op_cloud.region_name = 'testregion'
        with testtools.ExpectedException(
                exc.OpenStackCloudException,
                "Error getting image endpoint on testcloud:testregion:"
                " No service"):
            self.op_cloud.get_session_endpoint("image")

    @mock.patch.object(cloud_config.CloudConfig, 'get_session')
    def test_get_session_endpoint_unavailable(self, get_session_mock):
        session_mock = mock.Mock()
        session_mock.get_endpoint.return_value = None
        get_session_mock.return_value = session_mock
        image_endpoint = self.op_cloud.get_session_endpoint("image")
        self.assertIsNone(image_endpoint)

    @mock.patch.object(cloud_config.CloudConfig, 'get_session')
    def test_get_session_endpoint_identity(self, get_session_mock):
        session_mock = mock.Mock()
        get_session_mock.return_value = session_mock
        self.op_cloud.get_session_endpoint('identity')
        # occ > 1.26.0 fixes keystoneclient construction. Unfortunately, it
        # breaks our mocking of what keystoneclient does here. Since we're
        # close to just getting rid of ksc anyway, just put in a version match
        occ_version = du_version.StrictVersion(occ.__version__)
        if occ_version > du_version.StrictVersion('1.26.0'):
            kwargs = dict(
                interface='public', region_name='RegionOne',
                service_name=None, service_type='identity')
        else:
            kwargs = dict(interface=ksa_plugin.AUTH_INTERFACE)

        session_mock.get_endpoint.assert_called_with(**kwargs)

    @mock.patch.object(cloud_config.CloudConfig, 'get_session')
    def test_has_service_no(self, get_session_mock):
        session_mock = mock.Mock()
        session_mock.get_endpoint.return_value = None
        get_session_mock.return_value = session_mock
        self.assertFalse(self.op_cloud.has_service("image"))

    @mock.patch.object(cloud_config.CloudConfig, 'get_session')
    def test_has_service_yes(self, get_session_mock):
        session_mock = mock.Mock()
        session_mock.get_endpoint.return_value = 'http://fake.url'
        get_session_mock.return_value = session_mock
        self.assertTrue(self.op_cloud.has_service("image"))

    def test_list_hypervisors(self):
        '''This test verifies that calling list_hypervisors results in a call
        to nova client.'''
        self.register_uris([
            dict(method='GET',
                 uri=self.get_mock_url(
                     'compute', 'public', append=['os-hypervisors', 'detail']),
                 json={'hypervisors': [
                     fakes.make_fake_hypervisor('1', 'testserver1'),
                     fakes.make_fake_hypervisor('2', 'testserver2'),
                 ]}),
        ])

        r = self.op_cloud.list_hypervisors()

        self.assertEqual(2, len(r))
        self.assertEqual('testserver1', r[0]['hypervisor_hostname'])
        self.assertEqual('testserver2', r[1]['hypervisor_hostname'])

        self.assert_calls()
