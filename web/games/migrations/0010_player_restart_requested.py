from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("games", "0009_gameroom_winner_gameround_proof_format_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="restart_requested",
            field=models.BooleanField(default=False),
        ),
    ]
