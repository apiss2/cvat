# SPDX-License-Identifier: MIT
from django.urls import path

from .views import FilteredProjectViewSet, FilteredTaskViewSet

urlpatterns = []
for target, view in (("task", FilteredTaskViewSet), ("project", FilteredProjectViewSet)):
    prefix = f"{target}s/<int:pk>"
    init = {"basename": target, "detail": True}
    urlpatterns.extend([
        path(f"{prefix}/options", view.as_view({"get": "retrieve"}, **init),
             name=f"filtered-{target}-options"),
        path(f"{prefix}/preview", view.as_view({"post": "initiate_dataset_export"}, **init),
             {"preview_only": True}, name=f"filtered-{target}-preview"),
        path(f"{prefix}/export", view.as_view({"post": "initiate_dataset_export"}, **init),
             name=f"filtered-{target}-export"),
        path(f"{prefix}/download", view.as_view({"get": "download_dataset"}, **init),
             name=f"filtered-{target}-download"),
    ])
