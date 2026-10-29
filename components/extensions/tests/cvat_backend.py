# SPDX-License-Identifier: MIT
"""Isolated CVAT backend for explicit integration suites.

The real models, views and serializers use a temporary filesystem and SQLite.
App startup hooks and external services are deliberately not started. Call this
once, before importing CVAT application code, in a dedicated pytest process.
"""

from __future__ import annotations

import os
import tempfile
from importlib import import_module
from pathlib import Path


def configure_cvat(*, prefix: str):
    from django.apps import AppConfig
    from django.conf import settings
    from django.contrib.admin.apps import SimpleAdminConfig

    if settings.configured:
        raise RuntimeError("Run CVAT integration suites in separate Python processes")

    runtime = tempfile.TemporaryDirectory(prefix=prefix)
    os.environ["CVAT_BASE_DIR"] = runtime.name
    os.environ["DJANGO_SECRET_KEY"] = "isolated-integration-test-only"
    os.environ["CVAT_PROJECT_ANNOTATIONS_ENABLED"] = "1"
    base = import_module("cvat.settings.base")
    defaults = {name: value for name, value in vars(base).items() if name.isupper()}
    names = (
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.sites",
        "django.contrib.admin",
        "rest_framework.authtoken",
        "rest_framework_api_key",
        "allauth",
        "allauth.account",
        "allauth.socialaccount",
        "cvat.apps.iam",
        "cvat.apps.engine",
        "cvat.apps.organizations",
        "cvat.apps.dataset_manager",
        "cvat.apps.webhooks",
        "cvat.apps.access_tokens",
        "cvat.apps.quality_control",
        "cvat.apps.consensus",
    )
    defaults.update(
        INSTALLED_APPS=[],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        LOGGING_CONFIG=None,
        AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"],
        MIDDLEWARE=[],
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        IGNORE_TMP_FOLDER_CLEANUP_ERRORS=False,
        ALLOWED_HOSTS=["testserver"],
    )
    for name in (
        "DATA_ROOT",
        "MEDIA_DATA_ROOT",
        "CACHE_ROOT",
        "EXPORT_CACHE_ROOT",
        "JOBS_ROOT",
        "TASKS_ROOT",
        "PROJECTS_ROOT",
        "SHARE_ROOT",
        "LOGS_ROOT",
        "MIGRATIONS_LOGS_ROOT",
        "CLOUD_STORAGE_ROOT",
        "TMP_FILES_ROOT",
        "ASSETS_ROOT",
    ):
        defaults[name] = Path(runtime.name) / name.lower()
        defaults[name].mkdir(parents=True, exist_ok=True)
    settings.configure(**defaults)
    for name, value in vars(import_module("cvat.apps.engine.default_settings")).items():
        if name.isupper():
            setattr(settings, name, value)
    # Register real models without deployment startup hooks. The admin's
    # lightweight config supplies its lazy-site definition without autodiscovery.
    settings.INSTALLED_APPS = [
        (SimpleAdminConfig if name == "django.contrib.admin" else AppConfig)(
            name, import_module(name)
        )
        for name in names
    ]
    import django

    django.setup()
    # Dependencies such as dj-rest-auth check membership by app name. The
    # registry is already populated with the model-only configs above.
    settings.INSTALLED_APPS = list(names)
    from django.apps import apps
    from django.db import connection

    with connection.schema_editor() as editor:
        for model in apps.get_models():
            editor.create_model(model)
    return runtime
