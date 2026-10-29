# SPDX-License-Identifier: MIT
"""Run orchestration and deletion adapters with real SQLite rollback boundaries.

CVAT ORM queries, RQ and OPA are small in-memory adapters. Annotation/attribute
rows and transactions are real SQLite; this is not a PostgreSQL integration test.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from components.project_annotations import ANNOTATION_KEYS, PROTECTED_LABEL
from components.project_annotations.dicom import plan_records

ROOT = Path(__file__).resolve().parents[1]


class JobTimeoutException(Exception):
    """RQ timeout stand-in for the dependency-free orchestration tests."""


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def resolve(row, key):
    if key == "pk":
        key = "id"
    if isinstance(row, dict) and key in row:
        return row[key]
    value = row
    for part in key.split("__"):
        value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
    return value


def matches(row, conditions):
    for key, expected in conditions.items():
        if key.endswith("__in"):
            if resolve(row, key[:-4]) not in expected:
                return False
        elif key.endswith("__regex"):
            if re.search(expected, resolve(row, key[:-7])) is None:
                return False
        elif resolve(row, key) != expected:
            return False
    return True


class Q:
    def __init__(self, **conditions):
        self.predicate = lambda row: matches(row, conditions)

    def __or__(self, other):
        result = Q()
        result.predicate = lambda row: self.predicate(row) or other.predicate(row)
        return result


class Query(list):
    def __init__(self, values=(), locks=None):
        super().__init__(values)
        self.locks = locks

    def filter(self, *conditions, **kwargs):
        return Query(
            [
                row
                for row in self
                if matches(row, kwargs) and all(q.predicate(row) for q in conditions)
            ],
            self.locks,
        )

    def exclude(self, **kwargs):
        return Query([row for row in self if not matches(row, kwargs)], self.locks)

    def select_related(self, *args):
        return self

    def annotate(self, **kwargs):
        return Query(
            [
                dict(
                    row,
                    **{name: resolve(row, source).strip(" ") for name, source in kwargs.items()},
                )
                for row in self
            ],
            self.locks,
        )

    def distinct(self):
        result = Query()
        for row in self:
            if row not in result:
                result.append(row)
        return result

    def select_for_update(self):
        if self.locks is not None:
            self.locks.append(tuple(resolve(row, "id") for row in self))
        return self

    def order_by(self, *keys):
        return Query(
            sorted(self, key=lambda row: tuple(resolve(row, key) for key in keys)), self.locks
        )

    def values_list(self, *keys, flat=False):
        return Query(
            [
                resolve(row, keys[0]) if flat else tuple(resolve(row, key) for key in keys)
                for row in self
            ]
        )

    def iterator(self):
        return iter(self)

    def get(self, **kwargs):
        found = self.filter(**kwargs)
        if len(found) != 1:
            raise LookupError("Object is missing or ambiguous")
        return found[0]

    def first(self):
        return self[0] if self else None


class Manager:
    def __init__(self, rows, locks=None):
        self.rows, self.locks = rows, locks

    def __getattr__(self, name):
        return getattr(Query(self.rows(), self.locks), name)


class Harness:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE annotations (
                id INTEGER PRIMARY KEY, job_id INTEGER, task_id INTEGER,
                kind TEXT, frame INTEGER, label_id INTEGER, payload TEXT
            );
            CREATE TABLE attributes (
                id INTEGER PRIMARY KEY, annotation_id INTEGER REFERENCES annotations(id) ON DELETE CASCADE,
                spec_id INTEGER, value TEXT
            );
        """)
        self.locks, self.events = [], []
        self.fail_create = set()
        self.allow_project, self.active_user = True, True
        self.denied_tasks = set()
        self.labels = [
            {
                "id": 10,
                "name": "Quality",
                "type": "tag",
                "parent": None,
                "attributes": [
                    {
                        "id": 20,
                        "name": "quality",
                        "input_type": "select",
                        "values": ["good", "poor"],
                    }
                ],
            },
            {"id": 11, "name": "Finding", "type": "any", "parent": None, "attributes": []},
            {
                "id": 99,
                "name": PROTECTED_LABEL,
                "type": "tag",
                "parent": None,
                "attributes": [
                    {"id": index, "name": name, "input_type": "text", "values": []}
                    for index, name in enumerate(
                        ("StudyInstanceUID", "SOPInstanceUID", "PatientID", "StudyDate"), 101
                    )
                ],
            },
        ]
        self.tasks = {
            task_id: types.SimpleNamespace(
                id=task_id,
                pk=task_id,
                name=f"Task{task_id}",
                project_id=1,
                data_id=task_id,
                data=types.SimpleNamespace(size=2, validation_mode="none"),
                mode="annotation",
                dimension="2d",
            )
            for task_id in (7, 8)
        }
        self.jobs = [
            types.SimpleNamespace(id=task_id * 10, task_id=task_id, type="annotation")
            for task_id in self.tasks
        ]
        self.project = types.SimpleNamespace(
            id=1,
            pk=1,
            organization_id=None,
            tasks=Manager(lambda: list(self.tasks.values())),
            label_set=Manager(lambda: self.labels),
        )
        self.user = types.SimpleNamespace(id=1, is_active=True)
        self.meta = types.SimpleNamespace(user=self.user, org_id=None, progress=0, status="")
        self.meta.save = lambda: self.events.append(("progress", self.meta.progress))
        self.seed(7, 70, 0, 99, self.identity("1.2.3", "1.2.3.4"))
        self.seed(7, 70, 1, 99, self.identity("1.2.3", "1.2.3.5"))
        self.seed(8, 80, 0, 99, self.identity("2.3.4", "2.3.4.5"))
        self.seed(8, 80, 1, 99, self.identity("2.3.4", "2.3.4.6"))
        self.seed(7, 70, 1, 11)
        self.seed(8, 80, 1, 11)

    @staticmethod
    def identity(study, sop):
        return [
            {"spec_id": spec_id, "value": value}
            for spec_id, value in zip((101, 102, 103, 104), (study, sop, "P001", "20250101"))
        ]

    def seed(self, task_id, job_id, frame, label_id, attributes=(), kind="tags"):
        item = {"frame": frame, "label_id": label_id, "attributes": list(attributes)}
        cursor = self.db.execute(
            "INSERT INTO annotations(job_id,task_id,kind,frame,label_id,payload) VALUES(?,?,?,?,?,?)",
            (job_id, task_id, kind, frame, label_id, json.dumps(item)),
        )
        for attribute in attributes:
            self.db.execute(
                "INSERT INTO attributes(annotation_id,spec_id,value) VALUES(?,?,?)",
                (cursor.lastrowid, attribute["spec_id"], attribute["value"]),
            )
        return cursor.lastrowid

    def saved(self, task_id=None):
        annotation_rows = self.db.execute(
            "SELECT * FROM annotations WHERE (? IS NULL OR task_id=?) ORDER BY id",
            (task_id, task_id),
        ).fetchall()
        ids = {row[0] for row in annotation_rows}
        attribute_rows = [
            row for row in self.db.execute("SELECT * FROM attributes ORDER BY id") if row[1] in ids
        ]
        return annotation_rows, attribute_rows

    def tags(self, task_id):
        return [
            dict(json.loads(row[6]), id=row[0])
            for row in self.saved(task_id)[0]
            if row[3] == "tags"
        ]

    def model_tags(self):
        result = []
        names = {label["id"]: label["name"] for label in self.labels}
        for row in self.db.execute("SELECT id,job_id,task_id,kind,frame,label_id FROM annotations"):
            annotation_id, job_id, task_id, kind, frame, label_id = row
            if kind != "tags":
                continue
            job = next(job for job in self.jobs if job.id == job_id)
            result.append(
                {
                    "id": annotation_id,
                    "job_id": job_id,
                    "frame": frame,
                    "label_id": label_id,
                    "job__segment__task_id": task_id,
                    "job__segment__task__project_id": 1,
                    "job__type": job.type,
                    "job__segment__task__data__validation_layout__mode": self.tasks[
                        task_id
                    ].data.validation_mode,
                    "label__project_id": 1,
                    "label__name": names[label_id],
                }
            )
        return result

    def model_attributes(self):
        tags = {tag["id"]: tag for tag in self.model_tags()}
        specs = {
            spec["id"]: (label["name"], spec["name"])
            for label in self.labels
            for spec in label["attributes"]
        }
        return [
            {
                "image": tags[annotation_id],
                "image__job__segment__task_id": tags[annotation_id]["job__segment__task_id"],
                "image__job__segment__task__project_id": 1,
                "image__label__project_id": 1,
                "image__label__name": tags[annotation_id]["label__name"],
                "image__frame": tags[annotation_id]["frame"],
                "spec__label__project_id": 1,
                "spec__label__name": specs[spec_id][0],
                "spec__name": specs[spec_id][1],
                "value": value,
            }
            for annotation_id, spec_id, value in self.db.execute(
                "SELECT annotation_id,spec_id,value FROM attributes"
            )
            if annotation_id in tags
        ]

    @contextmanager
    def atomic(self):
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.rollback()
            self.events.append(("rollback",))
            raise
        else:
            self.db.commit()
            self.events.append(("commit",))

    @contextmanager
    def services(self):
        harness = self

        class TaskAnnotation:
            def __init__(self, task_id, write_only=True):
                self.db_task = harness.tasks[task_id]
                self.db_jobs = Query(
                    [job for job in harness.jobs if job.task_id == task_id], harness.locks
                )

            def _validate_input_annotations(self, payload):
                harness.events.append(("validate", self.db_task.id))

            def create(self, payload):
                assert harness.db.in_transaction
                harness.events.append(("create", self.db_task.id))
                for job in self.db_jobs:
                    for kind in ANNOTATION_KEYS:
                        for item in payload.get(kind, []):
                            harness.seed(
                                self.db_task.id,
                                job.id,
                                item["frame"],
                                item["label_id"],
                                item.get("attributes", []),
                                kind,
                            )
                            if self.db_task.id in harness.fail_create:
                                raise RuntimeError("sensitive decoder content must not be logged")

        class JobAnnotation:
            def __init__(self, job_id, db_job=None):
                self.id = job_id

            def init_from_db(self):
                self.data = {key: [] for key in ANNOTATION_KEYS}
                for row in harness.db.execute(
                    "SELECT id,kind,payload FROM annotations WHERE job_id=?", (self.id,)
                ):
                    self.data[row[1]].append(dict(json.loads(row[2]), id=row[0]))

            def delete(self, removal):
                assert harness.db.in_transaction
                harness.events.append(("delete", self.id))
                for items in removal.values():
                    for item in items:
                        harness.db.execute("DELETE FROM annotations WHERE id=?", (item["id"],))

        class LabeledDataSerializer:
            def __init__(self, data, **kwargs):
                self.validated_data = {
                    key: copy.deepcopy(data.get(key, [])) for key in ANNOTATION_KEYS
                }

            def is_valid(self, **kwargs):
                return True

        class LabelSerializer:
            def __init__(self, labels, many=False):
                self.data = copy.deepcopy(list(labels))

            @staticmethod
            def _validate_attribute_value(kind, value, choices):
                if kind == "select" and value not in choices.split("\n"):
                    raise ValueError("Invalid attribute value")

        class ProjectPermission:
            Scopes = types.SimpleNamespace(IMPORT_DATASET="import:dataset")

            @staticmethod
            def create_base_perm(*args, **kwargs):
                return types.SimpleNamespace(
                    check_access=lambda: types.SimpleNamespace(allow=harness.allow_project)
                )

        class TaskPermission:
            Scopes = types.SimpleNamespace(IMPORT_ANNOTATIONS="import:annotations")

            @staticmethod
            def create_base_perm(*args, obj, **kwargs):
                return types.SimpleNamespace(
                    check_access=lambda: types.SimpleNamespace(
                        allow=obj.id not in harness.denied_tasks
                    )
                )

        models = types.SimpleNamespace(
            Project=types.SimpleNamespace(objects=Manager(lambda: [self.project], self.locks)),
            Task=types.SimpleNamespace(
                objects=Manager(lambda: list(self.tasks.values()), self.locks)
            ),
            LabeledImage=types.SimpleNamespace(objects=Manager(self.model_tags)),
            LabeledImageAttributeVal=types.SimpleNamespace(objects=Manager(self.model_attributes)),
            JobType=types.SimpleNamespace(ANNOTATION="annotation", GROUND_TRUTH="ground_truth"),
            ValidationMode=types.SimpleNamespace(GT_POOL="gt_pool"),
            TaskMode=types.SimpleNamespace(INTERPOLATION="interpolation"),
        )
        modules = {
            "rq": module("rq", get_current_job=lambda: object()),
            "rq.timeouts": module("rq.timeouts", JobTimeoutException=JobTimeoutException),
            "django.contrib.auth": module(
                "django.contrib.auth",
                get_user_model=lambda: types.SimpleNamespace(
                    objects=Manager(lambda: [self.user] if self.active_user else [])
                ),
            ),
            "django.db": module("django.db", transaction=types.SimpleNamespace(atomic=self.atomic)),
            "django.db.models": module("django.db.models", Q=Q),
            "django.db.models.functions": module(
                "django.db.models.functions", Trim=lambda value: value
            ),
            "django.http": module("django.http", HttpRequest=types.SimpleNamespace),
            "cvat.apps.dataset_manager.annotation": module(
                "cvat.apps.dataset_manager.annotation", AnnotationIR=object
            ),
            "cvat.apps.dataset_manager.bindings": module(
                "cvat.apps.dataset_manager.bindings", CvatImportError=ValueError, TaskData=object
            ),
            "cvat.apps.dataset_manager.formats.registry": module(
                "cvat.apps.dataset_manager.formats.registry", make_importer=lambda _: None
            ),
            "cvat.apps.dataset_manager.task": module(
                "cvat.apps.dataset_manager.task",
                TaskAnnotation=TaskAnnotation,
                JobAnnotation=JobAnnotation,
                PatchAction=types.SimpleNamespace(CREATE="create"),
            ),
            "cvat.apps.dataset_manager.util": module(
                "cvat.apps.dataset_manager.util",
                TmpDirManager=types.SimpleNamespace(get_tmp_directory=tempfile.TemporaryDirectory),
            ),
            "cvat.apps.engine": module("cvat.apps.engine", models=models),
            "cvat.apps.engine.permissions": module(
                "cvat.apps.engine.permissions",
                ProjectPermission=ProjectPermission,
                TaskPermission=TaskPermission,
            ),
            "cvat.apps.engine.rq": module(
                "cvat.apps.engine.rq",
                ImportRQMeta=types.SimpleNamespace(for_job=lambda _: self.meta),
            ),
            "cvat.apps.engine.serializers": module(
                "cvat.apps.engine.serializers",
                LabelSerializer=LabelSerializer,
                LabeledDataSerializer=LabeledDataSerializer,
            ),
            "cvat.apps.engine.utils": module(
                "cvat.apps.engine.utils", av_scan_paths=lambda _: None
            ),
            "cvat.apps.iam.middleware": module(
                "cvat.apps.iam.middleware", get_organization=lambda _: {}
            ),
            "cvat.apps.iam.permissions": module(
                "cvat.apps.iam.permissions", get_iam_context=lambda *_: {}
            ),
        }
        with patch.dict(sys.modules, modules):
            annotations = load(
                "components.project_annotations._annotations_service_test", ROOT / "annotations.py"
            )
            with patch.dict(
                sys.modules, {"components.project_annotations.annotations": annotations}
            ):
                service = load("components.project_annotations._service_test", ROOT / "service.py")
                yield service, annotations


def record(task_id=7, **changes):
    row = {
        "study_instance_uid": "1.2.3" if task_id == 7 else "2.3.4",
        "sop_instance_uid": "1.2.3.4" if task_id == 7 else "2.3.4.5",
        "tags": [{"label": "Quality", "attributes": {"quality": "good"}}],
    }
    row.update(changes)
    return row


def zipped(files):
    source = io.BytesIO()
    with ZipFile(source, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, json.dumps(value))
    source.seek(0)
    return source


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.context = self.h.services()
        self.service, self.annotations = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.h.db.close()

    def dicom(self, *records, mode="replace"):
        return self.service._import_dicom(
            zipped({"sample.json": {"records": list(records)}}), self.h.project, self.h.meta, mode
        )

    def folders(self, mode="replace", both=True):
        payload = {
            "tags": [
                {"frame": 0, "label_id": 10, "attributes": [{"spec_id": 20, "value": "good"}]}
            ],
            "shapes": [],
            "tracks": [],
        }
        files = {
            f"parent/Task{task_id}/native.json": payload for task_id in ((7, 8) if both else (7,))
        }

        def stage(path, importer, task, directory, **kwargs):
            self.h.events.append(("stage", task.id))
            with path.open() as source:
                return json.load(source)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(self.service, "stage_native", stage),
        ):
            return self.service._import_folders(
                zipped(files),
                self.h.project,
                self.h.meta,
                types.SimpleNamespace(EXT="JSON", DIMENSION="2d"),
                directory,
                mode,
                False,
            )

    def test_dicom_replace_preserves_protected_rows_and_attributes(self):
        before = self.h.saved(7)
        self.assertIn("1 tasks succeeded; 0 tasks failed", self.dicom(record()))
        after = self.h.saved(7)
        protected_ids = {row[0] for row in before[0] if row[5] == 99}
        self.assertEqual(
            [row for row in after[0] if row[0] in protected_ids],
            [row for row in before[0] if row[0] in protected_ids],
        )
        self.assertEqual(after[1][: len(before[1])], before[1])
        self.assertEqual([tag["label_id"] for tag in self.h.tags(7) if tag["label_id"] != 99], [10])

    def test_dicom_append_retains_non_protected_annotations(self):
        before = self.h.saved(7)
        self.dicom(record(), mode="append")
        self.assertEqual(self.h.saved(7)[0][: len(before[0])], before[0])
        self.assertEqual(Counter(tag["label_id"] for tag in self.h.tags(7)), {99: 2, 11: 1, 10: 1})

    def test_real_transaction_rolls_back_delete_and_partial_create_then_continues(self):
        before = self.h.saved(7)
        self.h.fail_create.add(7)
        with self.assertLogs(self.service.logger, level="WARNING") as captured:
            message = self.dicom(record(), record(8))
        self.assertIn("1 tasks succeeded; 1 tasks failed", message)
        self.assertEqual(self.h.saved(7), before)
        self.assertEqual(Counter(tag["label_id"] for tag in self.h.tags(8)), {99: 2, 10: 1})
        self.assertEqual(
            [event for event in self.h.events if event[0] in ("rollback", "commit")],
            [("rollback",), ("commit",)],
        )
        self.assertNotIn("sensitive", " ".join(captured.output))

    def test_malformed_record_prevents_every_change_to_its_task(self):
        before = self.h.saved(7)
        bad = record(sop_instance_uid="1.2.3.5", tags=[{"label": "Missing"}])
        message = self.dicom(record(), bad, record(8))
        self.assertIn("1 tasks succeeded; 1 tasks failed", message)
        self.assertEqual(self.h.saved(7), before)
        self.assertNotIn(("create", 7), self.h.events)

    def test_dicom_reimport_replace_succeeds_append_rejects_duplicate(self):
        self.assertIn("1 tasks succeeded", self.dicom(record()))
        self.assertIn("1 tasks succeeded", self.dicom(record()))
        before = self.h.saved()
        self.assertIn("0 tasks succeeded; 1 tasks failed", self.dicom(record(), mode="append"))
        self.assertEqual(self.h.saved(), before)

    def test_normal_append_keeps_standard_duplicate_behavior_on_reimport(self):
        self.folders(mode="append", both=False)
        self.folders(mode="append", both=False)
        self.assertEqual(Counter(tag["label_id"] for tag in self.h.tags(7)), {99: 2, 11: 1, 10: 2})

    def test_normal_replace_removes_absent_frame_annotations_and_is_repeatable(self):
        self.assertIn("1 tasks succeeded", self.folders(both=False))
        self.assertIn("1 tasks succeeded", self.folders(both=False))
        self.assertEqual(Counter(tag["label_id"] for tag in self.h.tags(7)), {99: 2, 10: 1})

    def test_normal_import_failure_rolls_back_entire_task_and_continues(self):
        before = self.h.saved(7)
        self.h.fail_create.add(7)
        self.assertIn("1 tasks succeeded; 1 tasks failed", self.folders())
        self.assertEqual(self.h.saved(7), before)
        self.assertEqual(Counter(tag["label_id"] for tag in self.h.tags(8)), {99: 2, 10: 1})

    def test_permissions_are_rechecked_for_each_task(self):
        before = self.h.saved(8)
        self.h.meta.save = lambda: self.h.denied_tasks.add(8)
        self.assertIn("1 tasks succeeded; 1 tasks failed", self.dicom(record(), record(8)))
        self.assertEqual(self.h.saved(8), before)
        self.assertTrue(self.h.locks)

    def test_normal_preflight_failure_does_not_delete_and_allows_other_task(self):
        before = self.h.saved(7)
        native_validate = self.service.validate_payload

        def validate(annotation, payload, labels):
            if annotation.db_task.id == 7:
                raise ValueError("Bad frame or annotation attribute")
            return native_validate(annotation, payload, labels)

        with patch.object(self.service, "validate_payload", validate):
            self.assertIn("1 tasks succeeded; 1 tasks failed", self.folders())
        self.assertEqual(self.h.saved(7), before)
        self.assertNotIn(("delete", 70), self.h.events)

    def test_normal_target_rename_after_preflight_prevents_that_task_update(self):
        before = self.h.saved(7)
        native_lock = self.service.locked_task

        def locked(project_id, task_id, meta):
            result = native_lock(project_id, task_id, meta)
            if task_id == 7:
                self.h.tasks[7].name = "Renamed"
            return result

        with patch.object(self.service, "locked_task", locked):
            self.assertIn("1 tasks succeeded; 1 tasks failed", self.folders())
        self.assertEqual(self.h.saved(7), before)

    def test_workspace_change_after_preflight_prevents_writes(self):
        before = self.h.saved()
        self.h.project.organization_id = 2
        self.assertIn("0 tasks succeeded; 1 tasks failed", self.dicom(record()))
        self.assertEqual(self.h.saved(), before)

    def test_inactive_user_or_project_permission_prevents_writes(self):
        for name in ("active_user", "allow_project"):
            with self.subTest(name=name):
                before = self.h.saved()
                setattr(self.h, name, False)
                self.assertIn("0 tasks succeeded; 1 tasks failed", self.dicom(record()))
                self.assertEqual(self.h.saved(), before)
                setattr(self.h, name, True)

    def test_unmatched_and_unidentifiable_invalid_records_are_reported(self):
        message = self.dicom(record(sop_instance_uid="1.2.3.999"), {}, record())
        self.assertIn(
            "1 tasks succeeded; 0 tasks failed; 1 unmatched records; 1 invalid records", message
        )

    def test_nonexistent_target_is_counted_even_when_tag_definition_is_invalid(self):
        message = self.dicom(record(sop_instance_uid="1.2.3.999", tags=[{"label": "Missing"}]))
        self.assertIn(
            "0 tasks succeeded; 0 tasks failed; 1 unmatched records; 1 invalid records", message
        )

    def test_each_locked_task_revalidates_only_its_original_records(self):
        native_plan = self.service.plan_records
        sizes = []

        def planning(records, snapshots, labels, import_mode):
            sizes.append(len(records))
            return native_plan(records, snapshots, labels, import_mode)

        with patch.object(self.service, "plan_records", planning):
            message = self.dicom(record(), record(8), record(sop_instance_uid="1.2.3.5"))
        self.assertIn("2 tasks succeeded; 0 tasks failed", message)
        self.assertEqual(sizes, [3, 2, 1])

    def test_current_duplicate_from_concurrent_change_is_rechecked(self):
        native_lock = self.service.locked_task

        def locked(*args):
            result = native_lock(*args)
            self.h.seed(7, 70, 0, 10, [{"spec_id": 20, "value": "poor"}])
            return result

        before = self.h.saved(7)
        with patch.object(self.service, "locked_task", locked):
            self.assertIn("0 tasks succeeded; 1 tasks failed", self.dicom(record(), mode="append"))
        self.assertEqual(self.h.saved(7), before)

    def test_snapshot_uses_maximum_per_job_tag_multiplicity(self):
        self.h.jobs.append(types.SimpleNamespace(id=71, task_id=7, type="annotation"))
        self.h.seed(7, 71, 0, 99, self.h.identity("1.2.3", "1.2.3.4"))
        for job in (70, 70, 71):
            self.h.seed(7, job, 0, 10, [{"spec_id": 20, "value": "good"}])
        snapshot = next(item for item in self.service.snapshots(1) if item.id == 7)
        counts = Counter((item["frame"], item["label_id"]) for item in snapshot.existing_tags)
        self.assertEqual(counts[(0, 99)], 1)
        self.assertEqual(counts[(0, 10)], 2)
        self.assertEqual(len([item for item in snapshot.identities if item.frame == 0]), 1)

    def test_snapshot_reads_stored_date_and_does_not_invent_from_label_defaults(self):
        self.h.db.execute("UPDATE attributes SET value='2025/1/1' WHERE spec_id=104")
        snapshots = self.service.snapshots(1)
        self.assertEqual(snapshots[0].identities[0].values["StudyDate"], {"2025/1/1"})
        result = plan_records([record(study_date="20250101")], snapshots, self.h.labels, "replace")
        self.assertIn(7, result.updates)
        self.h.db.execute("DELETE FROM attributes WHERE spec_id=104")
        self.assertIn(
            7,
            plan_records(
                [record(study_date="20250101")], self.service.snapshots(1), self.h.labels, "replace"
            ).failed_tasks,
        )

    def test_snapshot_does_not_use_ordinary_ground_truth_job_as_match(self):
        self.h.jobs.append(types.SimpleNamespace(id=81, task_id=8, type="ground_truth"))
        self.h.seed(8, 81, 0, 99, self.h.identity("1.2.3", "1.2.3.4"))
        self.assertIn("1 tasks succeeded; 0 tasks failed", self.dicom(record()))

    def test_current_candidate_query_keeps_whitespace_padded_duplicate_sop(self):
        initial = self.service.snapshots(1, task_id=7, sop_uids={"1.2.3.4"})
        self.assertEqual([snapshot.id for snapshot in initial], [7])
        self.h.seed(8, 80, 0, 99, self.h.identity("1.2.3", "\t1.2.3.4\n"))
        current = self.service.snapshots(1, task_id=7, sop_uids={"1.2.3.4"})
        self.assertEqual([snapshot.id for snapshot in current], [7, 8])
        plan = plan_records([record()], current, self.h.labels, "replace")
        self.assertEqual(set(plan.failed_tasks), {7, 8})
        self.assertFalse(plan.updates)

    def test_snapshot_includes_ground_truth_job_for_gt_pool_tasks(self):
        self.h.tasks[8].data.validation_mode = "gt_pool"
        self.h.jobs.append(types.SimpleNamespace(id=81, task_id=8, type="ground_truth"))
        self.h.seed(8, 81, 0, 99, self.h.identity("1.2.3", "1.2.3.4"))
        snapshots = self.service.snapshots(1)
        plan = plan_records([record()], snapshots, self.h.labels, "replace")
        self.assertEqual(set(plan.failed_tasks), {7, 8})


if __name__ == "__main__":
    unittest.main()
