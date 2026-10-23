# SPDX-License-Identifier: MIT
from django.urls import path

from .views import JobCalibrationViewSet, TaskCalibrationViewSet

urlpatterns = [
    path(
        "tasks/<int:pk>",
        TaskCalibrationViewSet.as_view(
            {"get": "metadata", "put": "metadata"},
            basename="task",
            detail=True,
        ),
        name="physical-measurements-task",
    ),
    path(
        "jobs/<int:pk>",
        JobCalibrationViewSet.as_view({"get": "metadata"}, basename="job", detail=True),
        name="physical-measurements-job",
    ),
]
