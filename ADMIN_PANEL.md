# Live Admin Panel

The admin panel reads directly from `truth_or_dare.db`; it does not use demo data or JSON imports.

Start it from this directory:

```bash
.venv/bin/python admin_server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The server exposes:

- `/api/sessions`: live sessions, players, questions, answers, and timestamps
- `/api/media/<telegram-file-id>`: a local proxy for Telegram images

The media proxy uses `TELEGRAM_BOT_TOKEN` from `.env` and keeps the token out of the browser. Telegram image answers are stored as `file_id` values in `game_history`; the browser receives the image through the local proxy.

The server is read-only with respect to the database. Starting `bot.py` starts
the panel automatically and shuts it down with the bot. Refresh the panel after
new game activity to see updated sessions.

Media answers are deliberately classified before download:

- Telegram `photo` messages use the largest available `PhotoSize`.
- Image documents use `Document.IMAGE` and retain their original media kind.
- Telegram videos, animations, and video documents are supported too.
- Non-image/video documents, audio, stickers, and other attachments are not treated as game media.

The bot calls Telegram `get_file` immediately and saves the downloaded media in
`media/answers/`. Telegram does not expose a reliable bot-side "view once" flag,
so media that reaches the bot is treated as an answer and stored according to the
normal retention rules.
