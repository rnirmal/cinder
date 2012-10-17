# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Tests for Volume Code.

"""

import os
import datetime

import mox
import shutil
import tempfile

from cinder import context
from cinder import exception
from cinder import db
from cinder import flags
from cinder.tests.image import fake as fake_image
from cinder.openstack.common import importutils
from cinder.openstack.common.notifier import api as notifier_api
from cinder.openstack.common.notifier import test_notifier
from cinder.openstack.common import rpc
import cinder.policy
from cinder import quota
from cinder import test
from cinder.volume import iscsi

QUOTAS = quota.QUOTAS
FLAGS = flags.FLAGS


class VolumeTestCase(test.TestCase):
    """Test Case for volumes."""

    def setUp(self):
        super(VolumeTestCase, self).setUp()
        vol_tmpdir = tempfile.mkdtemp()
        self.flags(connection_type='fake',
                   volumes_dir=vol_tmpdir,
                   notification_driver=[test_notifier.__name__])
        self.volume = importutils.import_object(FLAGS.volume_manager)
        self.context = context.get_admin_context()
        self.stubs.Set(iscsi.TgtAdm, '_get_target', self.fake_get_target)
        fake_image.stub_out_image_service(self.stubs)
        test_notifier.NOTIFICATIONS = []

    def tearDown(self):
        try:
            shutil.rmtree(FLAGS.volumes_dir)
        except OSError:
            pass
        notifier_api._reset_drivers()
        super(VolumeTestCase, self).tearDown()

    def fake_get_target(obj, iqn):
        return 1

    @staticmethod
    def _create_volume(size=0, snapshot_id=None, image_id=None,
                       metadata=None):
        """Create a volume object."""
        vol = {}
        vol['size'] = size
        vol['snapshot_id'] = snapshot_id
        vol['image_id'] = image_id
        vol['user_id'] = 'fake'
        vol['project_id'] = 'fake'
        vol['availability_zone'] = FLAGS.storage_availability_zone
        vol['status'] = "creating"
        vol['attach_status'] = "detached"
        if metadata is not None:
            vol['metadata'] = metadata
        return db.volume_create(context.get_admin_context(), vol)

    def test_create_delete_volume(self):
        """Test volume can be created and deleted."""
        # Need to stub out reserve, commit, and rollback
        def fake_reserve(context, expire=None, **deltas):
            return ["RESERVATION"]

        def fake_commit(context, reservations):
            pass

        def fake_rollback(context, reservations):
            pass

        self.stubs.Set(QUOTAS, "reserve", fake_reserve)
        self.stubs.Set(QUOTAS, "commit", fake_commit)
        self.stubs.Set(QUOTAS, "rollback", fake_rollback)

        volume = self._create_volume()
        volume_id = volume['id']
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 0)
        self.volume.create_volume(self.context, volume_id)
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        self.assertEqual(volume_id, db.volume_get(context.get_admin_context(),
                         volume_id).id)

        self.volume.delete_volume(self.context, volume_id)
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 4)
        self.assertRaises(exception.NotFound,
                          db.volume_get,
                          self.context,
                          volume_id)

    def test_create_delete_volume_with_metadata(self):
        """Test volume can be created with metadata and deleted."""
        test_meta = {'fake_key': 'fake_value'}
        volume = self._create_volume(0, None, metadata=test_meta)
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)
        result_meta = {
            volume.volume_metadata[0].key: volume.volume_metadata[0].value}
        self.assertEqual(result_meta, test_meta)

        self.volume.delete_volume(self.context, volume_id)
        self.assertRaises(exception.NotFound,
                          db.volume_get,
                          self.context,
                          volume_id)

    def test_delete_busy_volume(self):
        """Test volume survives deletion if driver reports it as busy."""
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)

        self.mox.StubOutWithMock(self.volume.get_driver(), 'delete_volume')
        self.volume.get_driver().delete_volume(mox.IgnoreArg()) \
                                              .AndRaise(exception.VolumeIsBusy)
        self.mox.ReplayAll()
        res = self.volume.delete_volume(self.context, volume_id)
        self.assertEqual(True, res)
        volume_ref = db.volume_get(context.get_admin_context(), volume_id)
        self.assertEqual(volume_id, volume_ref.id)
        self.assertEqual("available", volume_ref.status)

        self.mox.UnsetStubs()
        self.volume.delete_volume(self.context, volume_id)

    def test_create_volume_from_snapshot(self):
        """Test volume can be created from a snapshot."""
        volume_src = self._create_volume()
        self.volume.create_volume(self.context, volume_src['id'])
        snapshot_id = self._create_snapshot(volume_src['id'])['id']
        self.volume.create_snapshot(self.context, volume_src['id'],
                                    snapshot_id)
        volume_dst = self._create_volume(0, snapshot_id)
        self.volume.create_volume(self.context, volume_dst['id'], snapshot_id)
        self.assertEqual(volume_dst['id'],
                         db.volume_get(
                             context.get_admin_context(),
                             volume_dst['id']).id)
        self.assertEqual(snapshot_id, db.volume_get(
                context.get_admin_context(),
                volume_dst['id']).snapshot_id)

        self.volume.delete_volume(self.context, volume_dst['id'])
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume_src['id'])

    def test_too_big_volume(self):
        """Ensure failure if a too large of a volume is requested."""
        # FIXME(vish): validation needs to move into the data layer in
        #              volume_create
        return True
        try:
            volume = self._create_volume(1001)
            self.volume.create_volume(self.context, volume)
            self.fail("Should have thrown TypeError")
        except TypeError:
            pass

    def test_run_attach_detach_volume(self):
        """Make sure volume can be attached and detached from instance."""
        instance_uuid = '12345678-1234-5678-1234-567812345678'
        mountpoint = "/dev/sdf"
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)
        db.volume_attached(self.context, volume_id, instance_uuid, mountpoint)
        vol = db.volume_get(context.get_admin_context(), volume_id)
        self.assertEqual(vol['status'], "in-use")
        self.assertEqual(vol['attach_status'], "attached")
        self.assertEqual(vol['mountpoint'], mountpoint)
        self.assertEqual(vol['instance_uuid'], instance_uuid)

        self.assertRaises(exception.VolumeAttached,
                          self.volume.delete_volume,
                          self.context,
                          volume_id)
        db.volume_detached(self.context, volume_id)
        vol = db.volume_get(self.context, volume_id)
        self.assertEqual(vol['status'], "available")

        self.volume.delete_volume(self.context, volume_id)
        self.assertRaises(exception.VolumeNotFound,
                          db.volume_get,
                          self.context,
                          volume_id)

    def test_concurrent_volumes_get_different_targets(self):
        """Ensure multiple concurrent volumes get different targets."""
        volume_ids = []
        targets = []

        def _check(volume_id):
            """Make sure targets aren't duplicated."""
            volume_ids.append(volume_id)
            admin_context = context.get_admin_context()
            iscsi_target = db.volume_get_iscsi_target_num(admin_context,
                                                          volume_id)
            self.assert_(iscsi_target not in targets)
            targets.append(iscsi_target)

        total_slots = FLAGS.iscsi_num_targets
        for _index in xrange(total_slots):
            self._create_volume()
        for volume_id in volume_ids:
            self.volume.delete_volume(self.context, volume_id)

    def test_multi_node(self):
        # TODO(termie): Figure out how to test with two nodes,
        # each of them having a different FLAG for storage_node
        # This will allow us to test cross-node interactions
        pass

    @staticmethod
    def _create_snapshot(volume_id, size='0'):
        """Create a snapshot object."""
        snap = {}
        snap['volume_size'] = size
        snap['user_id'] = 'fake'
        snap['project_id'] = 'fake'
        snap['volume_id'] = volume_id
        snap['status'] = "creating"
        return db.snapshot_create(context.get_admin_context(), snap)

    def test_create_delete_snapshot(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        snapshot_id = self._create_snapshot(volume['id'])['id']
        self.volume.create_snapshot(self.context, volume['id'], snapshot_id)
        self.assertEqual(snapshot_id,
                         db.snapshot_get(context.get_admin_context(),
                                         snapshot_id).id)

        self.volume.delete_snapshot(self.context, snapshot_id)
        self.assertRaises(exception.NotFound,
                          db.snapshot_get,
                          self.context,
                          snapshot_id)
        self.volume.delete_volume(self.context, volume['id'])

    def test_cant_delete_volume_in_use(self):
        """Test volume can't be deleted in invalid stats."""
        # create a volume and assign to host
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        volume['status'] = 'in-use'
        volume['host'] = 'fakehost'

        volume_api = cinder.volume.api.API()

        # 'in-use' status raises InvalidVolume
        self.assertRaises(exception.InvalidVolume,
                          volume_api.delete,
                          self.context,
                          volume)

        # clean up
        self.volume.delete_volume(self.context, volume['id'])

    def test_force_delete_volume(self):
        """Test volume can be forced to delete."""
        # create a volume and assign to host
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        volume['status'] = 'error_deleting'
        volume['host'] = 'fakehost'

        volume_api = cinder.volume.api.API()

        # 'error_deleting' volumes can't be deleted
        self.assertRaises(exception.InvalidVolume,
                          volume_api.delete,
                          self.context,
                          volume)

        # delete with force
        volume_api.delete(self.context, volume, force=True)

        # status is deleting
        volume = db.volume_get(context.get_admin_context(), volume['id'])
        self.assertEquals(volume['status'], 'deleting')

        # clean up
        self.volume.delete_volume(self.context, volume['id'])

    def test_cant_delete_volume_with_snapshots(self):
        """Test volume can't be deleted with dependent snapshots."""
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        snapshot_id = self._create_snapshot(volume['id'])['id']
        self.volume.create_snapshot(self.context, volume['id'], snapshot_id)
        self.assertEqual(snapshot_id,
                         db.snapshot_get(context.get_admin_context(),
                                         snapshot_id).id)

        volume['status'] = 'available'
        volume['host'] = 'fakehost'

        volume_api = cinder.volume.api.API()

        self.assertRaises(exception.InvalidVolume,
                          volume_api.delete,
                          self.context,
                          volume)
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume['id'])

    def test_can_delete_errored_snapshot(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        snapshot_id = self._create_snapshot(volume['id'])['id']
        self.volume.create_snapshot(self.context, volume['id'], snapshot_id)
        snapshot = db.snapshot_get(context.get_admin_context(),
                                   snapshot_id)

        volume_api = cinder.volume.api.API()

        snapshot['status'] = 'badstatus'
        self.assertRaises(exception.InvalidVolume,
                          volume_api.delete_snapshot,
                          self.context,
                          snapshot)

        snapshot['status'] = 'error'
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume['id'])

    def test_create_snapshot_force(self):
        """Test snapshot in use can be created forcibly."""

        def fake_cast(ctxt, topic, msg):
            pass
        self.stubs.Set(rpc, 'cast', fake_cast)
        instance_uuid = '12345678-1234-5678-1234-567812345678'

        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        db.volume_attached(self.context, volume['id'], instance_uuid,
                           '/dev/sda1')

        volume_api = cinder.volume.api.API()
        volume = volume_api.get(self.context, volume['id'])
        self.assertRaises(exception.InvalidVolume,
                          volume_api.create_snapshot,
                          self.context, volume,
                          'fake_name', 'fake_description')
        snapshot_ref = volume_api.create_snapshot_force(self.context,
                                                        volume,
                                                        'fake_name',
                                                        'fake_description')
        db.snapshot_destroy(self.context, snapshot_ref['id'])
        db.volume_destroy(self.context, volume['id'])

    def test_delete_busy_snapshot(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)
        snapshot_id = self._create_snapshot(volume_id)['id']
        self.volume.create_snapshot(self.context, volume_id, snapshot_id)

        self.mox.StubOutWithMock(self.volume.get_driver(), 'delete_snapshot')
        self.volume.get_driver().delete_snapshot(mox.IgnoreArg()) \
                                            .AndRaise(exception.SnapshotIsBusy)
        self.mox.ReplayAll()
        self.volume.delete_snapshot(self.context, snapshot_id)
        snapshot_ref = db.snapshot_get(self.context, snapshot_id)
        self.assertEqual(snapshot_id, snapshot_ref.id)
        self.assertEqual("available", snapshot_ref.status)

        self.mox.UnsetStubs()
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume_id)

    def _create_volume_from_image(self, expected_status,
                                  fakeout_copy_image_to_volume=False):
        """Call copy image to volume, Test the status of volume after calling
        copying image to volume."""
        def fake_local_path(volume):
            return dst_path

        def fake_copy_image_to_volume(context, volume, image_id):
            pass

        dst_fd, dst_path = tempfile.mkstemp()
        os.close(dst_fd)
        self.stubs.Set(self.volume.driver, 'local_path', fake_local_path)
        if fakeout_copy_image_to_volume:
            self.stubs.Set(self.volume, '_copy_image_to_volume',
                           fake_copy_image_to_volume)

        image_id = 'c905cedb-7281-47e4-8a62-f26bc5fc4c77'
        volume_id = 1
        # creating volume testdata
        db.volume_create(self.context, {'id': volume_id,
                            'updated_at': datetime.datetime(1, 1, 1, 1, 1, 1),
                            'display_description': 'Test Desc',
                            'size': 20,
                            'status': 'creating',
                            'instance_uuid': None,
                            'host': 'dummy'})
        try:
            self.volume.create_volume(self.context,
                                      volume_id,
                                      image_id=image_id)

            volume = db.volume_get(self.context, volume_id)
            self.assertEqual(volume['status'], expected_status)
        finally:
            # cleanup
            db.volume_destroy(self.context, volume_id)
            os.unlink(dst_path)

    def test_create_volume_from_image_status_downloading(self):
        """Verify that before copying image to volume, it is in downloading
        state."""
        self._create_volume_from_image('downloading', True)

    def test_create_volume_from_image_status_available(self):
        """Verify that before copying image to volume, it is in available
        state."""
        self._create_volume_from_image('available')

    def test_create_volume_from_image_exception(self):
        """Verify that create volume from image, the volume status is
        'downloading'."""
        dst_fd, dst_path = tempfile.mkstemp()
        os.close(dst_fd)

        self.stubs.Set(self.volume.driver, 'local_path', lambda x: dst_path)

        image_id = 'aaaaaaaa-0000-0000-0000-000000000000'
        # creating volume testdata
        volume_id = 1
        db.volume_create(self.context, {'id': volume_id,
                             'updated_at': datetime.datetime(1, 1, 1, 1, 1, 1),
                             'display_description': 'Test Desc',
                             'size': 20,
                             'status': 'creating',
                             'host': 'dummy'})

        self.assertRaises(exception.ImageNotFound,
                          self.volume.create_volume,
                          self.context,
                          volume_id,
                          None,
                          image_id)
        volume = db.volume_get(self.context, volume_id)
        self.assertEqual(volume['status'], "error")
        # cleanup
        db.volume_destroy(self.context, volume_id)
        os.unlink(dst_path)

    def test_copy_volume_to_image_status_available(self):
        dst_fd, dst_path = tempfile.mkstemp()
        os.close(dst_fd)

        def fake_local_path(volume):
            return dst_path

        self.stubs.Set(self.volume.driver, 'local_path', fake_local_path)

        image_id = '70a599e0-31e7-49b7-b260-868f441e862b'
        # creating volume testdata
        volume_id = 1
        db.volume_create(self.context, {'id': volume_id,
                             'updated_at': datetime.datetime(1, 1, 1, 1, 1, 1),
                             'display_description': 'Test Desc',
                             'size': 20,
                             'status': 'uploading',
                             'instance_uuid': None,
                             'host': 'dummy'})

        try:
            # start test
            self.volume.copy_volume_to_image(self.context,
                                                volume_id,
                                                image_id)

            volume = db.volume_get(self.context, volume_id)
            self.assertEqual(volume['status'], 'available')
        finally:
            # cleanup
            db.volume_destroy(self.context, volume_id)
            os.unlink(dst_path)

    def test_copy_volume_to_image_status_use(self):
        dst_fd, dst_path = tempfile.mkstemp()
        os.close(dst_fd)

        def fake_local_path(volume):
            return dst_path

        self.stubs.Set(self.volume.driver, 'local_path', fake_local_path)

        #image_id = '70a599e0-31e7-49b7-b260-868f441e862b'
        image_id = 'a440c04b-79fa-479c-bed1-0b816eaec379'
        # creating volume testdata
        volume_id = 1
        db.volume_create(self.context,
                         {'id': volume_id,
                         'updated_at': datetime.datetime(1, 1, 1, 1, 1, 1),
                         'display_description': 'Test Desc',
                         'size': 20,
                         'status': 'uploading',
                         'instance_uuid':
                            'b21f957d-a72f-4b93-b5a5-45b1161abb02',
                         'host': 'dummy'})

        try:
            # start test
            self.volume.copy_volume_to_image(self.context,
                                                volume_id,
                                                image_id)

            volume = db.volume_get(self.context, volume_id)
            self.assertEqual(volume['status'], 'in-use')
        finally:
            # cleanup
            db.volume_destroy(self.context, volume_id)
            os.unlink(dst_path)

    def test_copy_volume_to_image_exception(self):
        dst_fd, dst_path = tempfile.mkstemp()
        os.close(dst_fd)

        def fake_local_path(volume):
            return dst_path

        self.stubs.Set(self.volume.driver, 'local_path', fake_local_path)

        image_id = 'aaaaaaaa-0000-0000-0000-000000000000'
        # creating volume testdata
        volume_id = 1
        db.volume_create(self.context, {'id': volume_id,
                             'updated_at': datetime.datetime(1, 1, 1, 1, 1, 1),
                             'display_description': 'Test Desc',
                             'size': 20,
                             'status': 'in-use',
                             'host': 'dummy'})

        try:
            # start test
            self.assertRaises(exception.ImageNotFound,
                              self.volume.copy_volume_to_image,
                              self.context,
                              volume_id,
                              image_id)

            volume = db.volume_get(self.context, volume_id)
            self.assertEqual(volume['status'], 'available')
        finally:
            # cleanup
            db.volume_destroy(self.context, volume_id)
            os.unlink(dst_path)

    def test_create_volume_from_exact_sized_image(self):
        """Verify that an image which is exactly the same size as the
        volume, will work correctly."""
        class _FakeImageService:
            def __init__(self, db_driver=None, image_service=None):
                pass

            def show(self, context, image_id):
                return {'size': 2 * 1024 * 1024 * 1024}

        image_id = '70a599e0-31e7-49b7-b260-868f441e862b'

        try:
            volume_id = None
            volume_api = cinder.volume.api.API(
                                            image_service=_FakeImageService())
            volume = volume_api.create(self.context, 2, 'name', 'description',
                                       image_id=1)
            volume_id = volume['id']
            self.assertEqual(volume['status'], 'creating')

        finally:
            # cleanup
            db.volume_destroy(self.context, volume_id)

    def test_create_volume_from_oversized_image(self):
        """Verify that an image which is too big will fail correctly."""
        class _FakeImageService:
            def __init__(self, db_driver=None, image_service=None):
                pass

            def show(self, context, image_id):
                return {'size': 2 * 1024 * 1024 * 1024 + 1}

        image_id = '70a599e0-31e7-49b7-b260-868f441e862b'

        volume_api = cinder.volume.api.API(image_service=_FakeImageService())

        self.assertRaises(exception.InvalidInput,
                          volume_api.create,
                          self.context, 2,
                          'name', 'description', image_id=1)

    def _do_test_create_volume_with_size(self, size):
        def fake_reserve(context, expire=None, **deltas):
            return ["RESERVATION"]

        def fake_commit(context, reservations):
            pass

        def fake_rollback(context, reservations):
            pass

        self.stubs.Set(QUOTAS, "reserve", fake_reserve)
        self.stubs.Set(QUOTAS, "commit", fake_commit)
        self.stubs.Set(QUOTAS, "rollback", fake_rollback)

        volume_api = cinder.volume.api.API()

        volume = volume_api.create(self.context,
                                   size,
                                   'name',
                                   'description')
        self.assertEquals(volume['size'], int(size))

    def test_create_volume_int_size(self):
        """Test volume creation with int size."""
        self._do_test_create_volume_with_size(2)

    def test_create_volume_string_size(self):
        """Test volume creation with string size."""
        self._do_test_create_volume_with_size('2')

    def test_create_volume_with_bad_size(self):
        def fake_reserve(context, expire=None, **deltas):
            return ["RESERVATION"]

        def fake_commit(context, reservations):
            pass

        def fake_rollback(context, reservations):
            pass

        self.stubs.Set(QUOTAS, "reserve", fake_reserve)
        self.stubs.Set(QUOTAS, "commit", fake_commit)
        self.stubs.Set(QUOTAS, "rollback", fake_rollback)

        volume_api = cinder.volume.api.API()

        self.assertRaises(exception.InvalidInput,
                          volume_api.create,
                          self.context,
                          '2Gb',
                          'name',
                          'description')

    def test_create_volume_usage_notification(self):
        """Ensure create volume generates appropriate usage notification"""
        volume = self._create_volume()
        volume_id = volume['id']
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 0)
        self.volume.create_volume(self.context, volume_id)
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'], 'volume.create.start')
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEquals(msg['priority'], 'INFO')
        self.assertEquals(msg['event_type'], 'volume.create.end')
        payload = msg['payload']
        self.assertEquals(payload['tenant_id'], volume['project_id'])
        self.assertEquals(payload['user_id'], volume['user_id'])
        self.assertEquals(payload['volume_id'], volume['id'])
        self.assertEquals(payload['status'], 'creating')
        self.assertEquals(payload['size'], volume['size'])
        self.assertTrue('display_name' in payload)
        self.assertTrue('snapshot_id' in payload)
        self.assertTrue('launched_at' in payload)
        self.assertTrue('created_at' in payload)
        self.volume.delete_volume(self.context, volume_id)

    def test_begin_roll_detaching_volume(self):
        """Test begin_detaching and roll_detaching functions."""
        volume = self._create_volume()
        volume_api = cinder.volume.api.API()
        volume_api.begin_detaching(self.context, volume)
        volume = db.volume_get(self.context, volume['id'])
        self.assertEqual(volume['status'], "detaching")
        volume_api.roll_detaching(self.context, volume)
        volume = db.volume_get(self.context, volume['id'])
        self.assertEqual(volume['status'], "in-use")

    def test_volume_api_update(self):
        # create a raw vol
        volume = self._create_volume()
        # use volume.api to update name
        volume_api = cinder.volume.api.API()
        update_dict = {'display_name': 'test update name'}
        volume_api.update(self.context, volume, update_dict)
        # read changes from db
        vol = db.volume_get(context.get_admin_context(), volume['id'])
        self.assertEquals(vol['display_name'], 'test update name')

    def test_volume_api_update_snapshot(self):
        # create raw snapshot
        volume = self._create_volume()
        snapshot = self._create_snapshot(volume['id'])
        self.assertEquals(snapshot['display_name'], None)
        # use volume.api to update name
        volume_api = cinder.volume.api.API()
        update_dict = {'display_name': 'test update name'}
        volume_api.update_snapshot(self.context, snapshot, update_dict)
        # read changes from db
        snap = db.snapshot_get(context.get_admin_context(), snapshot['id'])
        self.assertEquals(snap['display_name'], 'test update name')


class DriverTestCase(test.TestCase):
    """Base Test class for Drivers."""
    driver_name = "cinder.volume.driver.FakeBaseDriver"

    def setUp(self):
        super(DriverTestCase, self).setUp()
        vol_tmpdir = tempfile.mkdtemp()
        self.flags(volume_driver=self.driver_name,
                   volumes_dir=vol_tmpdir)
        self.volume = importutils.import_object(FLAGS.volume_manager)
        self.context = context.get_admin_context()
        self.output = ""
        self.stubs.Set(iscsi.TgtAdm, '_get_target', self.fake_get_target)

        def _fake_execute(_command, *_args, **_kwargs):
            """Fake _execute."""
            return self.output, None
        self.volume.get_driver().set_execute(_fake_execute)

    def tearDown(self):
        try:
            shutil.rmtree(FLAGS.volumes_dir)
        except OSError:
            pass
        super(DriverTestCase, self).tearDown()

    def fake_get_target(obj, iqn):
        return 1

    def _attach_volume(self):
        """Attach volumes to an instance. """
        return []

    def _detach_volume(self, volume_id_list):
        """Detach volumes from an instance."""
        for volume_id in volume_id_list:
            db.volume_detached(self.context, volume_id)
            self.volume.delete_volume(self.context, volume_id)


class VolumeDriverTestCase(DriverTestCase):
    """Test case for VolumeDriver"""
    driver_name = "cinder.volume.driver.VolumeDriver"

    def test_delete_busy_volume(self):
        """Test deleting a busy volume."""
        self.stubs.Set(self.volume.get_driver(), '_volume_not_present',
                       lambda x: False)
        self.stubs.Set(self.volume.get_driver(), '_delete_volume',
                       lambda x, y: False)
        # Want DriverTestCase._fake_execute to return 'o' so that
        # volume.driver.delete_volume() raises the VolumeIsBusy exception.
        self.output = 'o'
        self.assertRaises(exception.VolumeIsBusy,
                          self.volume.get_driver().delete_volume,
                          {'name': 'test1', 'size': 1024})
        # when DriverTestCase._fake_execute returns something other than
        # 'o' volume.driver.delete_volume() does not raise an exception.
        self.output = 'x'
        self.volume.get_driver().delete_volume({'name': 'test1', 'size': 1024})


class ISCSITestCase(DriverTestCase):
    """Test Case for ISCSIDriver"""
    driver_name = "cinder.volume.driver.ISCSIDriver"

    def _attach_volume(self):
        """Attach volumes to an instance. """
        volume_id_list = []
        for index in xrange(3):
            vol = {}
            vol['size'] = 0
            vol_ref = db.volume_create(self.context, vol)
            self.volume.create_volume(self.context, vol_ref['id'])
            vol_ref = db.volume_get(self.context, vol_ref['id'])

            # each volume has a different mountpoint
            mountpoint = "/dev/sd" + chr((ord('b') + index))
            instance_uuid = '12345678-1234-5678-1234-567812345678'
            db.volume_attached(self.context, vol_ref['id'], instance_uuid,
                               mountpoint)
            volume_id_list.append(vol_ref['id'])

        return volume_id_list


class VolumePolicyTestCase(test.TestCase):

    def setUp(self):
        super(VolumePolicyTestCase, self).setUp()

        cinder.policy.reset()
        cinder.policy.init()

        self.context = context.get_admin_context()

    def tearDown(self):
        super(VolumePolicyTestCase, self).tearDown()
        cinder.policy.reset()

    def _set_rules(self, rules):
        cinder.common.policy.set_brain(cinder.common.policy.HttpBrain(rules))

    def test_check_policy(self):
        self.mox.StubOutWithMock(cinder.policy, 'enforce')
        target = {
            'project_id': self.context.project_id,
            'user_id': self.context.user_id,
        }
        cinder.policy.enforce(self.context, 'volume:attach', target)
        self.mox.ReplayAll()
        cinder.volume.api.check_policy(self.context, 'attach')

    def test_check_policy_with_target(self):
        self.mox.StubOutWithMock(cinder.policy, 'enforce')
        target = {
            'project_id': self.context.project_id,
            'user_id': self.context.user_id,
            'id': 2,
        }
        cinder.policy.enforce(self.context, 'volume:attach', target)
        self.mox.ReplayAll()
        cinder.volume.api.check_policy(self.context, 'attach', {'id': 2})
