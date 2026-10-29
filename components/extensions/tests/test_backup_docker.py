# SPDX-License-Identifier: MIT
"""Opt-in PostgreSQL/restic round trip using only uniquely named disposable volumes.

CVAT_BACKUP_DOCKER_TEST=1 python -m pytest \
    components/extensions/tests/test_backup_docker.py -v
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import pytest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

from test_backup import backup


@pytest.mark.skipif(
    os.environ.get("CVAT_BACKUP_DOCKER_TEST") != "1" or not shutil.which("docker"),
    reason="Requires explicit opt-in and a local Docker daemon",
)
class TestDockerRoundTrip:
    def test_incremental_and_postgres_restore(self):
        base = Path(tempfile.mkdtemp(prefix="cvatctl-backup-test-"))
        name = "cvatctl-backup-test-" + uuid.uuid4().hex
        db, restored = name + "-db", name + "-restored"
        state = base / "state"
        state.mkdir(mode=0o700)
        env = base / "test.env"
        env.write_text("CVAT_EXTENSIONS=\n")
        env.chmod(0o600)
        run = backup.run
        repo = backup.Repository(state)
        volumes = [name + "-data", name + "-restore-data"]
        try:
            repo.docker_check()
            run(["docker", "pull", "postgres:15-alpine"])
            for volume in volumes:
                run(["docker", "volume", "create", volume])
            subprocess.run(["git", "init", str(base)], check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(base),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "fixture",
                ],
                check=True,
                capture_output=True,
            )
            with redirect_stdout(io.StringIO()):
                repo.initialize(base / "repository", None)
            model = {
                "services": {
                    "cvat_server": {
                        "environment": {
                            "CVAT_POSTGRES_HOST": "cvat_db",
                            "CVAT_REDIS_INMEM_HOST": "cvat_redis_inmem",
                            "CVAT_REDIS_ONDISK_HOST": "cvat_redis_ondisk",
                        }
                    },
                    "cvat_db": {
                        "volumes": [
                            {"type": "volume", "source": "db", "target": "/var/lib/postgresql/data"}
                        ]
                    },
                    "cvat_redis_inmem": {},
                    "cvat_redis_ondisk": {},
                },
                "volumes": {"db": {"name": volumes[0]}},
            }
            identity = {
                "project": name,
                "daemon": run(["docker", "info", "--format", "{{.ID}}"]).strip(),
            }
            backup.save_json(
                state / "active.json", {"model": model, "identity": identity, "extensions": []}
            )
            backup.save_json(state / "active.compose.json", model)

            class Manager:
                root, env_file, project = base, env, name

                def __init__(self):
                    self.state = state

                def run(self, args):
                    return run(args)

                def assert_identity(self, saved):
                    assert saved["identity"] == identity

                def functions(self):
                    return []

                def registry_workers(self, model):
                    return []

                def down(self):
                    run(["docker", "stop", "--time", "120", db])

            def start(container, volume):
                run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        container,
                        "--label",
                        "com.docker.compose.project=" + name,
                        "--label",
                        "com.docker.compose.service=cvat_db",
                        "-e",
                        "POSTGRES_HOST_AUTH_METHOD=trust",
                        *backup.mount("volume", volume, "/var/lib/postgresql/data", readonly=False),
                        "postgres:15-alpine",
                    ]
                )
                for _ in range(120):
                    try:
                        run(["docker", "exec", container, "pg_isready", "-U", "postgres"])
                        return
                    except backup.BackupError:
                        time.sleep(0.5)
                pytest.fail("PostgreSQL did not become ready")

            start(db, volumes[0])
            run(
                [
                    "docker",
                    "exec",
                    db,
                    "psql",
                    "-U",
                    "postgres",
                    "-c",
                    "CREATE TABLE marker(value text); INSERT INTO marker VALUES ('before');",
                ]
            )
            # Random data makes deduplication observable without relying on timing.
            run(
                [
                    "docker",
                    "exec",
                    db,
                    "sh",
                    "-ec",
                    "dd if=/dev/urandom of=/var/lib/postgresql/data/test-payload bs=1048576 count=8",
                ]
            )
            with redirect_stdout(io.StringIO()):
                backup.create_backup(repo, Manager(), stop=True)
            first = repo.snapshots()[-1]["id"]
            with redirect_stdout(io.StringIO()) as output:
                backup.create_backup(repo, Manager())
            # Only metadata should be new while the database remains stopped.
            text = output.getvalue()
            report, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
            assert report['summary']['data_added'] < 1024 * 1024
            run(["docker", "start", db])
            for _ in range(120):
                try:
                    run(
                        [
                            "docker",
                            "exec",
                            db,
                            "psql",
                            "-U",
                            "postgres",
                            "-c",
                            "INSERT INTO marker VALUES ('after');",
                        ]
                    )
                    break
                except backup.BackupError:
                    time.sleep(0.5)
            else:
                pytest.fail("PostgreSQL did not restart")
            run(["docker", "stop", "--time", "120", db])
            target = base / "restore"
            with redirect_stdout(io.StringIO()):
                repo.restore(first, target)
            path = target / "source/volume" / hashlib.sha256(volumes[0].encode()).hexdigest()[:24]
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    *backup.mount("bind", path, "/from"),
                    *backup.mount("volume", volumes[1], "/to", readonly=False),
                    "--entrypoint",
                    "sh",
                    backup.IMAGE,
                    "-ec",
                    'test -z "$(ls -A /to)"; cp -a /from/. /to/',
                ]
            )
            start(restored, volumes[1])
            result = run(
                [
                    "docker",
                    "exec",
                    restored,
                    "psql",
                    "-U",
                    "postgres",
                    "-Atc",
                    "SELECT value FROM marker;",
                ]
            )
            assert result.strip() == 'before'
            repo.execute("check", "--read-data")
        finally:
            for container in (db, restored):
                subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            for volume in volumes:
                subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
            # restic restores original ownership. Cleanup is restricted to this test's mkdtemp.
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    *backup.mount("bind", base, "/cleanup", readonly=False),
                    "--entrypoint",
                    "sh",
                    backup.IMAGE,
                    "-ec",
                    "find /cleanup -mindepth 1 -delete",
                ],
                capture_output=True,
            )
            shutil.rmtree(base)
