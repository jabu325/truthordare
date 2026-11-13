import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from games.models import Prompt


class Command(BaseCommand):
    help = "Import prompts from the original Telegram bot database."

    def handle(self, *args, **options):

        source_db = (
            Path(settings.BASE_DIR).parent / "truth_or_dare.db"
        )

        if not source_db.exists():
            raise CommandError(
                f"Source database not found: {source_db}"
            )

        self.stdout.write(
            f"Reading prompts from: {source_db}"
        )

        try:
            connection = sqlite3.connect(
                f"{source_db.as_uri()}?mode=ro",
                uri=True,
            )

            connection.row_factory = sqlite3.Row

            rows = connection.execute("""
                SELECT type, difficulty, game_type, text
                FROM prompts
                ORDER BY id
            """).fetchall()

            connection.close()

        except sqlite3.Error as error:
            raise CommandError(
                f"Unable to read source database: {error}"
            )

        if not rows:
            raise CommandError("No prompts found in source database.")

        imported = 0
        skipped = 0

        with transaction.atomic():

            for row in rows:

                prompt_type = row["type"]
                difficulty = row["difficulty"]
                game_type = row["game_type"]
                prompt_text = row["text"]

                if not all((
                    prompt_type,
                    difficulty,
                    game_type,
                    prompt_text,
                )):
                    skipped += 1
                    continue

                _, created = Prompt.objects.get_or_create(
                    type=prompt_type,
                    difficulty=difficulty,
                    game_type=game_type,
                    text=prompt_text,
                )

                if created:
                    imported += 1
                else:
                    skipped += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS("Prompt import completed.")
        )

        self.stdout.write(f"Source records: {len(rows)}")
        self.stdout.write(f"Imported: {imported}")
        self.stdout.write(f"Skipped: {skipped}")
        self.stdout.write(
            f"Total web prompts: {Prompt.objects.count()}"
        )
