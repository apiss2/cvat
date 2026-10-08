"""Check additive installation and collision handling without requiring a CVAT checkout."""
import importlib.util
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer():
    script = BUNDLE / "scripts/apply.py"
    if not script.is_file():
        pytest.skip("Installation tests are run from the distribution bundle")
    spec = importlib.util.spec_from_file_location("itgformat_installer", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_repo(root, installer):
    registry = root / installer.REGISTRY
    registry.parent.mkdir(parents=True)
    registry.write_text(installer.ANCHOR + "import cvat.apps.dataset_manager.formats.kitti\n")
    return registry


def test_plan_is_additive_and_uses_itgformat(installer, tmp_path):
    registry = prepare_repo(tmp_path, installer)
    before = registry.read_bytes()
    plan = installer.build_plan(tmp_path, BUNDLE)
    assert registry.read_bytes() == before
    planned_registry = next(data for path, data in plan if path == registry)
    assert planned_registry.decode().count(installer.IMPORT) == 1
    assert planned_registry.decode().replace(installer.IMPORT, "") == before.decode()
    paths = {path.relative_to(tmp_path).as_posix() for path, _ in plan}
    assert "cvat/apps/dataset_manager/formats/itgformat/codec.py" in paths
    assert "docker-compose.itgformat.yml" in paths
    assert "docs/itgformat/format_spec.md" in paths
    assert "tests/itgformat/check_in_cvat.py" in paths


def test_identical_install_is_idempotent(installer, tmp_path):
    prepare_repo(tmp_path, installer)
    for path, data in installer.build_plan(tmp_path, BUNDLE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert installer.build_plan(tmp_path, BUNDLE) == []


def test_conflict_fails_before_writing_any_files(installer, tmp_path):
    registry = prepare_repo(tmp_path, installer)
    before = registry.read_bytes()
    target = tmp_path / "cvat/apps/dataset_manager/formats/itgformat/codec.py"
    target.parent.mkdir(parents=True)
    target.write_text("existing implementation\n")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        installer.build_plan(tmp_path, BUNDLE)
    assert registry.read_bytes() == before
    assert target.read_text() == "existing implementation\n"
    assert not (tmp_path / "docker-compose.itgformat.yml").exists()


def test_unexpected_registry_structure_is_rejected(installer, tmp_path):
    registry = prepare_repo(tmp_path, installer)
    registry.write_text("# no registration imports\n")
    with pytest.raises(ValueError, match="Registry anchor changed"):
        installer.build_plan(tmp_path, BUNDLE)
