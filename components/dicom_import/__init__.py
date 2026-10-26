# SPDX-License-Identifier: MIT


class DicomImportError(ValueError):
    """An input cannot be imported without ambiguity or silent data loss."""
