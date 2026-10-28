#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cold, incremental deployment backups. See docs/extensions/incremental-backup.md."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "restic/restic:0.19.1"
TAG = "cvatctl-backup-v1"
COMPLETE = "cvatctl-complete"
PENDING = "cvatctl-pending"


class BackupError(RuntimeError):
    pass


def run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        # Never print command output: inspect/config output may contain secrets.
        raise BackupError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout


def overlap(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def safe_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if any(c in str(path) for c in (",", "\n", "\r", "\0")):
        raise BackupError("Docker mount paths cannot contain commas or line breaks")
    return path


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise BackupError(f"A private directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise BackupError(f"Use a private directory (chmod 700): {path}")


def save_json(path: Path, value: Any) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".backup-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def deployment_lock(state: Path):
    private_directory(state)
    with (state / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("Another cvatctl operation is using this deployment") from exc
        yield


def mount(kind: str, source: str | Path, target: str, *, readonly: bool = True) -> list[str]:
    if any(c in str(source) for c in (",", "\n", "\r", "\0")):
        raise BackupError("Unsafe Docker mount source")
    value = f"type={kind},source={source},target={target}"
    if readonly:
        value += ",readonly"
    if kind == "volume":
        value += ",volume-nocopy"
    return ["--mount", value]


class Repository:
    def __init__(self, state: Path, runner: Callable[[list[str]], str] = run):
        self.state, self.run = state, runner
        self.config = state / "backup.json"
        self.cache = state / "backup-cache"
        self.values = json.loads(self.config.read_text()) if self.config.exists() else {}

    def docker_check(self) -> None:
        # A remote daemon would interpret local bind paths on a different machine.
        context = json.loads(self.run(["docker", "context", "inspect"]))[0]
        endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
        endpoint = endpoint or context["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith("unix://"):
            raise BackupError("Backups require a local Docker Unix socket (run on the Docker host)")
        if self.run(["docker", "info", "--format", "{{.OSType}}"]).strip() != "linux":
            raise BackupError("Only Linux Docker containers are supported")
        # Pull before stopping CVAT, not during the maintenance window.
        if not self.run(["docker", "image", "ls", "--quiet", IMAGE]).strip():
            self.run(["docker", "pull", IMAGE])

    @property
    def directory(self) -> Path:
        if not self.values:
            raise BackupError("Run cvatctl backup init --repository /absolute/backup/path first")
        return safe_path(self.values["repository"])

    @property
    def password(self) -> Path:
        return safe_path(self.values["password_file"])

    def command(self, *args: str, mounts: list[str] | None = None) -> list[str]:
        private_directory(self.directory)
        if not self.password.is_file() or self.password.stat().st_mode & 0o077:
            raise BackupError(
                "The repository password file is missing or is not private (chmod 600)"
            )
        if not self.password.stat().st_size:
            raise BackupError("The repository password file is empty")
        private_directory(self.cache)
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            *mount("bind", self.directory, "/repository", readonly=False),
            *mount("bind", self.password, "/run/restic-password"),
            *mount("bind", self.cache, "/cache", readonly=False),
            *(mounts or []),
            IMAGE,
            "--repo",
            "/repository",
            "--password-file",
            "/run/restic-password",
            "--cache-dir",
            "/cache",
            *args,
        ]

    def execute(self, *args: str, mounts: list[str] | None = None) -> str:
        return self.run(self.command(*args, mounts=mounts))

    def initialize(self, directory: Path, password: Path | None) -> None:
        if self.values:
            raise BackupError("Backup settings already exist; they will not be overwritten")
        directory = safe_path(directory)
        password = safe_path(password or self.state / "backup-password")
        if (
            overlap(directory, ROOT)
            or overlap(directory, self.state)
            or overlap(directory, password)
        ):
            raise BackupError("Repository must be outside the checkout, state and password paths")
        private_directory(directory)
        existing = any(directory.iterdir())
        if existing and not (directory / "config").is_file():
            raise BackupError("Repository directory is nonempty but is not a restic repository")
        if not password.exists():
            if existing:
                raise BackupError("An existing repository requires its original password file")
            private_directory(password.parent)
            fd = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_hex(32) + "\n")
        self.values = {"version": 1, "repository": str(directory), "password_file": str(password)}
        # Persist only after restic has accepted the password/repository.
        (
            self.execute("snapshots", "--json")
            if existing
            else self.execute("init", "--repository-version", "2")
        )
        save_json(self.config, self.values)
        print(
            f"Repository: {directory}\nPassword file: {password}\nKeep a separate secure copy of the password file."
        )

    def snapshots(self, *, host: str | None = None) -> list[dict[str, Any]]:
        args = ["snapshots", "--json", "--tag", TAG]
        if host:
            args += ["--host", host]
        items = json.loads(self.execute(*args)) or []
        return sorted(
            (
                x
                for x in items
                if COMPLETE in x.get("tags", []) and PENDING not in x.get("tags", [])
            ),
            key=lambda x: x["time"],
        )

    def select(self, reference: str) -> dict[str, Any]:
        items = self.snapshots()
        matches = (
            items[-1:]
            if reference == "latest"
            else [
                x
                for x in items
                if re.fullmatch(r"[0-9a-f]{8,64}", reference) and x["id"].startswith(reference)
            ]
        )
        if len(matches) != 1:
            raise BackupError("Snapshot must identify exactly one completed cvatctl backup")
        return matches[0]

    def restore(self, reference: str, target: Path) -> None:
        snapshot = self.select(reference)
        target = safe_path(target)
        if any(overlap(target, x) for x in (ROOT, self.state, self.directory, self.password)):
            raise BackupError("Restore target overlaps the checkout or backup storage")
        if target.exists():
            raise FileExistsError(f"Restore target already exists: {target}")
        manifest = json.loads(
            self.execute("dump", snapshot["id"], "/source/metadata/manifest.json")
        )
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != 1
            or not isinstance(manifest.get("sources"), list)
            or not manifest["sources"]
        ):
            raise BackupError("Snapshot has no supported source manifest")
        for entry in manifest["sources"]:
            if not isinstance(entry, dict) or entry.get("type") not in ("bind", "volume"):
                raise BackupError("Snapshot contains an unsupported source")
            source = entry.get("host_path" if entry["type"] == "volume" else "source")
            if not isinstance(source, str) or not Path(source).is_absolute():
                raise BackupError("Snapshot source has no absolute host path")
            if overlap(target, safe_path(source)):
                raise BackupError("Restore target overlaps original deployment storage")
        # Never mount production data or overwrite an existing directory.
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        print(
            self.execute(
                "restore",
                snapshot["id"],
                "--target",
                "/restore",
                "--verify",
                mounts=mount("bind", target, "/restore", readonly=False),
            )
        )
        print(f"Extracted to {target}. Production data was not changed.")


def sources(
    manager: Any, saved: dict[str, Any], containers: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, str]], list[str]]:
    """Resolve the recorded deployment, not potentially edited Compose/.env files."""
    model = saved["model"]
    services = model["services"]
    backend = services["cvat_server"].get("environment", {})
    for key, service in (
        ("CVAT_POSTGRES_HOST", "cvat_db"),
        ("CVAT_REDIS_INMEM_HOST", "cvat_redis_inmem"),
        ("CVAT_REDIS_ONDISK_HOST", "cvat_redis_ondisk"),
    ):
        if service not in services or backend.get(key) != service:
            raise BackupError(f"External or nonstandard database unsupported: {key}")
    if "model_registry" in saved.get("extensions", []) and "model-registry" not in services:
        raise BackupError("A remote model registry must be backed up on its own host")
    result: dict[tuple[str, str], dict[str, str]] = {}
    skipped: list[str] = []

    def add(kind: str, source: str) -> tuple[str, str] | None:
        if kind == "tmpfs":
            return
        if not source or kind not in ("volume", "bind"):
            raise BackupError("Anonymous volumes and unsupported mount types cannot be backed up")
        if kind == "bind":
            path = safe_path(source)
            mode = path.stat().st_mode
            if stat.S_ISSOCK(mode):
                skipped.append(str(path) + " (socket)")
                return
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise BackupError(f"Unsupported bind source: {path}")
            source = str(path)
        key = (kind, source)
        destination = f"/source/{kind}/{hashlib.sha256(source.encode()).hexdigest()[:24]}"
        result[key] = {"type": kind, "source": source, "path": destination}
        return key

    def destination(value: str) -> str:
        if not value or not Path(value).is_absolute():
            raise BackupError("A recorded/runtime mount is missing its absolute container target")
        # Normalize container paths lexically, never against the host filesystem.
        return os.path.normpath(value)

    file_sources = {}
    for section in ("configs", "secrets"):
        file_sources[section] = {}
        for name, value in model.get(section, {}).items():
            if not value.get("file"):
                raise BackupError(f"External {section} require a separate backup")
            file_sources[section][name] = add("bind", value["file"])

    expected_mounts = {}
    for service_name, service in services.items():
        expected = expected_mounts[service_name] = {}

        def expect(target: str, key: tuple[str, str] | None) -> None:
            if key is None:
                return
            target = destination(target)
            if target in expected and expected[target] != key:
                raise BackupError(f"Conflicting recorded mounts for service: {service_name}")
            expected[target] = key

        for item in service.get("volumes", []):
            kind, source = item.get("type", "volume"), item.get("source", "")
            if kind == "volume" and source:
                definition = model.get("volumes", {}).get(source)
                if not definition or not definition.get("name"):
                    raise BackupError(f"Missing recorded volume name: {source}")
                source = definition["name"]
            expect(item.get("target", ""), add(kind, source))
        for section, prefix in (("configs", "/"), ("secrets", "/run/secrets")):
            for item in service.get(section, []):
                item = {"source": item} if isinstance(item, str) else item
                name = item.get("source", "")
                if name not in file_sources[section]:
                    raise BackupError(f"Missing recorded {section} source: {name}")
                target = os.path.join(prefix, item.get("target") or name)
                expect(target, file_sources[section][name])
    # A manually modified container must not silently redirect us to old data.
    for container in containers or []:
        service_name = (container.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.service"
        )
        if service_name not in services:
            raise BackupError("Unrecorded Compose service found; reconcile the deployment first")
        actual = {}
        for runtime_mount in container.get("Mounts", []):
            kind = runtime_mount["Type"]
            if kind == "tmpfs":
                continue
            source = runtime_mount.get("Name" if kind == "volume" else "Source", "")
            if kind == "bind":
                source = str(safe_path(source))
                if Path(source).is_socket():
                    continue
            actual[destination(runtime_mount.get("Destination", ""))] = (kind, source)
        # ClickHouse declares a disposable log volume in its image. Docker
        # creates an anonymous volume even when Compose does not specify one.
        # Verify the declaration and the runtime mount before ignoring it.
        extras = set(actual) - set(expected_mounts[service_name])
        if extras and service_name == "cvat_clickhouse":
            log_target = "/var/log/clickhouse-server"
            if log_target in extras:
                image = container.get("Image")
                if image:
                    image_info = json.loads(
                        manager.run(["docker", "image", "inspect", image])
                    )[0]
                    declared = (image_info.get("Config") or {}).get("Volumes") or {}
                    if log_target in declared:
                        runtime = next(
                            (
                                item for item in container.get("Mounts", [])
                                if destination(item.get("Destination", "")) == log_target
                            ),
                            None,
                        )
                        if runtime and runtime.get("Type") == "volume" and runtime.get("Name"):
                            actual.pop(log_target)
                            skipped.append(
                                f"{service_name}:{log_target} (image-declared diagnostic volume)"
                            )
        if actual != expected_mounts[service_name]:
            raise BackupError(
                f"Runtime mounts differ from the recorded Compose configuration: {service_name}"
            )
    # The Nuclio functions and model workers live outside Compose.
    for item in [*manager.functions(), *manager.registry_workers(model)]:
        for item_mount in item.get("Mounts", []):
            kind = item_mount["Type"]
            add(kind, item_mount.get("Name" if kind == "volume" else "Source", ""))
    for source in saved.get("model_sources", {}).values():
        if source:
            add("bind", str((manager.root / Path(source).expanduser()).resolve()))
    if registry := services.get("model-registry"):
        data = Path(registry["environment"]["MR_HOST_DATA_DIR"])
        # The standard registry home also contains its credentials and config.
        if data.name != "data":
            raise BackupError("Nonstandard registry storage layout needs explicit backup support")
        add("bind", str(data.parent))
    for entry in result.values():
        if entry["type"] == "volume":
            value = json.loads(manager.run(["docker", "volume", "inspect", entry["source"]]))[0]
            if value["Driver"] != "local" or value.get("Options"):
                raise BackupError("Remote/plugin/bind-backed Docker volumes are unsupported")
            entry["host_path"] = value["Mountpoint"]
    if not any(x["type"] == "volume" for x in result.values()):
        raise BackupError("No persistent named volumes found")
    # Avoid copying nested bind directories twice (also covers registry/data).
    entries = sorted(result.values(), key=lambda x: (x["type"], x["source"]))
    entries = [
        x
        for x in entries
        if x["type"] != "bind"
        or not any(
            y["type"] == "bind" and Path(y["source"]) in Path(x["source"]).parents for y in entries
        )
    ]
    return entries, sorted(set(skipped))


def validate_destinations(repo: Repository, entries: list[dict[str, str]]) -> None:
    for entry in entries:
        source = safe_path(entry.get("host_path", entry["source"]))
        for destination in (repo.directory, repo.password, repo.cache, repo.state):
            if overlap(source, destination):
                raise BackupError(f"Backup storage/state overlaps a source: {source}")


def assert_quiet(manager: Any, entries: list[dict[str, str]]) -> None:
    ids = manager.run(["docker", "ps", "-q"]).split()
    containers = json.loads(manager.run(["docker", "inspect", *ids])) if ids else []
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == manager.project:
            raise BackupError("CVAT is running. Use backup create --stop or run cvatctl down first")
        for item in container.get("Mounts", []):
            if not item.get("RW", True):
                continue
            for entry in entries:
                same_volume = entry["type"] == "volume" and item.get("Name") == entry["source"]
                source = item.get("Source")
                shared_path = source and overlap(
                    Path(source), Path(entry.get("host_path", entry["source"]))
                )
                if same_volume or shared_path:
                    raise BackupError(
                        f"A running container can write backup data: {container['Id'][:12]}"
                    )


def create_backup(
    repo: Repository, manager: Any, *, stop: bool = False, plan: bool = False
) -> None:
    active = manager.state / "active.json"
    frozen = manager.state / "active.compose.json"
    if not active.is_file() or not frozen.is_file():
        raise BackupError("A recorded deployment is required; run cvatctl up first")
    saved = json.loads(active.read_text())
    manager.assert_identity(saved)
    if saved["model"] != json.loads(frozen.read_text()):
        raise BackupError("Recorded Compose files disagree; refusing to guess backup sources")
    identifiers = manager.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={manager.project}"]
    ).split()
    containers = json.loads(manager.run(["docker", "inspect", *identifiers])) if identifiers else []
    entries, skipped = sources(manager, saved, containers)
    validate_destinations(repo, entries)
    if plan:
        print(
            json.dumps(
                {"sources": entries, "excluded": skipped, "repository": str(repo.directory)},
                indent=2,
            )
        )
        return
    # Authenticate, resolve sources and read previous snapshots BEFORE stopping.
    host = (
        "cvatctl-"
        + hashlib.sha256(json.dumps(saved["identity"], sort_keys=True).encode()).hexdigest()[:24]
    )
    previous = repo.snapshots(host=host)
    source_mounts = []
    for entry in entries:
        source_mounts += mount(entry["type"], entry["source"], entry["path"])
    repo.execute("version", mounts=source_mounts)
    if stop:
        print(
            "Stopping CVAT using the recorded deployment. It will remain stopped for the update.",
            flush=True,
        )
        manager.down()
    assert_quiet(manager, entries)
    token = "run-" + secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix="backup-metadata-", dir=repo.state) as temporary:
        metadata = Path(temporary)
        shutil.copy2(manager.env_file, metadata / "environment.env")
        shutil.copy2(active, metadata / "active.json")
        shutil.copy2(frozen, metadata / "active.compose.json")
        for filename in ("suspended.json",):
            if (manager.state / filename).is_file():
                shutil.copy2(manager.state / filename, metadata / filename)
        save_json(metadata / "containers.json", containers)
        commit = manager.run(["git", "-C", str(manager.root), "rev-parse", "HEAD"]).strip()
        dirty = manager.run(["git", "-C", str(manager.root), "status", "--porcelain=v1"])
        (metadata / "source.patch").write_text(
            manager.run(["git", "-C", str(manager.root), "diff", "--binary", "HEAD"])
        )
        save_json(
            metadata / "manifest.json",
            {
                "format": 1,
                "strategy": "cold-filesystem",
                "git_commit": commit,
                "git_status": dirty,
                "sources": entries,
                "excluded": skipped,
                "identity": saved["identity"],
                "restic_image": IMAGE,
                "limitations": [
                    "No Docker images, untracked source files or remote object storage",
                    "Restore databases with the recorded versions and architecture",
                ],
            },
        )
        mounts = mount("bind", metadata, "/source/metadata")
        for entry in entries:
            mounts += mount(entry["type"], entry["source"], entry["path"])
        args = [
            "backup",
            "--json",
            "--host",
            host,
            "--tag",
            TAG,
            "--tag",
            PENDING,
            "--tag",
            token,
            "--group-by",
            "host,paths,tags",
        ]
        if previous:
            args += ["--parent", previous[-1]["id"]]
        print("Creating incremental snapshot...", flush=True)
        output = repo.execute(*args, "/source", mounts=mounts)
        summaries = [
            x
            for line in output.splitlines()
            if line.strip()
            for x in [json.loads(line)]
            if x.get("message_type") == "summary"
        ]
        if len(summaries) != 1 or not re.fullmatch(
            r"[0-9a-f]{8,64}", summaries[0].get("snapshot_id", "")
        ):
            raise BackupError("restic did not report a completed snapshot; no backup was promoted")
        assert_quiet(manager, entries)
        # Exit 3 and interrupted snapshots never become completed restore points.
        repo.execute("tag", "--remove", PENDING, "--add", COMPLETE, summaries[0]["snapshot_id"])
        completed = [x for x in repo.snapshots(host=host) if token in x.get("tags", [])]
        if len(completed) != 1:
            raise BackupError("Cannot confirm the completed snapshot")
        summaries[0]["snapshot_id"] = completed[0]["id"]
        print(json.dumps({"snapshot": completed[0]["id"], "summary": summaries[0]}, indent=2))
    print("CVAT remains stopped. Run cvatctl up after updating the source.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env")
    result.add_argument("--state-dir", type=Path)
    result.add_argument("command", choices=["backup"])
    result.add_argument("action", choices=["init", "plan", "create", "list", "check", "restore"])
    result.add_argument("snapshot", nargs="?")
    result.add_argument("--repository", type=Path)
    result.add_argument("--password-file", type=Path)
    result.add_argument(
        "--stop", action="store_true", help="Stop the recorded deployment and leave it stopped"
    )
    result.add_argument(
        "--read-data", action="store_true", help="For check: verify all stored data"
    )
    result.add_argument(
        "--target", type=Path, help="For restore: new extraction directory, never production data"
    )
    return result


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    options = parser()
    args = options.parse_args(argv)
    if (args.action == "init") != (args.repository is not None):
        options.error("--repository is required only for backup init")
    if args.password_file and args.action != "init":
        options.error("--password-file is only supported with init")
    if args.stop and args.action != "create":
        options.error("--stop is only supported with create")
    if args.read_data and args.action != "check":
        options.error("--read-data is only supported with check")
    if (args.action == "restore") != (args.target is not None):
        options.error("--target is required only for restore")
    if args.snapshot is not None and args.action != "restore":
        options.error("A snapshot ID is only supported with restore")
    sys.path.insert(0, str(ROOT))
    from components.extensions import manage
    from components.extensions.catalog import register
    from components.extensions.manage import Manager, OperationError, default_state

    env_file = safe_path(args.env_file)
    state = safe_path(args.state_dir or default_state(env_file))
    if overlap(state, ROOT):
        raise BackupError("Runtime state must be outside the checkout")
    with deployment_lock(state):
        repo = Repository(state)
        repo.docker_check()
        try:
            if args.action == "init":
                repo.initialize(args.repository, args.password_file)
            elif args.action in ("plan", "create"):
                register(manage)
                manager = Manager(ROOT, env_file, state)
                # Reject a .env that redirects Docker away from the helper's daemon.
                if (
                    manager.identity()["daemon"]
                    != repo.run(["docker", "info", "--format", "{{.ID}}"]).strip()
                ):
                    raise BackupError(
                        "The deployment and backup helper use different Docker daemons"
                    )
                create_backup(repo, manager, stop=args.stop, plan=args.action == "plan")
            elif args.action == "list":
                print(json.dumps(repo.snapshots(), indent=2))
            elif args.action == "check":
                print(repo.execute("check", *(["--read-data"] if args.read_data else [])))
            elif args.action == "restore":
                repo.restore(args.snapshot or "latest", args.target)
        except OperationError as exc:
            raise BackupError(str(exc)) from exc


if __name__ == "__main__":
    try:
        main()
    except (BackupError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            "Interrupted. CVAT may be stopped; inspect cvatctl status before resuming.",
            file=sys.stderr,
        )
        sys.exit(130)
