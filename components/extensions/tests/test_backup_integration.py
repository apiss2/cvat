# SPDX-License-Identifier: MIT
"""Exercise the real backup/manager/catalog integration without a Docker daemon."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from components.extensions import backup, manage


class ExtensionBackupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.env_file = self.base / "deployment.env"
        self.repository = self.base / "repository"
        self.repository.mkdir(mode=0o700)
        self.password = self.state / "password"
        self.password.write_text("test-password\n")
        self.password.chmod(0o600)
        backup.save_json(
            self.state / "backup.json",
            {
                "version": 1,
                "repository": str(self.repository),
                "password_file": str(self.password),
            },
        )
        self.identity = {
            "daemon": "test-daemon",
            "project": "backup-test",
            "network": "backup-test_cvat",
            "namespace": "nuclio",
        }
        self.model = {
            "services": {
                "cvat_server": {
                    "environment": {
                        "CVAT_POSTGRES_HOST": "cvat_db",
                        "CVAT_REDIS_INMEM_HOST": "cvat_redis_inmem",
                        "CVAT_REDIS_ONDISK_HOST": "cvat_redis_ondisk",
                        "CVAT_DICOM_IMPORT_ENABLED": "1",
                    },
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "cvat_data",
                            "target": "/home/django/data",
                        }
                    ],
                },
                "cvat_db": {
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "cvat_db",
                            "target": "/var/lib/postgresql/data",
                        }
                    ],
                },
                "cvat_redis_inmem": {},
                "cvat_redis_ondisk": {},
            },
            "volumes": {
                "cvat_data": {"name": "backup-test_cvat_data"},
                "cvat_db": {"name": "backup-test_cvat_db"},
            },
        }
        self.saved = {
            "identity": self.identity,
            "model": self.model,
            "extensions": ["dicom_import", "physical_measurements", "task_context"],
        }
        backup.save_json(self.state / "active.json", self.saved)
        backup.save_json(self.state / "active.compose.json", self.model)
        # Registration mutates module dictionaries. Start without the descriptor
        # and restore the previous process state, even when another test registered it.
        self.stack.enter_context(
            patch.dict(
                manage.EXTENSION_COMPOSE,
                {
                    key: value
                    for key, value in manage.EXTENSION_COMPOSE.items()
                    if key != "dicom_import"
                },
                clear=True,
            )
        )
        self.stack.enter_context(
            patch.dict(
                manage.EXTENSION_PLUGINS,
                {
                    key: value
                    for key, value in manage.EXTENSION_PLUGINS.items()
                    if key != "dicom_import"
                },
                clear=True,
            )
        )
        self.stack.enter_context(
            patch.object(
                manage,
                "COMPOSE_FILES",
                tuple(
                    path
                    for path in manage.COMPOSE_FILES
                    if path != "components/dicom_import/docker-compose.dicom.yml"
                ),
            )
        )
        self.stack.enter_context(patch.dict("os.environ", {}, clear=True))
        self.calls = []
        self.snapshots = []
        self.manifest = None
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=self.run_process))

    def run_process(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["docker", "context", "inspect"]:
            output = json.dumps([{"Endpoints": {"docker": {"Host": "unix:///test/docker.sock"}}}])
        elif args[:3] == ["docker", "info", "--format"]:
            output = "linux" if args[3] == "{{.OSType}}" else "test-daemon"
        elif args[:3] == ["docker", "image", "ls"]:
            output = "sha256:available"
        elif args[:3] == ["docker", "volume", "inspect"]:
            output = json.dumps(
                [
                    {
                        "Driver": "local",
                        "Options": None,
                        "Mountpoint": str(self.base / "volumes" / args[-1]),
                    }
                ]
            )
        elif args[:2] == ["docker", "ps"]:
            output = ""
        elif args[:2] == ["docker", "run"]:
            output = self.run_restic(args)
        elif args[:2] == ["git", "-C"]:
            output = "a" * 40 if args[-2:] == ["rev-parse", "HEAD"] else ""
        else:
            self.fail(f"Unexpected external command: {args}")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def run_restic(self, args):
        restic = args[args.index(backup.IMAGE) + 1 :]
        if "snapshots" in restic:
            return json.dumps(self.snapshots)
        if "version" in restic:
            return "restic test"
        if "backup" in restic:
            self.tags = [
                restic[index + 1] for index, value in enumerate(restic) if value == "--tag"
            ]
            metadata = next(value for value in args if "target=/source/metadata" in value)
            source = next(
                value.removeprefix("source=")
                for value in metadata.split(",")
                if value.startswith("source=")
            )
            self.manifest = json.loads((Path(source) / "manifest.json").read_text())
            self.assertEqual(json.loads((Path(source) / "active.json").read_text()), self.saved)
            return json.dumps({"message_type": "summary", "snapshot_id": "12345678"})
        if "tag" in restic:
            self.snapshots = [
                {
                    "id": "abcdef01" * 8,
                    "time": "2026-01-01T00:00:00Z",
                    "tags": [backup.COMPLETE, *[tag for tag in self.tags if tag != backup.PENDING]],
                }
            ]
            return ""
        self.fail(f"Unexpected restic command: {restic}")

    def exercise(self, action, *, dicom_enabled):
        selected = ["physical_measurements", "task_context"]
        if dicom_enabled:
            selected.append("dicom_import")
        self.env_file.write_text(
            f"CVAT_EXTENSIONS={','.join(selected)}\nCOMPOSE_PROJECT_NAME=backup-test\n"
        )
        self.env_file.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            backup.main(
                [
                    "--env-file",
                    str(self.env_file),
                    "--state-dir",
                    str(self.state),
                    "backup",
                    action,
                ]
            )
        manager = manage.Manager(manage.ROOT, self.env_file, self.state)
        self.assertEqual(manager.extensions, tuple(selected))
        self.assertEqual(
            "plugins/dicom-import" in manager.values["CVAT_CLIENT_PLUGINS"].split(":"),
            dicom_enabled,
        )
        self.assertFalse(set(manager.extensions) & manage.SERVERLESS_EXTENSIONS)
        if action == "plan":
            sources = json.loads(output.getvalue())["sources"]
            self.assertFalse(any(command[:2] == ["docker", "run"] for command in self.calls))
        else:
            self.assertIsNotNone(self.manifest)
            sources = self.manifest["sources"]
            self.assertEqual(len(self.snapshots), 1)
            self.assertIn(self.snapshots[0]["id"], output.getvalue())
        self.assertEqual(
            {source["source"] for source in sources},
            {"backup-test_cvat_data", "backup-test_cvat_db"},
        )
        self.assertEqual(json.loads((self.state / "active.json").read_text()), self.saved)

    def test_plan_with_all_three_extensions(self):
        self.exercise("plan", dicom_enabled=True)

    def test_create_with_all_three_extensions(self):
        self.exercise("create", dicom_enabled=True)

    def test_plan_after_disabling_dicom_preserves_recorded_sources(self):
        self.exercise("plan", dicom_enabled=False)

    def test_create_after_disabling_dicom_preserves_recorded_sources(self):
        self.exercise("create", dicom_enabled=False)


if __name__ == "__main__":
    unittest.main()
