# SPDX-License-Identifier: MIT
"""Regression coverage for restoring without writing into the original storage."""

import io
import pytest
from contextlib import redirect_stdout

from test_backup import Fixture, backup


class TestRestoreIsolation(Fixture):
    def assert_rejected_without_writes(self, target):
        with pytest.raises(backup.BackupError):
            self.repo.restore("latest", target)
        assert not target.exists()
        assert 'restore' not in self.events

    def test_refuses_child_of_original_named_volume(self):
        self.create()
        self.assert_rejected_without_writes(self.data / "restore")

    def test_refuses_child_of_original_bind(self):
        storage = self.base / "models"
        storage.mkdir()
        self.model["services"]["cvat_server"]["volumes"].append(
            {"type": "bind", "source": str(storage), "target": "/models"}
        )
        self.write_saved()
        self.create()
        self.assert_rejected_without_writes(storage / "restore")

    def test_resolves_symlink_to_original_storage(self):
        self.create()
        alias = self.base / "storage-alias"
        alias.symlink_to(self.data, target_is_directory=True)
        self.assert_rejected_without_writes(alias / "restore")

    def test_unreadable_manifest_does_not_create_target(self):
        self.create()
        self.fail = "dump"
        self.assert_rejected_without_writes(self.base / "restore")

    @pytest.mark.parametrize(
        "manifest",
        [
            None,
            [],
            {},
            {"format": 2, "sources": []},
            {"format": 1, "sources": []},
            {"format": 1, "sources": [None]},
            {"format": 1, "sources": [{"type": "volume", "source": "volume"}]},
            {"format": 1, "sources": [{"type": "bind", "source": "relative"}]},
        ],
    )
    def test_invalid_manifest_does_not_create_target(self, manifest):
        self.create()
        self.manifest = manifest
        self.assert_rejected_without_writes(self.base / "restore")

    def test_recovery_does_not_require_current_deployment(self):
        self.create()
        (self.state / "active.json").unlink()
        (self.state / "active.compose.json").unlink()
        target = self.base / "restore"
        with redirect_stdout(io.StringIO()):
            self.repo.restore("latest", target)
        assert target.is_dir()
        assert '--verify' in self.calls[-1]
        assert 'dump' in self.events
