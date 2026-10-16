# SPDX-License-Identifier: MIT
"""Unit tests require Python only; no Docker daemon or production data is used."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "backup.py"
spec = importlib.util.spec_from_file_location("cvat_backup", MODULE)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
cli_spec = importlib.util.spec_from_file_location("cvat_cli", MODULE.with_name("cli.py"))
cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        backup.private_directory(self.state)
        self.data = self.base / "data"
        self.data.mkdir()
        self.repository = self.base / "repository"
        self.repository.mkdir(mode=0o700)
        self.password = self.state / "password"
        self.password.write_text("test-only-password\n")
        self.password.chmod(0o600)
        backup.save_json(self.state / "backup.json", {
            "repository": str(self.repository), "password_file": str(self.password), "version": 1,
        })
        self.calls = []
        self.completed = []
        self.fail = None
        self.events = []
        self.repo = backup.Repository(self.state, self.restic)
        self.env_file = self.base / "config.env"
        self.env_file.write_text("CVAT_EXTENSIONS=\n")
        self.env_file.chmod(0o600)
        self.identity = {"daemon": "test", "project": "test", "network": "test", "namespace": "test"}
        self.model = {
            "services": {
                "cvat_server": {"environment": {
                    "CVAT_POSTGRES_HOST": "cvat_db", "CVAT_REDIS_INMEM_HOST": "cvat_redis_inmem",
                    "CVAT_REDIS_ONDISK_HOST": "cvat_redis_ondisk",
                }, "volumes": [{"type": "volume", "source": "data"}]},
                "cvat_db": {}, "cvat_redis_inmem": {}, "cvat_redis_ondisk": {},
            }, "volumes": {"data": {"name": "test_data"}},
        }
        self.saved = {"model": self.model, "identity": self.identity, "extensions": []}
        self.write_saved()
        fixture = self

        class Manager:
            state = fixture.state
            root = fixture.base
            env_file = fixture.env_file
            project = "test"
            running = False
            foreign = []
            down_calls = 0

            def assert_identity(self, saved):
                if saved["identity"] != fixture.identity:
                    raise backup.BackupError("Wrong identity")

            def functions(self):
                return []

            def registry_workers(self, model):
                return []

            def down(self):
                fixture.events.append("stop")
                self.down_calls += 1
                self.running = False

            def run(self, args):
                fixture.calls.append(args)
                if args[:3] == ["docker", "volume", "inspect"]:
                    return json.dumps([{"Driver": "local", "Options": None, "Mountpoint": str(fixture.data)}])
                if args[:3] == ["docker", "ps", "-q"]:
                    return "live" if self.running or self.foreign else ""
                if args[:2] == ["docker", "inspect"]:
                    return json.dumps(self.foreign or [{"Id": "live", "Config": {"Labels": {
                        "com.docker.compose.project": self.project}}, "Mounts": []}])
                if args[-2:] == ["rev-parse", "HEAD"]:
                    return "a" * 40
                return ""

        self.manager = Manager()

    def write_saved(self):
        backup.save_json(self.state / "active.json", self.saved)
        backup.save_json(self.state / "active.compose.json", self.model)

    def restic(self, args):
        self.calls.append(args)
        index = args.index(backup.IMAGE) + 1
        args = args[index:]
        operation = next((x for x in ("snapshots", "backup", "tag", "restore", "version", "check", "init") if x in args), "?")
        self.events.append(operation)
        if operation == self.fail:
            raise backup.BackupError("Injected command failure")
        if operation == "snapshots":
            return json.dumps(self.completed)
        if operation == "backup":
            self.tags = [args[n + 1] for n, x in enumerate(args) if x == "--tag"]
            return json.dumps({"message_type": "summary", "snapshot_id": "12345678", "data_added": 12})
        if operation == "tag":
            self.completed.append({"id": "abcdef01" * 8, "time": "2026-01-01T00:00:00Z",
                                   "tags": [backup.COMPLETE, *[x for x in self.tags if x != backup.PENDING]]})
        return ""

    def create(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            backup.create_backup(self.repo, self.manager, **kwargs)


class SafetyTests(Fixture):
    def test_no_live_backup(self):
        self.manager.running = True
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assertNotIn("backup", self.events)

    def test_stop_after_authentication_and_mount_preflight(self):
        self.manager.running = True
        self.create(stop=True)
        self.assertEqual(self.events[:4], ["snapshots", "version", "stop", "backup"])
        self.assertFalse(self.manager.running)
        self.assertEqual(self.manager.down_calls, 1)

    def test_bad_repository_does_not_stop_services(self):
        self.fail = "snapshots"
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertEqual(self.manager.down_calls, 0)

    def test_bad_mount_does_not_stop_services(self):
        self.fail = "version"
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertEqual(self.manager.down_calls, 0)

    def test_failed_snapshot_is_not_promoted(self):
        self.fail = "backup"
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertNotIn("tag", self.events)
        self.assertEqual(self.completed, [])
        self.assertEqual(list(self.state.glob("backup-metadata-*")), [])

    def test_failed_tag_does_not_report_success(self):
        self.fail = "tag"
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assertEqual(self.completed, [])

    def test_previous_completed_snapshot_is_explicit_parent(self):
        self.completed = [{"id": "98765432", "time": "2025", "tags": [backup.COMPLETE]}]
        self.create()
        command = next(x for x in self.calls if "backup" in x and "--parent" in x)
        self.assertEqual(command[command.index("--parent") + 1], "98765432")
        self.assertIn("host,paths,tags", command)

    def test_incomplete_snapshots_are_not_restore_candidates(self):
        self.completed = [{"id": "badbad01", "time": "2025", "tags": [backup.PENDING]}]
        self.assertEqual(self.repo.snapshots(), [])
        with self.assertRaises(backup.BackupError):
            self.repo.select("latest")

    def test_ambiguous_snapshot_is_rejected(self):
        self.completed = [{"id": "abcdef01" + x, "time": "2025", "tags": [backup.COMPLETE]}
                          for x in ("0" * 56, "1" * 56)]
        with self.assertRaises(backup.BackupError):
            self.repo.select("abcdef01")

    def test_unrecognized_snapshot_reference_is_rejected(self):
        with self.assertRaises(backup.BackupError):
            self.repo.select("--latest")

    def test_runtime_lock_is_shared(self):
        with backup.deployment_lock(self.state):
            with self.assertRaises(backup.BackupError):
                with backup.deployment_lock(self.state):
                    self.fail("unexpected lock acquisition")

    def test_password_permission_is_checked(self):
        self.password.chmod(0o644)
        with self.assertRaises(backup.BackupError):
            self.repo.command("snapshots")

    def test_empty_password_is_rejected(self):
        self.password.write_text("")
        with self.assertRaises(backup.BackupError):
            self.repo.command("snapshots")

    def test_backup_storage_cannot_be_inside_source(self):
        with self.assertRaises(backup.BackupError):
            backup.validate_destinations(self.repo, [{"type": "bind", "source": str(self.base)}])

    def test_foreign_writer_is_rejected(self):
        self.manager.foreign = [{"Id": "foreign", "Mounts": [{"Type": "volume", "Name": "test_data", "RW": True}]}]
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assertNotIn("backup", self.events)

    def test_foreign_parent_bind_writer_is_rejected(self):
        self.manager.foreign = [{"Id": "foreign", "Mounts": [{"Type": "bind", "Source": str(self.base), "RW": True}]}]
        with self.assertRaises(backup.BackupError):
            self.create()

    def test_readonly_foreign_reader_is_allowed(self):
        self.manager.foreign = [{"Id": "reader", "Mounts": [{"Type": "volume", "Name": "test_data", "RW": False}]}]
        self.create()

    def test_mismatched_recorded_compose_is_rejected(self):
        backup.save_json(self.state / "active.compose.json", {})
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertEqual(self.manager.down_calls, 0)

    def test_no_saved_deployment_is_rejected(self):
        (self.state / "active.json").unlink()
        with self.assertRaises(backup.BackupError):
            self.create()

    def test_external_database_is_rejected(self):
        self.model["services"]["cvat_server"]["environment"]["CVAT_POSTGRES_HOST"] = "external"
        self.write_saved()
        with self.assertRaises(backup.BackupError):
            self.create(stop=True)
        self.assertEqual(self.manager.down_calls, 0)

    def test_remote_registry_is_rejected(self):
        self.saved["extensions"] = ["model_registry"]
        self.write_saved()
        with self.assertRaises(backup.BackupError):
            self.create()

    def test_missing_bind_source_is_rejected(self):
        self.model["services"]["cvat_server"]["volumes"].append({"type": "bind", "source": str(self.base / "missing")})
        self.write_saved()
        with self.assertRaises(FileNotFoundError):
            self.create(stop=True)
        self.assertEqual(self.manager.down_calls, 0)

    def test_duplicate_and_nested_binds_are_collapsed(self):
        (self.data / "nested").mkdir()
        self.model["services"]["cvat_server"]["volumes"] += [
            {"type": "bind", "source": str(x)} for x in (self.data, self.data, self.data / "nested")]
        entries, _ = backup.sources(self.manager, self.saved)
        self.assertEqual(sum(x["type"] == "bind" for x in entries), 1)

    def test_anonymous_volumes_are_rejected(self):
        self.model["services"]["cvat_db"]["volumes"] = [{"type": "volume"}]
        with self.assertRaises(backup.BackupError):
            backup.sources(self.manager, self.saved)

    def test_backed_up_sources_are_readonly_and_never_copied_into_volumes(self):
        self.create()
        command = next(x for x in self.calls if "backup" in x)
        volumes = [x for x in command if "type=volume" in x]
        self.assertTrue(all("readonly" in x and "volume-nocopy" in x for x in volumes))
        self.assertIn("none", command)
        self.assertNotIn("test-only-password", " ".join(command))
        self.assertNotIn("--privileged", command)

    def test_plan_does_not_stop_or_create_snapshot(self):
        self.create(plan=True)
        self.assertEqual(self.manager.down_calls, 0)
        self.assertEqual(self.events, [])

    def test_restore_uses_only_new_target_and_verifies(self):
        self.create()
        target = self.base / "restore"
        with redirect_stdout(io.StringIO()):
            self.repo.restore("latest", target)
        command = self.calls[-1]
        self.assertIn("--verify", command)
        self.assertFalse(any("type=volume" in x for x in command))
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_restore_refuses_existing_even_empty_target(self):
        self.create()
        with self.assertRaises(FileExistsError):
            self.repo.restore("latest", self.data)

    def test_restore_refuses_repository_overlap(self):
        self.create()
        with self.assertRaises(backup.BackupError):
            self.repo.restore("latest", self.repository / "restore")

    def test_mount_paths_support_spaces_and_reject_commas(self):
        self.assertIn("source=/data/my files", backup.mount("bind", "/data/my files", "/source")[1])
        with self.assertRaises(backup.BackupError):
            backup.mount("bind", "/data/,readonly=false", "/source")

    def test_metadata_permissions_are_preserved(self):
        original = self.repo.run
        def inspect(args):
            if "backup" in args:
                binding = next(x for x in args if "target=/source/metadata" in x)
                source = Path(binding.split("source=", 1)[1].split(",target=", 1)[0])
                self.assertEqual((source / "environment.env").stat().st_mode & 0o777, 0o600)
                self.assertFalse((source / "password").exists())
                manifest = json.loads((source / "manifest.json").read_text())
                self.assertEqual(manifest["strategy"], "cold-filesystem")
            return original(args)
        self.repo.run = inspect
        self.create()

    def test_runtime_mount_drift_is_rejected(self):
        containers = [{"Config": {"Labels": {"com.docker.compose.service": "cvat_db"}},
                       "Mounts": [{"Type": "volume", "Name": "other_data"}]}]
        with self.assertRaises(backup.BackupError):
            backup.sources(self.manager, self.saved, containers)

    def test_recorded_runtime_mount_is_accepted(self):
        containers = [{"Config": {"Labels": {"com.docker.compose.service": "cvat_server"}},
                       "Mounts": [{"Type": "volume", "Name": "test_data"}]}]
        entries, _ = backup.sources(self.manager, self.saved, containers)
        self.assertEqual(entries[0]["source"], "test_data")


    def test_initialize_generates_private_password_and_settings(self):
        self.repo.values = {}
        self.repo.config.unlink()
        password = self.state / "generated-password"
        with redirect_stdout(io.StringIO()):
            self.repo.initialize(self.repository, password)
        self.assertEqual(password.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(password.read_text().strip()), 64)
        self.assertEqual(self.repo.config.stat().st_mode & 0o777, 0o600)
        self.assertIn("init", self.events)

    def test_existing_repository_requires_original_password(self):
        self.repo.values = {}
        self.repo.config.unlink()
        (self.repository / "config").write_text("existing")
        with self.assertRaises(backup.BackupError):
            self.repo.initialize(self.repository, self.state / "missing-password")
        self.assertFalse(self.repo.config.exists())

    def test_existing_repository_can_be_attached_without_reinitializing(self):
        self.repo.values = {}
        self.repo.config.unlink()
        (self.repository / "config").write_text("existing")
        with redirect_stdout(io.StringIO()):
            self.repo.initialize(self.repository, self.password)
        self.assertIn("snapshots", self.events)
        self.assertNotIn("init", self.events)

    def test_unknown_nonempty_repository_is_rejected(self):
        self.repo.values = {}
        self.repo.config.unlink()
        (self.repository / "keep-me").write_text("content")
        with self.assertRaises(backup.BackupError):
            self.repo.initialize(self.repository, self.password)
        self.assertEqual((self.repository / "keep-me").read_text(), "content")

    def test_init_failure_does_not_publish_configuration(self):
        self.repo.values = {}
        self.repo.config.unlink()
        self.fail = "init"
        with self.assertRaises(backup.BackupError):
            self.repo.initialize(self.repository, self.password)
        self.assertFalse(self.repo.config.exists())

    def test_settings_cannot_be_overwritten_by_init(self):
        with self.assertRaises(backup.BackupError):
            self.repo.initialize(self.repository, self.password)

    def test_writer_appearing_after_backup_prevents_promotion(self):
        original = self.repo.run
        def start_writer(args):
            result = original(args)
            if "backup" in args:
                self.manager.running = True
            return result
        self.repo.run = start_writer
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assertNotIn("tag", self.events)

    def test_remote_docker_context_is_rejected(self):
        self.repo.run = lambda args: json.dumps([{"Endpoints": {"docker": {"Host": "ssh://host"}}}])
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(backup.BackupError):
            self.repo.docker_check()

    def test_cached_helper_does_not_require_a_pull(self):
        commands = []
        def cached(args):
            commands.append(args)
            if args[1:3] == ["context", "inspect"]:
                return json.dumps([{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}])
            if args[1] == "info":
                return "linux"
            return "sha256:cached"
        self.repo.run = cached
        with patch.dict(os.environ, {}, clear=True):
            self.repo.docker_check()
        self.assertFalse(any("pull" in x for x in commands))


class CliTests(unittest.TestCase):
    def test_help_does_not_need_manager_or_docker(self):
        result = subprocess.run([sys.executable, str(MODULE), "backup", "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--stop", result.stdout)

    def test_existing_commands_are_delegated_without_reparsing(self):
        args = ["--env-file", "/tmp/backup", "--no-build", "up"]
        with patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit):
                cli.main(args)
        self.assertEqual(execute.call_args.args[1][2:], args)
        self.assertTrue(execute.call_args.args[1][1].endswith("manage.py"))


    def test_backup_command_is_routed_with_global_options(self):
        args = ["--state-dir", "/tmp/state", "backup", "--help"]
        with patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit):
                cli.main(args)
        self.assertEqual(execute.call_args.args[1][2:], args)
        self.assertTrue(execute.call_args.args[1][1].endswith("backup.py"))

    def test_backup_options_work_before_and_after_command(self):
        for args in (["--env-file", "/tmp/env", "backup", "create", "--stop"],
                     ["backup", "create", "--env-file", "/tmp/env", "--stop"]):
            parsed = backup.parser().parse_args(args)
            self.assertEqual(parsed.env_file, Path("/tmp/env"))
            self.assertTrue(parsed.stop)

    def test_create_cannot_silently_ignore_restore_option(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as result:
                backup.main(["backup", "create", "--target", "/tmp/test"])
        self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
