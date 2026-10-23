# SPDX-License-Identifier: MIT
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("engine", "0108_profile_registration_survey_fields")]

    operations = [
        migrations.CreateModel(
            name="TaskContext",
            fields=[
                (
                    "task",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="context",
                        serialize=False,
                        to="engine.task",
                    ),
                ),
                ("fields", models.JSONField(default=list)),
                ("notes", models.TextField(blank=True, default="")),
                ("version", models.PositiveBigIntegerField(default=1)),
                ("updated_date", models.DateTimeField(auto_now=True)),
            ],
            options={"default_permissions": ()},
        ),
    ]
