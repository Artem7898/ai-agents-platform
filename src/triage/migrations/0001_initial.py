import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TriageReceipt",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("idempotency_key", models.CharField(max_length=512, unique=True)),
                ("run_id", models.UUIDField(db_index=True)),
                ("workflow_id", models.UUIDField()),
                ("node_id", models.CharField(max_length=128)),
                ("ticket_id", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
                ("payload_hash", models.CharField(max_length=64)),
                ("category", models.CharField(max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="TriageSinkAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("idempotency_key", models.CharField(db_index=True, max_length=512)),
                ("payload_hash", models.CharField(max_length=64)),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("replayed", "Replayed"),
                            ("conflict", "Conflict"),
                        ],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "receipt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attempts",
                        to="triage.triagereceipt",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
    ]
