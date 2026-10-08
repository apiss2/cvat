# SPDX-License-Identifier: MIT
import importlib.util
import re
from pathlib import Path

import yaml
from ultrasam_constants import MAX_BODY_BYTES, UPSTREAM_COMMIT

ROOT = Path(__file__).resolve().parents[1]


def definition():
    return yaml.safe_load((ROOT / "nuclio/function-gpu.yaml").read_text())


def test_cvat_discovery_advertises_a_point_interactor():
    config = definition()
    annotations = config["metadata"]["annotations"]
    assert config["metadata"]["name"] == "pth-ultrasam-interactor"
    assert annotations["type"] == "interactor"
    assert int(annotations["version"]) == 2
    assert int(annotations["min_pos_points"]) == 1
    assert int(annotations["min_neg_points"]) == 0
    # CVAT v2.75 uses bool(value), so the string "false" would ENABLE box prompts.
    assert "startswith_box" not in annotations
    assert "startswith_box_optional" not in annotations


def test_model_build_uses_the_pinned_source_and_strict_postcopy_check():
    config = definition()["spec"]
    steps = config["build"]["directives"]
    assert UPSTREAM_COMMIT in " ".join(step["value"] for step in steps["preCopy"])
    checks = " ".join(step["value"] for step in steps["postCopy"])
    assert "validate_build.py" in checks
    assert "pip check" in checks
    assert "python -m pip freeze" in checks
    assert config["runtime"] == "python:3.10"
    assert config["readinessTimeoutSeconds"] >= 120
    for line in (ROOT / "nuclio/requirements.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.+-]*", line), line


def test_private_single_worker_trigger_and_handler_are_deployable():
    config = definition()["spec"]
    trigger = config["triggers"]["http"]
    assert trigger["maxWorkers"] == 1
    assert trigger["attributes"]["maxRequestBodySize"] == MAX_BODY_BYTES
    assert trigger["attributes"]["disablePortPublishing"] is True
    assert config["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert config["minReplicas"] == config["maxReplicas"] == 1
    filename, function = config["handler"].split(":")
    spec = importlib.util.spec_from_file_location(
        "ultrasam_nuclio_main", ROOT / f"nuclio/{filename}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(getattr(module, function))
    assert callable(module.init_context)
