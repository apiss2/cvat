"""Regression tests for orchestration. Docker/nuctl are simulated, not integration-tested."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "extension_manage", ROOT / "components/extensions/manage.py"
)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def write_env(path, text=""):
    public = (
        ""
        if "MR_PUBLIC_URL=" in text
        else "MR_PUBLIC_URL=http://localhost:8080/model-registry/\n"
    )
    path.write_text("SAM2_REDIS_PASSWORD=" + "a" * 64 + "\n" + public + text)
    path.chmod(0o600)
    return path


def model(selected=("itgformat", "sam2"), registry=None):
    backend = {"image": "cvat-local/server:itgformat-test", "pull_policy": "never"}
    env = {
        "CVAT_NUCLIO_INVOKE_METHOD": "dashboard",
        "CVAT_NUCLIO_FUNCTION_NAMESPACE": "nuclio",
    }
    services = {
        n: copy.deepcopy(backend)
        for n in (
            "cvat_server",
            "cvat_worker_import",
            "cvat_worker_export",
            "cvat_worker_annotation",
        )
    }
    services["cvat_server"]["environment"] = env.copy()
    services["cvat_worker_annotation"]["environment"] = env.copy()
    services["cvat_ui"] = {
        "image": "cvat-local/ui:sam2-test",
        "pull_policy": "never",
        "build": {
            "context": ".",
            "dockerfile": "Dockerfile.ui",
            "args": {"CLIENT_PLUGINS": "plugins/sam2"},
        },
    }
    services["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"] = ":".join(
        plugin
        for feature, plugin in ops.EXTENSION_PLUGINS.items()
        if feature in selected
    )
    if "sam2" in selected:
        services["sam2_redis"] = {"image": "redis:7.2.11-alpine"}
    if set(selected) & {"sam2", "ultrasam", "model_registry"}:
        services["nuclio"] = {
            "image": "quay.io/nuclio/dashboard:1.16.3-amd64",
            "ports": [{"host_ip": "127.0.0.1", "published": "8070", "target": 8070}],
        }
    if "model_registry" in selected:
        services["model-gateway"] = {
            "image": "cvat-local/model-gateway:extensions-test",
            "pull_policy": "never",
        }
        if registry is None or ops.local_registry(registry):
            values = registry or {}
            services["model-registry"] = {
                "image": "cvat-local/model-registry:extensions-test",
                "pull_policy": "never",
                "networks": {"cvat": {}},
                "environment": {
                    "MR_WORKER_IMAGE": "cvat-local/onnx-worker:extensions-test-cpu",
                    "MR_INSTANCE": values.get("MR_INSTANCE", "cvat"),
                    "MR_HOST_DATA_DIR": values.get(
                        "MR_HOST_DATA_DIR", "/srv/models/data"
                    ),
                },
            }
        for name in ("cvat_server", "cvat_worker_annotation"):
            services[name]["environment"].update(
                CVAT_NUCLIO_HOST="model-gateway",
                CVAT_NUCLIO_PORT="8070",
                CVAT_NUCLIO_DEFAULT_TIMEOUT="300",
            )
    services["traefik"] = {"image": "traefik:test"}
    return {
        "services": services,
        "networks": {"cvat": {"external": True, "name": "cvat_cvat"}},
        "volumes": {
            "sam2_redis_data": {"external": True, "name": "cvat_sam2_redis_data"}
        },
    }


def item(
    name="pth-sam2-tracker",
    running=True,
    network="cvat_cvat",
    project="cvat",
    namespace="nuclio",
):
    return {
        "Id": name + "-id",
        "Name": "/" + name,
        "Config": {
            "Image": "old-image",
            "Labels": {
                "nuclio.io/function-name": name,
                "nuclio.io/namespace": namespace,
                "nuclio.io/project-name": project,
            },
        },
        "State": {"Running": running, "Status": "running" if running else "exited"},
        "HostConfig": {
            "RestartPolicy": {"Name": "always", "MaximumRetryCount": 0},
            "PortBindings": {},
        },
        "NetworkSettings": {"Networks": {network: {}}},
    }


class Fake(ops.Manager):
    def __init__(self, root, env_file, state):
        super().__init__(root, env_file, state)
        self.calls = []
        self.items = []
        self.model = model(self.extensions, self.values)
        self.projects = "No projects found\n"
        self.running_compose = []
        self.rendered = []
        self.fail_build = False
        self.network_exists = True
        self.env.update(
            CVAT_EXT_SERVER_IMAGE=self.model["services"]["cvat_server"]["image"],
            CVAT_EXT_UI_IMAGE=self.model["services"]["cvat_ui"]["image"],
        )
        if "model-gateway" in self.model["services"]:
            self.env["CVAT_MODEL_GATEWAY_IMAGE"] = self.model["services"][
                "model-gateway"
            ]["image"]
        if registry := self.model["services"].get("model-registry"):
            self.env["MR_MANAGER_IMAGE"] = registry["image"]
            self.env["MR_WORKER_IMAGE"] = registry["environment"]["MR_WORKER_IMAGE"]

    def configuration(self):
        ops.validate_model(self.model, self.env)
        return copy.deepcopy(self.model)

    def run(self, args, *, stream=False, input_text=None):
        self.calls.append(args)
        if args[:3] == ["docker", "info", "--format"]:
            return "daemon-test\n"
        if args[:3] == ["docker", "compose", "version"]:
            return "2.24.4\n"
        if args[:2] == ["docker", "compose"]:
            if "config" in args:
                return json.dumps(self.model)
            if "build" in args and self.fail_build:
                raise ops.OperationError("build failed")
            if "ps" in args:
                return "container-id\n"
            if "exec" in args:
                return "OK\n"
            return ""
        if args[:2] == ["docker", "ps"]:
            filters = [args[i + 1] for i, v in enumerate(args) if v == "--filter"]
            if any("com.docker.compose.project" in x for x in filters):
                return "\n".join(self.running_compose)
            if any(f.startswith("volume=") for f in filters):
                return ""
            found = self.items
            for f in filters:
                if f.startswith("network="):
                    found = [
                        i for i in found if f[8:] in i["NetworkSettings"]["Networks"]
                    ]
                elif f.startswith("label="):
                    key, sep, value = f[6:].partition("=")
                    found = [
                        i
                        for i in found
                        if key in i["Config"]["Labels"]
                        and (not sep or i["Config"]["Labels"][key] == value)
                    ]
            return "\n".join(i["Id"] for i in found)
        if args[:3] == ["docker", "image", "inspect"]:
            return "sha256:expected\n"
        if args[:2] == ["docker", "build"]:
            if self.fail_build:
                raise ops.OperationError("build failed")
            return ""
        if args[:2] == ["docker", "inspect"]:
            if "--format" in args:
                return "sha256:expected\n"
            return json.dumps([i for i in self.items if i["Id"] in args[2:]])
        if args[:3] == ["docker", "volume", "ls"]:
            return "cvat_sam2_redis_data\n"
        if args[:3] == ["docker", "volume", "create"]:
            return args[-1]
        if args[:3] == ["docker", "network", "ls"]:
            return "cvat_cvat\n" if self.network_exists else ""
        if args[:3] == ["docker", "network", "inspect"]:
            return '[{"Driver":"bridge"}]'
        if args[:3] == ["docker", "network", "create"]:
            self.network_exists = True
            return "network-id"
        if args[:2] == ["docker", "update"]:
            for i in self.items:
                if i["Id"] == args[-1]:
                    value = args[2].split("=", 1)[1]
                    name, _, count = value.partition(":")
                    i["HostConfig"]["RestartPolicy"] = {
                        "Name": name,
                        "MaximumRetryCount": int(count or 0),
                    }
            return ""
        if args[:2] in (["docker", "stop"], ["docker", "start"]):
            for i in self.items:
                if i["Id"] == args[-1]:
                    i["State"]["Running"] = args[1] == "start"
            return ""
        if args[:2] == ["docker", "rm"]:
            self.items = [item for item in self.items if item["Id"] != args[-1]]
            return ""
        if args[:2] == ["nuctl", "version"]:
            return "Client version: 1.16.3\n"
        if args[:3] == ["nuctl", "get", "projects"]:
            return self.projects
        if args[:3] == ["nuctl", "create", "project"]:
            self.projects = '{"meta":{"name":"cvat"}}'
            return ""
        if args[:2] == ["nuctl", "deploy"]:
            path = Path(args[args.index("--file") + 1])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            data = yaml.safe_load(path.read_text())
            self.rendered.append(data)
            name = data["metadata"]["name"]
            self.items = [
                i
                for i in self.items
                if i["Config"]["Labels"]["nuclio.io/function-name"] != name
            ]
            obj = item(name, namespace=data["metadata"]["namespace"])
            obj["Config"]["Image"] = data["spec"]["build"]["image"]
            obj["Config"]["Env"] = [
                f"{entry['name']}={entry['value']}"
                for entry in data["spec"].get("env", [])
            ]
            self.items.append(obj)
            return ""
        raise AssertionError(args)


@pytest.fixture
def mgr(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".dockerignore").write_text("/cvat-model-registry/\n")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    for rel in ops.COMPOSE_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("services: {}\n")
    for source in ops.FUNCTION_SOURCES.values():
        shutil.copytree(ROOT / source, root / source)
    path = root / "tests/itgformat/check_in_cvat.py"
    path.parent.mkdir(parents=True)
    path.write_text('print("test placeholder")')
    return Fake(root, write_env(root / ".env"), state)


def test_env_defaults(tmp_path):
    v = ops.settings(write_env(tmp_path / "config", "COMPOSE_PROJECT_NAME=team\n"))
    assert v["CVAT_NETWORK_NAME"] == "team_cvat"
    assert v["SAM2_REDIS_VOLUME"] == "team_sam2_redis_data"


@pytest.mark.parametrize(
    "text", ["BAD=$(touch /tmp/bad)", "A=`id`", "export A=1", "A=x\nA=y", "A=a b"]
)
def test_env_rejects_expansion_and_ambiguity(tmp_path, text):
    with pytest.raises(ops.OperationError):
        ops.load_env(write_env(tmp_path / "config", text))


@pytest.mark.parametrize(
    "extra",
    [
        "SAM2_REDIS_PASSWORD=short",
        "NUCLIO_DASHBOARD_PORT=99999",
        "COMPOSE_PROJECT_NAME=../bad",
        "SAM2_REDIS_MAXMEMORY=bad",
        "SAM2_REDIS_VOLUME=/tmp/data",
    ],
)
def test_settings_invalid(tmp_path, extra):
    with pytest.raises(ops.OperationError):
        ops.settings(write_env(tmp_path / "config", extra))


def test_env_permissions(tmp_path):
    p = write_env(tmp_path / "config")
    p.chmod(0o644)
    with pytest.raises(ops.OperationError):
        ops.load_env(p)


def test_config_beats_stale_exports(mgr, monkeypatch):
    monkeypatch.setenv("SAM2_REDIS_PASSWORD", "old-secret")
    monkeypatch.setenv("CVAT_EXT_UI_IMAGE", "wrong")
    monkeypatch.setenv("COMPOSE_FILE", "/wrong.yml")
    fresh = ops.Manager(mgr.root, mgr.env_file, mgr.state)
    assert fresh.env["SAM2_REDIS_PASSWORD"] == "a" * 64
    assert "CVAT_EXT_UI_IMAGE" not in fresh.env
    assert "COMPOSE_FILE" not in fresh.env


def test_config_order_and_frozen(mgr):
    cmd = mgr.compose("up")
    paths = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-f"]
    disabled_paths = (
        *ops.EXTENSION_COMPOSE["model_registry"],
        *ops.EXTENSION_COMPOSE["ultrasam"],
        *ops.LOCAL_REGISTRY_COMPOSE,
    )
    assert paths == [
        str(mgr.root / p) for p in ops.COMPOSE_FILES if p not in disabled_paths
    ]
    assert "/dev/null" in mgr.compose("down", frozen=True)
    assert str(mgr.state / "active.compose.json") in mgr.compose("down", frozen=True)


def test_model_valid(mgr):
    ops.validate_model(mgr.model, mgr.env)


@pytest.mark.parametrize(
    "kind",
    [
        "new_worker",
        "wrong_ui",
        "missing_plugin",
        "public_dashboard",
        "public_redis",
        "nonexternal",
        "direct",
        "namespace",
        "pull_backend",
    ],
)
def test_model_fails_closed(mgr, kind):
    s = mgr.model["services"]
    if kind == "new_worker":
        s["cvat_worker_future"] = {"image": "cvat/server:v3"}
    elif kind == "wrong_ui":
        s["cvat_ui"]["build"]["dockerfile"] = "Dockerfile"
    elif kind == "missing_plugin":
        s["cvat_ui"]["build"]["args"] = {}
    elif kind == "public_dashboard":
        s["nuclio"]["ports"][0]["host_ip"] = "0.0.0.0"
    elif kind == "public_redis":
        s["sam2_redis"]["ports"] = ["6379:6379"]
    elif kind == "nonexternal":
        mgr.model["networks"]["cvat"]["external"] = False
    elif kind == "direct":
        s["cvat_server"]["environment"]["CVAT_NUCLIO_INVOKE_METHOD"] = "direct"
    elif kind == "namespace":
        s["cvat_server"]["environment"]["CVAT_NUCLIO_FUNCTION_NAMESPACE"] = "other"
    elif kind == "pull_backend":
        s["cvat_worker_import"]["pull_policy"] = "always"
    with pytest.raises(ops.OperationError):
        ops.validate_model(mgr.model, mgr.env)


def test_private_snapshot_preserves_compose_canonical_json(mgr):
    # `compose config --format json` already escapes literal dollars.
    mgr.model["services"]["sam2_redis"]["command"] = ["echo", "$$PASSWORD"]
    mgr.freeze(mgr.model)
    for f in ("active.json", "active.compose.json"):
        assert stat.S_IMODE((mgr.state / f).stat().st_mode) == 0o600
    snap = json.loads((mgr.state / "active.compose.json").read_text())
    assert snap == mgr.model


def test_stop_order_and_no_deletion(mgr):
    mgr.items = [item(), item("other-cvat-function", False)]
    mgr.down()
    commands = mgr.calls
    dashboard = next(
        i for i, c in enumerate(commands) if "stop" in c and c[-1] == "nuclio"
    )
    first_function = next(
        i for i, c in enumerate(commands) if c[:2] == ["docker", "update"]
    )
    compose_down = next(
        i
        for i, c in enumerate(commands)
        if c[:2] == ["docker", "compose"] and "down" in c
    )
    assert dashboard < first_function < compose_down
    assert not mgr.items[0]["State"]["Running"]
    suspended = ops.read_json(mgr.state / "suspended.json")["containers"]
    assert (
        len(suspended) == 1
        and next(iter(suspended.values()))["restart"]["Name"] == "always"
    )
    assert all(
        not {"rm", "prune", "delete", "-v", "--volumes", "--remove-orphans"} & set(c)
        for c in commands
    )


def test_stop_is_idempotent_and_restore_original_policy(mgr):
    mgr.items = [item(), item("disabled", False)]
    mgr.items[0]["HostConfig"]["RestartPolicy"] = {
        "Name": "on-failure",
        "MaximumRetryCount": 4,
    }
    mgr.down()
    mgr.down()
    mgr.restore_functions()
    assert mgr.items[0]["HostConfig"]["RestartPolicy"] == {
        "Name": "on-failure",
        "MaximumRetryCount": 4,
    }
    assert mgr.items[0]["State"]["Running"]
    assert not mgr.items[1]["State"]["Running"]
    assert not (mgr.state / "suspended.json").exists()


def test_missing_stopped_container_aborts(mgr):
    mgr.items = [item()]
    mgr.down()
    mgr.items = []
    with pytest.raises(ops.OperationError, match="missing"):
        mgr.restore_functions()
    assert (mgr.state / "suspended.json").exists()


@pytest.mark.parametrize("field,value", [("project", "other"), ("namespace", "other")])
def test_foreign_function_aborts_before_stopping(mgr, field, value):
    mgr.items = [item(**{field: value})]
    with pytest.raises(ops.OperationError):
        mgr.down()
    assert not any("stop" in c for c in mgr.calls)


def test_identity_change_aborts(mgr):
    mgr.freeze(mgr.model)
    mgr.values["CVAT_NETWORK_NAME"] = "other"
    with pytest.raises(ops.OperationError):
        mgr.down()
    assert not any("stop" in c for c in mgr.calls)


def test_other_network_function_name_protected(mgr):
    mgr.items = [item(network="other_network")]
    with pytest.raises(ops.OperationError, match="another deployment"):
        mgr.deploy(mgr.model)
    assert not any(c[:2] == ["nuctl", "deploy"] for c in mgr.calls)


def test_empty_project_deploy_and_secret_hygiene(mgr):
    mgr.deploy(mgr.model)
    assert any(c[:3] == ["nuctl", "create", "project"] for c in mgr.calls)
    assert len(mgr.rendered) == 2
    assert not list(mgr.state.glob("sam2-*.yaml"))
    assert all("a" * 64 not in " ".join(c) for c in mgr.calls)
    tracker = mgr.rendered[1]
    assert (
        next(
            e["value"]
            for e in tracker["spec"]["env"]
            if e["name"] == "SAM2_REDIS_PASSWORD"
        )
        == "a" * 64
    )
    assert tracker["spec"]["platform"]["attributes"]["mountMode"] == "volume"
    assert (
        tracker["spec"]["triggers"]["http"]["attributes"]["disablePortPublishing"]
        is True
    )


@pytest.mark.parametrize(
    "projects",
    [
        {"meta": {"name": "cvat", "namespace": "nuclio"}, "spec": {}},
        [{"meta": {"name": "cvat"}}, {"meta": {"name": "other"}}],
        {"cvat": {"meta": {"name": "cvat"}, "spec": {}}},
    ],
)
def test_deploy_recognizes_existing_nuclio_project(mgr, projects):
    mgr.projects = json.dumps(projects)
    mgr.deploy(mgr.model)
    assert not any(c[:3] == ["nuctl", "create", "project"] for c in mgr.calls)
    assert len(mgr.rendered) == 2


def test_deploy_reuses_unchanged_functions(mgr):
    mgr.deploy(mgr.model)
    mgr.calls = []
    mgr.deploy(mgr.model)
    assert not any(c[:2] == ["nuctl", "deploy"] for c in mgr.calls)


def test_deploy_source_change_rebuilds(mgr):
    mgr.deploy(mgr.model)
    mgr.calls = []
    p = mgr.root / ops.NUCLIO_SOURCE / "main.py"
    p.write_text(p.read_text() + "\n# changed\n")
    mgr.deploy(mgr.model)
    assert len([c for c in mgr.calls if c[:2] == ["nuctl", "deploy"]]) == 2


@pytest.mark.parametrize(
    "mode,name", [("image", "pth-sam2-interactor"), ("tracker", "pth-sam2-tracker")]
)
def test_deploy_selected(mgr, mode, name):
    mgr.deploy(mgr.model, mode)
    assert [x["metadata"]["name"] for x in mgr.rendered] == [name]


def test_failed_nuctl_removes_secret_file(mgr):
    original = mgr.run

    def run(args, **kwargs):
        if args[:2] == ["nuctl", "deploy"]:
            raise ops.OperationError("failed")
        return original(args, **kwargs)

    mgr.run = run
    with pytest.raises(ops.OperationError):
        mgr.deploy(mgr.model)
    assert not list(mgr.state.glob("sam2-*.yaml"))


def test_render_rejects_changed_layout():
    with pytest.raises(ops.OperationError):
        ops.render_function("namespace: elsewhere", "nuclio", "image", None)


def test_function_port_failure(mgr):
    obj = item()
    obj["HostConfig"]["PortBindings"] = {"8080/tcp": [{"HostPort": "10000"}]}
    mgr.items = [obj]
    with pytest.raises(ops.OperationError, match="port"):
        mgr.verify_functions(["pth-sam2-tracker"])


def test_up_builds_before_start_and_deploys(mgr):
    mgr.network_exists = False
    mgr.up()
    build = next(i for i, c in enumerate(mgr.calls) if "build" in c)
    start = next(i for i, c in enumerate(mgr.calls) if "up" in c)
    deploy = next(i for i, c in enumerate(mgr.calls) if c[:2] == ["nuctl", "deploy"])
    assert build < start < deploy
    assert (mgr.state / "active.compose.json").is_file()


def test_build_failure_does_not_start(mgr):
    mgr.fail_build = True
    with pytest.raises(ops.OperationError):
        mgr.up()
    assert not any("up" in c for c in mgr.calls)


def test_live_unrecorded_environment_aborts(mgr):
    mgr.running_compose = ["container"]
    with pytest.raises(ops.OperationError, match="down"):
        mgr.up()
    assert not any("build" in c for c in mgr.calls)


def test_check_checks_ui_and_all_backends(mgr):
    mgr.freeze(mgr.model)
    mgr.items = [item(n) for n in ops.FUNCTION_FILES]
    mgr.check()
    assert any("cvat_ui" in c and "ps" in c for c in mgr.calls)
    for name in (
        "cvat_server",
        "cvat_worker_import",
        "cvat_worker_export",
        "cvat_worker_annotation",
    ):
        assert any(name in c and "exec" in c for c in mgr.calls)


def test_redaction(mgr):
    assert mgr.redact("password=" + "a" * 64) == "password=<redacted>"


def test_real_git_tags(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL
        )

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / "file").write_text("one")
    git("add", ".")
    git("commit", "-m", "base")
    clean = ops.source_tag(tmp_path)
    (tmp_path / ".env").write_text("secret=private")
    assert ops.source_tag(tmp_path) == clean
    (tmp_path / "file").write_text("two")
    assert ops.source_tag(tmp_path) != clean
    git("switch", "-c", "release")
    with pytest.raises(ops.OperationError, match="dirty"):
        ops.source_tag(tmp_path)


def test_static_compose_layout():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "!override",
        lambda loader, node: (
            loader.construct_mapping(node, deep=True)
            if isinstance(node, yaml.MappingNode)
            else loader.construct_sequence(node, deep=True)
        ),
    )
    data = {
        p: yaml.load((ROOT / p).read_text(), Loader=Loader)
        for p in ops.COMPOSE_FILES[2:]
    }
    assert (
        data["components/sam2/docker-compose.sam2.yml"]["services"]["cvat_ui"]["build"][
            "dockerfile"
        ]
        == "Dockerfile.ui"
    )
    ultrasam = data["components/ultrasam/docker-compose.ultrasam.yml"]["services"]
    assert set(ultrasam) == {"nuclio"}
    assert ultrasam["nuclio"] == {
        "ports": ["127.0.0.1:${NUCLIO_DASHBOARD_PORT:-8070}:8070"]
    }
    assert set(
        data["components/itgformat/docker-compose.itgformat.yml"]["services"]
    ) == {
        "cvat_server",
        "cvat_worker_utils",
        "cvat_worker_import",
        "cvat_worker_export",
        "cvat_worker_annotation",
        "cvat_worker_webhooks",
        "cvat_worker_quality_reports",
        "cvat_worker_chunks",
        "cvat_worker_consensus",
    }
    assert not (ROOT / "docker-compose.itgformat.yml").exists()
    for path in (ROOT / ops.NUCLIO_SOURCE).glob("*-gpu.yaml"):
        config = yaml.safe_load(path.read_text())
        assert config["spec"]["platform"]["attributes"]["mountMode"] == "volume"
        assert (
            config["spec"]["platform"]["attributes"]["restartPolicy"]["name"]
            == "unless-stopped"
        )


def test_redis_volume_external_required(mgr):
    mgr.model["volumes"]["sam2_redis_data"]["external"] = False
    with pytest.raises(ops.OperationError, match="external volume"):
        ops.validate_model(mgr.model, mgr.env)


def test_existing_redis_volume_never_recreated(mgr):
    mgr.ensure_volume()
    assert not any(c[:3] == ["docker", "volume", "create"] for c in mgr.calls)


def test_missing_redis_volume_created(mgr):
    original = mgr.run

    def run(args, **kwargs):
        if args[:3] == ["docker", "volume", "ls"]:
            return ""
        return original(args, **kwargs)

    mgr.run = run
    mgr.ensure_volume()
    assert ["docker", "volume", "create", "cvat_sam2_redis_data"] in mgr.calls


def test_redis_volume_double_writer_aborts(mgr):
    original = mgr.run

    def run(args, **kwargs):
        if args[:2] == ["docker", "ps"] and "volume=cvat_sam2_redis_data" in args:
            return "old-redis"
        if args[:2] == ["docker", "inspect"]:
            return '[{"Config":{"Labels":{"com.docker.compose.project":"old"}}}]'
        return original(args, **kwargs)

    mgr.run = run
    with pytest.raises(ops.OperationError, match="old Redis"):
        mgr.ensure_volume()


def select_extensions(mgr, text):
    write_env(mgr.env_file, f"CVAT_EXTENSIONS={text}\n")
    return Fake(mgr.root, mgr.env_file, mgr.state)


@pytest.mark.parametrize(
    "selected",
    [
        "",
        "itgformat",
        "sam2",
        "ultrasam",
        "sam2,ultrasam",
        "ultrasam,model_registry",
        "model_registry",
        "itgformat,model_registry",
        "itgformat,sam2,model_registry",
        "itgformat,sam2,ultrasam,model_registry",
    ],
)
def test_selected_extensions_choose_compose_and_plugins(mgr, selected):
    fresh = select_extensions(mgr, selected)
    command = fresh.compose("config")
    files = [
        Path(command[index + 1]).relative_to(fresh.root).as_posix()
        for index, value in enumerate(command)
        if value == "-f"
    ]
    assert files[0] == "docker-compose.yml"
    assert files[-1] == ops.FINAL_COMPOSE
    for feature, paths in ops.EXTENSION_COMPOSE.items():
        assert all((path in files) == (feature in fresh.extensions) for path in paths)
    assert ("components/serverless/docker-compose.serverless.yml" in files) == bool(
        set(fresh.extensions) & {"sam2", "ultrasam", "model_registry"}
    )
    ops.validate_model(fresh.model, fresh.env)
    assert (
        fresh.env["CVAT_CLIENT_PLUGINS"]
        == fresh.model["services"]["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"]
    )


def test_registry_only_does_not_require_sam2_password(tmp_path):
    path = tmp_path / "config"
    path.write_text(
        "CVAT_EXTENSIONS=model_registry\nMR_PUBLIC_URL=http://localhost:8080/model-registry/\n"
    )
    path.chmod(0o600)
    values = ops.settings(path)
    assert values["CVAT_AI_GATEWAY_HOST"] == "model-gateway"
    assert values["CVAT_AI_GATEWAY_TIMEOUT"] == "300"
    assert values["CVAT_CLIENT_PLUGINS"] == "plugins/model-registry"


@pytest.mark.parametrize("value", ["sam2,sam2", "sam2,unknown", "sam2,", "ONNX"])
def test_unknown_or_duplicate_feature_rejected(tmp_path, value):
    with pytest.raises(ops.OperationError, match="CVAT_EXTENSIONS"):
        ops.settings(write_env(tmp_path / "config", f"CVAT_EXTENSIONS={value}\n"))


def test_legacy_registry_enable_and_explicit_selection_precedence(tmp_path):
    path = write_env(tmp_path / "config", "CVAT_MODEL_REGISTRY_ENABLED=1\n")
    assert ops.settings(path)["CVAT_EXTENSIONS"] == "itgformat,sam2,model_registry"
    path = write_env(path, "CVAT_MODEL_REGISTRY_ENABLED=1\nCVAT_EXTENSIONS=sam2\n")
    values = ops.settings(path)
    assert values["CVAT_MODEL_REGISTRY_ENABLED"] == "0"
    assert values["CVAT_CLIENT_PLUGINS"] == "plugins/sam2"


def test_feature_plugins_preserve_custom_plugins_without_stale_features(tmp_path):
    values = ops.settings(
        write_env(
            tmp_path / "config",
            "CVAT_EXTENSIONS=model_registry\nCVAT_CLIENT_PLUGINS=plugins/custom:plugins/sam2:plugins/custom\n",
        )
    )
    assert values["CVAT_CLIENT_PLUGINS"] == "plugins/custom:plugins/model-registry"


def test_gateway_overlay_added_once_for_legacy_extra(mgr):
    path = ops.EXTENSION_COMPOSE["model_registry"][0]
    write_env(
        mgr.env_file,
        f"CVAT_MODEL_REGISTRY_ENABLED=1\nCVAT_EXTRA_COMPOSE_FILES={path}\n",
    )
    fresh = Fake(mgr.root, mgr.env_file, mgr.state)
    assert fresh.compose("config").count(str(fresh.root / path)) == 1


def test_registry_build_and_up_use_one_compose_project(mgr):
    fresh = select_extensions(mgr, "model_registry")
    fresh.up()
    assert any(c[:2] == ["docker", "build"] and "gateway" in c for c in fresh.calls)
    assert any(c[:2] == ["docker", "build"] and "manager" in c for c in fresh.calls)
    assert any(
        c[:2] == ["docker", "build"]
        and any("Dockerfile.worker" in value for value in c)
        for c in fresh.calls
    )
    assert not any(c[0] == "nuctl" or "sam2_redis" in c for c in fresh.calls)
    saved = ops.read_json(fresh.state / "active.json")
    assert {"model-gateway", "model-registry"} <= saved["model"]["services"].keys()
    assert len(saved["images"]) == 5  # server, UI, gateway, manager, ONNX worker
    compose = [c for c in fresh.calls if c[:2] == ["docker", "compose"] and "up" in c]
    assert len(compose) == 1 and compose[0][compose[0].index("-p") + 1] == "cvat"
    assert (
        str(fresh.root / "components/model_registry/docker-compose.registry.yml")
        in compose[0]
    )


def test_no_build_uses_prebuilt_cvat_and_sam2_images(mgr):
    mgr.up(no_build=True)
    assert not any("build" in c for c in mgr.calls)
    deployments = [c for c in mgr.calls if c[:2] == ["nuctl", "deploy"]]
    assert len(deployments) == 2
    assert all("--run-image" in c and "--no-pull" in c for c in deployments)
    saved = ops.read_json(mgr.state / "active.json")
    assert len(saved["images"]) == 4
    assert saved["extensions"] == ["itgformat", "sam2"]


def test_missing_prebuilt_image_fails_before_startup(mgr):
    original = mgr.run

    def run(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            raise ops.OperationError("missing image")
        return original(args, **kwargs)

    mgr.run = run
    with pytest.raises(ops.OperationError, match="missing image"):
        mgr.up(no_build=True)
    assert not any("up" in c or "create" in c or "deploy" in c for c in mgr.calls)


def test_sam2_images_are_transferable_across_runtime_configuration(mgr):
    original = mgr.sam2_images("1.16.3")
    mgr.values["SAM2_REDIS_PASSWORD"] = "z" * 64
    mgr.values["CVAT_NETWORK_NAME"] = "production_cvat"
    mgr.values["NUCLIO_NAMESPACE"] = "production"
    assert mgr.sam2_images("1.16.3") == original
    assert mgr.sam2_images("1.16.4") != original


def test_changed_redis_credentials_redeploy_tracker_with_same_image(mgr):
    mgr.deploy(mgr.model)
    before = mgr.sam2_images("1.16.3")
    mgr.values["SAM2_REDIS_PASSWORD"] = "z" * 64
    mgr.calls = []
    mgr.deploy(mgr.model, no_build=True)
    deployments = [c for c in mgr.calls if c[:2] == ["nuctl", "deploy"]]
    assert len(deployments) == 1
    assert (
        deployments[0][deployments[0].index("--run-image") + 1]
        == before["pth-sam2-tracker"]
    )
    tracker = next(
        i
        for i in mgr.items
        if i["Config"]["Labels"]["nuclio.io/function-name"] == "pth-sam2-tracker"
    )
    assert "SAM2_REDIS_PASSWORD=" + "z" * 64 in tracker["Config"]["Env"]


def test_check_rejects_retagged_started_image(mgr):
    mgr.freeze(
        mgr.model, {mgr.model["services"]["cvat_server"]["image"]: "sha256:original"}
    )
    with pytest.raises(ops.OperationError, match="image tag changed"):
        mgr.check()


def test_registry_check_verifies_authenticated_catalog_without_sam2(mgr):
    fresh = select_extensions(mgr, "model_registry")
    fresh.freeze(fresh.model)
    fresh.check()
    assert any(
        "model-gateway" in c and "exec" in c and "X-Model-Catalog-Unavailable" in c[-1]
        for c in fresh.calls
    )
    assert not any(
        "ITGformat" in " ".join(c) or "pth-sam2" in " ".join(c) for c in fresh.calls
    )


def test_disabled_sam2_functions_remain_stopped(mgr):
    mgr.items = [item()]
    mgr.down()
    mgr.extensions = ("model_registry",)
    mgr.restore_functions()
    assert not mgr.items[0]["State"]["Running"]
    assert ops.read_json(mgr.state / "suspended.json")["containers"]


def test_general_overlay_pins_all_known_backend_workers():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "!override", lambda loader, node: loader.construct_mapping(node, deep=True)
    )
    final = yaml.load((ROOT / ops.FINAL_COMPOSE).read_text(), Loader=Loader)
    services = final["services"]
    expected = set(
        yaml.safe_load(
            (ROOT / "components/itgformat/docker-compose.itgformat.yml").read_text()
        )["services"]
    )
    assert set(services) - {"cvat_ui"} == expected
    for name in expected:
        assert (
            services[name]["image"]
            == "${CVAT_EXT_SERVER_IMAGE:?Use components/extensions/cvatctl}"
        )
        assert services[name]["pull_policy"] == "never"


def test_retagged_sam2_image_replaces_running_old_container(mgr):
    mgr.deploy(mgr.model)
    mgr.calls = []
    original = mgr.run

    def run(args, **kwargs):
        if args[:2] == ["docker", "inspect"] and "--format" in args:
            mgr.calls.append(args)
            return "sha256:previous\n"
        return original(args, **kwargs)

    mgr.run = run
    mgr.deploy(mgr.model, no_build=True)
    assert len([c for c in mgr.calls if c[:2] == ["nuctl", "deploy"]]) == 2


def test_ui_image_identity_includes_plugin_selection(mgr, monkeypatch):
    for path in (
        "Dockerfile",
        "Dockerfile.ui",
        ".dockerignore",
        "cvat-ui/plugins/sam2/src/ts/index.tsx",
        "cvat-ui/plugins/model-registry/src/ts/index.tsx",
        "components/model_registry/Dockerfile",
        "components/model_registry/Dockerfile.worker",
    ):
        source = mgr.root / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.touch()
    monkeypatch.setattr(ops, "source_tag", lambda root: "abc123")
    monkeypatch.setattr(ops, "validate_model", lambda model, env: None)
    sam2 = select_extensions(mgr, "sam2")
    ops.Manager.configuration(sam2)
    registry = select_extensions(mgr, "model_registry")
    ops.Manager.configuration(registry)
    assert sam2.env["CVAT_EXT_SERVER_IMAGE"] == registry.env["CVAT_EXT_SERVER_IMAGE"]
    assert sam2.env["CVAT_EXT_UI_IMAGE"] != registry.env["CVAT_EXT_UI_IMAGE"]


def registry_worker(mgr, *, home=None, running=True):
    data = Path(home or mgr.values["MR_HOST_DATA_DIR"])
    return {
        "Id": "onnx-worker-id",
        "Name": "/onnx-worker",
        "Config": {
            "Image": mgr.env["MR_WORKER_IMAGE"],
            "Labels": {
                "org.cvat-model-registry.instance": mgr.values["MR_INSTANCE"],
                "org.cvat-model-registry.model": "a" * 20,
                "org.cvat-model-registry.revision": "b" * 20,
            },
        },
        "Mounts": [
            {"Destination": "/model", "Source": str(data / "packages/model/revision")},
            {"Destination": "/run/model", "Source": str(data / "run/model-revision")},
        ],
        "State": {"Running": running, "Status": "running" if running else "exited"},
        "HostConfig": {"RestartPolicy": {"Name": "no"}, "PortBindings": {}},
        "NetworkSettings": {"Networks": {}},
    }


def test_registry_uses_repository_storage_and_shared_settings(mgr):
    fresh = select_extensions(mgr, "model_registry")
    home = fresh.root / "cvat-model-registry"
    assert fresh.values["MR_HOME"] == str(home)
    assert fresh.values["MR_HOST_DATA_DIR"] == str(home / "data")
    assert fresh.values["MR_CONFIG_DIR"] == str(home / "config")
    assert fresh.values["CVAT_MODEL_GATEWAY_SECRETS_DIR"] == str(
        home / "config/secrets"
    )
    assert fresh.values["MR_CVAT_NETWORK"] == fresh.values["CVAT_NETWORK_NAME"]
    assert fresh.values["MR_NAMESPACE"] == fresh.values["NUCLIO_NAMESPACE"]
    fresh.configuration()
    assert not home.exists()  # config stays read-only
    fresh.build()
    assert (home / ".gitignore").read_text() == "*\n"
    token = (home / "config/secrets/service_token").read_bytes()
    fresh.up(no_build=True)
    assert (home / "config/secrets/service_token").read_bytes() == token


def test_registry_existing_home_is_adopted_without_moving_data(mgr):
    home = mgr.root.parent / "existing-registry"
    (home / "data").mkdir(parents=True)
    (home / "data/registry.sqlite3").write_bytes(b"existing database")
    (home / "config/secrets").mkdir(parents=True)
    (home / "config/secrets/service_token").write_text("original secret\n")
    write_env(
        mgr.env_file,
        f"CVAT_EXTENSIONS=model_registry\nMR_HOME={home}\nMR_INSTANCE=team-models\n",
    )
    fresh = Fake(mgr.root, mgr.env_file, mgr.state)
    fresh.build()
    assert fresh.values["MR_INSTANCE"] == "team-models"
    assert (home / "config/secrets/service_token").read_text() == "original secret\n"
    assert (home / "data/registry.sqlite3").read_bytes() == b"existing database"
    assert not (fresh.root / "cvat-model-registry").exists()


def test_legacy_secret_directory_cannot_silently_switch(mgr):
    write_env(
        mgr.env_file,
        "CVAT_EXTENSIONS=model_registry\nCVAT_MODEL_GATEWAY_SECRETS_DIR=/previous/config/secrets\n",
    )
    with pytest.raises(ops.OperationError, match="MR_HOME"):
        Fake(mgr.root, mgr.env_file, mgr.state)


def test_registry_down_stops_creator_before_workers_and_preserves_files(mgr):
    fresh = select_extensions(mgr, "model_registry")
    fresh.prepare_registry()
    database = Path(fresh.values["MR_HOST_DATA_DIR"]) / "registry.sqlite3"
    database.write_bytes(b"existing database")
    fresh.items = [registry_worker(fresh)]
    fresh.down()
    stops = [c for c in fresh.calls if "stop" in c]
    manager_stop = next(i for i, c in enumerate(stops) if c[-1] == "model-registry")
    worker_stop = next(i for i, c in enumerate(stops) if c[-1] == "onnx-worker-id")
    assert manager_stop < worker_stop
    assert fresh.items == []
    assert database.read_bytes() == b"existing database"
    assert [c for c in fresh.calls if c[:2] == ["docker", "rm"]] == [
        ["docker", "rm", "onnx-worker-id"]
    ]
    assert all(not {"prune", "-v", "--volumes"} & set(c) for c in fresh.calls)
    fresh.down()  # Repeated down is safe with stopped disposable workers.


@pytest.mark.parametrize("operation", ["up", "down"])
def test_registry_foreign_worker_is_rejected_before_service_changes(mgr, operation):
    fresh = select_extensions(mgr, "model_registry")
    fresh.items = [registry_worker(fresh, home="/different/data")]
    with pytest.raises(ops.OperationError, match="foreign worker"):
        getattr(fresh, operation)()
    assert not any("up" in c or "stop" in c or "build" in c for c in fresh.calls)


def test_registry_down_uses_frozen_instance_and_storage(mgr):
    fresh = select_extensions(mgr, "model_registry")
    fresh.items = [registry_worker(fresh)]
    fresh.freeze(fresh.model)
    fresh.values["MR_INSTANCE"] = "new-instance"
    fresh.values["MR_HOST_DATA_DIR"] = "/new/data"
    fresh.down()
    assert fresh.items == []


@pytest.mark.parametrize("shared", ["data", "network"])
def test_registry_old_manager_must_stop_before_startup(mgr, shared):
    fresh = select_extensions(mgr, "model_registry")
    original = fresh.run

    def run(args, **kwargs):
        if args[:2] == ["docker", "ps"] and (
            shared == "data"
            and any(v.startswith("volume=") for v in args)
            or shared == "network"
            and "label=com.docker.compose.service=model-registry" in args
        ):
            return "old-manager"
        if args[:2] == ["docker", "inspect"] and "old-manager" in args:
            return json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {"com.docker.compose.project": "mr-team-models"}
                        }
                    }
                ]
            )
        return original(args, **kwargs)

    fresh.run = run
    with pytest.raises(ops.OperationError, match="old registry"):
        fresh.up()
    assert not any("up" in c or "build" in c for c in fresh.calls)


def test_registry_no_build_checks_worker_before_any_startup(mgr):
    fresh = select_extensions(mgr, "model_registry")
    original = fresh.run

    def run(args, **kwargs):
        if (
            args[:3] == ["docker", "image", "inspect"]
            and args[3] == fresh.env["MR_WORKER_IMAGE"]
        ):
            raise ops.OperationError("missing ONNX worker image")
        return original(args, **kwargs)

    fresh.run = run
    with pytest.raises(ops.OperationError, match="ONNX worker"):
        fresh.up(no_build=True)
    assert not any("up" in c or "build" in c for c in fresh.calls)


def test_remote_registry_does_not_add_local_manager_or_storage(mgr):
    write_env(
        mgr.env_file,
        "CVAT_EXTENSIONS=model_registry\nCVAT_MODEL_REGISTRY_URL=https://inference.example.internal\nCVAT_MODEL_GATEWAY_SECRETS_DIR=/srv/gateway-secrets\n",
    )
    fresh = Fake(mgr.root, mgr.env_file, mgr.state)
    command = fresh.compose("config")
    assert all(
        str(fresh.root / path) not in command for path in ops.LOCAL_REGISTRY_COMPOSE
    )
    fresh.up()
    builds = [c for c in fresh.calls if c[:2] == ["docker", "build"]]
    assert len(builds) == 1 and "gateway" in builds[0]
    assert "model-registry" not in fresh.model["services"]
    assert not (fresh.root / "cvat-model-registry").exists()


def test_registry_shell_exports_do_not_override_env_file(mgr, monkeypatch):
    monkeypatch.setenv("MR_INSTANCE", "wrong-instance")
    monkeypatch.setenv("MR_MANAGER_IMAGE", "wrong/image:tag")
    fresh = select_extensions(mgr, "model_registry")
    assert fresh.values["MR_INSTANCE"] == "cvat"
    assert fresh.env["MR_INSTANCE"] == "cvat"
    assert fresh.env["MR_MANAGER_IMAGE"] != "wrong/image:tag"


@pytest.mark.parametrize(
    "model_id,confirm", [(None, True), ("a" * 20, False), ("../bad", True)]
)
def test_purge_requires_explicit_confirmation_and_valid_model_id(
    mgr, model_id, confirm
):
    fresh = select_extensions(mgr, "model_registry")
    with pytest.raises(ops.OperationError, match="Purge requires"):
        fresh.purge_deleted(model_id, confirm=confirm)
    assert fresh.calls == []


@pytest.mark.parametrize("running", ["manager", "worker"])
def test_purge_requires_stopped_manager_and_workers(mgr, monkeypatch, running):
    fresh = select_extensions(mgr, "model_registry")
    invoked = []
    monkeypatch.setattr(ops, "purge_registry_model", lambda *args: invoked.append(args))
    if running == "worker":
        fresh.items = [registry_worker(fresh)]
    else:
        original = fresh.run

        def run(args, **kwargs):
            if args[:2] == ["docker", "ps"] and any(
                v.startswith("volume=") for v in args
            ):
                return "manager-container"
            return original(args, **kwargs)

        fresh.run = run
    with pytest.raises(ops.OperationError, match="down"):
        fresh.purge_deleted("a" * 20, confirm=True)
    assert invoked == []


def test_purge_uses_recorded_storage_after_stop(mgr, monkeypatch):
    fresh = select_extensions(mgr, "model_registry")
    fresh.freeze(fresh.model)
    expected = Path(fresh.values["MR_HOST_DATA_DIR"])
    fresh.values["MR_HOST_DATA_DIR"] = "/changed/data"
    invoked = []
    monkeypatch.setattr(ops, "purge_registry_model", lambda *args: invoked.append(args))
    fresh.purge_deleted("a" * 20, confirm=True)
    assert invoked == [(expected, "a" * 20)]


@pytest.mark.parametrize(
    "gpu,expected",
    [("", "0"), ("0", "0"), ("2", "2"), ("GPU-1234abcd-5678", "GPU-1234abcd-5678")],
)
def test_ultrasam_only_settings_need_no_redis_and_no_plugin(tmp_path, gpu, expected):
    path = tmp_path / "config"
    path.write_text(f"CVAT_EXTENSIONS=ultrasam\nULTRASAM_GPU_DEVICE={gpu}\n")
    path.chmod(0o600)
    values = ops.settings(path)
    assert values["CVAT_EXTENSIONS"] == "ultrasam"
    assert values["ULTRASAM_GPU_DEVICE"] == expected
    assert "SAM2_REDIS_PASSWORD" not in values
    assert values["CVAT_CLIENT_PLUGINS"] == ""
    assert values["CVAT_AI_GATEWAY_HOST"] == "nuclio"


@pytest.mark.parametrize("gpu", ["all", "-1", "0,1", "0;id", "MIG-1", "cuda:0"])
def test_ultrasam_invalid_gpu_selection_rejected(tmp_path, gpu):
    with pytest.raises(ops.OperationError, match="ULTRASAM_GPU_DEVICE"):
        ops.settings(
            write_env(
                tmp_path / "config",
                f"CVAT_EXTENSIONS=ultrasam\nULTRASAM_GPU_DEVICE={gpu}\n",
            )
        )


def test_ultrasam_shell_export_does_not_override_config(mgr, monkeypatch):
    monkeypatch.setenv("ULTRASAM_GPU_DEVICE", "99")
    fresh = select_extensions(mgr, "ultrasam")
    assert fresh.env["ULTRASAM_GPU_DEVICE"] == "0"


@pytest.mark.parametrize("no_build", [False, True])
def test_ultrasam_only_up_records_image_without_redis_or_sam2(mgr, no_build):
    fresh = select_extensions(mgr, "ultrasam")
    before = fresh.env_file.read_bytes()
    fresh.up(no_build=no_build)
    assert fresh.env_file.read_bytes() == before
    assert [entry["metadata"]["name"] for entry in fresh.rendered] == [
        "pth-ultrasam-interactor"
    ]
    assert not any(
        "sam2_redis" in call or call[:2] == ["docker", "volume"] for call in fresh.calls
    )
    assert fresh.env["CVAT_CLIENT_PLUGINS"] == ""
    saved = ops.read_json(fresh.state / "active.json")
    assert saved["extensions"] == ["ultrasam"]
    assert len(saved["images"]) == 3
    assert any(
        image.startswith("cvat.ultrasam-interactor:") for image in saved["images"]
    )
    assert saved["function_environment"]["pth-ultrasam-interactor"] == {
        "CUDA_VISIBLE_DEVICES": "0"
    }
    if no_build:
        deployment = next(
            call for call in fresh.calls if call[:2] == ["nuctl", "deploy"]
        )
        assert "--run-image" in deployment and "--no-pull" in deployment
        assert not any("build" in call for call in fresh.calls)
    fresh.check()
    query = next(call[-1] for call in fresh.calls if "LambdaGateway" in call[-1])
    assert "pth-ultrasam-interactor" in query
    assert "pth-sam2" not in query


def test_ultrasam_and_existing_extensions_deploy_each_function(mgr):
    fresh = select_extensions(mgr, "itgformat,sam2,ultrasam,model_registry")
    fresh.up(no_build=True)
    assert {entry["metadata"]["name"] for entry in fresh.rendered} == {
        "pth-sam2-interactor",
        "pth-sam2-tracker",
        "pth-ultrasam-interactor",
    }
    assert fresh.env["CVAT_CLIENT_PLUGINS"] == "plugins/sam2:plugins/model-registry"
    saved = ops.read_json(fresh.state / "active.json")
    assert len(saved["images"]) == 8
    fresh.check()
    assert any(
        "pth-ultrasam-interactor" in call[-1] and "pth-sam2-tracker" in call[-1]
        for call in fresh.calls
        if "LambdaGateway" in call[-1]
    )


def test_ultrasam_deploy_uses_own_source_network_and_gpu_without_redis_secret(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.values["ULTRASAM_GPU_DEVICE"] = "GPU-abcd-1234"
    fresh.deploy(fresh.model, feature="ultrasam")
    deployment = next(call for call in fresh.calls if call[:2] == ["nuctl", "deploy"])
    assert deployment[deployment.index("--path") + 1] == str(
        fresh.root / ops.ULTRASAM_SOURCE
    )
    assert json.loads(deployment[deployment.index("--platform-config") + 1]) == {
        "attributes": {"network": "cvat_cvat"},
    }
    function = fresh.rendered[0]
    env = {entry["name"]: entry["value"] for entry in function["spec"]["env"]}
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-abcd-1234"
    assert not any("REDIS" in key for key in env)
    assert function["metadata"]["namespace"] == "nuclio"
    assert (
        function["spec"]["triggers"]["http"]["attributes"]["disablePortPublishing"]
        is True
    )
    assert not list(fresh.state.glob("ultrasam-*.yaml"))


def test_ultrasam_deploy_reuses_image_but_changes_device(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.deploy(fresh.model, feature="ultrasam")
    images = fresh.ultrasam_images("1.16.3")
    fresh.calls = []
    fresh.deploy(fresh.model, feature="ultrasam")
    assert not any(call[:2] == ["nuctl", "deploy"] for call in fresh.calls)
    fresh.values["ULTRASAM_GPU_DEVICE"] = "1"
    assert fresh.ultrasam_images("1.16.3") == images
    fresh.deploy(fresh.model, feature="ultrasam", no_build=True)
    deployments = [call for call in fresh.calls if call[:2] == ["nuctl", "deploy"]]
    assert len(deployments) == 1
    assert (
        deployments[0][deployments[0].index("--run-image") + 1]
        == images["pth-ultrasam-interactor"]
    )
    assert "CUDA_VISIBLE_DEVICES=1" in fresh.items[0]["Config"]["Env"]


def test_ultrasam_source_fingerprint_changes_independently_of_sam2(mgr):
    fresh = select_extensions(mgr, "sam2,ultrasam")
    before = fresh.function_images("1.16.3")
    path = fresh.root / ops.ULTRASAM_SOURCE / "main.py"
    path.write_text(path.read_text() + "\n# source change\n")
    after = fresh.function_images("1.16.3")
    assert before["pth-ultrasam-interactor"] != after["pth-ultrasam-interactor"]
    assert before["pth-sam2-interactor"] == after["pth-sam2-interactor"]


def test_ultrasam_rejects_tracker_and_disabled_deploy_before_any_commands(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    with pytest.raises(ops.OperationError, match="image prompts only"):
        fresh.deploy(fresh.model, "tracker", feature="ultrasam")
    assert not fresh.calls
    with pytest.raises(ops.OperationError, match="Add ultrasam"):
        mgr.deploy(mgr.model, feature="ultrasam")
    assert not mgr.calls


def test_ultrasam_other_network_name_is_protected(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.items = [item("pth-ultrasam-interactor", network="other_network")]
    with pytest.raises(ops.OperationError, match="another deployment"):
        fresh.deploy(fresh.model, feature="ultrasam")
    assert not any(call[:2] == ["nuctl", "deploy"] for call in fresh.calls)


def test_ultrasam_snapshot_check_uses_recorded_selection_and_device(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.up(no_build=True)
    fresh.extensions = ("sam2",)
    fresh.values["ULTRASAM_GPU_DEVICE"] = "2"
    fresh.check()  # check uses the deployed snapshot even after .env changes.
    fresh.items[0]["Config"]["Env"] = ["CUDA_VISIBLE_DEVICES=2"]
    with pytest.raises(ops.OperationError, match="CUDA_VISIBLE_DEVICES"):
        fresh.check()


def test_ultrasam_snapshot_rejects_unrecorded_image(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.up(no_build=True)
    fresh.items[0]["Config"]["Image"] = "cvat.ultrasam-interactor:unexpected-gpu"
    with pytest.raises(ops.OperationError, match="recorded startup image"):
        fresh.check()


def test_ultrasam_changed_device_requires_stop_before_up(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.up(no_build=True)
    fresh.running_compose = ["existing-cvat"]
    fresh.calls = []
    fresh.values["ULTRASAM_GPU_DEVICE"] = "2"
    with pytest.raises(ops.OperationError, match="Run down"):
        fresh.up(no_build=True)
    assert not any(
        call[:2] == ["nuctl", "deploy"] or "up" in call for call in fresh.calls
    )


def test_ultrasam_disabled_stays_stopped_then_restores_when_enabled(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.up(no_build=True)
    fresh.down()
    fresh.extensions = ("sam2",)
    fresh.restore_functions()
    assert not fresh.items[0]["State"]["Running"]
    assert ops.read_json(fresh.state / "suspended.json")["containers"]
    fresh.extensions = ("ultrasam",)
    fresh.restore_functions()
    assert fresh.items[0]["State"]["Running"]
    assert not (fresh.state / "suspended.json").exists()


def test_missing_ultrasam_prebuilt_image_fails_before_startup(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    original = fresh.run

    def run(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"] and args[3].startswith(
            "cvat.ultrasam-"
        ):
            raise ops.OperationError("missing UltraSAM image")
        return original(args, **kwargs)

    fresh.run = run
    with pytest.raises(ops.OperationError, match="missing UltraSAM image"):
        fresh.up(no_build=True)
    assert not any(
        "up" in call or "create" in call or "deploy" in call for call in fresh.calls
    )


@pytest.mark.parametrize(
    "environment",
    [
        "",
        "  env:\n  - name: EXISTING\n    value: keep\n",
        '  env:\n  - name: CUDA_VISIBLE_DEVICES\n    value: "0"\n',
    ],
)
def test_runtime_gpu_environment_render_preserves_yaml(environment):
    source = "metadata:\n  name: test\nspec:\n  runtime: python:3.10\n" + environment
    rendered = ops.render_function_environment(
        source, "CUDA_VISIBLE_DEVICES", "GPU-abcd"
    )
    values = yaml.safe_load(rendered)["spec"]["env"]
    assert sum(entry["name"] == "CUDA_VISIBLE_DEVICES" for entry in values) == 1
    assert (
        next(
            entry["value"]
            for entry in values
            if entry["name"] == "CUDA_VISIBLE_DEVICES"
        )
        == "GPU-abcd"
    )
    if "EXISTING" in source:
        assert {"name": "EXISTING", "value": "keep"} in values


def test_runtime_gpu_environment_render_rejects_duplicate_or_changed_layout():
    for source in (
        'spec:\n  env:\n  - name: CUDA_VISIBLE_DEVICES\n    value: "0"\n  - name: CUDA_VISIBLE_DEVICES\n    value: "1"\n',
        'spec:\n  env:\n    - name: CUDA_VISIBLE_DEVICES\n      value: "0"\n',
    ):
        with pytest.raises(ops.OperationError, match="environment layout"):
            ops.render_function_environment(source, "CUDA_VISIBLE_DEVICES", "1")


@pytest.mark.parametrize("command", ["config", "doctor"])
def test_ultrasam_cli_configuration_reports_image_and_gpu(
    mgr, monkeypatch, capsys, command
):
    fresh = select_extensions(mgr, "ultrasam")
    monkeypatch.setattr(ops, "Manager", lambda *args: fresh)
    monkeypatch.setattr(
        ops.sys,
        "argv",
        [
            "cvatctl",
            "--env-file",
            str(fresh.env_file),
            "--state-dir",
            str(fresh.state),
            command,
        ],
    )
    ops.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["extensions"] == ["ultrasam"]
    assert summary["ultrasam_gpu_device"] == "0"
    assert set(summary["ultrasam_images"]) == {"pth-ultrasam-interactor"}
    assert "sam2_images" not in summary and "sam2_redis_volume" not in summary
    if command == "doctor":
        assert summary["nuclio_version"] == "1.16.3"


@pytest.mark.parametrize("mode", ["image", "all"])
def test_ultrasam_cli_manual_deploy_updates_snapshot_without_redis(
    mgr, monkeypatch, mode
):
    fresh = select_extensions(mgr, "ultrasam")
    fresh.up(no_build=True)
    fresh.calls = []
    monkeypatch.setattr(ops, "Manager", lambda *args: fresh)
    monkeypatch.setattr(
        ops.sys,
        "argv",
        [
            "cvatctl",
            "--env-file",
            str(fresh.env_file),
            "--state-dir",
            str(fresh.state),
            "deploy-ultrasam",
            mode,
        ],
    )
    ops.main()
    assert len([call for call in fresh.calls if call[:2] == ["nuctl", "deploy"]]) == 1
    assert not any(
        "sam2_redis" in call or call[:2] == ["docker", "volume"] for call in fresh.calls
    )
    assert len(ops.read_json(fresh.state / "active.json")["images"]) == 3


def test_ultrasam_cli_manual_deploy_rejects_changed_selection_even_when_compose_matches(
    mgr, monkeypatch
):
    fresh = select_extensions(mgr, "sam2,ultrasam")
    fresh.freeze(fresh.model)
    saved = ops.read_json(fresh.state / "active.json")
    saved["extensions"] = ["sam2"]
    ops.atomic_json(fresh.state / "active.json", saved)
    monkeypatch.setattr(ops, "Manager", lambda *args: fresh)
    monkeypatch.setattr(
        ops.sys,
        "argv",
        [
            "cvatctl",
            "--env-file",
            str(fresh.env_file),
            "--state-dir",
            str(fresh.state),
            "deploy-ultrasam",
        ],
    )
    with pytest.raises(ops.OperationError, match="down/up"):
        ops.main()
    assert not any(call[:2] == ["nuctl", "deploy"] for call in fresh.calls)


def test_ultrasam_cli_tracker_rejected_before_loading_settings(monkeypatch):
    monkeypatch.setattr(ops.sys, "argv", ["cvatctl", "deploy-ultrasam", "tracker"])
    with pytest.raises(SystemExit) as exc:
        ops.main()
    assert exc.value.code == 2


def test_init_does_not_overwrite_existing_environment(mgr, monkeypatch):
    original = mgr.env_file.read_bytes()
    monkeypatch.setattr(
        ops.sys,
        "argv",
        [
            "cvatctl",
            "--env-file",
            str(mgr.env_file),
            "--state-dir",
            str(mgr.state),
            "init",
        ],
    )
    with pytest.raises(ops.OperationError, match="already exists"):
        ops.main()
    assert mgr.env_file.read_bytes() == original


def test_ultrasam_nuctl_mismatch_fails_before_build_or_start(mgr):
    fresh = select_extensions(mgr, "ultrasam")
    original = fresh.run

    def run(args, **kwargs):
        if args[:2] == ["nuctl", "version"]:
            return "Client version: 1.15.0\n"
        return original(args, **kwargs)

    fresh.run = run
    with pytest.raises(ops.OperationError, match="nuctl must match"):
        fresh.up()
    assert not any(
        "build" in call or "up" in call or "create" in call or "deploy" in call
        for call in fresh.calls
    )


@pytest.mark.parametrize("file", ["main.py", "function-gpu.yaml"])
def test_missing_ultrasam_source_fails_before_build_or_start(mgr, file):
    fresh = select_extensions(mgr, "ultrasam")
    (fresh.root / ops.ULTRASAM_SOURCE / file).unlink()
    with pytest.raises(ops.OperationError, match="Missing function"):
        fresh.up()
    assert not any(
        "build" in call or "up" in call or "create" in call or "deploy" in call
        for call in fresh.calls
    )
