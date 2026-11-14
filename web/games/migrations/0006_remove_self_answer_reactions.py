from django.db import migrations
from django.db.models import F


def remove_self_answer_reactions(apps, schema_editor):
    AnswerReaction = apps.get_model("games", "AnswerReaction")
    invalid_reactions = AnswerReaction.objects.filter(
        game_round__challenged_player_id=F("player_id"),
    ).values("pk")
    AnswerReaction.objects.filter(pk__in=invalid_reactions).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("games", "0005_roommessage_answerreaction"),
    ]

    operations = [
        migrations.RunPython(
            remove_self_answer_reactions,
            migrations.RunPython.noop,
        ),
    ]
