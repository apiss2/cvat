# SPDX-License-Identifier: MIT
"""Expose the format in the catalogue, only for the independent project endpoint."""

from cvat.apps.dataset_manager.bindings import CvatImportError
from cvat.apps.dataset_manager.formats.registry import importer

from . import enabled


@importer(name="Annotation Tags DICOM Import", version="1.0", ext="ZIP", enabled=enabled())
def import_annotation_tags(source, temp_dir, instance_data, load_data_callback=None, **options):
    # The project service prepares all tasks first, then commits each task separately.
    # Calling this through dataset/task import must never clear or create anything.
    raise CvatImportError("Use Project > Import annotations for this format")
