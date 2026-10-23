# SPDX-License-Identifier: MIT
"""Reuse CVAT's task and job policies, including access-token policy plugins."""

from types import SimpleNamespace

from cvat.apps.access_tokens.permissions import PolicyEnforcer
from cvat.apps.engine.permissions import JobPermission, TaskPermission


class TaskContextPermission(TaskPermission):
    @classmethod
    def _get_scopes(cls, request, view, obj):
        scopes = [cls.Scopes.VIEW]
        if view.action == "update":
            scopes.append(cls.Scopes.UPDATE_DESC)
        return scopes

    @classmethod
    def create(cls, request, view, obj, iam_context):
        # Context field names are not CVAT task properties. Do not inspect them
        # as project/owner/storage changes in TaskPermission.create().
        return [
            cls.create_base_perm(request, view, scope, iam_context, obj)
            for scope in cls.get_scopes(request, view, obj)
        ]


class JobContextPermission(JobPermission):
    @classmethod
    def _get_scopes(cls, request, view, obj):
        # HEAD is the same read as GET; the native map only names GET explicitly.
        return [cls.Scopes.VIEW]


def can_edit_context(request, task):
    # Use the configured CVAT policy enforcer rather than calling OPA directly:
    # otherwise read-only access tokens would incorrectly appear writable.
    view = SimpleNamespace(
        iam_permission_class=TaskContextPermission,
        action="update",
        basename="task",
        detail=True,
    )
    return PolicyEnforcer().check_permission(request, view, task)
