# SPDX-License-Identifier: MIT
"""Small related models for extension tests without CVAT's media dependencies.

These are only fixtures. The extension model, storage, views, permission classes,
policy enforcer and Rego policies are the real implementations.
"""

from django.db import models


class Organization(models.Model):
    slug = models.CharField(max_length=32)
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE)


class Membership(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True)
    role = models.CharField(max_length=32)


class Project(models.Model):
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="+")
    assignee = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    organization = models.ForeignKey(Organization, on_delete=models.SET_NULL, null=True)
    updated_date = models.DateTimeField(auto_now=True)

    def touch(self):
        self.save(update_fields=["updated_date"])


class Task(models.Model):
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="+")
    assignee = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True)
    organization = models.ForeignKey(Organization, on_delete=models.SET_NULL, null=True)
    updated_date = models.DateTimeField(auto_now=True)

    def touch(self):
        self.save(update_fields=["updated_date"])


class Segment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE)


class Job(models.Model):
    segment = models.ForeignKey(Segment, on_delete=models.CASCADE)
    assignee = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True)

    @property
    def organization_id(self):
        return self.segment.task.organization_id

    @property
    def organization(self):
        return self.segment.task.organization
