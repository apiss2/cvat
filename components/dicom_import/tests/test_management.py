# SPDX-License-Identifier: MIT
import json
from types import SimpleNamespace

import pytest

from components.extensions.catalog import register


def manager(root):
    return SimpleNamespace(ROOT=root, EXTENSION_COMPOSE={'existing': ()},
        EXTENSION_PLUGINS={'existing': 'plugins/existing'}, COMPOSE_FILES=('base.yml',),
        OperationError=ValueError)


def descriptor(root, **changes):
    folder = root / 'components/dicom_import'
    folder.mkdir(parents=True)
    (root / 'cvat-ui/plugins/dicom-import/src/ts').mkdir(parents=True)
    (folder / 'docker-compose.dicom.yml').write_text('services: {}')
    value = {'name': 'dicom_import', 'client_plugin': 'plugins/dicom-import',
             'compose_files': ['components/dicom_import/docker-compose.dicom.yml']}
    value.update(changes)
    (folder / 'extension.json').write_text(json.dumps(value))


def test_register_preserves_existing_and_adds_optional_catalog(tmp_path):
    host = manager(tmp_path)
    descriptor(tmp_path)
    register(host)
    assert host.EXTENSION_COMPOSE['existing'] == ()
    assert host.EXTENSION_PLUGINS['dicom_import'] == 'plugins/dicom-import'
    assert host.COMPOSE_FILES == ('base.yml', 'components/dicom_import/docker-compose.dicom.yml')


def test_no_descriptors_leaves_catalog_unchanged(tmp_path):
    host = manager(tmp_path)
    register(host)
    assert host.EXTENSION_COMPOSE == {'existing': ()}


@pytest.mark.parametrize('changes', [
    {'name': 'existing'}, {'name': '../bad'}, {'client_plugin': '../bad'},
    {'compose_files': ['../secret']}, {'compose_files': ['/absolute']},
    {'compose_files': ['missing.yml']}, {'unexpected': True},
])
def test_invalid_descriptor_refused(tmp_path, changes):
    descriptor(tmp_path, **changes)
    with pytest.raises(ValueError):
        register(manager(tmp_path))
