# SPDX-License-Identifier: MIT
from django.db import models


class TaskCalibration(models.Model):
    task = models.OneToOneField(
        "engine.Task",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="physical_measurement_calibration",
    )
    document = models.JSONField()
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        default_permissions = ()
