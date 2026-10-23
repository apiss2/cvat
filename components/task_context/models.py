# SPDX-License-Identifier: MIT
from django.db import models


class TaskContext(models.Model):
    task = models.OneToOneField(
        "engine.Task", on_delete=models.CASCADE, primary_key=True, related_name="context"
    )
    fields = models.JSONField(default=list)
    notes = models.TextField(blank=True, default="")
    version = models.PositiveBigIntegerField(default=1)
    updated_date = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
