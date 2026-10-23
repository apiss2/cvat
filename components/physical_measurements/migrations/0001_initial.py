# SPDX-License-Identifier: MIT
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("engine", "0108_profile_registration_survey_fields")]

    operations = [
        migrations.CreateModel(
            name="TaskCalibration",
            fields=[
                (
                    "task",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="physical_measurement_calibration",
                        serialize=False,
                        to="engine.task",
                    ),
                ),
                ("document", models.JSONField()),
                ("revision", models.PositiveBigIntegerField(default=1)),
            ],
            options={"default_permissions": ()},
        ),
    ]
