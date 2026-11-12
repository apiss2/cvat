# SPDX-License-Identifier: MIT
"""Exercise the official quality migrations against stored pre-upgrade values.

Run separately with the CVAT server dependencies:
    python -m pytest -q components/extensions/tests/run_migration_tests.py

MigrationExecutor creates the real historical schema in an isolated SQLite
database, then applies the repository's original migrations. This is not a
PostgreSQL upgrade or a restored production database test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from components.extensions.tests.cvat_backend import configure_cvat

_RUNTIME = configure_cvat(prefix="cvat-upgrade-migrations-", create_schema=False)

from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

SOURCE_MIGRATION = ("quality_control", "0014_rename_consensus_replica_in_job_filter")
TARGET_MIGRATION = ("quality_control", "0018_qualityrequirement_boundary_f")
SIMILARITY_METRICS = {
    "tag": "equality",
    "points": "oks",
    "skeleton": "oks",
    "skeleton_keypoint": "oks",
    "rectangle": "iou",
    "polygon": "iou",
    "mask": "iou",
    "polyline": "iou",
    "ellipse": "iou",
}


def migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


@pytest.fixture
def old_quality():
    # Each test starts at the same historical state. Remove previous test
    # requirements first so a policy used by the rollback test cannot prevent
    # restoring the old schema for another independent test.
    table = "quality_control_qualityrequirement"
    if table in connection.introspection.table_names():
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(table)}")
    apps = migrate(SOURCE_MIGRATION)
    project = apps.get_model("engine", "Project").objects.create(name="Before upgrade")
    settings = apps.get_model("quality_control", "QualitySettings").objects.create(
        project=project, max_validations_per_job=0
    )
    return SimpleNamespace(
        model=apps.get_model("quality_control", "QualityRequirement"), settings=settings
    )


def test_quality_upgrade_preserves_scalar_thresholds_and_inheritance(old_quality, subtests):
    before = old_quality.model
    expected = {}
    for annotation_type, metric in SIMILARITY_METRICS.items():
        root = before.objects.create(
            settings=old_quality.settings,
            name=f"Base {annotation_type}",
            annotation_type=annotation_type,
            iou_threshold=0.61,
            target_metric="accuracy",
            target_metric_threshold=0.77,
            enabled=False,
            filter='{"==":[{"var":"label"},"target"]}',
        )
        child = before.objects.create(
            settings=old_quality.settings,
            name=f"Child {annotation_type}",
            parent=root,
            iou_threshold=0.72,
        )
        grandchild = before.objects.create(
            settings=old_quality.settings,
            name=f"Grandchild {annotation_type}",
            parent=child,
            iou_threshold=0.83,
        )
        inherited = before.objects.create(
            settings=old_quality.settings,
            name=f"Inherited {annotation_type}",
            parent=root,
            iou_threshold=None,
        )
        expected[root.pk] = (metric, 0.61, 0.8, 5.0)
        expected[child.pk] = (None, 0.72 if metric == "oks" else None, None, None)
        expected[grandchild.pk] = (None, 0.83 if metric == "oks" else None, None, None)
        expected[inherited.pk] = (None, None, None, None)

    null_root = before.objects.create(
        settings=old_quality.settings,
        name="Unset root threshold",
        annotation_type="points",
        iou_threshold=None,
    )
    expected[null_root.pk] = ("oks", 0.4, 0.8, 5.0)
    old_fields = [field.attname for field in before._meta.local_fields]
    stored = list(before.objects.order_by("id").values(*old_fields))

    apps = migrate(TARGET_MIGRATION)
    after = apps.get_model("quality_control", "QualityRequirement")
    # IDs, relationships, scalar/NULL IoU, names, filters and timestamps survive.
    assert list(after.objects.order_by("id").values(*old_fields)) == stored
    for requirement in after.objects.order_by("id"):
        with subtests.test(requirement=requirement.name):
            assert (
                requirement.similarity_metric,
                requirement.oks_threshold,
                requirement.boundary_f_threshold,
                requirement.boundary_f_tolerance,
            ) == expected[requirement.pk]


def test_quality_rollback_preserves_changed_point_overrides(old_quality, subtests):
    rows = []
    for annotation_type in ("points", "skeleton", "skeleton_keypoint", "rectangle"):
        root = old_quality.model.objects.create(
            settings=old_quality.settings,
            name=f"Base {annotation_type}",
            annotation_type=annotation_type,
            iou_threshold=0.61,
        )
        child = old_quality.model.objects.create(
            settings=old_quality.settings,
            name=f"Child {annotation_type}",
            parent=root,
            iou_threshold=0.72,
        )
        grandchild = old_quality.model.objects.create(
            settings=old_quality.settings,
            name=f"Grandchild {annotation_type}",
            parent=child,
            iou_threshold=None,
        )
        rows.append((annotation_type, root.pk, child.pk, grandchild.pk))

    apps = migrate(TARGET_MIGRATION)
    upgraded = apps.get_model("quality_control", "QualityRequirement")
    for _, root_id, child_id, grandchild_id in rows:
        upgraded.objects.filter(pk=root_id).update(oks_threshold=0.91)
        upgraded.objects.filter(pk=child_id).update(oks_threshold=0.92)
        upgraded.objects.filter(pk=grandchild_id).update(oks_threshold=0.93)

    restored = migrate(SOURCE_MIGRATION).get_model("quality_control", "QualityRequirement")
    for annotation_type, root_id, child_id, grandchild_id in rows:
        with subtests.test(annotation_type=annotation_type):
            expected = (0.61, 0.72, None) if annotation_type == "rectangle" else (0.91, 0.92, 0.93)
            assert (
                tuple(
                    restored.objects.get(pk=pk).iou_threshold
                    for pk in (root_id, child_id, grandchild_id)
                )
                == expected
            )


def test_quality_policy_survives_upgrade_and_blocks_lossy_rollback(old_quality):
    root = old_quality.model.objects.create(
        settings=old_quality.settings,
        name="Variable threshold",
        annotation_type="rectangle",
        iou_threshold=0.61,
    )
    policy_migration = ("quality_control", "0015_qualityrequirement_threshold_policy")
    policy = {"type": "iou", "mode": "constant", "value": 0.7}
    intermediate = migrate(policy_migration).get_model("quality_control", "QualityRequirement")
    intermediate.objects.filter(pk=root.pk).update(iou_threshold=policy)

    upgraded = migrate(TARGET_MIGRATION).get_model("quality_control", "QualityRequirement")
    saved = upgraded.objects.get(pk=root.pk)
    assert saved.iou_threshold == policy
    assert saved.oks_threshold == 0.4
    assert saved.similarity_metric == "iou"

    with pytest.raises(IrreversibleError, match="Convert IoU thresholds to numbers or NULL"):
        migrate(SOURCE_MIGRATION)
    executor = MigrationExecutor(connection)
    assert policy_migration in executor.loader.applied_migrations
    remaining = executor.loader.project_state([policy_migration]).apps.get_model(
        "quality_control", "QualityRequirement"
    )
    assert remaining.objects.get(pk=root.pk).iou_threshold == policy


def test_quality_upgrade_preserves_existing_conflicts():
    old_apps = migrate(TARGET_MIGRATION)
    project = old_apps.get_model("engine", "Project").objects.create(name="Before upgrade")
    report = old_apps.get_model("quality_control", "QualityReport").objects.create(
        project=project, target_last_updated=timezone.now(), data={}
    )
    old_conflicts = old_apps.get_model("quality_control", "AnnotationConflict").objects
    conflict = old_conflicts.create(
        report=report,
        frame=7,
        type="mismatching_attributes",
        severity="warning",
        attribute_names=["visibility"],
    )
    old_row = old_conflicts.values().get(pk=conflict.pk)
    old_apps.get_model("quality_control", "AnnotationId").objects.create(
        conflict=conflict, obj_id=42, job_id=3, type="tag"
    )

    apps = migrate(("quality_control", "0019_annotationconflict_requirements"))
    new_conflicts = apps.get_model("quality_control", "AnnotationConflict").objects
    assert new_conflicts.values().get(pk=conflict.pk) == {**old_row, "requirements": []}
    assert new_conflicts.get(pk=conflict.pk).annotation_ids.get().obj_id == 42
