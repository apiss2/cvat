# SPDX-License-Identifier: MIT
"""Optional imports into existing project tasks, without schema changes."""

DICOM_FORMAT = "Annotation Tags DICOM Import 1.0"
PROTECTED_LABEL = "DicomTag"
ANNOTATION_KEYS = ("tags", "shapes", "tracks", "intervals")


class ProjectAnnotationImportError(ValueError):
    """A public, identifier-free import error."""


def enabled():
    import os

    return os.environ.get("CVAT_PROJECT_ANNOTATIONS_ENABLED") == "1"


def supported_format(name):
    return name != "DICOM 1.0" and not name.startswith("Task Guide ")
