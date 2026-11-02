# SPDX-License-Identifier: MIT
"""Pytest unit tests; no Docker daemon or production data is used."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import pytest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "backup.py"
spec = importlib.util.spec_from_file_location("cvat_backup", MODULE)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
cli_spec = importlib.util.spec_from_file_location("cvat_cli", MODULE.with_name("cli.py"))
cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli)


class Fixture:
    @pytest.fixture(autouse=True)
    def backup_state(self, tmp_path):
        self.base = tmp_path
        self.state = self.base / "state"
        backup.private_directory(self.state)
        self.data = self.base / "data"
        self.data.mkdir()
        self.repository = self.base / "repository"
        self.repository.mkdir(mode=0o700)
        self.password = self.state / "password"
        self.password.write_text("test-only-password\n")
        self.password.chmod(0o600)
        backup.save_json(
            self.state / "backup.json",
            {
                "repository": str(self.repository),
                "password_file": str(self.password),
                "version": 1,
            },
        )
        self.calls = []
        self.completed = []
        self.fail = None
        self.events = []
        self.repo = backup.Repository(self.state, self.restic)
        self.env_file = self.base / "config.env"
        self.env_file.write_text("CVAT_EXTENSIONS=\n")
        self.env_file.chmod(0o600)
        self.identity = {
            "daemon": "test",
            "project": "test",
            "network": "test",
            "namespace": "test",
        }
        self.model = {
            "services": {
                "cvat_server": {
                    "environment": {
                        "CVAT_POSTGRES_HOST": "cvat_db",
                        "CVAT_REDIS_INMEM_HOST": "cvat_redis_inmem",
                        "CVAT_REDIS_ONDISK_HOST": "cvat_redis_ondisk",
                    },
                    "volumes": [
                        {"type": "volume", "source": "data", "target": "/home/django/data"}
                    ],
                },
                "cvat_db": {},
                "cvat_redis_inmem": {},
                "cvat_redis_ondisk": {},
            },
            "volumes": {"data": {"name": "test_data"}},
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
                    return json.dumps(
                        [{"Driver": "local", "Options": None, "Mountpoint": str(fixture.data)}]
                    )
                if args[:3] == ["docker", "ps", "-q"]:
                    return "live" if self.running or self.foreign else ""
                if args[:2] == ["docker", "inspect"]:
                    return json.dumps(
                        self.foreign
                        or [
                            {
                                "Id": "live",
                                "Config": {"Labels": {"com.docker.compose.project": self.project}},
                                "Mounts": [],
                            }
                        ]
                    )
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
        operation = next(
            (
                x
                for x in (
                    "snapshots",
                    "backup",
                    "tag",
                    "restore",
                    "dump",
                    "version",
                    "check",
                    "init",
                )
                if x in args
            ),
            "?",
        )
        self.events.append(operation)
        if operation == self.fail:
            raise backup.BackupError("Injected command failure")
        if operation == "snapshots":
            return json.dumps(self.completed)
        if operation == "backup":
            binding = next(x for x in self.calls[-1] if "target=/source/metadata" in x)
            metadata = Path(binding.split("source=", 1)[1].split(",target=", 1)[0])
            self.manifest = json.loads((metadata / "manifest.json").read_text())
            self.tags = [args[n + 1] for n, x in enumerate(args) if x == "--tag"]
            return json.dumps(
                {"message_type": "summary", "snapshot_id": "12345678", "data_added": 12}
            )
        if operation == "dump":
            return json.dumps(self.manifest)
        if operation == "tag":
            self.completed.append(
                {
                    "id": "abcdef01" * 8,
                    "time": "2026-01-01T00:00:00Z",
                    "tags": [backup.COMPLETE, *[x for x in self.tags if x != backup.PENDING]],
                }
            )
        return ""

    def create(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            backup.create_backup(self.repo, self.manager, **kwargs)


class TestSafety(Fixture):
    def test_no_live_backup(self):
        self.manager.running = True
        with pytest.raises(backup.BackupError):
            self.create()
        assert 'backup' not in self.events

    def test_stop_after_authentication_and_mount_preflight(self):
        self.manager.running = True
        self.create(stop=True)
        assert self.events[:4] == ['snapshots', 'version', 'stop', 'backup']
        assert not self.manager.running
        assert self.manager.down_calls == 1

    def test_bad_repository_does_not_stop_services(self):
        self.fail = "snapshots"
        with pytest.raises(backup.BackupError):
            self.create(stop=True)
        assert self.manager.down_calls == 0

    def test_bad_mount_does_not_stop_services(self):
        self.fail = "version"
        with pytest.raises(backup.BackupError):
            self.create(stop=True)
        assert self.manager.down_calls == 0

    def test_failed_snapshot_is_not_promoted(self):
        self.fail = "backup"
        with pytest.raises(backup.BackupError):
            self.create(stop=True)
        assert 'tag' not in self.events
        assert self.completed == []
        assert list(self.state.glob('backup-metadata-*')) == []

    def test_failed_tag_does_not_report_success(self):
        self.fail = "tag"
        with pytest.raises(backup.BackupError):
            self.create()
        assert self.completed == []

    def test_previous_completed_snapshot_is_explicit_parent(self):
        self.completed = [{"id": "98765432", "time": "2025", "tags": [backup.COMPLETE]}]
        self.create()
        command = next(x for x in self.calls if "backup" in x and "--parent" in x)
        assert command[command.index('--parent') + 1] == '98765432'
        assert 'host,paths,tags' in command

    def test_incomplete_snapshots_are_not_restore_candidates(self):
        self.completed = [{"id": "badbad01", "time": "2025", "tags": [backup.PENDING]}]
        assert self.repo.snapshots() == []
        with pytest.raises(backup.BackupError):
            self.repo.select("latest")

    def test_ambiguous_snapshot_is_rejected(self):
        self.completed = [
            {"id": "abcdef01" + x, "time": "2025", "tags": [backup.COMPLETE]}
            for x in ("0" * 56, "1" * 56)
        ]
        with pytest.raises(backup.BackupError):
            self.repo.select("abcdef01")

    def test_unrecognized_snapshot_reference_is_rejected(self):
        with pytest.raises(backup.BackupError):
            self.repo.select("--latest")

    def test_runtime_lock_is_shared(self):
        with backup.deployment_lock(self.state):
            with pytest.raises(backup.BackupError):
                with backup.deployment_lock(self.state):
                    pytest.fail("unexpected lock acquisition")

    def test_password_permission_is_checked(self):
        self.password.chmod(0o644)
        with pytest.raises(backup.BackupError):
            self.repo.command("snapshots")

    def test_empty_password_is_rejected(self):
        self.password.write_text("")
        with pytest.raises(backup.BackupError):
            self.repo.command("snapshots")

    def test_backup_storage_cannot_be_inside_source(self):
        with pytest.raises(backup.BackupError):
            backup.validate_destinations(self.repo, [{"type": "bind", "source": str(self.base)}])

    def test_foreign_writer_is_rejected(self):
        self.manager.foreign = [
            {"Id": "foreign", "Mounts": [{"Type": "volume", "Name": "test_data", "RW": True}]}
        ]
        with pytest.raises(backup.BackupError):
            self.create()
        assert 'backup' not in self.events

    def test_foreign_parent_bind_writer_is_rejected(self):
        self.manager.foreign = [
            {"Id": "foreign", "Mounts": [{"Type": "bind", "Source": str(self.base), "RW": True}]}
        ]
        with pytest.raises(backup.BackupError):
            self.create()

    def test_readonly_foreign_reader_is_allowed(self):
        self.manager.foreign = [
            {"Id": "reader", "Mounts": [{"Type": "volume", "Name": "test_data", "RW": False}]}
        ]
        self.create()

    def test_mismatched_recorded_compose_is_rejected(self):
        backup.save_json(self.state / "active.compose.json", {})
        with pytest.raises(backup.BackupError):
            self.create(stop=True)
        assert self.manager.down_calls == 0

    def test_no_saved_deployment_is_rejected(self):
        (self.state / "active.json").unlink()
        with pytest.raises(backup.BackupError):
            self.create()

    def test_external_database_is_rejected(self):
        self.model["services"]["cvat_server"]["environment"]["CVAT_POSTGRES_HOST"] = "external"
        self.write_saved()
        with pytest.raises(backup.BackupError):
            self.create(stop=True)
        assert self.manager.down_calls == 0

    def test_remote_registry_is_rejected(self):
        self.saved["extensions"] = ["model_registry"]
        self.write_saved()
        with pytest.raises(backup.BackupError):
            self.create()

    def test_missing_bind_source_is_rejected(self):
        self.model["services"]["cvat_server"]["volumes"].append(
            {
                "type": "bind",
                "source": str(self.base / "missing"),
                "target": "/missing",
            }
        )
        self.write_saved()
        with pytest.raises(FileNotFoundError):
            self.create(stop=True)
        assert self.manager.down_calls == 0

    def test_duplicate_and_nested_binds_are_collapsed(self):
        (self.data / "nested").mkdir()
        self.model["services"]["cvat_server"]["volumes"] += [
            {"type": "bind", "source": str(x), "target": f"/bind/{index}"}
            for index, x in enumerate((self.data, self.data, self.data / "nested"))
        ]
        entries, _ = backup.sources(self.manager, self.saved)
        assert sum((x['type'] == 'bind' for x in entries)) == 1

    def test_anonymous_volumes_are_rejected(self):
        self.model["services"]["cvat_db"]["volumes"] = [
            {"type": "volume", "target": "/var/lib/postgresql/data"}
        ]
        with pytest.raises(backup.BackupError):
            backup.sources(self.manager, self.saved)

    def test_backed_up_sources_are_readonly_and_never_copied_into_volumes(self):
        self.create()
        command = next(x for x in self.calls if "backup" in x)
        volumes = [x for x in command if "type=volume" in x]
        assert all(('readonly' in x and 'volume-nocopy' in x for x in volumes))
        assert 'none' in command
        assert 'test-only-password' not in ' '.join(command)
        assert '--privileged' not in command

    def test_plan_does_not_stop_or_create_snapshot(self):
        self.create(plan=True)
        assert self.manager.down_calls == 0
        assert self.events == []

    def test_restore_uses_only_new_target_and_verifies(self):
        self.create()
        target = self.base / "restore"
        with redirect_stdout(io.StringIO()):
            self.repo.restore("latest", target)
        command = self.calls[-1]
        assert '--verify' in command
        assert not any(('type=volume' in x for x in command))
        assert target.stat().st_mode & 0o777 == 0o700

    def test_restore_refuses_existing_even_empty_target(self):
        self.create()
        with pytest.raises(FileExistsError):
            self.repo.restore("latest", self.data)

    def test_restore_refuses_repository_overlap(self):
        self.create()
        with pytest.raises(backup.BackupError):
            self.repo.restore("latest", self.repository / "restore")

    def test_mount_paths_support_spaces_and_reject_commas(self):
        assert 'source=/data/my files' in backup.mount('bind', '/data/my files', '/source')[1]
        with pytest.raises(backup.BackupError):
            backup.mount("bind", "/data/,readonly=false", "/source")

    def test_metadata_permissions_are_preserved(self):
        original = self.repo.run

        def inspect(args):
            if "backup" in args:
                binding = next(x for x in args if "target=/source/metadata" in x)
                source = Path(binding.split("source=", 1)[1].split(",target=", 1)[0])
                assert (source / 'environment.env').stat().st_mode & 0o777 == 0o600
                assert not (source / 'password').exists()
                manifest = json.loads((source / "manifest.json").read_text())
                assert manifest['strategy'] == 'cold-filesystem'
            return original(args)

        self.repo.run = inspect
        self.create()

    def test_runtime_mount_drift_is_rejected(self):
        containers = [
            {
                "Config": {"Labels": {"com.docker.compose.service": "cvat_db"}},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "other_data",
                        "Destination": "/var/lib/postgresql/data",
                    }
                ],
            }
        ]
        with pytest.raises(backup.BackupError):
            backup.sources(self.manager, self.saved, containers)

    def test_recorded_runtime_mount_is_accepted(self):
        containers = [
            {
                "Config": {"Labels": {"com.docker.compose.service": "cvat_server"}},
                "Mounts": [
                    {"Type": "volume", "Name": "test_data", "Destination": "/home/django/data"}
                ],
            }
        ]
        entries, _ = backup.sources(self.manager, self.saved, containers)
        assert entries[0]['source'] == 'test_data'

    def test_initialize_generates_private_password_and_settings(self):
        self.repo.values = {}
        self.repo.config.unlink()
        password = self.state / "generated-password"
        with redirect_stdout(io.StringIO()):
            self.repo.initialize(self.repository, password)
        assert password.stat().st_mode & 0o777 == 0o600
        assert len(password.read_text().strip()) == 64
        assert self.repo.config.stat().st_mode & 0o777 == 0o600
        assert 'init' in self.events

    def test_existing_repository_requires_original_password(self):
        self.repo.values = {}
        self.repo.config.unlink()
        (self.repository / "config").write_text("existing")
        with pytest.raises(backup.BackupError):
            self.repo.initialize(self.repository, self.state / "missing-password")
        assert not self.repo.config.exists()

    def test_existing_repository_can_be_attached_without_reinitializing(self):
        self.repo.values = {}
        self.repo.config.unlink()
        (self.repository / "config").write_text("existing")
        with redirect_stdout(io.StringIO()):
            self.repo.initialize(self.repository, self.password)
        assert 'snapshots' in self.events
        assert 'init' not in self.events

    def test_unknown_nonempty_repository_is_rejected(self):
        self.repo.values = {}
        self.repo.config.unlink()
        (self.repository / "keep-me").write_text("content")
        with pytest.raises(backup.BackupError):
            self.repo.initialize(self.repository, self.password)
        assert (self.repository / 'keep-me').read_text() == 'content'

    def test_init_failure_does_not_publish_configuration(self):
        self.repo.values = {}
        self.repo.config.unlink()
        self.fail = "init"
        with pytest.raises(backup.BackupError):
            self.repo.initialize(self.repository, self.password)
        assert not self.repo.config.exists()

    def test_settings_cannot_be_overwritten_by_init(self):
        with pytest.raises(backup.BackupError):
            self.repo.initialize(self.repository, self.password)

    def test_writer_appearing_after_backup_prevents_promotion(self):
        original = self.repo.run

        def start_writer(args):
            result = original(args)
            if "backup" in args:
                self.manager.running = True
            return result

        self.repo.run = start_writer
        with pytest.raises(backup.BackupError):
            self.create()
        assert 'tag' not in self.events

    def test_remote_docker_context_is_rejected(self):
        self.repo.run = lambda args: json.dumps([{"Endpoints": {"docker": {"Host": "ssh://host"}}}])
        with patch.dict(os.environ, {}, clear=True), pytest.raises(backup.BackupError):
            self.repo.docker_check()

    def test_cached_helper_does_not_require_a_pull(self):
        commands = []

        def cached(args):
            commands.append(args)
            if args[1:3] == ["context", "inspect"]:
                return json.dumps(
                    [{"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}]
                )
            if args[1] == "info":
                return "linux"
            return "sha256:cached"

        self.repo.run = cached
        with patch.dict(os.environ, {}, clear=True):
            self.repo.docker_check()
        assert not any(('pull' in x for x in commands))


class TestCli:
    def test_help_does_not_need_manager_or_docker(self):
        result = subprocess.run(
            [sys.executable, str(MODULE), "backup", "--help"], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        assert '--stop' in result.stdout

    def test_existing_commands_are_delegated_without_reparsing(self):
        args = ["--env-file", "/tmp/backup", "--no-build", "up"]
        with patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execute:
            with pytest.raises(SystemExit):
                cli.main(args)
        assert execute.call_args.args[1][2:] == args
        assert execute.call_args.args[1][1].endswith('entrypoint.py')

    def test_backup_command_is_routed_with_global_options(self):
        args = ["--state-dir", "/tmp/state", "backup", "--help"]
        with patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execute:
            with pytest.raises(SystemExit):
                cli.main(args)
        assert execute.call_args.args[1][2:] == args
        assert execute.call_args.args[1][1].endswith('backup.py')

    def test_model_id_named_backup_is_not_a_backup_command(self):
        args = ["--model-id", "backup", "--confirm", "purge-deleted"]
        with patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execute:
            with pytest.raises(SystemExit):
                cli.main(args)
        assert execute.call_args.args[1][2:] == args
        assert execute.call_args.args[1][1].endswith('entrypoint.py')

    def test_backup_options_work_before_and_after_command(self):
        for args in (
            ["--env-file", "/tmp/env", "backup", "create", "--stop"],
            ["backup", "create", "--env-file", "/tmp/env", "--stop"],
        ):
            parsed = backup.parser().parse_args(args)
            assert parsed.env_file == Path('/tmp/env')
            assert parsed.stop

    def test_create_cannot_silently_ignore_restore_option(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as result:
                backup.main(["backup", "create", "--target", "/tmp/test"])
        assert result.value.code == 2
