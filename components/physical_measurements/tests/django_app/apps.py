# SPDX-License-Identifier: MIT
from django.apps import AppConfig


class EngineTestConfig(AppConfig):
    name = "components.physical_measurements.tests.django_app"
    label = "engine"
    default_auto_field = "django.db.models.AutoField"
