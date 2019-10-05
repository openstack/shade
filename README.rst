Introduction
============

.. warning::

  shade has been superceded by `openstacksdk`_ and no longer takes new
  features. The existing code will continue to be maintained indefinitely
  for bugfixes as necessary, but improvements will be deferred to
  `openstacksdk`_. Please update your applications to use `openstacksdk`_
  directly.

shade is a simple client library for interacting with OpenStack clouds. The
key word here is *simple*. Clouds can do many many many things - but there are
probably only about 10 of them that most people care about with any
regularity. If you want to do complicated things, you should probably use
the lower level client libraries - or even the REST API directly. However,
if what you want is to be able to write an application that talks to clouds
no matter what crazy choices the deployer has made in an attempt to be
more hipster than their self-entitled narcissist peers, then shade is for you.

shade started its life as some code inside of ansible. ansible has a bunch
of different OpenStack related modules, and there was a ton of duplicated
code. Eventually, between refactoring that duplication into an internal
library, and adding logic and features that the OpenStack Infra team had
developed to run client applications at scale, it turned out that we'd written
nine-tenths of what we'd need to have a standalone library.

.. _usage_example:

Example
=======

Sometimes an example is nice.

#. Create a ``clouds.yml`` file::

     clouds:
      mordred:
        region_name: RegionOne
        auth:
          username: 'mordred'
          password: XXXXXXX
          project_name: 'shade'
          auth_url: 'https://montytaylor-sjc.openstack.blueboxgrid.com:5001/v2.0'

   Please note: *os-client-config* will look for a file called ``clouds.yaml``
   in the following locations:

   * Current Directory
   * ``~/.config/openstack``
   * ``/etc/openstack``

   More information at https://pypi.org/project/os-client-config


#. Create a server with *shade*, configured with the ``clouds.yml`` file::

    import shade

    # Initialize and turn on debug logging
    shade.simple_logging(debug=True)

    # Initialize cloud
    # Cloud configs are read with os-client-config
    cloud = shade.openstack_cloud(cloud='mordred')

    # Upload an image to the cloud
    image = cloud.create_image(
        'ubuntu-trusty', filename='ubuntu-trusty.qcow2', wait=True)

    # Find a flavor with at least 512M of RAM
    flavor = cloud.get_flavor_by_ram(512)

    # Boot a server, wait for it to boot, and then do whatever is needed
    # to get a public ip for it.
    cloud.create_server(
        'my-server', image=image, flavor=flavor, wait=True, auto_ip=True)


Links
=====

* `Issue Tracker <https://storyboard.openstack.org/#!/project/760>`_
* `Code Review <https://review.openstack.org/#/q/status:open+project:openstack/shade,n,z>`_
* `Documentation <https://docs.openstack.org/shade/latest/>`_
* `Source <https://opendev.org/openstack/shade>`_
* `PyPI <https://pypi.org/project/shade/>`_
* `Mailing list <http://lists.openstack.org/cgi-bin/mailman/listinfo/openstack>`_
* `Release notes <https://docs.openstack.org/releasenotes/shade>`_
* `Contributing <https://docs.openstack.org/shade/latest/contributor/index.html>`_

.. _openstacksdk: https://docs.openstack.org/openstacksdk/latest/user/
