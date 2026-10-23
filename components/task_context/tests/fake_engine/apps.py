# SPDX-License-Identifier: MIT
from django.apps import AppConfig


class TestEngineConfig(AppConfig):
    name = "components.task_context.tests.fake_engine"
    label = "engine"
    default_auto_field = "django.db.models.AutoField"
