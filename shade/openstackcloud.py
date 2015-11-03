# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import hashlib
import ipaddress
import operator
import os_client_config
import os_client_config.defaults
import threading
import time

from dogpile import cache

from cinderclient.v1 import client as cinder_client
from designateclient.v1 import Client as designate_client
import glanceclient
import glanceclient.exc
from glanceclient.common import utils as glance_utils
from heatclient import client as heat_client
from heatclient.common import template_utils
import keystoneauth1.exceptions
from keystoneauth1 import plugin as ksa_plugin
from keystoneauth1 import session as ksa_session
from keystoneclient.v2_0 import client as k2_client
from keystoneclient.v3 import client as k3_client
from neutronclient.v2_0 import client as neutron_client
from novaclient import client as nova_client
from novaclient import exceptions as nova_exceptions
import swiftclient.client as swift_client
import swiftclient.service as swift_service
import swiftclient.exceptions as swift_exceptions
import troveclient.client as trove_client

from shade.exc import *  # noqa
from shade import _log
from shade import meta
from shade import task_manager
from shade import _tasks
from shade import _utils


OBJECT_MD5_KEY = 'x-object-meta-x-shade-md5'
OBJECT_SHA256_KEY = 'x-object-meta-x-shade-sha256'
IMAGE_MD5_KEY = 'owner_specified.shade.md5'
IMAGE_SHA256_KEY = 'owner_specified.shade.sha256'
# Rackspace returns this for intermittent import errors
IMAGE_ERROR_396 = "Image cannot be imported. Error code: '396'"
DEFAULT_OBJECT_SEGMENT_SIZE = 1073741824  # 1GB
# This halves the current default for Swift
DEFAULT_MAX_FILE_SIZE = (5 * 1024 * 1024 * 1024 + 2) / 2


OBJECT_CONTAINER_ACLS = {
    'public': ".r:*,.rlistings",
    'private': '',
}


def _no_pending_volumes(volumes):
    '''If there are any volumes not in a steady state, don't cache'''
    for volume in volumes:
        if volume['status'] not in ('available', 'error'):
            return False
    return True


def _no_pending_images(images):
    '''If there are any images not in a steady state, don't cache'''
    for image in images:
        if image.status not in ('active', 'deleted', 'killed'):
            return False
    return True


def _no_pending_stacks(stacks):
    '''If there are any stacks not in a steady state, don't cache'''
    for stack in stacks:
        status = stack['status']
        if '_COMPLETE' not in status and '_FAILED' not in status:
            return False
    return True


class OpenStackCloud(object):
    """Represent a connection to an OpenStack Cloud.

    OpenStackCloud is the entry point for all cloud operations, regardless
    of which OpenStack service those operations may ultimately come from.
    The operations on an OpenStackCloud are resource oriented rather than
    REST API operation oriented. For instance, one will request a Floating IP
    and that Floating IP will be actualized either via neutron or via nova
    depending on how this particular cloud has decided to arrange itself.

    :param TaskManager manager: Optional task manager to use for running
                                OpenStack API tasks. Unless you're doing
                                rate limiting client side, you almost
                                certainly don't need this. (optional)
    :param CloudConfig cloud_config: Cloud config object from os-client-config
                                     In the future, this will be the only way
                                     to pass in cloud configuration, but is
                                     being phased in currently.
    """
    _SERVER_LIST_AGE = 5  # TODO(mordred) Make this configurable

    def __init__(
            self,
            cloud_config=None,
            manager=None, **kwargs):

        self.log = _log.setup_logging('shade')
        if not cloud_config:
            config = os_client_config.OpenStackConfig()
            cloud_config = config.get_one_cloud(**kwargs)

        self.name = cloud_config.name
        self.auth = cloud_config.get_auth_args()
        self.region_name = cloud_config.region_name
        self.default_interface = cloud_config.get_interface()
        self.private = cloud_config.config.get('private', False)
        self.api_timeout = cloud_config.config['api_timeout']
        self.image_api_use_tasks = cloud_config.config['image_api_use_tasks']
        self.secgroup_source = cloud_config.config['secgroup_source']
        self.force_ipv4 = cloud_config.force_ipv4

        self._external_networks = []
        self._external_network_name_or_id = cloud_config.config.get(
            'external_network', None)
        self._use_external_network = cloud_config.config.get(
            'use_external_network', True)

        self._internal_networks = []
        self._internal_network_name_or_id = cloud_config.config.get(
            'internal_network', None)
        self._use_internal_network = cloud_config.config.get(
            'use_internal_network', True)

        # Variables to prevent us from going through the network finding
        # logic again if we've done it once. This is different from just
        # the cached value, since "None" is a valid value to find.
        self._external_network_stamp = False
        self._internal_network_stamp = False

        if manager is not None:
            self.manager = manager
        else:
            self.manager = task_manager.TaskManager(
                name=self.name, client=self)

        (self.verify, self.cert) = cloud_config.get_requests_verify_args()

        self._servers = []
        self._servers_time = 0
        self._servers_lock = threading.Lock()

        cache_expiration_time = cloud_config.get_cache_expiration_time()
        cache_class = cloud_config.get_cache_class()
        cache_arguments = cloud_config.get_cache_arguments()
        cache_expiration = cloud_config.get_cache_expiration()

        if cache_class != 'dogpile.cache.null':
            self._cache = cache.make_region(
                function_key_generator=self._make_cache_key
            ).configure(
                cache_class,
                expiration_time=cache_expiration_time,
                arguments=cache_arguments)
        else:
            def _fake_invalidate(unused):
                pass

            class _FakeCache(object):
                def invalidate(self):
                    pass

            # Don't cache list_servers if we're not caching things.
            # Replace this with a more specific cache configuration
            # soon.
            self._SERVER_LIST_AGE = 2
            self._cache = _FakeCache()
            # Undecorate cache decorated methods. Otherwise the call stacks
            # wind up being stupidly long and hard to debug
            for method in _utils._decorated_methods:
                meth_obj = getattr(self, method, None)
                if not meth_obj:
                    continue
                if (hasattr(meth_obj, 'invalidate')
                        and hasattr(meth_obj, 'func')):
                    new_func = functools.partial(meth_obj.func, self)
                    new_func.invalidate = _fake_invalidate
                    setattr(self, method, new_func)

        # If server expiration time is set explicitly, use that. Otherwise
        # fall back to whatever it was before
        self._SERVER_LIST_AGE = cache_expiration.get(
            'server', self._SERVER_LIST_AGE)

        self._container_cache = dict()
        self._file_hash_cache = dict()

        self._keystone_session = None

        self._cinder_client = None
        self._designate_client = None
        self._glance_client = None
        self._glance_endpoint = None
        self._heat_client = None
        self._keystone_client = None
        self._neutron_client = None
        self._nova_client = None
        self._swift_client = None
        self._swift_service = None
        self._trove_client = None

        self._local_ipv6 = _utils.localhost_supports_ipv6()

        self.cloud_config = cloud_config

    def _make_cache_key(self, namespace, fn):
        fname = fn.__name__
        if namespace is None:
            name_key = self.name
        else:
            name_key = '%s:%s' % (self.name, namespace)

        def generate_key(*args, **kwargs):
            arg_key = ','.join(args)
            kw_keys = sorted(kwargs.keys())
            kwargs_key = ','.join(
                ['%s:%s' % (k, kwargs[k]) for k in kw_keys if k != 'cache'])
            ans = "_".join(
                [str(name_key), fname, arg_key, kwargs_key])
            return ans
        return generate_key

    def _get_client(
            self, service_key, client_class, interface_key='endpoint_type',
            pass_version_arg=True, **kwargs):
        try:
            interface = self.cloud_config.get_interface(service_key)
            # trigger exception on lack of service
            self.get_session_endpoint(service_key)
            constructor_args = dict(
                session=self.keystone_session,
                service_name=self.cloud_config.get_service_name(service_key),
                service_type=self.cloud_config.get_service_type(service_key),
                region_name=self.region_name)
            constructor_args.update(kwargs)
            constructor_args[interface_key] = interface
            if pass_version_arg:
                version = self.cloud_config.get_api_version(service_key)
                constructor_args['version'] = version
            client = client_class(**constructor_args)
        except Exception:
            self.log.debug(
                "Couldn't construct {service} object".format(
                    service=service_key), exc_info=True)
            raise
        if client is None:
            raise OpenStackCloudException(
                "Failed to instantiate {service} client."
                " This could mean that your credentials are wrong.".format(
                    service=service_key))
        return client

    @property
    def nova_client(self):
        if self._nova_client is None:
            self._nova_client = self._get_client(
                'compute', nova_client.Client)
        return self._nova_client

    def _get_identity_client_class(self):
        if self.cloud_config.get_api_version('identity') == '3':
            return k3_client.Client
        elif self.cloud_config.get_api_version('identity') in ('2', '2.0'):
            return k2_client.Client
        raise OpenStackCloudException(
            "Unknown identity API version: {version}".format(
                version=self.cloud_config.get_api_version('identity')))

    @property
    def keystone_session(self):
        if self._keystone_session is None:

            try:
                keystone_auth = self.cloud_config.get_auth()
                if not keystone_auth:
                    raise OpenStackCloudException(
                        "Problem with auth parameters")
                self._keystone_session = ksa_session.Session(
                    auth=keystone_auth,
                    verify=self.verify,
                    cert=self.cert,
                    timeout=self.api_timeout)
            except Exception as e:
                raise OpenStackCloudException(
                    "Error authenticating to keystone: %s " % str(e))
        return self._keystone_session

    @property
    def keystone_client(self):
        if self._keystone_client is None:
            self._keystone_client = self._get_client(
                'identity', self._get_identity_client_class())
        return self._keystone_client

    @property
    def service_catalog(self):
        return self.keystone_session.auth.get_access(
            self.keystone_session).service_catalog.catalog

    @property
    def auth_token(self):
        # Keystone's session will reuse a token if it is still valid.
        # We don't need to track validity here, just get_token() each time.
        return self.keystone_session.get_token()

    @property
    def _project_manager(self):
        # Keystone v2 calls this attribute tenants
        # Keystone v3 calls it projects
        # Yay for usable APIs!
        if self.cloud_config.get_api_version('identity').startswith('2'):
            return self.keystone_client.tenants
        return self.keystone_client.projects

    def _get_project_param_dict(self, name_or_id):
        project_dict = dict()
        if name_or_id:
            project = self.get_project(name_or_id)
            if not project:
                return project_dict
            if self.cloud_config.get_api_version('identity') == '3':
                project_dict['default_project'] = project['id']
            else:
                project_dict['tenant_id'] = project['id']
        return project_dict

    def _get_domain_param_dict(self, domain_id):
        """Get a useable domain."""

        # Keystone v3 requires domains for user and project creation. v2 does
        # not. However, keystone v2 does not allow user creation by non-admin
        # users, so we can throw an error to the user that does not need to
        # mention api versions
        if self.cloud_config.get_api_version('identity') == '3':
            if not domain_id:
                raise OpenStackCloudException(
                    "User creation requires an explicit domain_id argument.")
            else:
                return {'domain': domain_id}
        else:
            return {}

    def _get_identity_params(self, domain_id=None, project=None):
        """Get the domain and project/tenant parameters if needed.

        keystone v2 and v3 are divergent enough that we need to pass or not
        pass project or tenant_id or domain or nothing in a sane manner.
        """
        ret = {}
        ret.update(self._get_domain_param_dict(domain_id))
        ret.update(self._get_project_param_dict(project))
        return ret

    @_utils.cache_on_arguments()
    def list_projects(self):
        """List Keystone Projects.

        :returns: a list of dicts containing the project description.

        :raises: ``OpenStackCloudException``: if something goes wrong during
            the openstack API call.
        """
        try:
            projects = self.manager.submitTask(_tasks.ProjectList())
        except Exception as e:
            self.log.debug("Failed to list projects", exc_info=True)
            raise OpenStackCloudException(str(e))
        return meta.obj_list_to_dict(projects)

    def search_projects(self, name_or_id=None, filters=None):
        """Seach Keystone projects.

        :param name: project name or id.
        :param filters: a dict containing additional filters to use.

        :returns: a list of dict containing the role description

        :raises: ``OpenStackCloudException``: if something goes wrong during
            the openstack API call.
        """
        projects = self.list_projects()
        return _utils._filter_list(projects, name_or_id, filters)

    def get_project(self, name_or_id, filters=None):
        """Get exactly one Keystone project.

        :param id: project name or id.
        :param filters: a dict containing additional filters to use.

        :returns: a list of dicts containing the project description.

        :raises: ``OpenStackCloudException``: if something goes wrong during
            the openstack API call.
        """
        return _utils._get_entity(self.search_projects, name_or_id, filters)

    def update_project(self, name_or_id, description=None, enabled=True):
        try:
            proj = self.get_project(name_or_id)
            if not proj:
                raise OpenStackCloudException(
                    "Project %s not found." % name_or_id)

            params = {}
            if self.api_versions['identity'] == '3':
                params['project'] = proj['id']
            else:
                params['tenant_id'] = proj['id']

            project = self.manager.submitTask(_tasks.ProjectUpdate(
                description=description,
                enabled=enabled,
                **params))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in updating project {project}: {message}".format(
                    project=name_or_id, message=str(e)))
        self.list_projects.invalidate()
        return meta.obj_to_dict(project)

    def create_project(
            self, name, description=None, domain_id=None, enabled=True):
        """Create a project."""
        try:
            params = self._get_domain_param_dict(domain)
            if self.api_versions['identity'] == '3':
                params['name'] = name
            else:
                params['tenant_name'] = name

            project = self.manager.submitTask(_tasks.ProjectCreate(
                project_name=name, description=description, enabled=enabled,
                **params))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in creating project {project}: {message}".format(
                    project=name, message=str(e)))
        self.list_projects.invalidate()
        return meta.obj_to_dict(project)

    def delete_project(self, name_or_id):
        try:
            project = self.update_project(name_or_id, enabled=False)
            params = {}
            if self.api_versions['identity'] == '3':
                params['project'] = project['id']
            else:
                params['tenant'] = project['id']
            self.manager.submitTask(_tasks.ProjectDelete(**params))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in deleting project {project}: {message}".format(
                    project=name_or_id, message=str(e)))

    @_utils.cache_on_arguments()
    def list_users(self):
        """List Keystone Users.

        :returns: a list of dicts containing the user description.

        :raises: ``OpenStackCloudException``: if something goes wrong during
            the openstack API call.
        """
        try:
            users = self.manager.submitTask(_tasks.UserList())
        except Exception as e:
            raise OpenStackCloudException(
                "Failed to list users: {0}".format(str(e))
            )
        return _utils.normalize_users(meta.obj_list_to_dict(users))

    def search_users(self, name_or_id=None, filters=None):
        """Seach Keystone users.

        :param string name: user name or id.
        :param dict filters: a dict containing additional filters to use.

        :returns: a list of dict containing the role description

        :raises: ``OpenStackCloudException``: if something goes wrong during
            the openstack API call.
        """
        users = self.list_users()
        return _utils._filter_list(users, name_or_id, filters)

    def get_user(self, name_or_id, filters=None):
        """Get exactly one Keystone user.

        :param string name_or_id: user name or id.
        :param dict filters: a dict containing additional filters to use.

        :returns: a single dict containing the user description.

        :raises: ``OpenStackCloudException``: if something goes wrong during
            the openstack API call.
        """
        return _utils._get_entity(self.search_users, name_or_id, filters)

    def get_user_by_id(self, user_id, normalize=True):
        """Get a Keystone user by ID.

        :param string user_id: user ID
        :param bool normalize: Flag to control dict normalization

        :returns: a single dict containing the user description
        """
        try:
            user = meta.obj_to_dict(
                self.manager.submitTask(_tasks.UserGet(user=user_id))
            )
        except Exception as e:
            raise OpenStackCloudException(
                "Error getting user with ID {user_id}: {message}".format(
                    user_id=user_id, message=str(e)))
        if user and normalize:
            return _utils.normalize_users([user])[0]
        return user

    # NOTE(Shrews): Keystone v2 supports updating only name, email and enabled.
    @_utils.valid_kwargs('name', 'email', 'enabled', 'domain_id', 'password',
                         'description', 'default_project')
    def update_user(self, name_or_id, **kwargs):
        self.list_users.invalidate(self)
        user = self.get_user(name_or_id)
        # normalized dict won't work
        kwargs['user'] = self.get_user_by_id(user['id'], normalize=False)

        if self.cloud_config.get_api_version('identity') != '3':
            # Do not pass v3 args to a v2 keystone.
            kwargs.pop('domain_id', None)
            kwargs.pop('password', None)
            kwargs.pop('description', None)
            kwargs.pop('default_project', None)
        elif 'domain_id' in kwargs:
            # The incoming parameter is domain_id in order to match the
            # parameter name in create_user(), but UserUpdate() needs it
            # to be domain.
            kwargs['domain'] = kwargs.pop('domain_id')

        try:
            user = self.manager.submitTask(_tasks.UserUpdate(**kwargs))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in updating user {user}: {message}".format(
                    user=name_or_id, message=str(e)))
        self.list_users.invalidate(self)
        return _utils.normalize_users([meta.obj_to_dict(user)])[0]

    def create_user(
            self, name, password=None, email=None, default_project=None,
            enabled=True, domain_id=None):
        """Create a user."""
        try:
            identity_params = self._get_identity_params(
                domain_id, default_project)
            user = self.manager.submitTask(_tasks.UserCreate(
                name=name, password=password, email=email,
                enabled=enabled, **identity_params))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in creating user {user}: {message}".format(
                    user=name, message=str(e)))
        self.list_users.invalidate(self)
        return _utils.normalize_users([meta.obj_to_dict(user)])[0]

    def delete_user(self, name_or_id):
        self.list_users.invalidate(self)
        user = self.get_user(name_or_id)
        if not user:
            self.log.debug(
                "User {0} not found for deleting".format(name_or_id))
            return False

        # normalized dict won't work
        user = self.get_user_by_id(user['id'], normalize=False)
        try:
            self.manager.submitTask(_tasks.UserDelete(user=user))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in deleting user {user}: {message}".format(
                    user=name_or_id, message=str(e)))
        self.list_users.invalidate(self)
        return True

    @property
    def glance_client(self):
        if self._glance_client is None:
            endpoint, version = glance_utils.strip_version(
                self.get_session_endpoint(service_key='image'))
            # TODO(mordred): Put check detected vs. configured version
            # and warn if they're different.
            self._glance_client = self._get_client(
                'image', glanceclient.Client, interface_key='interface',
                endpoint=endpoint)
        return self._glance_client

    @property
    def heat_client(self):
        if self._heat_client is None:
            self._heat_client = self._get_client(
                'orchestration', heat_client.Client)
        return self._heat_client

    def get_template_contents(
            self, template_file=None, template_url=None,
            template_object=None, files=None):
        try:
            return template_utils.get_template_contents(
                template_file=template_file, template_url=template_url,
                template_object=template_object, files=files)
        except Exception as e:
            raise OpenStackCloudException(
                "Error in processing template files: %s" % str(e))

    @property
    def swift_client(self):
        if self._swift_client is None:
            try:
                token = self.keystone_session.get_token()
                endpoint = self.get_session_endpoint(
                    service_key='object-store')
                self._swift_client = swift_client.Connection(
                    preauthurl=endpoint,
                    preauthtoken=token,
                    auth_version=self.cloud_config.get_api_version('identity'),
                    os_options=dict(
                        auth_token=token,
                        object_storage_url=endpoint,
                        region_name=self.region_name),
                    timeout=self.api_timeout,
                )
            except OpenStackCloudException:
                raise
            except Exception as e:
                raise OpenStackCloudException(
                    "Error constructing swift client: %s", str(e))
        return self._swift_client

    @property
    def swift_service(self):
        if self._swift_service is None:
            try:
                endpoint = self.get_session_endpoint(
                    service_key='object-store')
                options = dict(os_auth_token=self.auth_token,
                               os_storage_url=endpoint,
                               os_region_name=self.region_name)
                self._swift_service = swift_service.SwiftService(
                    options=options)
            except OpenStackCloudException:
                raise
            except Exception as e:
                raise OpenStackCloudException(
                    "Error constructing swift client: %s", str(e))
        return self._swift_service

    @property
    def cinder_client(self):

        if self._cinder_client is None:
            self._cinder_client = self._get_client(
                'volume', cinder_client.Client)
        return self._cinder_client

    @property
    def trove_client(self):
        if self._trove_client is None:
            self.get_session_endpoint(service_key='database')
            # Make the connection - can't use keystone session until there
            # is one
            self._trove_client = trove_client.Client(
                self.cloud_config.get_api_version('database'),
                session=self.keystone_session,
                region_name=self.region_name,
                service_type=self.cloud_config.get_service_type('database'),
            )

            if self._trove_client is None:
                raise OpenStackCloudException(
                    "Failed to instantiate Trove client."
                    " This could mean that your credentials are wrong.")

            self._trove_client = self._get_client(
                'database', trove_client.Client)
        return self._trove_client

    @property
    def neutron_client(self):
        if self._neutron_client is None:
            self._neutron_client = self._get_client(
                'network', neutron_client.Client, pass_version_arg=False)
        return self._neutron_client

    @property
    def designate_client(self):
        if self._designate_client is None:
            self._designate_client = self._get_client(
                'dns', designate_client.Client)
        return self._designate_client

    def create_stack(
            self, name,
            template_file=None, template_url=None,
            template_object=None, files=None,
            rollback=True,
            wait=False, timeout=180,
            **parameters):
        tpl_files, template = template_utils.get_template_contents(
            template_file=template_file,
            template_url=template_url,
            template_object=template_object,
            files=files)
        params = dict(
            stack_name=name,
            disable_rollback=not rollback,
            parameters=parameters,
            template=template,
            files=tpl_files,
        )
        try:
            stack = self.manager.submitTask(_tasks.StackCreate(**params))
        except Exception as e:
            raise OpenStackCloudException(
                "Error creating stack {name}: {message}".format(
                    name=name, message=e.message))
        if not wait:
            return stack
        for count in _iterate_timeout(
                timeout,
                "Timed out waiting for heat stack to finish"):
            if self.get_stack(name, cache=False):
                return stack

    def delete_stack(self, name_or_id):
        """Delete a Heat Stack

        :param name_or_id: Stack name or id.

        :returns: True if delete succeeded, False otherwise.

        :raises: ``OpenStackCloudException`` if something goes wrong during
            the openstack API call
        """
        stack = self.get_stack(name_or_id=name_or_id)
        if stack is None:
            self.log.debug("Stack %s not found for deleting" % name_or_id)
            return False

        try:
            self.manager.submitTask(_tasks.StackDelete(id=stack['id']))
        except Exception:
            raise OpenStackCloudException(
                "Failed to delete stack {id}".format(id=stack['id']))
        return True

    def get_name(self):
        return self.name

    def get_region(self):
        return self.region_name

    def get_flavor_name(self, flavor_id):
        flavor = self.get_flavor(flavor_id)
        if flavor:
            return flavor['name']
        return None

    def get_flavor_by_ram(self, ram, include=None):
        """Get a flavor based on amount of RAM available.

        Finds the flavor with the least amount of RAM that is at least
        as much as the specified amount. If `include` is given, further
        filter based on matching flavor name.

        :param int ram: Minimum amount of RAM.
        :param string include: If given, will return a flavor whose name
            contains this string as a substring.
        """
        flavors = self.list_flavors()
        for flavor in sorted(flavors, key=operator.itemgetter('ram')):
            if (flavor['ram'] >= ram and
                    (not include or include in flavor['name'])):
                return flavor
        raise OpenStackCloudException(
            "Could not find a flavor with {ram} and '{include}'".format(
                ram=ram, include=include))

    def get_session_endpoint(self, service_key):
        override_endpoint = self.cloud_config.get_endpoint(service_key)
        if override_endpoint:
            return override_endpoint
        try:
            # keystone is a special case in keystone, because what?
            if service_key == 'identity':
                endpoint = self.keystone_session.get_endpoint(
                    interface=ksa_plugin.AUTH_INTERFACE)
            else:
                endpoint = self.keystone_session.get_endpoint(
                    service_type=self.cloud_config.get_service_type(
                        service_key),
                    service_name=self.cloud_config.get_service_name(
                        service_key),
                    interface=self.cloud_config.get_interface(service_key),
                    region_name=self.region_name)
        except keystoneauth1.exceptions.catalog.EndpointNotFound as e:
            self.log.debug(
                "Endpoint not found in %s cloud: %s", self.name, str(e))
            endpoint = None
        except Exception as e:
            raise OpenStackCloudException(
                "Error getting %s endpoint: %s" % (service_key, str(e)))
        return endpoint

    def has_service(self, service_key):
        if not self.cloud_config.config.get('has_%s' % service_key, True):
            self.log.debug(
                "Overriding {service_key} entry in catalog per config".format(
                    service_key=service_key))
            return False
        try:
            endpoint = self.get_session_endpoint(service_key)
        except OpenStackCloudException:
            return False
        if endpoint:
            return True
        else:
            return False

    @_utils.cache_on_arguments()
    def _nova_extensions(self):
        extensions = set()

        try:
            resp, body = self.manager.submitTask(
                _tasks.NovaUrlGet(url='/extensions'))
            for x in body['extensions']:
                extensions.add(x['alias'])
        except Exception as e:
            raise OpenStackCloudException(
                "error fetching extension list for nova: {msg}".format(
                    msg=str(e)))

        return extensions

    def _has_nova_extension(self, extension_name):
        return extension_name in self._nova_extensions()

    def search_keypairs(self, name_or_id=None, filters=None):
        keypairs = self.list_keypairs()
        return _utils._filter_list(keypairs, name_or_id, filters)

    def search_networks(self, name_or_id=None, filters=None):
        """Search OpenStack networks

        :param name_or_id: Name or id of the desired network.
        :param filters: a dict containing additional filters to use. e.g.
                        {'router:external': True}

        :returns: a list of dicts containing the network description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        networks = self.list_networks(filters)
        return _utils._filter_list(networks, name_or_id, filters)

    def search_routers(self, name_or_id=None, filters=None):
        """Search OpenStack routers

        :param name_or_id: Name or id of the desired router.
        :param filters: a dict containing additional filters to use. e.g.
                        {'admin_state_up': True}

        :returns: a list of dicts containing the router description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        routers = self.list_routers(filters)
        return _utils._filter_list(routers, name_or_id, filters)

    def search_netpartitions(self, name_or_id=None, filters=None):
        """Search OpenStack netpartitions

        :param name_or_id: Name or id of the desired netpartition.
        :param filters: a dict containing additional filters to use.

        :returns: a list of dicts containing the netpartition description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        netpartitions = self.list_netpartitions(filters)
        return _utils._filter_list(netpartitions, name_or_id, filters)

    def search_subnets(self, name_or_id=None, filters=None):
        """Search OpenStack subnets

        :param name_or_id: Name or id of the desired subnet.
        :param filters: a dict containing additional filters to use. e.g.
                        {'enable_dhcp': True}

        :returns: a list of dicts containing the subnet description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        subnets = self.list_subnets(filters)
        return _utils._filter_list(subnets, name_or_id, filters)

    def search_ports(self, name_or_id=None, filters=None):
        """Search OpenStack ports

        :param name_or_id: Name or id of the desired port.
        :param filters: a dict containing additional filters to use. e.g.
                        {'device_id': '2711c67a-b4a7-43dd-ace7-6187b791c3f0'}

        :returns: a list of dicts containing the port description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        ports = self.list_ports(filters)
        return _utils._filter_list(ports, name_or_id, filters)

    def search_volumes(self, name_or_id=None, filters=None):
        volumes = self.list_volumes()
        return _utils._filter_list(
            volumes, name_or_id, filters, name_key='display_name')

    def search_volume_snapshots(self, name_or_id=None, filters=None):
        volumesnapshots = self.list_volume_snapshots()
        return _utils._filter_list(
            volumesnapshots, name_or_id, filters, name_key='display_name')

    def search_flavors(self, name_or_id=None, filters=None):
        flavors = self.list_flavors()
        return _utils._filter_list(flavors, name_or_id, filters)

    def search_security_groups(self, name_or_id=None, filters=None):
        groups = self.list_security_groups()
        return _utils._filter_list(groups, name_or_id, filters)

    def search_servers(self, name_or_id=None, filters=None, detailed=False):
        servers = self.list_servers(detailed=detailed)
        return _utils._filter_list(servers, name_or_id, filters)

    def search_images(self, name_or_id=None, filters=None):
        images = self.list_images()
        return _utils._filter_list(images, name_or_id, filters)

    def search_floating_ip_pools(self, name=None, filters=None):
        pools = self.list_floating_ip_pools()
        return _utils._filter_list(pools, name, filters)

    # Note (dguerri): when using Neutron, this can be optimized using
    # server-side search.
    # There are some cases in which such optimization is not possible (e.g.
    # nested attributes or list of objects) so we need to use the client-side
    # filtering when we can't do otherwise.
    # The same goes for all neutron-related search/get methods!
    def search_floating_ips(self, id=None, filters=None):
        floating_ips = self.list_floating_ips()
        return _utils._filter_list(floating_ips, id, filters)

    def search_zones(self, name_or_id=None, filters=None):
        zones = self.list_zones()
        return _utils._filter_list(zones, name_or_id, filters)

    def _search_records(self, zone_id, name_or_id=None, filters=None):
        records = self._list_records(zone_id=zone_id)
        return _utils._filter_list(records, name_or_id, filters)

    def search_stacks(self, name_or_id=None, filters=None):
        """Search Heat stacks.

        :param name_or_id: Name or id of the desired stack.
        :param filters: a dict containing additional filters to use. e.g.
                {'stack_status': 'CREATE_COMPLETE'}

        :returns: a list of dict containing the stack description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        stacks = self.list_stacks()
        return _utils._filter_list(
            stacks, name_or_id, filters, name_name='stack_name')

    def list_keypairs(self):
        """List all available keypairs.

        :returns: A list of keypair dicts.

        """
        try:
            return meta.obj_list_to_dict(
                self.manager.submitTask(_tasks.KeypairList())
            )
        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching keypair list: %s" % str(e))

    def list_networks(self, filters=None):
        """List all available networks.

        :param filters: (optional) dict of filter conditions to push down
        :returns: A list of network dicts.

        """
        # Translate None from search interface to empty {} for kwargs below
        if not filters:
            filters = {}
        with _utils.neutron_exceptions("Error fetching network list"):
            return self.manager.submitTask(
                _tasks.NetworkList(**filters))['networks']

    def list_routers(self, filters=None):
        """List all available routers.

        :param filters: (optional) dict of filter conditions to push down
        :returns: A list of router dicts.

        """
        # Translate None from search interface to empty {} for kwargs below
        if not filters:
            filters = {}
        with _utils.neutron_exceptions("Error fetching router list"):
            return self.manager.submitTask(
                _tasks.RouterList(**filters))['routers']

    def list_netpartitions(self, filters=None):
        """List all available netpartitions.

        :param filters: (optional) dict of filter conditions to push down
        :returns: A list of subnet dicts.

        """
        # Translate None from search interface to empty {} for kwargs below
        if not filters:
            filters = {}
        with self._neutron_exceptions("Error fetching netpartition list"):
            return self.manager.submitTask(
                _tasks.NetpartitionList(**filters))['net_partitions']

    def list_subnets(self, filters=None):
        """List all available subnets.

        :param filters: (optional) dict of filter conditions to push down
        :returns: A list of subnet dicts.

        """
        # Translate None from search interface to empty {} for kwargs below
        if not filters:
            filters = {}
        with _utils.neutron_exceptions("Error fetching subnet list"):
            return self.manager.submitTask(
                _tasks.SubnetList(**filters))['subnets']

    def list_ports(self, filters=None):
        """List all available ports.

        :param filters: (optional) dict of filter conditions to push down
        :returns: A list of port dicts.

        """
        # Translate None from search interface to empty {} for kwargs below
        if not filters:
            filters = {}
        with _utils.neutron_exceptions("Error fetching port list"):
            return self.manager.submitTask(_tasks.PortList(**filters))['ports']

    @_utils.cache_on_arguments(should_cache_fn=_no_pending_volumes)
    def list_volumes(self, cache=True):
        """List all available volumes.

        :returns: A list of volume dicts.

        """
        if not cache:
            warnings.warn('cache argument to list_volumes is deprecated. Use '
                          'invalidate instead.')
        try:
            return meta.obj_list_to_dict(
                self.manager.submitTask(_tasks.VolumeList())
            )
        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching volume list: %s" % e)

    @_utils.cache_on_arguments()
    def list_flavors(self):
        """List all available flavors.

        :returns: A list of flavor dicts.

        """
        try:
            return meta.obj_list_to_dict(
                self.manager.submitTask(_tasks.FlavorList(is_public=None))
            )
        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching flavor list: %s" % e)

    @_utils.cache_on_arguments(should_cache_fn=_no_pending_stacks)
    def list_stacks(self):
        """List all Heat stacks.

        :returns: a list of dict containing the stack description.

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call.
        """
        try:
            stacks = self.manager.submitTask(_tasks.StackList())
        except Exception as e:
            raise OpenStackCloudException(str(e))
        return meta.obj_list_to_dict(stacks)

    def list_server_security_groups(self, server):
        """List all security groups associated with the given server.

        :returns: A list of security group dicts.
        """

        groups = meta.obj_list_to_dict(
            self.manager.submitTask(
                _tasks.ServerListSecurityGroups(server=server['id'])))

        return _utils.normalize_nova_secgroups(groups)

    def list_security_groups(self):
        """List all available security groups.

        :returns: A list of security group dicts.

        """
        # Handle neutron security groups
        if self.secgroup_source == 'neutron':
            # Neutron returns dicts, so no need to convert objects here.
            with _utils.neutron_exceptions(
                    "Error fetching security group list"):
                return self.manager.submitTask(
                    _tasks.NeutronSecurityGroupList())['security_groups']

        # Handle nova security groups
        elif self.secgroup_source == 'nova':
            try:
                groups = meta.obj_list_to_dict(
                    self.manager.submitTask(_tasks.NovaSecurityGroupList())
                )
            except Exception:
                raise OpenStackCloudException(
                    "Error fetching security group list"
                )
            return _utils.normalize_nova_secgroups(groups)

        # Security groups not supported
        else:
            raise OpenStackCloudUnavailableFeature(
                "Unavailable feature: security groups"
            )

    def list_servers(self, detailed=False):
        """List all available servers.

        :returns: A list of server dicts.

        """
        if (time.time() - self._servers_time) >= self._SERVER_LIST_AGE:
            # Since we're using cached data anyway, we don't need to
            # have more than one thread actually submit the list
            # servers task.  Let the first one submit it while holding
            # a lock, and the non-blocking acquire method will cause
            # subsequent threads to just skip this and use the old
            # data until it succeeds.
            # For the first time, when there is no data, make the call
            # blocking.
            if self._servers_lock.acquire(len(self._servers) == 0):
                try:
                    self._servers = self._list_servers(detailed=detailed)
                    self._servers_time = time.time()
                finally:
                    self._servers_lock.release()
        return self._servers

    def _list_servers(self, detailed=False):
        try:
            servers = meta.obj_list_to_dict(
                self.manager.submitTask(_tasks.ServerList()))

            if detailed:
                return [
                    meta.get_hostvars_from_server(self, server)
                    for server in servers
                ]
            else:
                return servers
        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching server list: %s" % e)

    @_utils.cache_on_arguments(should_cache_fn=_no_pending_images)
    def list_images(self, filter_deleted=True):
        """Get available glance images.

        :param filter_deleted: Control whether deleted images are returned.
        :returns: A list of glance images.
        """
        # First, try to actually get images from glance, it's more efficient
        images = []
        try:

            # Creates a generator - does not actually talk to the cloud API
            # hardcoding page size for now. We'll have to get MUCH smarter
            # if we want to deal with page size per unit of rate limiting
            image_gen = self.glance_client.images.list(page_size=1000)
            # Deal with the generator to make a list
            image_list = self.manager.submitTask(
                _tasks.GlanceImageList(image_gen=image_gen))

            if image_list:
                if getattr(image_list[0], 'validate', None):
                    # glanceclient returns a "warlock" object if you use v2
                    image_list = meta.warlock_list_to_dict(image_list)
                else:
                    # glanceclient returns a normal object if you use v1
                    image_list = meta.obj_list_to_dict(image_list)

        except glanceclient.exc.HTTPInternalServerError:
            # We didn't have glance, let's try nova
            # If this doesn't work - we just let the exception propagate
            try:
                image_list = meta.obj_list_to_dict(
                    self.manager.submitTask(_tasks.NovaImageList())
                )
            except Exception as e:
                raise OpenStackCloudException(
                    "Error fetching image list: %s" % e)

        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching image list: %s" % e)

        for image in image_list:
            # The cloud might return DELETED for invalid images.
            # While that's cute and all, that's an implementation detail.
            if not filter_deleted:
                images.append(image)
            elif image.status != 'DELETED':
                images.append(image)
        return images

    def list_floating_ip_pools(self):
        """List all available floating IP pools.

        :returns: A list of floating IP pool dicts.

        """
        if not self._has_nova_extension('os-floating-ip-pools'):
            raise OpenStackCloudUnavailableExtension(
                'Floating IP pools extension is not available on target cloud')

        try:
            return meta.obj_list_to_dict(
                self.manager.submitTask(_tasks.FloatingIPPoolList())
            )
        except Exception as e:
            raise OpenStackCloudException(
                "error fetching floating IP pool list: {msg}".format(
                    msg=str(e)))

    def list_floating_ips(self):
        """List all available floating IPs.

        :returns: A list of floating IP dicts.

        """
        if self.has_service('network'):
            try:
                return _utils.normalize_neutron_floating_ips(
                    self._neutron_list_floating_ips())
            except OpenStackCloudURINotFound as e:
                self.log.debug(
                    "Something went wrong talking to neutron API: "
                    "'{msg}'. Trying with Nova.".format(msg=str(e)))
                # Fall-through, trying with Nova

        floating_ips = self._nova_list_floating_ips()
        return _utils.normalize_nova_floating_ips(floating_ips)

    def _neutron_list_floating_ips(self):
        with _utils.neutron_exceptions("error fetching floating IPs list"):
            return self.manager.submitTask(
                _tasks.NeutronFloatingIPList())['floatingips']

    def _nova_list_floating_ips(self):
        try:
            return meta.obj_list_to_dict(
                self.manager.submitTask(_tasks.NovaFloatingIPList()))
        except Exception as e:
            raise OpenStackCloudException(
                "error fetching floating IPs list: {msg}".format(msg=str(e)))

    def list_zones(self):
        """List all available DNS zones.

        :returns: A list of zone dicts.

        """
        try:
            return self.manager.submitTask(_tasks.ZoneList())
        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching zone list: %s" % e)

    def _list_records(self, zone_id):
        # TODO(mordred) switch domain= to zone= after the Big Rename
        try:
            return self.manager.submitTask(_tasks.RecordList(domain=zone_id))
        except Exception as e:
            raise OpenStackCloudException(
                "Error fetching record list: %s" % e)

    def use_external_network(self):
        return self._use_external_network

    def use_internal_network(self):
        return self._use_internal_network

    def _get_network(
            self,
            name_or_id,
            use_network_func,
            network_cache,
            network_stamp,
            filters):
        if not use_network_func():
            return []
        if network_cache:
            return network_cache
        if network_stamp:
            return []
        if not self.has_service('network'):
            return []
        if name_or_id:
            ext_net = self.get_network(name_or_id)
            if not ext_net:
                raise OpenStackCloudException(
                    "Network {network} was provided for external"
                    " access and that network could not be found".format(
                        network=name_or_id))
            else:
                return []
        try:
            # TODO(mordred): Rackspace exposes neutron but it does not
            # work. I think that overriding what the service catalog
            # reports should be a thing os-client-config should handle
            # in a vendor profile - but for now it does not. That means
            # this search_networks can just totally fail. If it does though,
            # that's fine, clearly the neutron introspection is not going
            # to work.
            return self.search_networks(filters=filters)
        except OpenStackCloudException:
            pass
        return []

    def get_external_networks(self):
        """Return the networks that are configured to route northbound.

        :returns: A list of network dicts if one is found
        """
        self._external_networks = self._get_network(
            self._external_network_name_or_id,
            self.use_external_network,
            self._external_networks,
            self._external_network_stamp,
            filters={'router:external': True})
        self._external_network_stamp = True
        return self._external_networks

    def get_internal_networks(self):
        """Return the networks that are configured to not route northbound.

        :returns: A list of network dicts if one is found
        """
        self._internal_networks = self._get_network(
            self._internal_network_name_or_id,
            self.use_internal_network,
            self._internal_networks,
            self._internal_network_stamp,
            filters={
                'router:external': False,
            })
        self._internal_network_stamp = True
        return self._internal_networks

    def get_keypair(self, name_or_id, filters=None):
        """Get a keypair by name or ID.

        :param name_or_id: Name or ID of the keypair.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A keypair dict or None if no matching keypair is
        found.

        """
        return _utils._get_entity(self.search_keypairs, name_or_id, filters)

    def get_network(self, name_or_id, filters=None):
        """Get a network by name or ID.

        :param name_or_id: Name or ID of the network.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A network dict or None if no matching network is
        found.

        """
        return _utils._get_entity(self.search_networks, name_or_id, filters)

    def get_router(self, name_or_id, filters=None):
        """Get a router by name or ID.

        :param name_or_id: Name or ID of the router.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A router dict or None if no matching router is
        found.

        """
        return _utils._get_entity(self.search_routers, name_or_id, filters)

    def get_netpartition(self, name_or_id, filters=None):
        """Get a netpartition by name or ID.

        :param name_or_id: Name or ID of the netpartition.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A netpartition dict or None if no matching netpartition is
        found.

        """
        return _utils._get_entity(self.search_netpartitions, name_or_id, filters)

    def get_subnet(self, name_or_id, filters=None):
        """Get a subnet by name or ID.

        :param name_or_id: Name or ID of the subnet.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A subnet dict or None if no matching subnet is
        found.

        """
        return _utils._get_entity(self.search_subnets, name_or_id, filters)

    def get_port(self, name_or_id, filters=None):
        """Get a port by name or ID.

        :param name_or_id: Name or ID of the port.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A port dict or None if no matching port is found.

        """
        return _utils._get_entity(self.search_ports, name_or_id, filters)

    def get_volume(self, name_or_id, filters=None):
        """Get a volume by name or ID.

        :param name_or_id: Name or ID of the volume.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A volume dict or None if no matching volume is
        found.

        """
        return _utils._get_entity(self.search_volumes, name_or_id, filters)

    def get_flavor(self, name_or_id, filters=None):
        """Get a flavor by name or ID.

        :param name_or_id: Name or ID of the flavor.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A flavor dict or None if no matching flavor is
        found.

        """
        return _utils._get_entity(self.search_flavors, name_or_id, filters)

    def get_security_group(self, name_or_id, filters=None):
        """Get a security group by name or ID.

        :param name_or_id: Name or ID of the security group.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A security group dict or None if no matching
                  security group is found.

        """
        return _utils._get_entity(
            self.search_security_groups, name_or_id, filters)

    def get_server(self, name_or_id=None, filters=None, detailed=False):
        """Get a server by name or ID.

        :param name_or_id: Name or ID of the server.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A server dict or None if no matching server is
        found.

        """
        searchfunc = functools.partial(self.search_servers,
                                       detailed=detailed)
        return _utils._get_entity(searchfunc, name_or_id, filters)

    def get_server_by_id(self, id):
        return meta.obj_to_dict(
            self.manager.submitTask(_tasks.ServerGet(server=id)))

    def get_image(self, name_or_id, filters=None):
        """Get an image by name or ID.

        :param name_or_id: Name or ID of the image.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: An image dict or None if no matching image is found.

        """
        return _utils._get_entity(self.search_images, name_or_id, filters)

    def get_floating_ip(self, id, filters=None):
        """Get a floating IP by ID

        :param id: ID of the floating IP.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A floating IP dict or None if no matching floating
        IP is found.

        """
        return _utils._get_entity(self.search_floating_ips, id, filters)

    def get_zone(self, name_or_id, filters=None):
        """Get a DNS zone by name or ID.

        :param name_or_id: Name or ID of the DNS zone.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A zone dict or None if no matching DNS zone is
        found.

        """
        return _utils._get_entity(self.search_zones, name_or_id, filters)

    def _get_record(self, zone_id, name_or_id, filters=None):
        f = lambda name_or_id, filters: self._search_records(
            zone_id, name_or_id, filters)
        return _utils._get_entity(f, name_or_id, filters)

    def get_stack(self, name_or_id, filters=None):
        """Get exactly one Heat stack.

        :param name_or_id: Name or id of the desired stack.
        :param filters: a dict containing additional filters to use. e.g.
                {'stack_status': 'CREATE_COMPLETE'}

        :returns: a dict containing the stack description

        :raises: ``OpenStackCloudException`` if something goes wrong during the
            openstack API call or if multiple matches are found.
        """
        return _utils._get_entity(
            self.search_stacks, name_or_id, filters)

    def create_keypair(self, name, public_key):
        """Create a new keypair.

        :param name: Name of the keypair being created.
        :param public_key: Public key for the new keypair.

        :raises: OpenStackCloudException on operation error.
        """
        try:
            return meta.obj_to_dict(
                self.manager.submitTask(_tasks.KeypairCreate(
                    name=name, public_key=public_key))
            )
        except Exception as e:
            raise OpenStackCloudException(
                "Unable to create keypair %s: %s" % (name, e)
            )

    def delete_keypair(self, name):
        """Delete a keypair.

        :param name: Name of the keypair to delete.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        """
        try:
            self.manager.submitTask(_tasks.KeypairDelete(key=name))
        except nova_exceptions.NotFound:
            self.log.debug("Keypair %s not found for deleting" % name)
            return False
        except Exception as e:
            raise OpenStackCloudException(
                "Unable to delete keypair %s: %s" % (name, e)
            )
        return True

    # TODO(Shrews): This will eventually need to support tenant ID and
    # provider networks, which are admin-level params.
    def create_network(self, name, shared=False, admin_state_up=True,
                       external=False):
        """Create a network.

        :param string name: Name of the network being created.
        :param bool shared: Set the network as shared.
        :param bool admin_state_up: Set the network administrative state to up.
        :param bool external: Whether this network is externally accessible.

        :returns: The network object.
        :raises: OpenStackCloudException on operation error.
        """

        network = {
            'name': name,
            'shared': shared,
            'admin_state_up': admin_state_up,
            'router:external': external
        }

        with _utils.neutron_exceptions(
                "Error creating network {0}".format(name)):
            net = self.manager.submitTask(
                _tasks.NetworkCreate(body=dict({'network': network})))
        # Turns out neutron returns an actual dict, so no need for the
        # use of meta.obj_to_dict() here (which would not work against
        # a dict).
        return net['network']

    def delete_network(self, name_or_id):
        """Delete a network.

        :param name_or_id: Name or ID of the network being deleted.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        """
        network = self.get_network(name_or_id)
        if not network:
            self.log.debug("Network %s not found for deleting" % name_or_id)
            return False

        with _utils.neutron_exceptions(
                "Error deleting network {0}".format(name_or_id)):
            self.manager.submitTask(
                _tasks.NetworkDelete(network=network['id']))

        return True

    def _build_external_gateway_info(self, ext_gateway_net_id, enable_snat,
                                     ext_fixed_ips):
        info = {}
        if ext_gateway_net_id:
            info['network_id'] = ext_gateway_net_id
        if enable_snat is not None:
            info['enable_snat'] = enable_snat
        if ext_fixed_ips:
            info['external_fixed_ips'] = ext_fixed_ips
        if info:
            return info
        return None

    def add_router_interface(self, router, subnet_id=None, port_id=None):
        """Attach a subnet to an internal router interface.

        Either a subnet ID or port ID must be specified for the internal
        interface. Supplying both will result in an error.

        :param dict router: The dict object of the router being changed
        :param string subnet_id: The ID of the subnet to use for the interface
        :param string port_id: The ID of the port to use for the interface

        :returns: A dict with the router id (id), subnet ID (subnet_id),
            port ID (port_id) and tenant ID (tenant_id).

        :raises: OpenStackCloudException on operation error.
        """
        body = {}
        if subnet_id:
            body['subnet_id'] = subnet_id
        if port_id:
            body['port_id'] = port_id

        with _utils.neutron_exceptions(
            "Error attaching interface to router {0}".format(router['id'])
        ):
            return self.manager.submitTask(
                _tasks.RouterAddInterface(router=router['id'], body=body)
            )

    def remove_router_interface(self, router, subnet_id=None, port_id=None):
        """Detach a subnet from an internal router interface.

        If you specify both subnet and port ID, the subnet ID must
        correspond to the subnet ID of the first IP address on the port
        specified by the port ID. Otherwise an error occurs.

        :param dict router: The dict object of the router being changed
        :param string subnet_id: The ID of the subnet to use for the interface
        :param string port_id: The ID of the port to use for the interface

        :returns: None on success

        :raises: OpenStackCloudException on operation error.
        """
        body = {}
        if subnet_id:
            body['subnet_id'] = subnet_id
        if port_id:
            body['port_id'] = port_id

        with _utils.neutron_exceptions(
            "Error detaching interface from router {0}".format(router['id'])
        ):
            return self.manager.submitTask(
                _tasks.RouterRemoveInterface(router=router['id'], body=body)
            )

    def list_router_interfaces(self, router, interface_type=None):
        """List all interfaces for a router.

        :param dict router: A router dict object.
        :param string interface_type: One of None, "internal", or "external".
            Controls whether all, internal interfaces or external interfaces
            are returned.

        :returns: A list of port dict objects.
        """
        ports = self.search_ports(filters={'device_id': router['id']})

        if interface_type:
            filtered_ports = []
            ext_fixed = (router['external_gateway_info']['external_fixed_ips']
                         if router['external_gateway_info']
                         else [])

            # Compare the subnets (subnet_id, ip_address) on the ports with
            # the subnets making up the router external gateway. Those ports
            # that match are the external interfaces, and those that don't
            # are internal.
            for port in ports:
                matched_ext = False
                for port_subnet in port['fixed_ips']:
                    for router_external_subnet in ext_fixed:
                        if port_subnet == router_external_subnet:
                            matched_ext = True
                if interface_type == 'internal' and not matched_ext:
                    filtered_ports.append(port)
                elif interface_type == 'external' and matched_ext:
                    filtered_ports.append(port)
            return filtered_ports

        return ports

    def create_router(self, name=None, admin_state_up=True,
                      ext_gateway_net_id=None, enable_snat=None,
                      ext_fixed_ips=None):
        """Create a logical router.

        :param string name: The router name.
        :param bool admin_state_up: The administrative state of the router.
        :param string ext_gateway_net_id: Network ID for the external gateway.
        :param bool enable_snat: Enable Source NAT (SNAT) attribute.
        :param list ext_fixed_ips:
            List of dictionaries of desired IP and/or subnet on the
            external network. Example::

              [
                {
                  "subnet_id": "8ca37218-28ff-41cb-9b10-039601ea7e6b",
                  "ip_address": "192.168.10.2"
                }
              ]

        :returns: The router object.
        :raises: OpenStackCloudException on operation error.
        """
        router = {
            'admin_state_up': admin_state_up
        }
        if name:
            router['name'] = name
        ext_gw_info = self._build_external_gateway_info(
            ext_gateway_net_id, enable_snat, ext_fixed_ips
        )
        if ext_gw_info:
            router['external_gateway_info'] = ext_gw_info

        with _utils.neutron_exceptions(
                "Error creating router {0}".format(name)):
            new_router = self.manager.submitTask(
                _tasks.RouterCreate(body=dict(router=router)))
        # Turns out neutron returns an actual dict, so no need for the
        # use of meta.obj_to_dict() here (which would not work against
        # a dict).
        return new_router['router']

    def update_router(self, name_or_id, name=None, admin_state_up=None,
                      ext_gateway_net_id=None, enable_snat=None,
                      ext_fixed_ips=None):
        """Update an existing logical router.

        :param string name_or_id: The name or UUID of the router to update.
        :param string name: The new router name.
        :param bool admin_state_up: The administrative state of the router.
        :param string ext_gateway_net_id:
            The network ID for the external gateway.
        :param bool enable_snat: Enable Source NAT (SNAT) attribute.
        :param list ext_fixed_ips:
            List of dictionaries of desired IP and/or subnet on the
            external network. Example::

              [
                {
                  "subnet_id": "8ca37218-28ff-41cb-9b10-039601ea7e6b",
                  "ip_address": "192.168.10.2"
                }
              ]

        :returns: The router object.
        :raises: OpenStackCloudException on operation error.
        """
        router = {}
        if name:
            router['name'] = name
        if admin_state_up is not None:
            router['admin_state_up'] = admin_state_up
        ext_gw_info = self._build_external_gateway_info(
            ext_gateway_net_id, enable_snat, ext_fixed_ips
        )
        if ext_gw_info:
            router['external_gateway_info'] = ext_gw_info

        if not router:
            self.log.debug("No router data to update")
            return

        curr_router = self.get_router(name_or_id)
        if not curr_router:
            raise OpenStackCloudException(
                "Router %s not found." % name_or_id)

        with _utils.neutron_exceptions(
                "Error updating router {0}".format(name_or_id)):
            new_router = self.manager.submitTask(
                _tasks.RouterUpdate(
                    router=curr_router['id'], body=dict(router=router)))

        # Turns out neutron returns an actual dict, so no need for the
        # use of meta.obj_to_dict() here (which would not work against
        # a dict).
        return new_router['router']

    def delete_router(self, name_or_id):
        """Delete a logical router.

        If a name, instead of a unique UUID, is supplied, it is possible
        that we could find more than one matching router since names are
        not required to be unique. An error will be raised in this case.

        :param name_or_id: Name or ID of the router being deleted.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        """
        router = self.get_router(name_or_id)
        if not router:
            self.log.debug("Router %s not found for deleting" % name_or_id)
            return False

        with _utils.neutron_exceptions(
                "Error deleting router {0}".format(name_or_id)):
            self.manager.submitTask(
                _tasks.RouterDelete(router=router['id']))

        return True

    def get_image_exclude(self, name_or_id, exclude):
        for image in self.search_images(name_or_id):
            if exclude:
                if exclude not in image.name:
                    return image
            else:
                return image
        return None

    def get_image_name(self, image_id, exclude=None):
        image = self.get_image_exclude(image_id, exclude)
        if image:
            return image.name
        return None

    def get_image_id(self, image_name, exclude=None):
        image = self.get_image_exclude(image_name, exclude)
        if image:
            return image.id
        return None

    def create_image_snapshot(self, name, server, **metadata):
        image_id = str(self.manager.submitTask(_tasks.ImageSnapshotCreate(
            image_name=name, server=server, metadata=metadata)))
        self.list_images.invalidate(self)
        return self.get_image(image_id)

    def delete_image(self, name_or_id, wait=False, timeout=3600):
        image = self.get_image(name_or_id)
        try:
            # Note that in v1, the param name is image, but in v2,
            # it's image_id
            glance_api_version = self.cloud_config.get_api_version('image')
            if glance_api_version == '2':
                self.manager.submitTask(
                    _tasks.ImageDelete(image_id=image.id))
            elif glance_api_version == '1':
                self.manager.submitTask(
                    _tasks.ImageDelete(image=image.id))
            self.list_images.invalidate(self)
        except Exception as e:
            raise OpenStackCloudException(
                "Error in deleting image: %s" % str(e))

        if wait:
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the image to be deleted."):
                self._cache.invalidate()
                if self.get_image(image.id) is None:
                    return

    def create_image(
            self, name, filename, container='images',
            md5=None, sha256=None,
            disk_format=None, container_format=None,
            disable_vendor_agent=True,
            wait=False, timeout=3600, **kwargs):

        if not disk_format:
            disk_format = self.cloud_config.config['image_format']
        if not container_format:
            if disk_format == 'vhd':
                container_format = 'ovf'
            else:
                container_format = 'bare'
        if not md5 or not sha256:
            (md5, sha256) = self._get_file_hashes(filename)
        current_image = self.get_image(name)
        if (current_image and current_image.get(IMAGE_MD5_KEY, '') == md5
                and current_image.get(IMAGE_SHA256_KEY, '') == sha256):
            self.log.debug(
                "image {name} exists and is up to date".format(name=name))
            return current_image
        kwargs[IMAGE_MD5_KEY] = md5
        kwargs[IMAGE_SHA256_KEY] = sha256

        if disable_vendor_agent:
            kwargs.update(self.cloud_config.config['disable_vendor_agent'])

        # We can never have nice things. Glance v1 took "is_public" as a
        # boolean. Glance v2 takes "visibility". If the user gives us
        # is_public, we know what they mean. If they give us visibility, they
        # know that they mean.
        if self.cloud_config.get_api_version('image') == '2':
            if 'is_public' in kwargs:
                is_public = kwargs.pop('is_public')
                if is_public:
                    kwargs['visibility'] = 'public'
                else:
                    kwargs['visibility'] = 'private'

        try:
            # This makes me want to die inside
            if self.image_api_use_tasks:
                return self._upload_image_task(
                    name, filename, container,
                    current_image=current_image,
                    wait=wait, timeout=timeout, **kwargs)
            else:
                image_kwargs = dict(properties=kwargs)
                if disk_format:
                    image_kwargs['disk_format'] = disk_format
                if container_format:
                    image_kwargs['container_format'] = container_format

                return self._upload_image_put(name, filename, **image_kwargs)
        except OpenStackCloudException:
            self.log.debug("Image creation failed", exc_info=True)
            raise
        except Exception as e:
            raise OpenStackCloudException(
                "Image creation failed: {message}".format(message=str(e)))

    def _upload_image_put_v2(self, name, image_data, **image_kwargs):
        if 'properties' in image_kwargs:
            img_props = image_kwargs.pop('properties')
            for k, v in iter(img_props.items()):
                image_kwargs[k] = str(v)
        image = self.manager.submitTask(_tasks.ImageCreate(
            name=name, **image_kwargs))
        self.manager.submitTask(_tasks.ImageUpload(
            image_id=image.id, image_data=image_data))
        return image

    def _upload_image_put_v1(self, name, image_data, **image_kwargs):
        image = self.manager.submitTask(_tasks.ImageCreate(
            name=name, **image_kwargs))
        self.manager.submitTask(_tasks.ImageUpdate(
            image=image, data=image_data))
        return image

    def _upload_image_put(self, name, filename, **image_kwargs):
        image_data = open(filename, 'rb')
        # Because reasons and crying bunnies
        if self.cloud_config.get_api_version('image') == '2':
            image = self._upload_image_put_v2(name, image_data, **image_kwargs)
        else:
            image = self._upload_image_put_v1(name, image_data, **image_kwargs)
        self._cache.invalidate()
        return self.get_image(image.id)

    def _upload_image_task(
            self, name, filename, container, current_image=None,
            wait=True, timeout=None, **image_properties):
        self.create_object(
            container, name, filename,
            md5=image_properties.get('md5', None),
            sha256=image_properties.get('sha256', None))
        if not current_image:
            current_image = self.get_image(name)
        # TODO(mordred): Can we do something similar to what nodepool does
        # using glance properties to not delete then upload but instead make a
        # new "good" image and then mark the old one as "bad"
        # self.glance_client.images.delete(current_image)
        task_args = dict(
            type='import', input=dict(
                import_from='{container}/{name}'.format(
                    container=container, name=name),
                image_properties=dict(name=name)))
        glance_task = self.manager.submitTask(
            _tasks.ImageTaskCreate(**task_args))
        self.list_images.invalidate(self)
        if wait:
            image_id = None
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the image to import."):
                try:
                    if image_id is None:
                        status = self.manager.submitTask(
                            _tasks.ImageTaskGet(task_id=glance_task.id))
                except glanceclient.exc.HTTPServiceUnavailable:
                    # Intermittent failure - catch and try again
                    continue

                if status.status == 'success':
                    image_id = status.result['image_id']
                    try:
                        image = self.get_image(image_id)
                    except glanceclient.exc.HTTPServiceUnavailable:
                        # Intermittent failure - catch and try again
                        continue
                    if image is None:
                        continue
                    self.update_image_properties(
                        image=image,
                        **image_properties)
                    return self.get_image(status.result['image_id'])
                if status.status == 'failure':
                    if status.message == IMAGE_ERROR_396:
                        glance_task = self.manager.submitTask(
                            _tasks.ImageTaskCreate(**task_args))
                        self.list_images.invalidate(self)
                    else:
                        raise OpenStackCloudException(
                            "Image creation failed: {message}".format(
                                message=status.message),
                            extra_data=status)
        else:
            return meta.warlock_to_dict(glance_task)

    def update_image_properties(
            self, image=None, name_or_id=None, **properties):
        if image is None:
            image = self.get_image(name_or_id)

        img_props = {}
        for k, v in iter(properties.items()):
            if v and k in ['ramdisk', 'kernel']:
                v = self.get_image_id(v)
                k = '{0}_id'.format(k)
            img_props[k] = v

        # This makes me want to die inside
        if self.cloud_config.get_api_version('image') == '2':
            return self._update_image_properties_v2(image, img_props)
        else:
            return self._update_image_properties_v1(image, img_props)

    def _update_image_properties_v2(self, image, properties):
        img_props = {}
        for k, v in iter(properties.items()):
            if image.get(k, None) != v:
                img_props[k] = str(v)
        if not img_props:
            return False
        self.manager.submitTask(_tasks.ImageUpdate(
            image_id=image.id, **img_props))
        self.list_images.invalidate(self)
        return True

    def _update_image_properties_v1(self, image, properties):
        img_props = {}
        for k, v in iter(properties.items()):
            if image.properties.get(k, None) != v:
                img_props[k] = v
        if not img_props:
            return False
        self.manager.submitTask(_tasks.ImageUpdate(
            image=image, properties=img_props))
        self.list_images.invalidate(self)
        return True

    def create_volume(self, wait=True, timeout=None, **kwargs):
        """Create a volume.

        :param wait: If true, waits for volume to be created.
        :param timeout: Seconds to wait for volume creation. None is forever.
        :param volkwargs: Keyword arguments as expected for cinder client.

        :returns: The created volume object.

        :raises: OpenStackCloudTimeout if wait time exceeded.
        :raises: OpenStackCloudException on operation error.
        """

        try:
            volume = self.manager.submitTask(_tasks.VolumeCreate(**kwargs))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in creating volume: %s" % str(e))
        self.list_volumes.invalidate(self)

        volume = meta.obj_to_dict(volume)

        if volume['status'] == 'error':
            raise OpenStackCloudException("Error in creating volume")

        if wait:
            vol_id = volume['id']
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the volume to be available."):
                volume = self.get_volume(vol_id)

                if not volume:
                    continue

                if volume['status'] == 'available':
                    break

                if volume['status'] == 'error':
                    raise OpenStackCloudException(
                        "Error in creating volume, please check logs")

        return volume

    def delete_volume(self, name_or_id=None, wait=True, timeout=None):
        """Delete a volume.

        :param name_or_id: Name or unique ID of the volume.
        :param wait: If true, waits for volume to be deleted.
        :param timeout: Seconds to wait for volume deletion. None is forever.

        :raises: OpenStackCloudTimeout if wait time exceeded.
        :raises: OpenStackCloudException on operation error.
        """

        self.list_volumes.invalidate(self)
        volume = self.get_volume(name_or_id)

        if not volume:
            self.log.debug(
                "Volume {name_or_id} does not exist".format(
                    name_or_id=name_or_id),
                exc_info=True)
            return False

        try:
            self.manager.submitTask(
                _tasks.VolumeDelete(volume=volume['id']))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in deleting volume: %s" % str(e))

        self.list_volumes.invalidate(self)
        if wait:
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the volume to be deleted."):

                if not self.get_volume(volume['id']):
                    break

        return True

    def get_volumes(self, server, cache=True):
        volumes = []
        for volume in self.list_volumes(cache=cache):
            for attach in volume['attachments']:
                if attach['server_id'] == server['id']:
                    volumes.append(volume)
        return volumes

    def get_volume_id(self, name_or_id):
        volume = self.get_volume(name_or_id)
        if volume:
            return volume['id']
        return None

    def volume_exists(self, name_or_id):
        return self.get_volume(name_or_id) is not None

    def get_volume_attach_device(self, volume, server_id):
        """Return the device name a volume is attached to for a server.

        This can also be used to verify if a volume is attached to
        a particular server.

        :param volume: Volume dict
        :param server_id: ID of server to check

        :returns: Device name if attached, None if volume is not attached.
        """
        for attach in volume['attachments']:
            if server_id == attach['server_id']:
                return attach['device']
        return None

    def detach_volume(self, server, volume, wait=True, timeout=None):
        """Detach a volume from a server.

        :param server: The server dict to detach from.
        :param volume: The volume dict to detach.
        :param wait: If true, waits for volume to be detached.
        :param timeout: Seconds to wait for volume detachment. None is forever.

        :raises: OpenStackCloudTimeout if wait time exceeded.
        :raises: OpenStackCloudException on operation error.
        """
        dev = self.get_volume_attach_device(volume, server['id'])
        if not dev:
            raise OpenStackCloudException(
                "Volume %s is not attached to server %s"
                % (volume['id'], server['id'])
            )

        try:
            self.manager.submitTask(
                _tasks.VolumeDetach(attachment_id=volume['id'],
                                    server_id=server['id']))
        except Exception as e:
            raise OpenStackCloudException(
                "Error detaching volume %s from server %s: %s" %
                (volume['id'], server['id'], e)
            )

        if wait:
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for volume %s to detach." % volume['id']):
                try:
                    vol = self.get_volume(volume['id'])
                except Exception:
                    self.log.debug(
                        "Error getting volume info %s" % volume['id'],
                        exc_info=True)
                    continue

                if vol['status'] == 'available':
                    return

                if vol['status'] == 'error':
                    raise OpenStackCloudException(
                        "Error in detaching volume %s" % volume['id']
                    )

    def attach_volume(self, server, volume, device=None,
                      wait=True, timeout=None):
        """Attach a volume to a server.

        This will attach a volume, described by the passed in volume
        dict (as returned by get_volume()), to the server described by
        the passed in server dict (as returned by get_server()) on the
        named device on the server.

        If the volume is already attached to the server, or generally not
        available, then an exception is raised. To re-attach to a server,
        but under a different device, the user must detach it first.

        :param server: The server dict to attach to.
        :param volume: The volume dict to attach.
        :param device: The device name where the volume will attach.
        :param wait: If true, waits for volume to be attached.
        :param timeout: Seconds to wait for volume attachment. None is forever.

        :raises: OpenStackCloudTimeout if wait time exceeded.
        :raises: OpenStackCloudException on operation error.
        """
        dev = self.get_volume_attach_device(volume, server['id'])
        if dev:
            raise OpenStackCloudException(
                "Volume %s already attached to server %s on device %s"
                % (volume['id'], server['id'], dev)
            )

        if volume['status'] != 'available':
            raise OpenStackCloudException(
                "Volume %s is not available. Status is '%s'"
                % (volume['id'], volume['status'])
            )

        try:
            vol = self.manager.submitTask(
                _tasks.VolumeAttach(volume_id=volume['id'],
                                    server_id=server['id'],
                                    device=device))
            vol = meta.obj_to_dict(vol)

        except Exception as e:
            raise OpenStackCloudException(
                "Error attaching volume %s to server %s: %s" %
                (volume['id'], server['id'], e)
            )

        if wait:
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for volume %s to attach." % volume['id']):
                try:
                    vol = self.get_volume(volume['id'])
                except Exception:
                    self.log.debug(
                        "Error getting volume info %s" % volume['id'],
                        exc_info=True)
                    continue

                if self.get_volume_attach_device(vol, server['id']):
                    break

                # TODO(Shrews) check to see if a volume can be in error status
                #              and also attached. If so, we should move this
                #              above the get_volume_attach_device call
                if vol['status'] == 'error':
                    raise OpenStackCloudException(
                        "Error in attaching volume %s" % volume['id']
                    )
        return vol

    def create_volume_snapshot(self, volume_id, force=False,
                               display_name=None, display_description=None,
                               wait=True, timeout=None):
        """Create a volume.

        :param volume_id: the id of the volume to snapshot.
        :param force: If set to True the snapshot will be created even if the
                      volume is attached to an instance, if False it will not
        :param display_name: name of the snapshot, one will be generated if
                             one is not provided
        :param display_description: description of the snapshot, one will be
                                    one is not provided
        :param wait: If true, waits for volume snapshot to be created.
        :param timeout: Seconds to wait for volume snapshot creation. None is
                        forever.

        :returns: The created volume object.

        :raises: OpenStackCloudTimeout if wait time exceeded.
        :raises: OpenStackCloudException on operation error.
        """
        try:
            snapshot = self.manager.submitTask(
                _tasks.VolumeSnapshotCreate(
                    volume_id=volume_id, force=force,
                    display_name=display_name,
                    display_description=display_description)
                )

        except Exception as e:
            raise OpenStackCloudException(
                "Error creating snapshot of volume %s: %s" % (volume_id, e)
            )

        snapshot = meta.obj_to_dict(snapshot)

        if wait:
            snapshot_id = snapshot['id']
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the volume snapshot to be available."
                    ):
                snapshot = self.get_volume_snapshot_by_id(snapshot_id)

                if snapshot['status'] == 'available':
                    break

                if snapshot['status'] == 'error':
                    raise OpenStackCloudException(
                        "Error in creating volume, please check logs")

        return snapshot

    def get_volume_snapshot_by_id(self, snapshot_id):
        """Takes a snapshot_id and gets a dict of the snapshot
        that maches that id.

        Note: This is more efficient than get_volume_snapshot.

        param: snapshot_id: ID of the volume snapshot.

        """
        try:
            snapshot = self.manager.submitTask(
                _tasks.VolumeSnapshotGet(
                    snapshot_id=snapshot_id
                )
            )

        except Exception as e:
            raise OpenStackCloudException(
                "Error getting snapshot %s: %s" % (snapshot_id, e)
            )

        return meta.obj_to_dict(snapshot)

    def get_volume_snapshot(self, name_or_id, filters=None):
        """Get a volume by name or ID.

        :param name_or_id: Name or ID of the volume snapshot.
        :param dict filters:
            A dictionary of meta data to use for further filtering. Elements
            of this dictionary may, themselves, be dictionaries. Example::

                {
                  'last_name': 'Smith',
                  'other': {
                      'gender': 'Female'
                  }
                }

        :returns: A volume dict or None if no matching volume is
        found.

        """
        return _utils._get_entity(self.search_volume_snapshots, name_or_id,
                                  filters)

    def list_volume_snapshots(self, detailed=True, search_opts=None):
        """List all volume snapshots.

        :returns: A list of volume snapshots dicts.

        """
        try:
            return meta.obj_list_to_dict(
                self.manager.submitTask(
                    _tasks.VolumeSnapshotList(detailed=detailed,
                                              search_opts=search_opts)
                )
            )

        except Exception as e:
            raise OpenStackCloudException(
                "Error getting a list of snapshots: %s" % e
            )

    def delete_volume_snapshot(self, name_or_id=None, wait=False,
                               timeout=None):
        """Delete a volume snapshot.

        :param name_or_id: Name or unique ID of the volume snapshot.
        :param wait: If true, waits for volume snapshot to be deleted.
        :param timeout: Seconds to wait for volume snapshot deletion. None is
                        forever.

        :raises: OpenStackCloudTimeout if wait time exceeded.
        :raises: OpenStackCloudException on operation error.
        """

        volumesnapshot = self.get_volume_snapshot(name_or_id)

        if not volumesnapshot:
            return False

        try:
            self.manager.submitTask(
                _tasks.VolumeSnapshotDelete(
                    snapshot=volumesnapshot['id']
                )
            )
        except Exception as e:
            raise OpenStackCloudException(
                "Error in deleting volume snapshot: %s" % str(e))

        if wait:
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the volume snapshot to be deleted."):
                if not self.get_volume_snapshot(volumesnapshot['id']):
                    break

        return True

    def get_server_id(self, name_or_id):
        server = self.get_server(name_or_id)
        if server:
            return server['id']
        return None

    def get_server_private_ip(self, server):
        return meta.get_server_private_ip(server, self)

    def get_server_public_ip(self, server):
        return meta.get_server_external_ipv4(self, server)

    def get_server_meta(self, server):
        # TODO(mordred) remove once ansible has moved to Inventory interface
        server_vars = meta.get_hostvars_from_server(self, server)
        groups = meta.get_groups_from_server(self, server, server_vars)
        return dict(server_vars=server_vars, groups=groups)

    def get_openstack_vars(self, server):
        return meta.get_hostvars_from_server(self, server)

    def _expand_server_vars(self, server):
        # Used by nodepool
        # TODO(mordred) remove after these make it into what we
        # actually want the API to be.
        return meta.expand_server_vars(self, server)

    def available_floating_ip(self, network=None, server=None):
        """Get a floating IP from a network or a pool.

        Return the first available floating IP or allocate a new one.

        :param network: Nova pool name or Neutron network name or id.
        :param server: Server the IP is for if known

        :returns: a (normalized) structure with a floating IP address
                  description.
        """
        if self.has_service('network'):
            try:
                f_ips = _utils.normalize_neutron_floating_ips(
                    self._neutron_available_floating_ips(
                        network=network, server=server))
                return f_ips[0]
            except OpenStackCloudURINotFound as e:
                self.log.debug(
                    "Something went wrong talking to neutron API: "
                    "'{msg}'. Trying with Nova.".format(msg=str(e)))
                # Fall-through, trying with Nova

        f_ips = _utils.normalize_nova_floating_ips(
            self._nova_available_floating_ips(pool=network)
        )
        return f_ips[0]

    def _neutron_available_floating_ips(
            self, network=None, project_id=None, server=None):
        """Get a floating IP from a Neutron network.

        Return a list of available floating IPs or allocate a new one and
        return it in a list of 1 element.

        :param network: Nova pool name or Neutron network name or id.
        :param server: (server) Server the Floating IP is for

        :returns: a list of floating IP addresses.

        :raises: ``OpenStackCloudResourceNotFound``, if an external network
                 that meets the specified criteria cannot be found.
        """
        if project_id is None:
            # Make sure we are only listing floatingIPs allocated the current
            # tenant. This is the default behaviour of Nova
            project_id = self.keystone_session.get_project_id()

        with _utils.neutron_exceptions("unable to get available floating IPs"):
            networks = self.get_external_networks()
            if not networks:
                raise OpenStackCloudResourceNotFound(
                    "unable to find an external network")

            filters = {
                'port_id': None,
                'floating_network_id': networks[0]['id'],
                'tenant_id': project_id

            }
            floating_ips = self._neutron_list_floating_ips()
            available_ips = _utils._filter_list(
                floating_ips, name_or_id=None, filters=filters)
            if available_ips:
                return available_ips

            # No available IP found or we didn't try
            # allocate a new Floating IP
            f_ip = self._neutron_create_floating_ip(
                network_name_or_id=networks[0]['id'], server=server)

            return [f_ip]

    def _nova_available_floating_ips(self, pool=None):
        """Get available floating IPs from a floating IP pool.

        Return a list of available floating IPs or allocate a new one and
        return it in a list of 1 element.

        :param pool: Nova floating IP pool name.

        :returns: a list of floating IP addresses.

        :raises: ``OpenStackCloudResourceNotFound``, if a floating IP pool
                 is not specified and cannot be found.
        """

        try:
            if pool is None:
                pools = self.list_floating_ip_pools()
                if not pools:
                    raise OpenStackCloudResourceNotFound(
                        "unable to find a floating ip pool")
                pool = pools[0]['name']

            filters = {
                'instance_id': None,
                'pool': pool
            }

            floating_ips = self._nova_list_floating_ips()
            available_ips = _utils._filter_list(
                floating_ips, name_or_id=None, filters=filters)
            if available_ips:
                return available_ips

            # No available IP found or we did not try.
            # Allocate a new Floating IP
            f_ip = self._nova_create_floating_ip(pool=pool)

            return [f_ip]

        except Exception as e:
            raise OpenStackCloudException(
                "unable to create floating IP in pool {pool}: {msg}".format(
                    pool=pool, msg=str(e)))

    def create_floating_ip(self, network=None, server=None):
        """Allocate a new floating IP from a network or a pool.

        :param network: Nova pool name or Neutron network name or id.
        :param server: (optional) Server dict for the server to create
                       the IP for and to which it should be attached

        :returns: a floating IP address

        :raises: ``OpenStackCloudException``, on operation error.
        """
        if self.has_service('network'):
            try:
                f_ips = _utils.normalize_neutron_floating_ips(
                    [self._neutron_create_floating_ip(
                        network_name_or_id=network, server=server)]
                )
                return f_ips[0]
            except OpenStackCloudURINotFound as e:
                self.log.debug(
                    "Something went wrong talking to neutron API: "
                    "'{msg}'. Trying with Nova.".format(msg=str(e)))
                # Fall-through, trying with Nova

        # Else, we are using Nova network
        f_ips = _utils.normalize_nova_floating_ips(
            [self._nova_create_floating_ip(pool=network)])
        return f_ips[0]

    def _neutron_create_floating_ip(
            self, network_name_or_id=None, server=None):
        with _utils.neutron_exceptions(
                "unable to create floating IP for net "
                "{0}".format(network_name_or_id)):
            if network_name_or_id:
                networks = [self.get_network(network_name_or_id)]
                if not networks:
                    raise OpenStackCloudResourceNotFound(
                        "unable to find network for floating ips with id "
                        "{0}".format(network_name_or_id))
            else:
                networks = self.get_external_networks()
                if not networks:
                    raise OpenStackCloudResourceNotFound(
                        "Unable to find an external network in this cloud"
                        " which makes getting a floating IP impossible")
            kwargs = {
                'floating_network_id': networks[0]['id'],
            }
            if server:
                (port, fixed_address) = self._get_free_fixed_port(server)
                if port:
                    kwargs['port_id'] = port['id']
                    kwargs['fixed_ip_address'] = fixed_address
            return self.manager.submitTask(_tasks.NeutronFloatingIPCreate(
                body={'floatingip': kwargs}))['floatingip']

    def _nova_create_floating_ip(self, pool=None):
        try:
            if pool is None:
                pools = self.list_floating_ip_pools()
                if not pools:
                    raise OpenStackCloudResourceNotFound(
                        "unable to find a floating ip pool")
                pool = pools[0]['name']

            pool_ip = self.manager.submitTask(
                _tasks.NovaFloatingIPCreate(pool=pool))
            return meta.obj_to_dict(pool_ip)

        except Exception as e:
            raise OpenStackCloudException(
                "unable to create floating IP in pool {pool}: {msg}".format(
                    pool=pool, msg=str(e)))

    def delete_floating_ip(self, floating_ip_id):
        """Deallocate a floating IP from a tenant.

        :param floating_ip_id: a floating IP address id.

        :returns: True if the IP address has been deleted, False if the IP
                  address was not found.

        :raises: ``OpenStackCloudException``, on operation error.
        """
        if self.has_service('network'):
            try:
                return self._neutron_delete_floating_ip(floating_ip_id)
            except OpenStackCloudURINotFound as e:
                self.log.debug(
                    "Something went wrong talking to neutron API: "
                    "'{msg}'. Trying with Nova.".format(msg=str(e)))
                # Fall-through, trying with Nova

        # Else, we are using Nova network
        return self._nova_delete_floating_ip(floating_ip_id)

    def _neutron_delete_floating_ip(self, floating_ip_id):
        try:
            with _utils.neutron_exceptions("unable to delete floating IP"):
                self.manager.submitTask(
                    _tasks.NeutronFloatingIPDelete(floatingip=floating_ip_id))
        except OpenStackCloudResourceNotFound:
            return False

        return True

    def _nova_delete_floating_ip(self, floating_ip_id):
        try:
            self.manager.submitTask(
                _tasks.NovaFloatingIPDelete(floating_ip=floating_ip_id))
        except nova_exceptions.NotFound:
            return False
        except Exception as e:
            raise OpenStackCloudException(
                "unable to delete floating IP id {fip_id}: {msg}".format(
                    fip_id=floating_ip_id, msg=str(e)))

        return True

    def _attach_ip_to_server(
            self, server, floating_ip,
            fixed_address=None, wait=False,
            timeout=60, skip_attach=False):
        """Attach a floating IP to a server.

        :param server: Server dict
        :param floating_ip: Floating IP dict to attach
        :param fixed_address: (optional) fixed address to which attach the
                              floating IP to.
        :param wait: (optional) Wait for the address to appear as assigned
                     to the server in Nova. Defaults to False.
        :param timeout: (optional) Seconds to wait, defaults to 60.
                        See the ``wait`` parameter.
        :param skip_attach: (optional) Skip the actual attach and just do
                            the wait. Defaults to False.

        :returns: None

        :raises: OpenStackCloudException, on operation error.
        """
        # Short circuit if we're asking to attach an IP that's already
        # attached
        ext_ip = meta.get_server_ip(server, ext_tag='floating')
        if ext_ip == floating_ip['floating_ip_address']:
            return

        if self.has_service('network'):
            if not skip_attach:
                try:
                    self._neutron_attach_ip_to_server(
                        server=server, floating_ip=floating_ip,
                        fixed_address=fixed_address)
                except OpenStackCloudURINotFound as e:
                    self.log.debug(
                        "Something went wrong talking to neutron API: "
                        "'{msg}'. Trying with Nova.".format(msg=str(e)))
                    # Fall-through, trying with Nova
        else:
            # Nova network
            self._nova_attach_ip_to_server(
                server_id=server['id'], floating_ip_id=floating_ip['id'],
                fixed_address=fixed_address)

        if wait:
            # Wait for the address to be assigned to the server
            server_id = server['id']
            for _ in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the floating IP to be attached."):
                server = self.get_server_by_id(server_id)
                ext_ip = meta.get_server_ip(server, ext_tag='floating')
                if ext_ip == floating_ip['floating_ip_address']:
                    return

    def _get_free_fixed_port(self, server, fixed_address=None):
        ports = self.search_ports(filters={'device_id': server['id']})
        if not ports:
            return (None, None)
        port = None
        if not fixed_address:
            if len(ports) > 1:
                raise OpenStackCloudException(
                    "More than one port was found for server {server}"
                    " and no fixed_address was specified. It is not"
                    " possible to infer correct behavior. Please specify"
                    " a fixed_address - or file a bug in shade describing"
                    " how you think this should work.")
            # We're assuming one, because we have no idea what to do with
            # more than one.
            port = ports[0]
            # Select the first available IPv4 address
            for address in port.get('fixed_ips', list()):
                try:
                    ip = ipaddress.ip_address(address['ip_address'])
                except Exception:
                    continue
                if ip.version == 4:
                    fixed_address = address['ip_address']
                    return port, fixed_address
            raise OpenStackCloudException(
                "unable to find a free fixed IPv4 address for server "
                "{0}".format(server_id))
        # unfortunately a port can have more than one fixed IP:
        # we can't use the search_ports filtering for fixed_address as
        # they are contained in a list. e.g.
        #
        #   "fixed_ips": [
        #     {
        #       "subnet_id": "008ba151-0b8c-4a67-98b5-0d2b87666062",
        #       "ip_address": "172.24.4.2"
        #     }
        #   ]
        #
        # Search fixed_address
        for p in ports:
            for fixed_ip in p['fixed_ips']:
                if fixed_address == fixed_ip['ip_address']:
                    return (p, fixed_address)
        return (None, None)

    def _neutron_attach_ip_to_server(
            self, server, floating_ip, fixed_address=None):
        with _utils.neutron_exceptions(
                "unable to bind a floating ip to server "
                "{0}".format(server['id'])):

            # Find an available port
            (port, fixed_address) = self._get_free_fixed_port(
                server, fixed_address=fixed_address)
            if not port:
                raise OpenStackCloudException(
                    "unable to find a port for server {0}".format(
                        server['id']))

            floating_ip_args = {'port_id': port['id']}
            if fixed_address is not None:
                floating_ip_args['fixed_ip_address'] = fixed_address

            return self.manager.submitTask(_tasks.NeutronFloatingIPUpdate(
                floatingip=floating_ip['id'],
                body={'floatingip': floating_ip_args}
            ))['floatingip']

    def _nova_attach_ip_to_server(self, server_id, floating_ip_id,
                                  fixed_address=None):
        try:
            f_ip = self.get_floating_ip(id=floating_ip_id)
            return self.manager.submitTask(_tasks.NovaFloatingIPAttach(
                server=server_id, address=f_ip['floating_ip_address'],
                fixed_address=fixed_address))
        except Exception as e:
            raise OpenStackCloudException(
                "error attaching IP {ip} to instance {id}: {msg}".format(
                    ip=floating_ip_id, id=server_id, msg=str(e)))

    def detach_ip_from_server(self, server_id, floating_ip_id):
        """Detach a floating IP from a server.

        :param server_id: id of a server.
        :param floating_ip_id: Id of the floating IP to detach.

        :returns: True if the IP has been detached, or False if the IP wasn't
                  attached to any server.

        :raises: ``OpenStackCloudException``, on operation error.
        """
        if self.has_service('network'):
            try:
                return self._neutron_detach_ip_from_server(
                    server_id=server_id, floating_ip_id=floating_ip_id)
            except OpenStackCloudURINotFound as e:
                self.log.debug(
                    "Something went wrong talking to neutron API: "
                    "'{msg}'. Trying with Nova.".format(msg=str(e)))
                # Fall-through, trying with Nova

        # Nova network
        self._nova_detach_ip_from_server(
            server_id=server_id, floating_ip_id=floating_ip_id)

    def _neutron_detach_ip_from_server(self, server_id, floating_ip_id):
        with _utils.neutron_exceptions(
                "unable to detach a floating ip from server "
                "{0}".format(server_id)):
            f_ip = self.get_floating_ip(id=floating_ip_id)
            if f_ip is None or not f_ip['attached']:
                return False
            self.manager.submitTask(_tasks.NeutronFloatingIPUpdate(
                floatingip=floating_ip_id,
                body={'floatingip': {'port_id': None}}))

            return True

    def _nova_detach_ip_from_server(self, server_id, floating_ip_id):
        try:
            f_ip = self.get_floating_ip(id=floating_ip_id)
            if f_ip is None:
                raise OpenStackCloudException(
                    "unable to find floating IP {0}".format(floating_ip_id))
            self.manager.submitTask(_tasks.NovaFloatingIPDetach(
                server=server_id, address=f_ip['floating_ip_address']))
        except nova_exceptions.Conflict as e:
            self.log.debug(
                "nova floating IP detach failed: {msg}".format(msg=str(e)),
                exc_info=True)
            return False
        except Exception as e:
            raise OpenStackCloudException(
                "error detaching IP {ip} from instance {id}: {msg}".format(
                    ip=floating_ip_id, id=server_id, msg=str(e)))

        return True

    def _add_ip_from_pool(
            self, server, network, fixed_address=None, reuse=True):
        """Add a floating IP to a sever from a given pool

        This method reuses available IPs, when possible, or allocate new IPs
        to the current tenant.
        The floating IP is attached to the given fixed address or to the
        first server port/fixed address

        :param server: Server dict
        :param network: Nova pool name or Neutron network name or id.
        :param fixed_address: a fixed address
        :param reuse: Try to reuse existing ips. Defaults to True.

        :returns: the floating IP assigned
        """
        if reuse:
            f_ip = self.available_floating_ip(network=network)
        else:
            f_ip = self.create_floating_ip(network=network)

        self._attach_ip_to_server(
            server=server, floating_ip=f_ip, fixed_address=fixed_address)

        return f_ip

    def add_ip_list(self, server, ips):
        """Attach a list of IPs to a server.

        :param server: a server object
        :param ips: list of IP addresses (floating IPs)

        :returns: None

        :raises: ``OpenStackCloudException``, on operation error.
        """
        # ToDo(dguerri): this makes no sense as we cannot attach multiple
        # floating IPs to a single fixed_address (this is true for both
        # neutron and nova). I will leave this here for the moment as we are
        # refactoring floating IPs methods.
        for ip in ips:
            f_ip = self.get_floating_ip(
                id=None, filters={'floating_ip_address': ip})
            self._attach_ip_to_server(
                server=server, floating_ip=f_ip)

    def add_auto_ip(self, server, wait=False, timeout=60, reuse=True):
        """Add a floating IP to a server.

        This method is intended for basic usage. For advanced network
        architecture (e.g. multiple external networks or servers with multiple
        interfaces), use other floating IP methods.

        This method can reuse available IPs, or allocate new IPs to the current
        project.

        :param server: a server dictionary.
        :param reuse: Whether or not to attempt to reuse IPs, defaults
                      to True.
        :param wait: (optional) Wait for the address to appear as assigned
                     to the server in Nova. Defaults to False.
        :param timeout: (optional) Seconds to wait, defaults to 60.
                        See the ``wait`` parameter.
        :param reuse: Try to reuse existing ips. Defaults to True.

        :returns: Floating IP address attached to server.

        """
        skip_attach = False
        if reuse:
            f_ip = self.available_floating_ip()
        else:
            f_ip = self.create_floating_ip(server=server)
            if server:
                # This gets passed in for both nova and neutron
                # but is only meaninful for the neutron logic branch
                skip_attach = True

        self._attach_ip_to_server(
            server=server, floating_ip=f_ip, wait=wait, timeout=timeout,
            skip_attach=skip_attach)

        return f_ip

    def add_ips_to_server(
            self, server, auto_ip=True, ips=None, ip_pool=None,
            wait=False, timeout=60, reuse=True):
        if ip_pool:
            self._add_ip_from_pool(server, ip_pool, reuse=reuse)
        elif ips:
            self.add_ip_list(server, ips)
        elif auto_ip:
            if self.get_server_public_ip(server):
                return server
            self.add_auto_ip(
                server, wait=wait, timeout=timeout, reuse=reuse)
        else:
            return server

        # this may look redundant, but if there is now a
        # floating IP, then it needs to be obtained from
        # a recent server object if the above code path exec'd
        try:
            server = self.get_server_by_id(server['id'])
        except Exception as e:
            raise OpenStackCloudException(
                "Error in getting info from instance: %s " % str(e))
        return server

    @_utils.valid_kwargs(
        'meta', 'files', 'userdata',
        'reservation_id', 'return_raw', 'min_count',
        'max_count', 'security_groups', 'key_name',
        'availability_zone', 'block_device_mapping',
        'block_device_mapping_v2', 'nics', 'scheduler_hints',
        'config_drive', 'admin_pass', 'disk_config')
    def create_server(
            self, name, image, flavor,
            auto_ip=True, ips=None, ip_pool=None,
            root_volume=None, terminate_volume=False,
            wait=False, timeout=180, reuse_ips=True,
            **kwargs):
        """Create a virtual server instance.

        :param name: Something to name the server.
        :param image: Image dict or id to boot with.
        :param flavor: Flavor dict or id to boot onto.
        :param auto_ip: Whether to take actions to find a routable IP for
                        the server. (defaults to True)
        :param ips: List of IPs to attach to the server (defaults to None)
        :param ip_pool: Name of the network or floating IP pool to get an
                        address from. (defaults to None)
        :param root_volume: Name or id of a volume to boot from
                            (defaults to None)
        :param terminate_volume: If booting from a volume, whether it should
                                 be deleted when the server is destroyed.
                                 (defaults to False)
        :param meta: (optional) A dict of arbitrary key/value metadata to
                     store for this server. Both keys and values must be
                     <=255 characters.
        :param files: (optional, deprecated) A dict of files to overwrite
                      on the server upon boot. Keys are file names (i.e.
                      ``/etc/passwd``) and values
                      are the file contents (either as a string or as a
                      file-like object). A maximum of five entries is allowed,
                      and each file must be 10k or less.
        :param reservation_id: a UUID for the set of servers being requested.
        :param min_count: (optional extension) The minimum number of
                          servers to launch.
        :param max_count: (optional extension) The maximum number of
                          servers to launch.
        :param security_groups: A list of security group names
        :param userdata: user data to pass to be exposed by the metadata
                      server this can be a file type object as well or a
                      string.
        :param key_name: (optional extension) name of previously created
                      keypair to inject into the instance.
        :param availability_zone: Name of the availability zone for instance
                                  placement.
        :param block_device_mapping: (optional) A dict of block
                      device mappings for this server.
        :param block_device_mapping_v2: (optional) A dict of block
                      device mappings for this server.
        :param nics:  (optional extension) an ordered list of nics to be
                      added to this server, with information about
                      connected networks, fixed IPs, port etc.
        :param scheduler_hints: (optional extension) arbitrary key-value pairs
                            specified by the client to help boot an instance
        :param config_drive: (optional extension) value for config drive
                            either boolean, or volume-id
        :param disk_config: (optional extension) control how the disk is
                            partitioned when the server is created.  possible
                            values are 'AUTO' or 'MANUAL'.
        :param admin_pass: (optional extension) add a user supplied admin
                           password.
        :param wait: (optional) Wait for the address to appear as assigned
                     to the server in Nova. Defaults to False.
        :param timeout: (optional) Seconds to wait, defaults to 60.
                        See the ``wait`` parameter.
        :returns: A dict representing the created server.
        :raises: OpenStackCloudException on operation error.
        """
        if root_volume:
            if terminate_volume:
                suffix = ':::1'
            else:
                suffix = ':::0'
            volume_id = self.get_volume_id(root_volume) + suffix
            if 'block_device_mapping' not in kwargs:
                kwargs['block_device_mapping'] = dict()
            kwargs['block_device_mapping']['vda'] = volume_id

        try:
            server = self.manager.submitTask(_tasks.ServerCreate(
                name=name, image=image, flavor=flavor, **kwargs))
            # This is a direct get task call to skip the list_servers
            # cache which has absolutely no chance of containing the
            # new server
            server = self.get_server_by_id(server.id)
            server_id = server['id']
        except Exception as e:
            raise OpenStackCloudException(
                "Error in creating instance: {0}".format(e))
        if server.status == 'ERROR':
            raise OpenStackCloudException(
                "Error in creating the server.")
        if wait:
            # There is no point in iterating faster than the list_servers cache
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for the server to come up.",
                    wait=self._SERVER_LIST_AGE):
                try:
                    # Use the get_server call so that the list_servers
                    # cache can be leveraged
                    server = self.get_server(server_id)
                except Exception:
                    continue
                if not server:
                    continue

                server = self.get_active_server(
                    server=server, reuse=reuse_ips,
                    auto_ip=auto_ip, ips=ips, ip_pool=ip_pool,
                    wait=wait, timeout=timeout)
                if server:
                    return server
        return server

    def get_active_server(
            self, server, auto_ip=True, ips=None, ip_pool=None,
            reuse=True, wait=False, timeout=180):

        if server['status'] == 'ERROR':
            raise OpenStackCloudException(
                "Error in creating the server", extra_data=dict(server=server))

        if server['status'] == 'ACTIVE':
            if 'addresses' in server and server['addresses']:
                return self.add_ips_to_server(
                    server, auto_ip, ips, ip_pool, reuse=reuse, wait=wait)

            self.log.debug(
                'Server {server} reached ACTIVE state without'
                ' being allocated an IP address.'
                ' Deleting server.'.format(server=server['id']))
            try:
                self._delete_server(
                    server=server, wait=wait, timeout=timeout)
            except Exception as e:
                raise OpenStackCloudException(
                    'Server reached ACTIVE state without being'
                    ' allocated an IP address AND then could not'
                    ' be deleted: {0}'.format(e),
                    extra_data=dict(server=server))
            raise OpenStackCloudException(
                'Server reached ACTIVE state without being'
                ' allocated an IP address.',
                extra_data=dict(server=server))
        return None

    def rebuild_server(self, server_id, image_id, wait=False, timeout=180):
        try:
            server = self.manager.submitTask(_tasks.ServerRebuild(
                server=server_id, image=image_id))
        except Exception as e:
            raise OpenStackCloudException(
                "Error in rebuilding instance: {0}".format(e))
        if wait:
            for count in _utils._iterate_timeout(
                    timeout,
                    "Timeout waiting for server {0} to "
                    "rebuild.".format(server_id)):
                try:
                    server = self.get_server_by_id(server_id)
                except Exception:
                    continue

                if server['status'] == 'ACTIVE':
                    return server

                if server['status'] == 'ERROR':
                    raise OpenStackCloudException(
                        "Error in rebuilding the server",
                        extra_data=dict(server=server))
        return meta.obj_to_dict(server)

    def delete_server(
            self, name_or_id, wait=False, timeout=180, delete_ips=False):
        server = self.get_server(name_or_id)
        return self._delete_server(
            server, wait=wait, timeout=timeout, delete_ips=delete_ips)

    def _delete_server(
            self, server, wait=False, timeout=180, delete_ips=False):
        if server:
            if delete_ips:
                floating_ip = meta.get_server_ip(server, ext_tag='floating')
                if floating_ip:
                    ips = self.search_floating_ips(filters={
                        'floating_ip_address': floating_ip})
                    if len(ips) != 1:
                        raise OpenStackException(
                            "Tried to delete floating ip {floating_ip}"
                            " associated with server {id} but there was"
                            " an error finding it. Something is exceptionally"
                            " broken.".format(
                                floating_ip=floating_ip,
                                id=server['id']))
                    self.delete_floating_ip(ips[0]['id'])
            try:
                self.manager.submitTask(
                    _tasks.ServerDelete(server=server['id']))
            except nova_exceptions.NotFound:
                return
            except Exception as e:
                raise OpenStackCloudException(
                    "Error in deleting server: {0}".format(e))
        else:
            return
        if not wait:
            return
        for count in _utils._iterate_timeout(
                timeout,
                "Timed out waiting for server to get deleted."):
            try:
                server = self.get_server_by_id(server['id'])
                if not server:
                    return
                server = meta.obj_to_dict(server)
            except nova_exceptions.NotFound:
                return
            except Exception as e:
                raise OpenStackCloudException(
                    "Error in deleting server: {0}".format(e))

    def list_containers(self):
        try:
            return meta.obj_to_dict(self.manager.submitTask(
                _tasks.ContainerList()))
        except swift_exceptions.ClientException as e:
            raise OpenStackCloudException(
                "Container list failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def get_container(self, name, skip_cache=False):
        if skip_cache or name not in self._container_cache:
            try:
                container = self.manager.submitTask(
                    _tasks.ContainerGet(container=name))
                self._container_cache[name] = container
            except swift_exceptions.ClientException as e:
                if e.http_status == 404:
                    return None
                raise OpenStackCloudException(
                    "Container fetch failed: %s (%s/%s)" % (
                        e.http_reason, e.http_host, e.http_path))
        return self._container_cache[name]

    def create_container(self, name, public=False):
        container = self.get_container(name)
        if container:
            return container
        try:
            self.manager.submitTask(
                _tasks.ContainerCreate(container=name))
            if public:
                self.set_container_access(name, 'public')
            return self.get_container(name, skip_cache=True)
        except swift_exceptions.ClientException as e:
            raise OpenStackCloudException(
                "Container creation failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def delete_container(self, name):
        try:
            self.manager.submitTask(
                _tasks.ContainerDelete(container=name))
        except swift_exceptions.ClientException as e:
            if e.http_status == 404:
                return
            raise OpenStackCloudException(
                "Container deletion failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def update_container(self, name, headers):
        try:
            self.manager.submitTask(
                _tasks.ContainerUpdate(container=name, headers=headers))
        except swift_exceptions.ClientException as e:
            raise OpenStackCloudException(
                "Container update failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def set_container_access(self, name, access):
        if access not in OBJECT_CONTAINER_ACLS:
            raise OpenStackCloudException(
                "Invalid container access specified: %s.  Must be one of %s"
                % (access, list(OBJECT_CONTAINER_ACLS.keys())))
        header = {'x-container-read': OBJECT_CONTAINER_ACLS[access]}
        self.update_container(name, header)

    def get_container_access(self, name):
        container = self.get_container(name, skip_cache=True)
        if not container:
            raise OpenStackCloudException("Container not found: %s" % name)
        acl = container.get('x-container-read', '')
        try:
            return [p for p, a in OBJECT_CONTAINER_ACLS.items()
                    if acl == a].pop()
        except IndexError:
            raise OpenStackCloudException(
                "Could not determine container access for ACL: %s." % acl)

    def _get_file_hashes(self, filename):
        if filename not in self._file_hash_cache:
            md5 = hashlib.md5()
            sha256 = hashlib.sha256()
            with open(filename, 'rb') as file_obj:
                for chunk in iter(lambda: file_obj.read(8192), b''):
                    md5.update(chunk)
                    sha256.update(chunk)
            self._file_hash_cache[filename] = dict(
                md5=md5.hexdigest(), sha256=sha256.hexdigest())
        return (self._file_hash_cache[filename]['md5'],
                self._file_hash_cache[filename]['sha256'])

    @_utils.cache_on_arguments()
    def get_object_capabilities(self):
        return self.manager.submitTask(_tasks.ObjectCapabilities())

    def get_object_segment_size(self, segment_size):
        '''get a segment size that will work given capabilities'''
        if segment_size is None:
            segment_size = DEFAULT_OBJECT_SEGMENT_SIZE
        try:
            caps = self.get_object_capabilities()
        except swift_exceptions.ClientException as e:
            if e.http_status == 412:
                server_max_file_size = DEFAULT_MAX_FILE_SIZE
                self.log.info(
                    "Swift capabilities not supported. "
                    "Using default max file size.")
            else:
                raise OpenStackCloudException(
                    "Could not determine capabilities")
        else:
            server_max_file_size = caps.get('swift', {}).get('max_file_size',
                                                             0)

        if segment_size > server_max_file_size:
            return server_max_file_size
        return segment_size

    def is_object_stale(
        self, container, name, filename, file_md5=None, file_sha256=None):

        metadata = self.get_object_metadata(container, name)
        if not metadata:
            self.log.debug(
                "swift stale check, no object: {container}/{name}".format(
                    container=container, name=name))
            return True

        if file_md5 is None or file_sha256 is None:
            (file_md5, file_sha256) = self._get_file_hashes(filename)

        if metadata.get(OBJECT_MD5_KEY, '') != file_md5:
            self.log.debug(
                "swift md5 mismatch: {filename}!={container}/{name}".format(
                    filename=filename, container=container, name=name))
            return True
        if metadata.get(OBJECT_SHA256_KEY, '') != file_sha256:
            self.log.debug(
                "swift sha256 mismatch: {filename}!={container}/{name}".format(
                    filename=filename, container=container, name=name))
            return True

        self.log.debug(
            "swift object up to date: {container}/{name}".format(
                container=container, name=name))
        return False

    def create_object(
            self, container, name, filename=None,
            md5=None, sha256=None, segment_size=None,
            **headers):
        """Create a file object

        :param container: The name of the container to store the file in.
            This container will be created if it does not exist already.
        :param name: Name for the object within the container.
        :param filename: The path to the local file whose contents will be
            uploaded.
        :param md5: A hexadecimal md5 of the file. (Optional), if it is known
            and can be passed here, it will save repeating the expensive md5
            process. It is assumed to be accurate.
        :param sha256: A hexadecimal sha256 of the file. (Optional) See md5.
        :param segment_size: Break the uploaded object into segments of this
            many bytes. (Optional) Shade will attempt to discover the maximum
            value for this from the server if it is not specified, or will use
            a reasonable default.
        :param headers: These will be passed through to the object creation
            API as HTTP Headers.

        :raises: ``OpenStackCloudException`` on operation error.
        """
        if not filename:
            filename = name

        segment_size = self.get_object_segment_size(segment_size)

        if not md5 or not sha256:
            (md5, sha256) = self._get_file_hashes(filename)
        headers[OBJECT_MD5_KEY] = md5
        headers[OBJECT_SHA256_KEY] = sha256
        header_list = sorted([':'.join([k, v]) for (k, v) in headers.items()])

        # On some clouds this is not necessary. On others it is. I'm confused.
        self.create_container(container)

        if self.is_object_stale(container, name, filename, md5, sha256):
            self.log.debug(
                "swift uploading {filename} to {container}/{name}".format(
                    filename=filename, container=container, name=name))
            upload = swift_service.SwiftUploadObject(source=filename,
                                                     object_name=name)
            for r in self.manager.submitTask(_tasks.ObjectCreate(
                container=container, objects=[upload],
                options=dict(header=header_list,
                             segment_size=segment_size))):
                if not r['success']:
                    raise OpenStackCloudException(
                        'Failed at action ({action}) [{error}]:'.format(**r))

    def list_objects(self, container):
        try:
            return meta.obj_to_dict(self.manager.submitTask(
                _tasks.ObjectList(container)))
        except swift_exceptions.ClientException as e:
            raise OpenStackCloudException(
                "Object list failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def delete_object(self, container, name):
        if not self.get_object_metadata(container, name):
            return
        try:
            self.manager.submitTask(_tasks.ObjectDelete(
                container=container, obj=name))
        except swift_exceptions.ClientException as e:
            raise OpenStackCloudException(
                "Object deletion failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def get_object_metadata(self, container, name):
        try:
            return self.manager.submitTask(_tasks.ObjectMetadata(
                container=container, obj=name))
        except swift_exceptions.ClientException as e:
            if e.http_status == 404:
                return None
            raise OpenStackCloudException(
                "Object metadata fetch failed: %s (%s/%s)" % (
                    e.http_reason, e.http_host, e.http_path))

    def create_netpartition(self, netpartition_name, netpartition_id=None):
        """Create a netpartiton.

        :param string netpartition_name:
           The unique name for the net partition. If a non-unique name is
           supplied, an exception is raised.
        :param string netpartition_id:
           The ID of the net partition.

        :returns: The new net partition object.
        :raises: OpenStackCloudException on operation error.
        """

        # The body of the neutron message for the netpartition we wish to create.
        # This includes attributes that are required or have defaults.
        netpartition = {
            'name': netpartition_name
        }

        # Add optional attributes to the message.
        if netpartition_id:
            netpartition['tenant_id'] = netpartition_id

        with self._neutron_exceptions(
                "Error creating netpartition "
                "{0}".format(netpartition_name)):
            new_netpartition = self.manager.submitTask(
                _tasks.NetpartitionCreate(body=dict(net_partition=netpartition)))

        return new_netpartition['net_partition']

    def delete_netpartition(self, name_or_id):
        """Delete a netpartition.

        If a name, instead of a unique UUID, is supplied, it is possible
        that we could find more than one matching netpartition since names are
        not required to be unique. An error will be raised in this case.

        :param name_or_id: Name or ID of the netpartition being deleted.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        """
        netpartition = self.get_netpartition(name_or_id)
        if not netpartition:
            self.log.debug("Netpartition %s not found for deleting" % name_or_id)
            return False

        with self._neutron_exceptions(                "Error deleting netpartition {0}".format(name_or_id)):
            self.manager.submitTask(
                _tasks.NetpartitionDelete(netpartition=netpartition['id']))
        return True


    def create_subnet(self, network_name_or_id, cidr, ip_version=4,
                      enable_dhcp=False, subnet_name=None, tenant_id=None,
                      allocation_pools=None, gateway_ip=None,
                      dns_nameservers=None, host_routes=None,
                      ipv6_ra_mode=None, ipv6_address_mode=None,
                      net_partition=None):
        """Create a subnet on a specified network.

        :param string network_name_or_id:
           The unique name or ID of the attached network. If a non-unique
           name is supplied, an exception is raised.
        :param string cidr:
           The CIDR.
        :param int ip_version:
           The IP version, which is 4 or 6.
        :param bool enable_dhcp:
           Set to ``True`` if DHCP is enabled and ``False`` if disabled.
           Default is ``False``.
        :param string subnet_name:
           The name of the subnet.
        :param string tenant_id:
           The ID of the tenant who owns the network. Only administrative users
           can specify a tenant ID other than their own.
        :param list allocation_pools:
           A list of dictionaries of the start and end addresses for the
           allocation pools. For example::

             [
               {
                 "start": "192.168.199.2",
                 "end": "192.168.199.254"
               }
             ]

        :param string gateway_ip:
           The gateway IP address. When you specify both allocation_pools and
           gateway_ip, you must ensure that the gateway IP does not overlap
           with the specified allocation pools.
        :param list dns_nameservers:
           A list of DNS name servers for the subnet. For example::

             [ "8.8.8.7", "8.8.8.8" ]

        :param list host_routes:
           A list of host route dictionaries for the subnet. For example::

             [
               {
                 "destination": "0.0.0.0/0",
                 "nexthop": "123.456.78.9"
               },
               {
                 "destination": "192.168.0.0/24",
                 "nexthop": "192.168.0.1"
               }
             ]

        :param string ipv6_ra_mode:
           IPv6 Router Advertisement mode. Valid values are: 'dhcpv6-stateful',
           'dhcpv6-stateless', or 'slaac'.
        :param string ipv6_address_mode:
           IPv6 address mode. Valid values are: 'dhcpv6-stateful',
           'dhcpv6-stateless', or 'slaac'.
        :param string net_partition:
           The name of the Neutron net-partition.

        :returns: The new subnet object.
        :raises: OpenStackCloudException on operation error.
        """

        network = self.get_network(network_name_or_id)
        if not network:
            raise OpenStackCloudException(
                "Network %s not found." % network_name_or_id)

        # The body of the neutron message for the subnet we wish to create.
        # This includes attributes that are required or have defaults.
        subnet = {
            'network_id': network['id'],
            'cidr': cidr,
            'ip_version': ip_version,
            'enable_dhcp': enable_dhcp
        }

        # Add optional attributes to the message.
        if subnet_name:
            subnet['name'] = subnet_name
        if tenant_id:
            subnet['tenant_id'] = tenant_id
        if allocation_pools:
            subnet['allocation_pools'] = allocation_pools
        if gateway_ip:
            subnet['gateway_ip'] = gateway_ip
        if dns_nameservers:
            subnet['dns_nameservers'] = dns_nameservers
        if host_routes:
            subnet['host_routes'] = host_routes
        if ipv6_ra_mode:
            subnet['ipv6_ra_mode'] = ipv6_ra_mode
        if ipv6_address_mode:
            subnet['ipv6_address_mode'] = ipv6_address_mode
        if net_partition:
            subnet['net_partition'] = net_partition

        with _utils.neutron_exceptions(
                "Error creating subnet on network "
                "{0}".format(network_name_or_id)):
            new_subnet = self.manager.submitTask(
                _tasks.SubnetCreate(body=dict(subnet=subnet)))

        return new_subnet['subnet']

    def delete_subnet(self, name_or_id):
        """Delete a subnet.

        If a name, instead of a unique UUID, is supplied, it is possible
        that we could find more than one matching subnet since names are
        not required to be unique. An error will be raised in this case.

        :param name_or_id: Name or ID of the subnet being deleted.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        """
        subnet = self.get_subnet(name_or_id)
        if not subnet:
            self.log.debug("Subnet %s not found for deleting" % name_or_id)
            return False

        with _utils.neutron_exceptions(
                "Error deleting subnet {0}".format(name_or_id)):
            self.manager.submitTask(
                _tasks.SubnetDelete(subnet=subnet['id']))
        return True

    def update_subnet(self, name_or_id, subnet_name=None, enable_dhcp=None,
                      gateway_ip=None, allocation_pools=None,
                      dns_nameservers=None, host_routes=None):
        """Update an existing subnet.

        :param string name_or_id:
           Name or ID of the subnet to update.
        :param string subnet_name:
           The new name of the subnet.
        :param bool enable_dhcp:
           Set to ``True`` if DHCP is enabled and ``False`` if disabled.
        :param string gateway_ip:
           The gateway IP address. When you specify both allocation_pools and
           gateway_ip, you must ensure that the gateway IP does not overlap
           with the specified allocation pools.
        :param list allocation_pools:
           A list of dictionaries of the start and end addresses for the
           allocation pools. For example::

             [
               {
                 "start": "192.168.199.2",
                 "end": "192.168.199.254"
               }
             ]

        :param list dns_nameservers:
           A list of DNS name servers for the subnet. For example::

             [ "8.8.8.7", "8.8.8.8" ]

        :param list host_routes:
           A list of host route dictionaries for the subnet. For example::

             [
               {
                 "destination": "0.0.0.0/0",
                 "nexthop": "123.456.78.9"
               },
               {
                 "destination": "192.168.0.0/24",
                 "nexthop": "192.168.0.1"
               }
             ]

        :returns: The updated subnet object.
        :raises: OpenStackCloudException on operation error.
        """
        subnet = {}
        if subnet_name:
            subnet['name'] = subnet_name
        if enable_dhcp is not None:
            subnet['enable_dhcp'] = enable_dhcp
        if gateway_ip:
            subnet['gateway_ip'] = gateway_ip
        if allocation_pools:
            subnet['allocation_pools'] = allocation_pools
        if dns_nameservers:
            subnet['dns_nameservers'] = dns_nameservers
        if host_routes:
            subnet['host_routes'] = host_routes

        if not subnet:
            self.log.debug("No subnet data to update")
            return

        curr_subnet = self.get_subnet(name_or_id)
        if not curr_subnet:
            raise OpenStackCloudException(
                "Subnet %s not found." % name_or_id)

        with _utils.neutron_exceptions(
                "Error updating subnet {0}".format(name_or_id)):
            new_subnet = self.manager.submitTask(
                _tasks.SubnetUpdate(
                    subnet=curr_subnet['id'], body=dict(subnet=subnet)))
        # Turns out neutron returns an actual dict, so no need for the
        # use of meta.obj_to_dict() here (which would not work against
        # a dict).
        return new_subnet['subnet']

    @_utils.valid_kwargs('name', 'admin_state_up', 'mac_address', 'fixed_ips',
                         'subnet_id', 'ip_address', 'security_groups',
                         'allowed_address_pairs', 'extra_dhcp_opts',
                         'device_owner', 'device_id')
    def create_port(self, network_id, **kwargs):
        """Create a port

        :param network_id: The ID of the network. (Required)
        :param name: A symbolic name for the port. (Optional)
        :param admin_state_up: The administrative status of the port,
            which is up (true, default) or down (false). (Optional)
        :param mac_address: The MAC address. (Optional)
        :param fixed_ips: List of ip_addresses and subnet_ids. See subnet_id
            and ip_address. (Optional)
            For example::

              [
                {
                  "ip_address": "10.29.29.13",
                  "subnet_id": "a78484c4-c380-4b47-85aa-21c51a2d8cbd"
                }, ...
              ]
        :param subnet_id: If you specify only a subnet ID, OpenStack Networking
            allocates an available IP from that subnet to the port. (Optional)
            If you specify both a subnet ID and an IP address, OpenStack
            Networking tries to allocate the specified address to the port.
        :param ip_address: If you specify both a subnet ID and an IP address,
            OpenStack Networking tries to allocate the specified address to
            the port.
        :param security_groups: List of security group UUIDs. (Optional)
        :param allowed_address_pairs: Allowed address pairs list (Optional)
            For example::

              [
                {
                  "ip_address": "23.23.23.1",
                  "mac_address": "fa:16:3e:c4:cd:3f"
                }, ...
              ]
        :param extra_dhcp_opts: Extra DHCP options. (Optional).
            For example::

              [
                {
                  "opt_name": "opt name1",
                  "opt_value": "value1"
                }, ...
              ]
        :param device_owner: The ID of the entity that uses this port.
            For example, a DHCP agent.  (Optional)
        :param device_id: The ID of the device that uses this port.
            For example, a virtual server. (Optional)

        :returns: a dictionary describing the created port.

        :raises: ``OpenStackCloudException`` on operation error.
        """
        kwargs['network_id'] = network_id

        with _utils.neutron_exceptions(
                "Error creating port for network {0}".format(network_id)):
            return self.manager.submitTask(
                _tasks.PortCreate(body={'port': kwargs}))['port']

    @_utils.valid_kwargs('name', 'admin_state_up', 'fixed_ips',
                         'security_groups', 'allowed_address_pairs',
                         'extra_dhcp_opts', 'device_owner')
    def update_port(self, name_or_id, **kwargs):
        """Update a port

        Note: to unset an attribute use None value. To leave an attribute
        untouched just omit it.

        :param name_or_id: name or id of the port to update. (Required)
        :param name: A symbolic name for the port. (Optional)
        :param admin_state_up: The administrative status of the port,
            which is up (true) or down (false). (Optional)
        :param fixed_ips: List of ip_addresses and subnet_ids. (Optional)
            If you specify only a subnet ID, OpenStack Networking allocates
            an available IP from that subnet to the port.
            If you specify both a subnet ID and an IP address, OpenStack
            Networking tries to allocate the specified address to the port.
            For example::

              [
                {
                  "ip_address": "10.29.29.13",
                  "subnet_id": "a78484c4-c380-4b47-85aa-21c51a2d8cbd"
                }, ...
              ]
        :param security_groups: List of security group UUIDs. (Optional)
        :param allowed_address_pairs: Allowed address pairs list (Optional)
            For example::

              [
                {
                  "ip_address": "23.23.23.1",
                  "mac_address": "fa:16:3e:c4:cd:3f"
                }, ...
              ]
        :param extra_dhcp_opts: Extra DHCP options. (Optional).
            For example::

              [
                {
                  "opt_name": "opt name1",
                  "opt_value": "value1"
                }, ...
              ]
        :param device_owner: The ID of the entity that uses this port.
            For example, a DHCP agent.  (Optional)

        :returns: a dictionary describing the updated port.

        :raises: OpenStackCloudException on operation error.
        """
        port = self.get_port(name_or_id=name_or_id)
        if port is None:
            raise OpenStackCloudException(
                "failed to find port '{port}'".format(port=name_or_id))

        with _utils.neutron_exceptions(
                "Error updating port {0}".format(name_or_id)):
            return self.manager.submitTask(
                _tasks.PortUpdate(
                    port=port['id'], body={'port': kwargs}))['port']

    def delete_port(self, name_or_id):
        """Delete a port

        :param name_or_id: id or name of the port to delete.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        """
        port = self.get_port(name_or_id=name_or_id)
        if port is None:
            self.log.debug("Port %s not found for deleting" % name_or_id)
            return False

        with _utils.neutron_exceptions(
                "Error deleting port {0}".format(name_or_id)):
            self.manager.submitTask(_tasks.PortDelete(port=port['id']))
        return True

    def create_security_group(self, name, description):
        """Create a new security group

        :param string name: A name for the security group.
        :param string description: Describes the security group.

        :returns: A dict representing the new security group.

        :raises: OpenStackCloudException on operation error.
        :raises: OpenStackCloudUnavailableFeature if security groups are
                 not supported on this cloud.
        """
        if self.secgroup_source == 'neutron':
            with _utils.neutron_exceptions(
                    "Error creating security group {0}".format(name)):
                group = self.manager.submitTask(
                    _tasks.NeutronSecurityGroupCreate(
                        body=dict(security_group=dict(name=name,
                                                      description=description))
                    )
                )
            return group['security_group']

        elif self.secgroup_source == 'nova':
            try:
                group = meta.obj_to_dict(
                    self.manager.submitTask(
                        _tasks.NovaSecurityGroupCreate(
                            name=name, description=description
                        )
                    )
                )
            except Exception as e:
                raise OpenStackCloudException(
                    "failed to create security group '{name}': {msg}".format(
                        name=name, msg=str(e)))
            return _utils.normalize_nova_secgroups([group])[0]

        # Security groups not supported
        else:
            raise OpenStackCloudUnavailableFeature(
                "Unavailable feature: security groups"
            )

    def delete_security_group(self, name_or_id):
        """Delete a security group

        :param string name_or_id: The name or unique ID of the security group.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        :raises: OpenStackCloudUnavailableFeature if security groups are
                 not supported on this cloud.
        """
        secgroup = self.get_security_group(name_or_id)
        if secgroup is None:
            self.log.debug('Security group %s not found for deleting' %
                           name_or_id)
            return False

        if self.secgroup_source == 'neutron':
            with _utils.neutron_exceptions(
                    "Error deleting security group {0}".format(name_or_id)):
                self.manager.submitTask(
                    _tasks.NeutronSecurityGroupDelete(
                        security_group=secgroup['id']
                    )
                )
            return True

        elif self.secgroup_source == 'nova':
            try:
                self.manager.submitTask(
                    _tasks.NovaSecurityGroupDelete(group=secgroup['id'])
                )
            except Exception as e:
                raise OpenStackCloudException(
                    "failed to delete security group '{group}': {msg}".format(
                        group=name_or_id, msg=str(e)))
            return True

        # Security groups not supported
        else:
            raise OpenStackCloudUnavailableFeature(
                "Unavailable feature: security groups"
            )

    @_utils.valid_kwargs('name', 'description')
    def update_security_group(self, name_or_id, **kwargs):
        """Update a security group

        :param string name_or_id: Name or ID of the security group to update.
        :param string name: New name for the security group.
        :param string description: New description for the security group.

        :returns: A dictionary describing the updated security group.

        :raises: OpenStackCloudException on operation error.
        """
        secgroup = self.get_security_group(name_or_id)

        if secgroup is None:
            raise OpenStackCloudException(
                "Security group %s not found." % name_or_id)

        if self.secgroup_source == 'neutron':
            with _utils.neutron_exceptions(
                    "Error updating security group {0}".format(name_or_id)):
                group = self.manager.submitTask(
                    _tasks.NeutronSecurityGroupUpdate(
                        security_group=secgroup['id'],
                        body={'security_group': kwargs})
                )
            return group['security_group']

        elif self.secgroup_source == 'nova':
            try:
                group = meta.obj_to_dict(
                    self.manager.submitTask(
                        _tasks.NovaSecurityGroupUpdate(
                            group=secgroup['id'], **kwargs)
                    )
                )
            except Exception as e:
                raise OpenStackCloudException(
                    "failed to update security group '{group}': {msg}".format(
                        group=name_or_id, msg=str(e)))
            return _utils.normalize_nova_secgroups([group])[0]

        # Security groups not supported
        else:
            raise OpenStackCloudUnavailableFeature(
                "Unavailable feature: security groups"
            )

    def create_security_group_rule(self,
                                   secgroup_name_or_id,
                                   port_range_min=None,
                                   port_range_max=None,
                                   protocol=None,
                                   remote_ip_prefix=None,
                                   remote_group_id=None,
                                   direction='ingress',
                                   ethertype='IPv4'):
        """Create a new security group rule

        :param string secgroup_name_or_id:
            The security group name or ID to associate with this security
            group rule. If a non-unique group name is given, an exception
            is raised.
        :param int port_range_min:
            The minimum port number in the range that is matched by the
            security group rule. If the protocol is TCP or UDP, this value
            must be less than or equal to the port_range_max attribute value.
            If nova is used by the cloud provider for security groups, then
            a value of None will be transformed to -1.
        :param int port_range_max:
            The maximum port number in the range that is matched by the
            security group rule. The port_range_min attribute constrains the
            port_range_max attribute. If nova is used by the cloud provider
            for security groups, then a value of None will be transformed
            to -1.
        :param string protocol:
            The protocol that is matched by the security group rule. Valid
            values are None, tcp, udp, and icmp.
        :param string remote_ip_prefix:
            The remote IP prefix to be associated with this security group
            rule. This attribute matches the specified IP prefix as the
            source IP address of the IP packet.
        :param string remote_group_id:
            The remote group ID to be associated with this security group
            rule.
        :param string direction:
            Ingress or egress: The direction in which the security group
            rule is applied. For a compute instance, an ingress security
            group rule is applied to incoming (ingress) traffic for that
            instance. An egress rule is applied to traffic leaving the
            instance.
        :param string ethertype:
            Must be IPv4 or IPv6, and addresses represented in CIDR must
            match the ingress or egress rules.

        :returns: A dict representing the new security group rule.

        :raises: OpenStackCloudException on operation error.
        """

        secgroup = self.get_security_group(secgroup_name_or_id)
        if not secgroup:
            raise OpenStackCloudException(
                "Security group %s not found." % secgroup_name_or_id)

        if self.secgroup_source == 'neutron':
            # NOTE: Nova accepts -1 port numbers, but Neutron accepts None
            # as the equivalent value.
            rule_def = {
                'security_group_id': secgroup['id'],
                'port_range_min':
                    None if port_range_min == -1 else port_range_min,
                'port_range_max':
                    None if port_range_max == -1 else port_range_max,
                'protocol': protocol,
                'remote_ip_prefix': remote_ip_prefix,
                'remote_group_id': remote_group_id,
                'direction': direction,
                'ethertype': ethertype
            }

            with _utils.neutron_exceptions(
                    "Error creating security group rule"):
                rule = self.manager.submitTask(
                    _tasks.NeutronSecurityGroupRuleCreate(
                        body={'security_group_rule': rule_def})
                )
            return rule['security_group_rule']

        elif self.secgroup_source == 'nova':
            # NOTE: Neutron accepts None for protocol. Nova does not.
            if protocol is None:
                raise OpenStackCloudException('Protocol must be specified')

            if direction == 'egress':
                self.log.debug(
                    'Rule creation failed: Nova does not support egress rules'
                )
                raise OpenStackCloudException('No support for egress rules')

            # NOTE: Neutron accepts None for ports, but Nova requires -1
            # as the equivalent value for ICMP.
            #
            # For TCP/UDP, if both are None, Neutron allows this and Nova
            # represents this as all ports (1-65535). Nova does not accept
            # None values, so to hide this difference, we will automatically
            # convert to the full port range. If only a single port value is
            # specified, it will error as normal.
            if protocol == 'icmp':
                if port_range_min is None:
                    port_range_min = -1
                if port_range_max is None:
                    port_range_max = -1
            elif protocol in ['tcp', 'udp']:
                if port_range_min is None and port_range_max is None:
                    port_range_min = 1
                    port_range_max = 65535

            try:
                rule = meta.obj_to_dict(
                    self.manager.submitTask(
                        _tasks.NovaSecurityGroupRuleCreate(
                            parent_group_id=secgroup['id'],
                            ip_protocol=protocol,
                            from_port=port_range_min,
                            to_port=port_range_max,
                            cidr=remote_ip_prefix,
                            group_id=remote_group_id
                        )
                    )
                )
            except Exception as e:
                raise OpenStackCloudException(
                    "failed to create security group rule: {msg}".format(
                        msg=str(e)))
            return _utils.normalize_nova_secgroup_rules([rule])[0]

        # Security groups not supported
        else:
            raise OpenStackCloudUnavailableFeature(
                "Unavailable feature: security groups"
            )

    def delete_security_group_rule(self, rule_id):
        """Delete a security group rule

        :param string rule_id: The unique ID of the security group rule.

        :returns: True if delete succeeded, False otherwise.

        :raises: OpenStackCloudException on operation error.
        :raises: OpenStackCloudUnavailableFeature if security groups are
                 not supported on this cloud.
        """

        if self.secgroup_source == 'neutron':
            try:
                with _utils.neutron_exceptions(
                        "Error deleting security group rule "
                        "{0}".format(rule_id)):
                    self.manager.submitTask(
                        _tasks.NeutronSecurityGroupRuleDelete(
                            security_group_rule=rule_id)
                    )
            except OpenStackCloudResourceNotFound:
                return False
            return True

        elif self.secgroup_source == 'nova':
            try:
                self.manager.submitTask(
                    _tasks.NovaSecurityGroupRuleDelete(rule=rule_id)
                )
            except nova_exceptions.NotFound:
                return False
            except Exception as e:
                raise OpenStackCloudException(
                    "failed to delete security group rule {id}: {msg}".format(
                        id=rule_id, msg=str(e)))
            return True

        # Security groups not supported
        else:
            raise OpenStackCloudUnavailableFeature(
                "Unavailable feature: security groups"
            )
