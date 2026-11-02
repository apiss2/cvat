# SPDX-License-Identifier: MIT
"""Check metric correctness and reject misleading or stale measurement inputs."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import coverage
import pytest

from components.extensions import cli, quality


SAMPLE_SOURCE = """def branchy(flag):
    if flag:
        return 1
    return 2


def never():
    return 3


def outer():
    def inner(flag):
        if flag:
            return 1
        return 2
    return inner(True)


class Demo:
    def call(self, flag):
        if flag:
            return 1
        return 2
"""

UPSTREAM_SOURCE = """def changed(flag):
    if flag:
        return 10
    return 20


def untouched(flag):
    if flag:
        return 1
    return 0
"""

TEST_SOURCE = """import os
from pathlib import Path
import runpy


def test_partial():
    root = Path(__file__).resolve().parents[1]
    sample = runpy.run_path(str(root / 'components/extensions/sample.py'))
    assert sample['branchy'](True) == 1
    assert sample['outer']() == 1
    mixed = runpy.run_path(str(root / 'cvat/apps/engine/existing.py'))
    assert mixed['changed'](True) == 11
    assert os.getenv('QUALITY_TEST_FAIL') != '1'
"""


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def commit(root, message):
    git(root, "add", ".")
    git(
        root, "-c", "user.name=Quality test", "-c", "user.email=quality@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", message,
    )


@pytest.fixture
def sample_repo(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    mixed = root / "cvat/apps/engine/existing.py"
    mixed.parent.mkdir(parents=True)
    mixed.write_text(UPSTREAM_SOURCE)
    commit(root, "upstream")
    git(root, "update-ref", "refs/remotes/upstream/master", "HEAD")

    component = root / "components/extensions"
    component.mkdir(parents=True)
    (component / "sample.py").write_text(SAMPLE_SOURCE)
    (component / "unreached.py").write_text("def lost():\n    return 7\n")
    mixed.write_text(UPSTREAM_SOURCE.replace("return 10", "return 11"))
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(TEST_SOURCE)
    commit(root, "extension")
    return root


@pytest.fixture
def measured(sample_repo, tmp_path, monkeypatch):
    """Produce real coverage, including a source file that was never imported."""
    monkeypatch.chdir(sample_repo)
    cov = coverage.Coverage(
        config_file=False, data_file=str(tmp_path / ".coverage"), branch=True,
        source=["components/extensions", "cvat/apps/engine"],
    )
    cov.start()
    try:
        sample = runpy.run_path("components/extensions/sample.py")
        assert sample["branchy"](True) == 1
        assert sample["outer"]() == 1
        mixed = runpy.run_path("cvat/apps/engine/existing.py")
        assert mixed["changed"](True) == 11
    finally:
        cov.stop()
    cov.save()
    output = tmp_path / "coverage.json"
    cov.json_report(outfile=str(output))
    return quality.discover_scope(sample_repo), json.loads(output.read_text())


@pytest.mark.parametrize(
    "complexity,fraction,expected",
    [(1, 0, 2), (2, 2 / 3, 2 + 4 / 27), (10, 0.5, 22.5), (10, 1, 10)],
)
def test_crap_uses_fractional_statement_coverage(complexity, fraction, expected):
    assert quality.crap_score(complexity, fraction) == pytest.approx(expected)


@pytest.mark.parametrize("complexity,fraction", [(0, 0.5), (2, -0.1), (2, 1.1), (2, float("nan"))])
def test_crap_rejects_invalid_complexity_or_coverage(complexity, fraction):
    with pytest.raises(quality.QualityError):
        quality.crap_score(complexity, fraction)


def test_never_imported_source_is_included_with_zero_coverage(measured):
    scope, report = measured
    path = "components/extensions/unreached.py"
    assert scope["files"][path]["kind"] == "added"
    data = report["files"][path]
    assert data["summary"]["num_statements"] == 2
    assert data["summary"]["covered_lines"] == 0
    functions = quality.function_metrics(scope, report)
    lost, = [item for item in functions if item["path"] == path]
    assert lost["name"] == "lost"
    assert lost["statements"] == 1
    assert lost["covered_statements"] == 0
    assert lost["statement_coverage"] == 0
    assert lost["crap"] == 2


def test_function_coverage_is_not_replaced_with_the_file_average(measured):
    scope, report = measured
    rows = {
        item["name"]: item for item in quality.function_metrics(scope, report)
        if item["path"] == "components/extensions/sample.py"
    }
    branchy = rows["branchy"]
    assert branchy["complexity"] == 2
    assert branchy["statements"] == 3
    assert branchy["covered_statements"] == 2
    assert branchy["statement_coverage"] == pytest.approx(2 / 3)
    assert branchy["branches"] == 2
    assert branchy["covered_branches"] == 1
    assert branchy["branch_coverage"] == 0.5
    assert branchy["crap"] == pytest.approx(2 + 4 / 27)
    assert rows["never"]["crap"] == 2
    assert rows["Demo.call"]["crap"] == 6


def test_nested_functions_and_methods_are_counted_once(measured):
    scope, report = measured
    rows = [
        item for item in quality.function_metrics(scope, report)
        if item["path"] == "components/extensions/sample.py"
    ]
    assert sorted(item["name"] for item in rows) == [
        "Demo.call", "branchy", "never", "outer", "outer.inner",
    ]
    outer, = [item for item in rows if item["name"] == "outer"]
    inner, = [item for item in rows if item["name"] == "outer.inner"]
    assert outer["complexity"] == 1
    assert outer["statement_coverage"] == 1
    assert outer["crap"] == 1
    assert inner["complexity"] == 2
    assert inner["statements"] == 3
    assert inner["covered_statements"] == 2
    assert inner["crap"] == pytest.approx(2 + 4 / 27)


def test_changed_line_coverage_and_changed_function_coverage_are_distinct(measured):
    scope, report = measured
    path = "cvat/apps/engine/existing.py"
    item = scope["files"][path]
    assert item["kind"] == "modified"
    assert item["changed_lines"] == [3]
    assert set(item["changed_lines"]) <= set(report["files"][path]["executed_lines"])
    functions = [row for row in quality.function_metrics(scope, report) if row["path"] == path]
    changed, = functions
    assert changed["name"] == "changed"
    assert changed["statements"] == 3
    assert changed["covered_statements"] == 2
    assert changed["statement_coverage"] == pytest.approx(2 / 3)
    assert changed["crap"] == pytest.approx(2 + 4 / 27)


@pytest.mark.parametrize("relative", [".", "reports", "components/results"])
def test_output_inside_checkout_is_rejected(sample_repo, relative):
    with pytest.raises(quality.QualityError):
        quality.validate_output(sample_repo, sample_repo / relative)


def test_output_symlink_into_checkout_is_rejected(sample_repo, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(sample_repo, target_is_directory=True)
    with pytest.raises(quality.QualityError):
        quality.validate_output(sample_repo, alias / "reports")


def test_external_output_directory_is_accepted(sample_repo, tmp_path):
    expected = tmp_path / "measurements"
    assert quality.validate_output(sample_repo, expected) == expected.resolve()


@pytest.mark.parametrize(
    "arguments,target",
    [
        (["quality", "--help"], "quality.py"),
        (["--env-file", "/tmp/test.env", "quality", "--help"], "quality.py"),
        (["--model-id", "quality", "--confirm", "purge-deleted"], "entrypoint.py"),
    ],
)
def test_cli_routes_quality_without_consuming_other_argument_values(monkeypatch, arguments, target):
    calls = []

    def execute(program, argv):
        calls.append((program, argv))
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execv", execute)
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 0
    assert len(calls) == 1
    program, argv = calls[0]
    assert program == sys.executable
    assert Path(argv[1]).name == target
    assert argv[2:] == arguments


def test_quality_help_works_without_a_repository_or_external_commands(tmp_path):
    env = {**os.environ, "PATH": ""}
    result = subprocess.run(
        [sys.executable, str(Path(cli.__file__).resolve()), "quality", "--help"],
        cwd=tmp_path, env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "quality" in result.stdout


@pytest.fixture
def measurement_suites(sample_repo, monkeypatch):
    """Independent suites make it possible to distinguish data from a failed run."""
    other = sample_repo / "tests/test_other.py"
    other.write_text(
        "from pathlib import Path\nimport runpy\n\n"
        "def test_other():\n"
        "    source = Path(__file__).resolve().parents[1] / 'components/extensions/unreached.py'\n"
        "    assert runpy.run_path(str(source))['lost']() == 7\n"
    )
    commit(sample_repo, "second suite")
    monkeypatch.setattr(
        quality, "SUITES",
        {"tiny": ["tests/test_sample.py"], "other": ["tests/test_other.py"],
         "absent": ["tests/test_absent.py"]},
    )
    return sample_repo


def run_suite(root, output, suite):
    return quality.run_suite(
        root=root, suite=suite, python=sys.executable, output=output,
        upstream="upstream/master",
    )


def summarize(root, output, suites=None):
    return quality.report(
        root=root, output=output, upstream="upstream/master", suites=suites,
    )


def test_partial_measurement_identifies_unrun_suites(measurement_suites, tmp_path):
    output = tmp_path / "measurement"
    manifest = run_suite(measurement_suites, output, "tiny")
    assert manifest["status"] == "passed"
    assert manifest["exit_code"] == 0
    summary = summarize(measurement_suites, output)
    assert summary["passed"] == ["tiny"]
    assert summary["failed"] == []
    assert set(summary["missing"]) == {"other", "absent"}
    assert summary["partial"] is True
    lost, = [item for item in summary["functions"] if item["name"] == "lost"]
    assert lost["covered_statements"] == 0


def test_latest_failed_suite_does_not_reuse_previous_success(
    measurement_suites, tmp_path, monkeypatch
):
    output = tmp_path / "measurement"
    assert run_suite(measurement_suites, output, "tiny")["status"] == "passed"
    assert run_suite(measurement_suites, output, "other")["status"] == "passed"
    monkeypatch.setenv("QUALITY_TEST_FAIL", "1")
    failed = run_suite(measurement_suites, output, "tiny")
    assert failed["status"] == "failed"
    assert failed["exit_code"] != 0
    summary = summarize(measurement_suites, output)
    assert summary["passed"] == ["other"]
    assert summary["failed"] == ["tiny"]
    assert summary["missing"] == ["absent"]
    assert summary["partial"] is True
    branchy, = [item for item in summary["functions"] if item["name"] == "branchy"]
    assert branchy["covered_statements"] == 0
    assert branchy["statement_coverage"] == 0
    assert branchy["crap"] == 6
    lost, = [item for item in summary["functions"] if item["name"] == "lost"]
    assert lost["covered_statements"] == 1
    monkeypatch.setattr(
        quality, "SUITES", {"tiny": ["tests/test_sample.py"], "other": ["tests/test_other.py"]},
    )
    gated = quality.report(
        root=measurement_suites, output=output, upstream="upstream/master", max_crap=1000,
    )
    assert gated["missing"] == []
    assert gated["failed"] == ["tiny"]
    assert gated["gate_passed"] is False


def test_failed_only_measurement_cannot_produce_a_success_report(
    measurement_suites, tmp_path, monkeypatch
):
    output = tmp_path / "measurement"
    monkeypatch.setenv("QUALITY_TEST_FAIL", "1")
    assert run_suite(measurement_suites, output, "tiny")["status"] == "failed"
    with pytest.raises(quality.QualityError):
        summarize(measurement_suites, output, suites=["tiny"])


@pytest.mark.parametrize(
    "path", ["components/extensions/sample.py", "tests/test_sample.py"]
)
def test_changed_source_or_tests_reject_stale_measurement(
    measurement_suites, tmp_path, path
):
    output = tmp_path / "measurement"
    assert run_suite(measurement_suites, output, "tiny")["status"] == "passed"
    changed = measurement_suites / path
    changed.write_text(changed.read_text() + "\nCHANGED_MEASUREMENT_INPUT = True\n")
    with pytest.raises(quality.QualityError):
        summarize(measurement_suites, output, suites=["tiny"])


def test_documentation_only_commit_does_not_invalidate_measurement(
    measurement_suites, tmp_path
):
    output = tmp_path / "measurement"
    assert run_suite(measurement_suites, output, "tiny")["status"] == "passed"
    (measurement_suites / "README.md").write_text("# Current feature instructions\n")
    commit(measurement_suites, "document the feature")
    summary = summarize(measurement_suites, output, suites=["tiny"])
    assert summary["passed"] == ["tiny"]
    assert summary["failed"] == []


def test_non_python_changes_are_reported_as_unmeasured(sample_repo):
    relative = "cvat-ui/plugins/example/src/ts/index.ts"
    source = sample_repo / relative
    source.parent.mkdir(parents=True)
    source.write_text("export const example = () => 1;\n")
    commit(sample_repo, "add a UI extension")
    scope = quality.discover_scope(sample_repo)
    unmeasured = [entry for entry in scope["unmeasured"] if entry["path"] == relative]
    assert len(unmeasured) == 1
    assert unmeasured[0]["reason"]
    assert relative not in scope["files"]


def test_repeated_reports_do_not_retain_hits_from_unselected_suites(
    measurement_suites, tmp_path
):
    output = tmp_path / "measurement"
    assert run_suite(measurement_suites, output, "tiny")["status"] == "passed"
    assert run_suite(measurement_suites, output, "other")["status"] == "passed"

    def coverage_for(summary, name):
        row, = [item for item in summary["functions"] if item["name"] == name]
        return row["covered_statements"]

    both = summarize(measurement_suites, output, suites=["tiny", "other"])
    assert coverage_for(both, "branchy") == 2
    assert coverage_for(both, "lost") == 1
    other = summarize(measurement_suites, output, suites=["other"])
    assert other["passed"] == ["other"]
    assert coverage_for(other, "branchy") == 0
    assert coverage_for(other, "lost") == 1
    tiny = summarize(measurement_suites, output, suites=["tiny"])
    assert tiny["passed"] == ["tiny"]
    assert coverage_for(tiny, "branchy") == 2
    assert coverage_for(tiny, "lost") == 0


def test_crap_gate_respects_exact_boundary_on_complete_measurement(
    measurement_suites, tmp_path, monkeypatch
):
    monkeypatch.setattr(quality, "SUITES", {"tiny": ["tests/test_sample.py"]})
    output = tmp_path / "measurement"
    assert run_suite(measurement_suites, output, "tiny")["status"] == "passed"
    boundary = quality.report(
        root=measurement_suites, output=output, upstream="upstream/master", max_crap=6,
    )
    assert boundary["missing"] == []
    assert boundary["failed"] == []
    assert boundary["partial"] is False
    assert boundary["gate_passed"] is True
    below = quality.report(
        root=measurement_suites, output=output, upstream="upstream/master", max_crap=5.99,
    )
    assert below["gate_passed"] is False


def test_partial_measurement_cannot_pass_a_crap_gate(measurement_suites, tmp_path):
    output = tmp_path / "measurement"
    assert run_suite(measurement_suites, output, "tiny")["status"] == "passed"
    summary = quality.report(
        root=measurement_suites, output=output, upstream="upstream/master", max_crap=1000,
    )
    assert summary["partial"] is True
    assert summary["gate_passed"] is False


def test_all_skipped_suite_is_not_a_successful_measurement(
    sample_repo, tmp_path, monkeypatch
):
    skipped = sample_repo / "tests/test_skipped.py"
    skipped.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='Requires an external test environment')\n"
        "def test_not_run():\n"
        "    raise AssertionError('The skipped test must never execute')\n"
    )
    commit(sample_repo, "add an optional test")
    monkeypatch.setattr(quality, "SUITES", {"skip_only": ["tests/test_skipped.py"]})
    output = tmp_path / "measurement"
    manifest = run_suite(sample_repo, output, "skip_only")
    assert manifest["pytest_exit_code"] == 0
    assert manifest["tests"] == 1
    assert manifest["skipped"] == 1
    assert manifest["status"] == "failed"
    assert manifest["exit_code"] != 0
    assert manifest["error"]
    with pytest.raises(quality.QualityError):
        summarize(sample_repo, output)


@pytest.mark.parametrize("change", ["signature", "decorator"])
def test_signature_and_decorator_changes_select_the_whole_function(
    sample_repo, tmp_path, monkeypatch, change
):
    original = """def keep(function):
    return function


def alternative(function):
    return function


@keep
def configure(
    value,
    extra=1,
):
    if value:
        return extra
    return 0
"""
    relative = "cvat/apps/engine/existing.py"
    source = sample_repo / relative
    source.write_text(original)
    commit(sample_repo, "upstream declaration")
    git(sample_repo, "update-ref", "refs/remotes/upstream/master", "HEAD")
    changed = original.replace("extra=1", "extra=2") if change == "signature" else original.replace("@keep", "@alternative")
    source.write_text(changed)
    commit(sample_repo, "modify declaration")
    monkeypatch.chdir(sample_repo)
    cov = coverage.Coverage(
        config_file=False, data_file=str(tmp_path / ".coverage"), branch=True,
        source=["cvat/apps/engine"],
    )
    cov.start()
    try:
        assert runpy.run_path(relative)["configure"](False) == 0
    finally:
        cov.stop()
    cov.save()
    output = tmp_path / "declaration.json"
    cov.json_report(outfile=str(output))
    scope = quality.discover_scope(sample_repo)
    data = json.loads(output.read_text())
    configure, = quality.function_metrics(scope, data)
    assert configure["name"] == "configure"
    assert configure["complexity"] == 2
    assert configure["statements"] == 3
    assert configure["covered_statements"] == 2
    assert configure["crap"] == pytest.approx(2 + 4 / 27)


@pytest.mark.parametrize("deletion", ["branch_only", "whole_function"])
def test_pure_deletion_changes_functions_without_inventing_changed_lines(
    sample_repo, tmp_path, monkeypatch, deletion
):
    original = """def changed(flag):
    if flag:
        return 1
    return 2


def survivor(flag):
    if flag:
        return 3
    return 4
"""
    relative = "cvat/apps/engine/existing.py"
    source = sample_repo / relative
    source.write_text(original)
    expectation = (
        "assert values['changed'](False) == 2" if deletion == "branch_only"
        else "assert values['survivor'](True) == 3"
    )
    (sample_repo / "tests/test_sample.py").write_text(
        "from pathlib import Path\nimport runpy\n\n"
        "def test_deletion():\n"
        "    source = Path(__file__).resolve().parents[1] / 'cvat/apps/engine/existing.py'\n"
        "    values = runpy.run_path(str(source))\n"
        f"    {expectation}\n"
    )
    commit(sample_repo, "upstream before deletion")
    git(sample_repo, "update-ref", "refs/remotes/upstream/master", "HEAD")
    if deletion == "branch_only":
        source.write_text(original.replace("    if flag:\n        return 1\n", ""))
    else:
        source.write_text(original[original.index("def survivor"):])
    commit(sample_repo, "delete code without adding lines")
    monkeypatch.setattr(quality, "SUITES", {"tiny": ["tests/test_sample.py"]})
    output = tmp_path / "measurement"
    assert run_suite(sample_repo, output, "tiny")["status"] == "passed"
    summary = summarize(sample_repo, output)
    assert summary["scope"]["files"][relative]["changed_lines"] == []
    changed_lines = summary["totals"]["changed_lines"]
    assert changed_lines["statements"] == 0
    assert changed_lines["covered_statements"] == 0
    assert changed_lines["statement_coverage"] is None
    if deletion == "branch_only":
        changed, = summary["functions"]
        assert changed["name"] == "changed"
        assert changed["complexity"] == 1
        assert changed["statements"] == 1
        assert changed["covered_statements"] == 1
        assert changed["crap"] == 1
    else:
        assert summary["functions"] == []
        assert summary["totals"]["modified"]["covered_statements"] > 0
