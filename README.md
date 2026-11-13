# Truth or Dare

Truth or Dare is available as a Telegram bot and as a browser-based game. The
web version is a Django app with a chat-style interface and installable PWA.

## Features

- Telegram bot for playing Truth or Dare in Telegram.
- Local admin panel for reviewing bot sessions and supported media answers.
- Two-player web rooms with invite codes, nicknames, turn-based rounds, and
  recent-first round history.
- Rejoining a seat with the same room code and nickname.
- Player activity indicators and PWA installation support.
- SQLite for local development; PostgreSQL can be configured for the web app.

## Telegram bot

Use Python 3.12 or newer. From the repository root, install the dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a root `.env` file containing your Telegram bot token:

```dotenv
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
```

Start the bot:

```bash
python bot.py
```

The local admin panel is started with the bot and is available at
http://127.0.0.1:8765. To run it separately:

```bash
python admin_server.py
```

See [ADMIN_PANEL.md](ADMIN_PANEL.md) for panel details.

## Web app

The Django project is in `web/`. From the repository root:

```bash
cd web
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For local development, create `web/.env`:

```dotenv
SECRET_KEY=replace-this-with-a-local-development-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
```

The web app uses `web/db.sqlite3` when `DATABASE_URL` is unset. Apply database
migrations, then start the development server:

```bash
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000. If the original Telegram bot database is present at
the repository root and contains prompts, import them into the web database
with:

```bash
python manage.py import_prompts
```

When using PostgreSQL, set `DATABASE_URL` in the environment. For production,
also set a strong `SECRET_KEY`, `DEBUG=False`, and `ALLOWED_HOSTS` to the
deployed hostname. Serve the app over HTTPS for PWA installation. The web
project does not yet include a hosting-provider-specific deployment
configuration; use persistent PostgreSQL storage rather than relying on an
ephemeral host filesystem for game data.

## Data and secrets

Keep `.env` files and bot tokens private. SQLite database files contain local
application data; do not include local database changes in source commits.
