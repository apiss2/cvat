# SPDX-License-Identifier: MIT
"""Minimal engine relations for isolated SQLite persistence tests, not IAM tests."""

from django.db import models


class DimensionType:
    DIM_2D = "2d"


class MediaType:
    IMAGE = "image"


class ValidationMode:
    GT_POOL = "gt_pool"


class Project(models.Model):
    updated_date = models.DateTimeField(auto_now=True)


class Data(models.Model):
    size = models.PositiveIntegerField(default=6)
    start_frame = models.PositiveIntegerField(default=100)
    frame_step = models.PositiveIntegerField(default=3)
    validation_mode = models.CharField(max_length=20, default="")

    def get_frame_step(self):
        return self.frame_step


class Task(models.Model):
    data = models.ForeignKey(Data, on_delete=models.CASCADE, null=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True)
    dimension = models.CharField(max_length=2, default="2d")
    media_type = models.CharField(max_length=20, default="image")
    updated_date = models.DateTimeField(auto_now=True)
