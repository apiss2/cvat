# SPDX-License-Identifier: MIT
import os

from cvat.apps.dataset_manager.bindings import CvatImportError, ProjectData
from cvat.apps.dataset_manager.formats.registry import importer

from . import DicomImportError


@importer(name='DICOM', version='1.0', ext='ZIP',
          enabled=os.environ.get('CVAT_DICOM_IMPORT_ENABLED') == '1')
def import_dicom(source, temp_dir, instance_data, load_data_callback=None, **options):
    if not isinstance(instance_data, ProjectData):
        raise CvatImportError('DICOM import is available for projects only')
    from .service import import_project
    try:
        import_project(source, temp_dir, instance_data.db_project)
    except DicomImportError as exc:
        raise CvatImportError(str(exc)) from exc
