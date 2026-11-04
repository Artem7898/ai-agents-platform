"""New runs freeze the registered structured-output contract before dispatch."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("runs", "0006_workflowrun_billing_owner")]

    operations = [
        migrations.AddField(
            model_name="workflowrun",
            name="output_schema_snapshots",
            field=models.JSONField(default=dict),
        ),
    ]
