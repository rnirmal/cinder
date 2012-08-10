#    Copyright 2012 OpenStack LLC
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

from sqlalchemy import Index, MetaData, Table, Column, DateTime, Integer
from sqlalchemy import String, Boolean
from migrate import ForeignKeyConstraint

from cinder.openstack.common import log as logging

LOG = logging.getLogger(__name__)


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine
    dialect = migrate_engine.url.get_dialect().name

    # Create a new table volume_backends
    volume_backends = Table('volume_backends', meta,
                            Column('created_at', DateTime),
                            Column('updated_at', DateTime),
                            Column('deleted_at', DateTime),
                            Column('deleted', Boolean),
                            Column('id', Integer, primary_key=True,
                                   nullable=False),
                            Column('name', String(length=255,
                                                  convert_unicode=False,
                                                  assert_unicode=None,
                                                  unicode_error=None,
                                                  _warn_on_bytestring=False),
                                   nullable=False),
                            Column('description', String(length=255)),
                            mysql_engine='InnoDB',
                            )
    volume_backends.create()
    Index('name', volume_backends.c.name, unique=True).create(migrate_engine)

    # Load the volumes table to add the extra column 'backend_id'
    volumes = Table('volumes', meta, autoload=True)
    backend_id = Column('backend_id', Integer)
    volumes.create_column(backend_id)

    try:
        if not dialect.startswith('sqlite'):
            ForeignKeyConstraint(columns=[volumes.c.backend_id],
                                 refcolumns=[volume_backends.c.id]).create()
    except Exception:
        LOG.error(_("foreign key constraint couldn't be added"))
        raise


def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine
    dialect = migrate_engine.url.get_dialect().name

    # Drop the foreign key backend_id in the volumes table
    volume_backends = Table('volume_backends', meta, autoload=True)
    volumes = Table('volumes', meta, autoload=True)
    try:
        if not dialect.startswith('sqlite'):
            ForeignKeyConstraint(columns=[volumes.c.backend_id],
                                 refcolumns=[volume_backends.c.id]).drop()
    except Exception:
        LOG.error(_("foreign key constraint couldn't be removed"))
        raise
    volumes.drop_column('backend_id')

    # Drop the volume_backends table
    volume_backends.drop()
