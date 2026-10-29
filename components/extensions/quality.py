#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure extension Python code without loading deployment configuration."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree


SUITES = {
    name: [f"components/{name}/tests"]
    for name in (
        "extensions", "itgformat", "filtered_export", "task_guide", "dicom_import",
        "project_annotations", "model_registry", "sam2", "sam31", "ultrasam",
    )
}
SUITES["project_orm"] = ["components/project_annotations/tests/run_orm_tests.py"]
OPTIONAL_SUITES = {"project_orm"}
TOOL_VERSIONS = {"coverage": "7.13.5", "radon": "6.0.1"}
EXCLUDED_PARTS = {"tests", "examples", "migrations", "node_modules"}
JAVASCRIPT_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs"}
NOTES = [
    "Only Python is measured. UI TypeScript/JavaScript needs source-map-aware instrumentation.",
    "SAM3.1 tests also execute temporary source copies; their hits are not assigned to original files.",
    "Mocked dependencies and statement coverage do not prove assertion quality or deployed behavior.",
    "CRAP uses each function's statement coverage; branch coverage is reported separately.",
    "Raw coverage JSON includes full upstream file context. Use metrics.json for extension and changed-line totals.",
]


class QualityError(RuntimeError):
    """Invalid measurement scope, environment, or stored result."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise QualityError(result.stderr.strip() or "Git command failed")
    return result.stdout


def _excluded(path: str) -> bool:
    item = Path(path)
    return (
        bool(EXCLUDED_PARTS.intersection(item.parts))
        or item.name.startswith(("test_", "_test_"))
        or item.name == "conftest.py"
        or path == "components/extensions/quality.py"
    )


def discover_scope(root: Path, upstream: str = "upstream/master") -> dict:
    """Find added code and modified upstream code, including working-tree edits."""
    root = Path(root).resolve()
    upstream_sha = _git(root, "rev-parse", "--verify", "--end-of-options", f"{upstream}^{{commit}}").strip()
    base = _git(root, "merge-base", "HEAD", upstream_sha).strip()
    fields = _git(root, "diff", "--name-status", "--no-renames", "-z", base, "--").split("\0")
    changes = {fields[i + 1]: fields[i] for i in range(0, len(fields) - 1, 2)}
    for path in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if path:
            changes[path] = "A"
    files, unmeasured, fingerprint = {}, [], []
    for path, status in sorted(changes.items()):
        source = root / path
        if {"__pycache__", ".pytest_cache"}.intersection(source.parts) or source.suffix in {".pyc", ".pyo"}:
            continue
        # Include test code, fixtures and dependencies in the snapshot, while
        # allowing documentation-only edits without invalidating measurements.
        digest = None
        if source.is_symlink():
            digest = hashlib.sha256(os.readlink(source).encode()).hexdigest()
        elif source.is_file():
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if source.suffix != ".md" and source.parts[len(root.parts):len(root.parts) + 1] not in (("docs",), ("site",)):
            fingerprint.append((path, status, digest))
        if digest is None or _excluded(path) or source.suffix not in {".py", *JAVASCRIPT_SUFFIXES}:
            continue
        if source.is_symlink():
            raise QualityError(f"Measurement source must be a regular file: {path}")
        kind = "added" if status == "A" else "modified"
        if source.suffix != ".py":
            unmeasured.append({"path": path, "kind": kind, "reason": "JavaScript/TypeScript instrumentation is not configured"})
            continue
        functions_with_deleted_lines = []
        if kind == "added":
            changed_lines = list(range(1, len(source.read_text().splitlines()) + 1))
        else:
            diff = _git(root, "diff", "--no-ext-diff", "--no-color", "--unified=0", base, "--", path)
            changed_lines = []
            deleted_lines = set()
            hunks = re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE)
            for old_start, old_count, new_start, new_count in hunks:
                changed_lines.extend(range(int(new_start), int(new_start) + (int(new_count) if new_count else 1)))
                deleted_lines.update(range(int(old_start), int(old_start) + (int(old_count) if old_count else 1)))
            if deleted_lines:
                previous = ast.parse(_git(root, "show", f"{base}:{path}"), filename=path)
                for name, node in _functions(previous):
                    start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
                    if any(start <= line <= node.end_lineno for line in deleted_lines):
                        functions_with_deleted_lines.append(name)
        files[path] = {
            "kind": kind, "changed_lines": changed_lines, "sha256": digest,
            "functions_with_deleted_lines": functions_with_deleted_lines,
        }
    snapshot = hashlib.sha256(json.dumps([base, fingerprint], ensure_ascii=False).encode()).hexdigest()
    return {
        "root": str(root), "head": _git(root, "rev-parse", "HEAD").strip(),
        "upstream": upstream, "upstream_sha": upstream_sha, "base": base,
        "fingerprint": snapshot, "files": files, "unmeasured": unmeasured,
    }


def validate_output(root: Path, output: Path) -> Path:
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(Path(root).resolve()):
        raise QualityError("Quality output must be outside the Git checkout")
    return output


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _require_tools() -> None:
    for name, expected in TOOL_VERSIONS.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "not installed"
        if actual != expected:
            raise QualityError(f"Requires {name}=={expected}; found {actual}")


def crap_score(complexity: int, coverage_fraction: float) -> float:
    if complexity < 1 or not math.isfinite(coverage_fraction) or not 0 <= coverage_fraction <= 1:
        raise QualityError("CRAP requires positive complexity and coverage between 0 and 1")
    return complexity ** 2 * (1 - coverage_fraction) ** 3 + complexity


def _counts(summary: dict) -> dict:
    statements, branches = summary["num_statements"], summary.get("num_branches", 0)
    covered, covered_branches = summary["covered_lines"], summary.get("covered_branches", 0)
    return {
        "statements": statements, "covered_statements": covered,
        "statement_coverage": covered / statements if statements else None,
        "branches": branches, "covered_branches": covered_branches,
        "branch_coverage": covered_branches / branches if branches else None,
    }


def _coverage_files(scope: dict, coverage_report: dict) -> dict:
    root = Path(scope["root"])
    targets = {(root / name).resolve(): name for name in scope["files"]}
    result = {}
    for name, value in coverage_report["files"].items():
        for path in ((root / name).resolve(), Path(name).resolve()):
            if path in targets:
                result[targets[path]] = value
                break
    missing = set(scope["files"]) - result.keys()
    if missing:
        raise QualityError(f"Coverage report omits source files: {sorted(missing)}")
    return result


def _functions(tree: ast.AST, prefix: str = ""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}.{node.name}" if prefix else node.name
            if not isinstance(node, ast.ClassDef):
                yield name, node
            yield from _functions(node, name)
        else:
            yield from _functions(node, prefix)


def function_metrics(scope: dict, coverage_report: dict) -> list[dict]:
    """Match actual per-function coverage with Radon, including nested functions."""
    _require_tools()
    from radon.complexity import cc_visit_ast

    coverage_files = _coverage_files(scope, coverage_report)
    rows = []
    for path, item in scope["files"].items():
        tree = ast.parse((Path(scope["root"]) / path).read_text(), filename=path)
        regions = coverage_files[path]["functions"]
        for name, node in _functions(tree):
            start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            changed = any(start <= line <= node.end_lineno for line in item["changed_lines"])
            # Deleted lines have no current executable-line denominator. Match
            # their old function by name, not an adjacent line in the new file.
            if item["kind"] == "modified" and not changed and name not in item.get("functions_with_deleted_lines", []):
                continue
            region = regions.get(name)
            if region is None or region.get("start_line") != node.lineno:
                raise QualityError(f"Cannot match function coverage by name and line: {path}:{node.lineno} {name}")
            # Radon separates closure complexity. Analyze each named function
            # independently; class aggregates and global file coverage are unused.
            block = cc_visit_ast(ast.Module(body=[node], type_ignores=[]))[0]
            counts = _counts(region["summary"])
            fraction = counts["statement_coverage"]
            rows.append({
                "path": path, "name": name, "line": node.lineno, "kind": item["kind"],
                "complexity": block.complexity, **counts,
                "crap": crap_score(block.complexity, fraction) if fraction is not None else None,
            })
    return sorted(rows, key=lambda row: (-(row["crap"] or 0), row["path"], row["line"]))


def _config(path: Path, scope: dict, data_file: Path) -> None:
    include = "\n".join("    " + str(Path(scope['root']) / name).replace("$", "$$") for name in scope["files"])
    data_file = str(data_file).replace("$", "$$")
    path.write_text(
        f"[run]\nbranch = true\nparallel = true\ndata_file = {data_file}\n"
        f"include =\n{include}\npatch =\n    subprocess\n    execv\n"
        "[report]\nignore_errors = false\nskip_empty = false\n"
    )


def run_suite(root: Path, suite: str, python: str, output: Path, upstream: str = "upstream/master") -> dict:
    if suite not in SUITES:
        raise QualityError(f"Unknown suite: {suite}")
    root = Path(root).resolve()
    output = validate_output(root, output)
    scope = discover_scope(root, upstream)
    if not scope["files"]:
        raise QualityError("No extension Python source files were found")
    run_id = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    directory = validate_output(root, output / suite / run_id)
    directory.mkdir(parents=True, mode=0o700)
    config = directory / "coverage.ini"
    _config(config, scope, directory / ".coverage")
    command = [str(python), "-m", "coverage", "run", f"--rcfile={config}", "-m", "pytest", "-q", f"--junitxml={directory / 'junit.xml'}", *SUITES[suite]]
    manifest = {
        "version": 1, "suite": suite, "run_id": run_id, "scope": scope,
        "command": command, "python": str(python), "packages": {}, "status": "running",
        "exit_code": None, "coverage_files": [], "log": "pytest.log",
        "pytest_exit_code": None, "tests": None, "failures": None, "errors": None, "skipped": None,
    }
    _write_json(directory / "manifest.json", manifest)
    env = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), env.get("PYTHONPATH", "")]))
    probe = (
        "import sys,json,importlib.metadata as m;print(json.dumps({"
        "'python':sys.version,'executable':sys.executable,"
        "'packages':{d.metadata['Name'].lower():d.version for d in m.distributions()}}))"
    )
    try:
        result = subprocess.run([str(python), "-c", probe], capture_output=True, text=True, env=env, check=True)
        environment = json.loads(result.stdout)
        manifest.update(environment)
        if manifest["packages"].get("coverage") != TOOL_VERSIONS["coverage"]:
            raise QualityError(f"Test Python requires coverage=={TOOL_VERSIONS['coverage']}")
        if "pytest" not in manifest["packages"]:
            raise QualityError("Test Python requires pytest")
        with (directory / "pytest.log").open("w") as log:
            completed = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
        manifest["exit_code"] = completed.returncode
        manifest["pytest_exit_code"] = completed.returncode
        xml = directory / "junit.xml"
        if xml.is_file():
            suites = ElementTree.parse(xml).getroot().iter("testsuite")
            counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
            for entry in suites:
                for key in counts:
                    counts[key] += int(entry.get(key, "0"))
            manifest.update(counts)
        manifest["coverage_files"] = sorted(path.name for path in directory.glob(".coverage.*") if path.is_file())
        if discover_scope(root, upstream)["fingerprint"] != scope["fingerprint"]:
            raise QualityError("Source changed while the suite was running; rerun the suite")
        if completed.returncode == 0 and not manifest["coverage_files"]:
            raise QualityError("The successful test run did not produce coverage data")
        if completed.returncode == 0 and (manifest["tests"] is None or manifest["tests"] <= manifest["skipped"]):
            raise QualityError("No test cases executed: JUnit results are missing or all cases were skipped")
        manifest["status"] = "passed" if completed.returncode == 0 else "failed"
    except (OSError, subprocess.SubprocessError, ValueError, QualityError, ElementTree.ParseError) as exc:
        manifest.update(status="failed", exit_code=manifest["exit_code"] or 1, error=str(exc))
    finally:
        _write_json(directory / "manifest.json", manifest)
    manifest["manifest_path"] = str(directory / "manifest.json")
    return manifest


def _totals(scope: dict, coverage_report: dict) -> dict:
    files = _coverage_files(scope, coverage_report)
    totals = {
        name: {"num_statements": 0, "covered_lines": 0, "num_branches": 0, "covered_branches": 0}
        for name in ("added", "modified", "changed_lines")
    }
    for path, item in scope["files"].items():
        data = files[path]
        for key in totals[item["kind"]]:
            totals[item["kind"]][key] += data["summary"].get(key, 0)
        if item["kind"] == "modified":
            changed = set(item["changed_lines"])
            totals["changed_lines"]["covered_lines"] += len(changed.intersection(data["executed_lines"]))
            totals["changed_lines"]["num_statements"] += len(changed.intersection(set(data["executed_lines"]) | set(data["missing_lines"])))
            hit = [arc for arc in data.get("executed_branches", []) if arc[0] in changed]
            missed = [arc for arc in data.get("missing_branches", []) if arc[0] in changed]
            totals["changed_lines"]["covered_branches"] += len(hit)
            totals["changed_lines"]["num_branches"] += len(hit) + len(missed)
    return {name: _counts(value) for name, value in totals.items()}


def report(root: Path, output: Path, upstream: str = "upstream/master", suites: list[str] | None = None, max_crap: float | None = None) -> dict:
    _require_tools()
    import coverage

    root = Path(root).resolve()
    output = validate_output(root, output)
    scope = discover_scope(root, upstream)
    selected = list(SUITES) if suites is None else list(dict.fromkeys(suites))
    if set(selected) - SUITES.keys():
        raise QualityError(f"Unknown suites: {sorted(set(selected) - SUITES.keys())}")
    if max_crap is not None and (not math.isfinite(max_crap) or max_crap < 0):
        raise QualityError("--max-crap must be a finite nonnegative number")
    passed, failed, data_files, runs = [], [], [], []
    for suite in selected:
        candidates = sorted((output / suite).glob("*/manifest.json"))
        if not candidates:
            continue
        manifest_path = candidates[-1]
        manifest = json.loads(manifest_path.read_text())
        if manifest["scope"]["fingerprint"] != scope["fingerprint"]:
            raise QualityError(f"Stored source snapshot differs for {suite}; rerun it or select other suites")
        runs.append({
            "suite": suite, "manifest": str(manifest_path), "status": manifest["status"],
            **{key: manifest.get(key) for key in ("tests", "failures", "errors", "skipped")},
        })
        if manifest["status"] != "passed" or manifest["exit_code"] != 0:
            failed.append(suite)
            continue
        paths = [manifest_path.parent / name for name in manifest["coverage_files"]]
        if not paths or any(not path.is_file() or path.resolve().parent != manifest_path.parent.resolve() for path in paths):
            raise QualityError(f"Missing or invalid coverage data for {suite}")
        data_files.extend(map(str, paths))
        passed.append(suite)
    if not passed:
        raise QualityError(f"No successful current-source suites to report; failed: {failed or 'none'}")
    directory = validate_output(root, output / "report")
    directory.mkdir(parents=True, exist_ok=True)
    cov = coverage.Coverage(config_file=False, branch=True, data_file=str(directory / ".coverage"))
    cov.erase()
    cov.combine(data_paths=data_files, strict=True, keep=True)
    cov.save()
    sources = [str(root / path) for path in scope["files"]]
    coverage_path = directory / "coverage.json"
    cov.json_report(morfs=sources, outfile=str(coverage_path))
    for kind in ("added", "modified"):
        context = [str(root / path) for path, item in scope["files"].items() if item["kind"] == kind]
        if context:
            title = "Independent extension files" if kind == "added" else "Modified upstream files: full context"
            cov.html_report(morfs=context, directory=str(directory / "html" / kind), title=title)
    data = json.loads(coverage_path.read_text())
    functions = function_metrics(scope, data)
    missing = [name for name in SUITES if name not in OPTIONAL_SUITES and name not in passed and name not in failed]
    missing += [name for name in selected if name in OPTIONAL_SUITES and name not in passed and name not in failed and suites is not None]
    violations = [row for row in functions if max_crap is not None and row["crap"] is not None and row["crap"] > max_crap]
    partial = bool(missing or failed)
    result = {
        "version": 1, "scope": scope, "tools": TOOL_VERSIONS, "passed": passed,
        "failed": failed, "missing": missing, "partial": partial,
        "optional_not_run": [name for name in OPTIONAL_SUITES if name not in passed],
        "runs": runs, "totals": _totals(scope, data), "functions": functions,
        "unmeasured": scope["unmeasured"], "notes": NOTES,
        "max_crap": max_crap, "violations": violations,
        "gate_passed": not violations and (max_crap is None or not partial),
    }
    _write_json(directory / "metrics.json", result)
    with (directory / "functions.csv").open("w", newline="") as stream:
        fields = ["path", "name", "line", "kind", "complexity", "statements", "covered_statements", "statement_coverage", "branches", "covered_branches", "branch_coverage", "crap"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(functions)
    return result


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["quality"]:
        argv.pop(0)
    parser = argparse.ArgumentParser(prog="cvatctl quality", description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("scope", "run", "report"):
        action = actions.add_parser(name)
        action.add_argument("--upstream", default="upstream/master")
        if name == "scope":
            action.add_argument("--json", action="store_true", help="Include the detailed source snapshot and changed line numbers")
        else:
            action.add_argument("--output", type=Path, required=True)
        if name == "run":
            action.add_argument("--suite", choices=list(SUITES), required=True)
            action.add_argument("--python", default=sys.executable)
        elif name == "report":
            action.add_argument("--suite", action="append", choices=list(SUITES))
            action.add_argument("--max-crap", type=float)
    args = parser.parse_args(argv)
    root = Path(root or Path(__file__).resolve().parents[2])
    try:
        if args.action == "scope":
            scope = discover_scope(root, args.upstream)
            if args.json:
                print(json.dumps(scope, ensure_ascii=False, indent=2))
            else:
                print(f"Upstream base: {scope['base']}; source snapshot: {scope['fingerprint']}")
                print(f"Python files: {len(scope['files'])}")
                for path, item in scope["files"].items():
                    print(f"  {item['kind']}: {path}")
                print(f"Unmeasured JavaScript/TypeScript files: {len(scope['unmeasured'])}")
                for item in scope["unmeasured"]:
                    print(f"  {item['kind']}: {item['path']}")
        elif args.action == "run":
            result = run_suite(root, args.suite, args.python, args.output, args.upstream)
            print(f"{result['suite']}: {result['status']}; {result['manifest_path']}")
            print(f"Tests: {result['tests']}; failures: {result['failures']}; errors: {result['errors']}; skipped: {result['skipped']}")
            if result.get("error"):
                print(result["error"], file=sys.stderr)
            return result["exit_code"]
        else:
            result = report(root, args.output, args.upstream, args.suite, args.max_crap)
            print(f"Python report: {'partial' if result['partial'] else 'all standard suites completed'}")
            print(f"Passed: {result['passed']}; failed: {result['failed']}; missing: {result['missing']}")
            for run in result["runs"]:
                print(f"  {run['suite']}: tests={run['tests']}, skipped={run['skipped']}, failures={run['failures']}, errors={run['errors']}")
            for kind in ("added", "changed_lines"):
                total = result["totals"][kind]
                line = total["statement_coverage"]
                branch = total["branch_coverage"]
                print(f"{kind}: {total['covered_statements']}/{total['statements']} statements ({line:.1%})" if line is not None else f"{kind}: no executable statements")
                if branch is not None:
                    print(f"  branches: {total['covered_branches']}/{total['branches']} ({branch:.1%})")
            print(f"Unmeasured UI files: {len(result['unmeasured'])}; report: {args.output / 'report'}")
            for item in result["functions"][:10]:
                if item["crap"] is not None:
                    print(f"CRAP {item['crap']:.2f}, CC {item['complexity']}: {item['path']}:{item['line']} {item['name']}")
            return 1 if result["failed"] or not result["gate_passed"] else 0
    except (QualityError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
