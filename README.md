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
- Staff-only web game records dashboard using Django admin accounts.
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

The staff-only game records dashboard is available at `/management/`. Sign in
with the same Django staff account used for `/admin/`. Create the first admin
account locally with `python manage.py createsuperuser` while the web app's
database environment is configured.

When using PostgreSQL, set `DATABASE_URL` in the environment. For production,
also set a strong `SECRET_KEY`, `DEBUG=False`, and `ALLOWED_HOSTS` to the
deployed hostname. Serve the app over HTTPS for PWA installation. The web
app includes a Render deployment configuration; use persistent PostgreSQL
storage rather than relying on an ephemeral host filesystem for game data.

### Cloudinary proof media

Create a Cloudinary account and set its `CLOUDINARY_URL` value in
`web/.env.local` for local development and in the Render service's environment
for production.
Treat this value as a secret. Dare proof images upload to authenticated
Cloudinary storage; the app does not expose the Cloudinary delivery URL. Each
player can request the proof once through the game, though this cannot prevent
screenshots or saving an image after it has been displayed.

Image proof (JPG, PNG, GIF, or WebP) is optional and limited to 5 MB per image.
MP4 and WebM video proof is also supported up to 25 MB per video. Players can
view proof once; staff can replay proofs in `/management`. The other player
must review every answer with a thumbs up or thumbs down. A thumbs-down costs
the answering player one of five hearts; losing all hearts ends the game.
Either player can request a restart at any time; both players must confirm
before the fresh setup begins. Earlier rounds remain in the room history.

### Deploying on Render

The repository includes a Render Blueprint in `render.yaml`. In Render, create
a new Blueprint from this repository and provide `DATABASE_URL` and
`DATABASE_URL_UNPOOLED` when prompted. Set `DATABASE_URL` to the pooled
PostgreSQL connection URL for normal web traffic and `DATABASE_URL_UNPOOLED` to
the direct connection URL for startup migrations. The Blueprint installs the
web requirements, collects static files during the build, applies migrations
using the direct connection, and launches Django through Gunicorn using the
pooled connection.

The Render hostname is added to Django's allowed hosts and trusted HTTPS
origins automatically. If you use a custom domain, add it to both the
`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` environment variables, as applicable.
Do not set `DEBUG=True` in production.

For an existing manually configured Render service, set its root directory to
`web`, use the build and start commands from `render.yaml`, and add both
database URLs to its environment. After the first successful deploy creates the
schema, import prompts once from a local checkout linked to the same Neon
branch:

```bash
cd web
.venv/bin/python manage.py import_prompts
```

The import command skips prompts that already exist, so it can safely be run
again if needed.

## Data and secrets

Keep `.env` files and bot tokens private. SQLite database files contain local
application data; do not include local database changes in source commits.
