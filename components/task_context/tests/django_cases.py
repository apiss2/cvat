# SPDX-License-Identifier: MIT
"""Isolated Django + SQLite API tests against CVAT's real OPA policies.

The surrounding media-heavy engine models/imports use small test fixtures. The
extension model, database operations, Django session/CSRF checks, native task/job
permission builders, access-token policy enforcer and Rego files are exercised.
This does not replace CVAT deployment tests or PostgreSQL concurrency tests.
"""

import importlib
import json
import socket
import subprocess
import sys
import time
import unittest
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OPA_BINARY = sys.argv.pop(1)

import django
from django.conf import settings


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    sys.modules[name] = result
    return result


def iam_context():
    return {"organization": None, "organization_specified": False, "privilege": "user"}


class IAMMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.iam_context = iam_context()
        return self.get_response(request)


def grouped(values, key):
    result = defaultdict(list)
    for value in values:
        result[key(value)].append(value)
    return result


@dataclass
class AccessToken:
    id: int
    read_only: bool


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    OPA_PORT = sock.getsockname()[1]

OPA_URL = f"http://127.0.0.1:{OPA_PORT}"
RULE_DIRS = [
    ROOT / "cvat/apps" / app / "rules"
    for app in ("iam", "engine", "organizations", "access_tokens")
]
RULE_FILES = [str(path) for directory in RULE_DIRS for path in directory.glob("*.rego")]
OPA = subprocess.Popen(
    [
        OPA_BINARY,
        "run",
        "--server",
        f"--addr=127.0.0.1:{OPA_PORT}",
        "--log-level=error",
        *RULE_FILES,
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text=True,
)

try:
    import requests

    for _ in range(100):
        if OPA.poll() is not None:
            raise RuntimeError(OPA.stderr.read())
        try:
            if requests.get(OPA_URL + "/health", timeout=0.1).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(0.02)
    else:
        raise RuntimeError("OPA did not become ready")

    settings.configure(
        SECRET_KEY="task-context-tests-only",
        ALLOWED_HOSTS=["testserver"],
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "rest_framework",
            "components.task_context.tests.fake_engine",
            "components.task_context",
        ],
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            __name__ + ".IAMMiddleware",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        MIGRATION_MODULES={"engine": None, "task_context": None},
        CVAT_TASK_CONTEXT_ENABLED=True,
        IAM_OPA_DATA_URL=OPA_URL + "/v1/data",
        SMOKESCREEN_ENABLED=False,
        OBJECTS_NOT_RELATED_WITH_ORG=[],
        USE_TZ=True,
        REST_FRAMEWORK={
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "rest_framework.authentication.SessionAuthentication"
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "rest_framework.permissions.IsAuthenticated",
                "cvat.apps.access_tokens.permissions.PolicyEnforcer",
            ],
            "DEFAULT_RENDERER_CLASSES": ["cvat.apps.engine.renderers.CVATAPIRenderer"],
        },
    )
    django.setup()

    from rest_framework.request import Request

    from components.task_context.tests.fake_engine.models import (
        Job,
        Membership,
        Organization,
        Project,
        Segment,
        Task,
    )

    module("cvat.apps.engine", __path__=[str(ROOT / "cvat/apps/engine")])
    module(
        "cvat.apps.engine.models",
        Task=Task,
        Job=Job,
        Project=Project,
        **{
            name: type(name, (), {})
            for name in ("AnnotationGuide", "CloudStorage", "Comment", "Issue", "Label", "Location")
        },
    )
    module("cvat.apps.organizations.models", Organization=Organization, Membership=Membership)
    module("cvat.apps.access_tokens.models", AccessToken=AccessToken)
    module("cvat.apps.engine.types", ExtendedRequest=Request)
    module("cvat.apps.engine.rq", ExportRequestId=type("ExportRequestId", (), {}))
    module("cvat.apps.engine.utils", grouped=grouped, is_dataset_export=lambda request: False)
    module(
        "cvat.apps.engine.location",
        StorageType=SimpleNamespace(TARGET="target", SOURCE="source"),
        get_location_configuration=lambda *args, **kwargs: None,
    )

    from cvat.apps.iam.utils import add_opa_rules_path

    for directory in RULE_DIRS:
        add_opa_rules_path(directory)

    from django.apps import apps
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.state import ProjectState
    from django.test import TestCase, override_settings
    from django.urls import include, path
    from rest_framework.exceptions import ValidationError
    from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

    from components.task_context.backup import read_backup_context, write_backup_context
    from components.task_context.models import TaskContext
    from components.task_context.storage import (
        export_context,
        read_context,
        replace_context,
        restore_context,
    )
    from components.task_context.views import JobContextViewSet, TaskContextViewSet

    urlpatterns = [path("api/task-context/", include("components.task_context.urls"))]
    call_command("migrate", run_syncdb=True, verbosity=0)
    # Apply the extension's actual migration; only the surrounding engine schema
    # is substituted by the small fixtures above.
    state = ProjectState.from_apps(apps)
    state.remove_model("task_context", "taskcontext")
    migration = importlib.import_module(
        "components.task_context.migrations.0001_initial"
    ).Migration(
        "0001_initial",
        "task_context",
    )
    with connection.schema_editor() as editor:
        editor.delete_model(TaskContext)
        migration.apply(state, editor)

    TASK_VIEW = TaskContextViewSet.as_view(
        {"get": "retrieve", "put": "update"}, basename="task", detail=True
    )
    JOB_VIEW = JobContextViewSet.as_view({"get": "retrieve"}, basename="job", detail=True)
    DOCUMENT = {"fields": [{"name": "撮影場所", "value": "施設A"}], "notes": "確認事項"}

    class ContextTests(TestCase):
        def setUp(self):
            self.owner = User.objects.create_user("owner")
            self.worker = User.objects.create_user("worker")
            self.outsider = User.objects.create_user("outsider")
            self.task = Task.objects.create(owner=self.owner)
            self.segment = Segment.objects.create(task=self.task)
            self.job = Job.objects.create(segment=self.segment, assignee=self.worker)
            self.factory = APIRequestFactory()

        def request(
            self,
            *,
            method="get",
            target="task",
            user="owner",
            body=None,
            token=None,
            pk=None,
            accept=None,
        ):
            user = getattr(self, user) if isinstance(user, str) else user
            method_fn = getattr(self.factory, method)
            request = method_fn(
                "/", data=body, format="json", HTTP_ACCEPT=accept or "application/vnd.cvat+json"
            )
            request.iam_context = iam_context()
            if user is not None:
                force_authenticate(request, user=user, token=token)
            instance = self.task if target == "task" else self.job
            return (TASK_VIEW if target == "task" else JOB_VIEW)(request, pk=pk or instance.pk)

        def test_empty_get_does_not_create_a_record(self):
            response = self.request()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.data,
                {
                    "task_id": self.task.pk,
                    "fields": [],
                    "notes": "",
                    "version": 0,
                    "can_edit": True,
                },
            )
            self.assertEqual(TaskContext.objects.count(), 0)
            self.assertEqual(response["Cache-Control"], "no-store")

        def test_save_reload_and_clear_preserve_version(self):
            response = self.request(method="put", body={**DOCUMENT, "version": 0})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["version"], 1)
            self.assertEqual(self.request().data["fields"], DOCUMENT["fields"])
            response = self.request(method="put", body={"fields": [], "notes": "", "version": 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["version"], 2)
            self.assertEqual(TaskContext.objects.count(), 1)

        def test_stale_writer_is_rejected_without_overwriting(self):
            self.request(method="put", body={**DOCUMENT, "version": 0})
            response = self.request(
                method="put", body={"fields": [], "notes": "stale", "version": 0}
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.request().data["notes"], DOCUMENT["notes"])
            self.assertEqual(self.request().data["version"], 1)

        def test_job_only_assignee_can_read_but_cannot_edit_task_details(self):
            replace_context(self.task, {**DOCUMENT, "version": 0})
            self.assertEqual(self.request(user="worker").status_code, 403)
            response = self.request(user="worker", target="job")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["fields"], DOCUMENT["fields"])
            self.assertFalse(response.data["can_edit"])
            self.assertEqual(
                self.request(
                    user="worker", method="put", body={**DOCUMENT, "version": 1}
                ).status_code,
                403,
            )
            self.assertEqual(
                self.request(
                    user="worker", target="job", method="put", body={**DOCUMENT, "version": 1}
                ).status_code,
                405,
            )

        def test_outsider_cannot_read_or_write_task_or_job(self):
            for target in ("task", "job"):
                self.assertEqual(self.request(user="outsider", target=target).status_code, 403)
            self.assertEqual(
                self.request(
                    user="outsider", method="put", body={**DOCUMENT, "version": 0}
                ).status_code,
                403,
            )
            self.assertEqual(TaskContext.objects.count(), 0)

        def test_unauthenticated_access_is_rejected(self):
            self.assertEqual(self.request(user=None).status_code, 403)
            self.assertEqual(
                self.request(user=None, method="put", body={**DOCUMENT, "version": 0}).status_code,
                403,
            )

        def test_read_only_token_is_read_only_in_ui_and_api(self):
            token = AccessToken(1, read_only=True)
            response = self.request(token=token)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.data["can_edit"])
            response = self.request(token=token, method="put", body={**DOCUMENT, "version": 0})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(TaskContext.objects.count(), 0)

        def test_write_token_retains_native_permission(self):
            token = AccessToken(2, read_only=False)
            response = self.request(token=token, method="put", body={**DOCUMENT, "version": 0})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertTrue(response.data["can_edit"])
            self.assertEqual(self.request(token=token, user="outsider").status_code, 403)

        def test_organization_membership_and_revocation_are_checked(self):
            org = Organization.objects.create(slug="research", owner=self.owner)
            self.task.organization = org
            self.task.save()
            membership = Membership.objects.create(organization=org, user=self.owner, role="owner")
            Membership.objects.create(organization=org, user=self.worker, role="worker")
            other = Organization.objects.create(slug="other", owner=self.outsider)
            Membership.objects.create(organization=other, user=self.outsider, role="owner")
            self.assertEqual(self.request().status_code, 200)
            self.assertEqual(self.request(user="worker", target="job").status_code, 200)
            self.assertEqual(self.request(user="outsider", target="job").status_code, 403)
            membership.is_active = False
            membership.save()
            self.assertEqual(self.request().status_code, 403)

        def test_disabled_feature_retains_data_and_task_cascade(self):
            replace_context(self.task, {**DOCUMENT, "version": 0})
            with override_settings(CVAT_TASK_CONTEXT_ENABLED=False):
                self.assertEqual(self.request().status_code, 404)
                self.assertEqual(
                    self.request(method="put", body={**DOCUMENT, "version": 1}).status_code, 404
                )
                self.assertEqual(export_context(self.task), DOCUMENT)
            self.assertEqual(self.request().data["version"], 1)
            self.task.delete()
            self.assertEqual(TaskContext.objects.count(), 0)

        def test_task_and_project_backup_timestamps_change(self):
            project = Project.objects.create(owner=self.owner)
            self.task.project = project
            self.task.save()
            task_time, project_time = self.task.updated_date, project.updated_date
            replace_context(self.task, {**DOCUMENT, "version": 0})
            self.task.refresh_from_db()
            project.refresh_from_db()
            self.assertGreater(self.task.updated_date, task_time)
            self.assertGreater(project.updated_date, project_time)

        def test_validation_errors_do_not_write(self):
            for body in (
                [],
                {},
                {**DOCUMENT, "version": True},
                {**DOCUMENT, "version": 0, "owner_id": self.outsider.pk},
            ):
                response = self.request(method="put", body=body)
                self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(TaskContext.objects.count(), 0)

        def test_head_and_unsupported_response_formats(self):
            self.assertEqual(self.request(method="head").status_code, 200)
            self.assertEqual(self.request(method="head", target="job").status_code, 200)
            self.assertEqual(self.request(method="options").status_code, 405)
            self.assertEqual(self.request(accept="text/html").status_code, 406)
            self.assertEqual(self.request(pk=99999).status_code, 404)

        def test_session_authentication_requires_csrf_for_save(self):
            client = APIClient(enforce_csrf_checks=True)
            client.force_login(self.owner)
            url = f"/api/task-context/tasks/{self.task.pk}/"
            self.assertEqual(client.get(url).status_code, 200)
            self.assertEqual(
                client.put(url, {**DOCUMENT, "version": 0}, format="json").status_code, 403
            )
            csrf = "a" * 32
            client.cookies["csrftoken"] = csrf
            response = client.put(
                url, {**DOCUMENT, "version": 0}, format="json", HTTP_X_CSRFTOKEN=csrf
            )
            self.assertEqual(response.status_code, 200, response.data)

        def test_backup_roundtrip_resets_version_and_is_task_local(self):
            replace_context(self.task, {**DOCUMENT, "version": 0})
            replace_context(self.task, {**DOCUMENT, "version": 1})
            other = Task.objects.create(owner=self.owner)
            replace_context(other, {"fields": [], "notes": "other", "version": 0})
            archive = BytesIO()
            with ZipFile(archive, "w") as output:
                write_backup_context(output, self.task, "task_0")
                write_backup_context(output, other, "task_1/")
            with ZipFile(archive) as source:
                self.assertIsNone(read_backup_context(source))
                restored = Task.objects.create(owner=self.owner)
                restore_context(restored, read_backup_context(source, "task_0/"))
                self.assertEqual(read_context(restored)["version"], 1)
                self.assertEqual(export_context(restored), DOCUMENT)
                self.assertEqual(read_backup_context(source, "task_1")["notes"], "other")

        def test_legacy_backup_and_never_edited_task_have_no_context(self):
            archive = BytesIO()
            with ZipFile(archive, "w") as output:
                write_backup_context(output, self.task)
            with ZipFile(archive) as source:
                self.assertEqual(source.namelist(), [])
                self.assertIsNone(read_backup_context(source))

        def test_backup_rejects_invalid_or_oversized_content(self):
            for content in (
                b"{",
                b"\xff",
                b"[]",
                b"x" * (65536 + 1),
                b'{"fields":[],"notes":"","extra":1}',
            ):
                archive = BytesIO()
                with ZipFile(archive, "w") as output:
                    output.writestr("task_context.json", content)
                with ZipFile(archive) as source, self.assertRaises(ValidationError):
                    read_backup_context(source)

        def test_backup_rejects_duplicate_context_entries(self):
            import warnings

            archive = BytesIO()
            with warnings.catch_warnings(), ZipFile(archive, "w") as output:
                warnings.simplefilter("ignore", UserWarning)
                for _ in range(2):
                    output.writestr("task_context.json", json.dumps(DOCUMENT))
            with ZipFile(archive) as source, self.assertRaises(ValidationError):
                read_backup_context(source)

    program = unittest.main(exit=False, verbosity=2)
    # unittest.main(exit=False) allows the child OPA server to be cleaned up.
    if not program.result.wasSuccessful():
        raise SystemExit(1)
finally:
    if OPA.poll() is None:
        OPA.terminate()
        OPA.wait(timeout=5)
    if OPA.stderr is not None:
        OPA.stderr.close()
