# SPDX-License-Identifier: MIT
from django.urls import path

from .views import JobContextViewSet, TaskContextViewSet

urlpatterns = [
    path(
        "tasks/<int:pk>/",
        TaskContextViewSet.as_view(
            {"get": "retrieve", "put": "update"},
            basename="task",
            detail=True,
        ),
        name="task-context",
    ),
    path(
        "jobs/<int:pk>/",
        JobContextViewSet.as_view({"get": "retrieve"}, basename="job", detail=True),
        name="job-task-context",
    ),
]
