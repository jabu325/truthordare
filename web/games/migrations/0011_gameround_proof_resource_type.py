from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("games", "0010_player_restart_requested"),
    ]

    operations = [
        migrations.AddField(
            model_name="gameround",
            name="proof_resource_type",
            field=models.CharField(default="image", max_length=10),
        ),
    ]
