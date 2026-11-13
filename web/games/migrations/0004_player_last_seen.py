from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("games", "0003_gameroom_prompt_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="last_seen",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
