# SPDX-License-Identifier: MIT
"""Keep task-guide text on disk while all import records are validated."""

from dataclasses import dataclass
from tempfile import TemporaryFile

_READ_SIZE = 64 * 1024


@dataclass(frozen=True)
class TextReference:
    offset: int
    byte_length: int


class TextStore:
    """Store UTF-8 text in one private temporary file for an import."""

    def __init__(self, *, dir=None):
        self._file = TemporaryFile(mode="w+b", dir=dir)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._file.close()

    def write(self, text: str) -> TextReference:
        encoded = text.encode("utf-8")
        self._file.seek(0, 2)
        reference = TextReference(self._file.tell(), len(encoded))
        self._file.write(encoded)
        return reference

    def read(self, reference: TextReference) -> str:
        self._file.seek(reference.offset)
        encoded = self._file.read(reference.byte_length)
        if len(encoded) != reference.byte_length:
            raise OSError("Incomplete staged task-guide text")
        return encoded.decode("utf-8")

    def matches(self, reference: TextReference, text: str) -> bool:
        """Compare every byte, without materializing the stored text again."""
        encoded = text.encode("utf-8")
        if len(encoded) != reference.byte_length:
            return False
        self._file.seek(reference.offset)
        for offset in range(0, len(encoded), _READ_SIZE):
            expected = encoded[offset : offset + _READ_SIZE]
            if self._file.read(len(expected)) != expected:
                return False
        return True
