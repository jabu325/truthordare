# Django Game Deployment Guide

This guide records the deployment approach used for the Truth or Dare web
game: Django hosted on Render, with Neon PostgreSQL for persistent data.
Adapt names, regions, Python/Django versions, and commands to each new project.

## 1. Prepare the Django app

Before deploying, confirm the app:

- Reads `DATABASE_URL` from the process environment.
- Uses PostgreSQL when that variable is set and a local-only database for
  development otherwise.
- Includes a PostgreSQL driver and a production WSGI server such as Gunicorn
  in its deployment requirements.
- Serves collected static files using WhiteNoise or another configured static
  file host.
- Has production settings for `DEBUG=False`, a secret `SECRET_KEY`, allowed
  hosts, HTTPS/CSRF trusted origins, and proxy SSL headers.
- Has committed migrations for every database model.
- Ignores local `.env`, `.env.local`, database, and provider-link files.

Do not commit passwords, database URLs, API keys, or local production data.

## 2. Create and link the Neon database

1. Create a Neon project in the region closest to the Render service. Region
   choice is important because moving an existing project requires creating a
   new project and migrating data.
2. Select the intended production branch and database.
3. Use the pooled connection string for app traffic and the direct/unpooled
   connection string for Django migrations.
4. Put them into the hosting provider's secret environment settings:

   ```text
   DATABASE_URL=<Neon pooled PostgreSQL connection string>
   DATABASE_URL_UNPOOLED=<Neon direct PostgreSQL connection string>
   ```

   Copy the values from the provider; never put the actual credentials in
   source control or documentation.
5. For local work, store credentials in an ignored file such as
   `web/.env.local`. Load this file only for local development. Confirm the
   application is pointing to the intended Neon branch before running
   management commands that change data.

## 3. Configure the Render web service

For this repository, the Django project root is `web/`. Configure a Render
web service as follows:

```text
Root Directory: web
Python Version: 3.12.3
Build Command: pip install -r requirements.txt && python manage.py collectstatic --noinput
Start Command: test -n "$DATABASE_URL_UNPOOLED" && DATABASE_URL="$DATABASE_URL_UNPOOLED" python manage.py migrate && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT
```

The start command fails rather than starting if the direct URL is missing. It
temporarily selects the direct URL for migrations, then starts Gunicorn with
the normal pooled `DATABASE_URL`.

The Render Blueprint is in `render.yaml`. A manually created Render service
does not necessarily adopt Blueprint changes; configure its Root Directory,
commands, and environment variables in the dashboard. A Blueprint-managed
service can be synchronized from the Blueprint.

Set at least:

```text
DEBUG=False
SECRET_KEY=<strong generated secret>
DATABASE_URL=<pooled Neon URL>
DATABASE_URL_UNPOOLED=<direct Neon URL>
```

The app recognizes Render's `RENDER_EXTERNAL_HOSTNAME`. For custom domains,
configure Django allowed hosts and CSRF trusted origins as needed.

## 4. Deploy and verify

Push the application and migration files to the Git branch connected to
Render, then watch the deploy logs. Verify:

1. Dependencies install successfully.
2. `collectstatic` completes.
3. The start command applies migrations using the direct database connection.
4. Gunicorn starts and binds to `0.0.0.0:$PORT`.
5. The public HTTPS homepage, CSS, images, and PWA manifest load.
6. A room can be created and joined, and a complete round is saved and visible
   after refreshing.
7. `/admin/` and `/management/` require a Django staff login.

If Render reports that `manage.py` is missing, verify that **Root Directory**
is set to `web`; the file is not at the repository root.

## 5. Seed initial data

Migrations create tables but do not automatically populate game prompts. After
the first deploy succeeds, run the import command from a local checkout whose
environment points to the same Neon production branch:

```bash
cd web
.venv/bin/python manage.py import_prompts
```

This repository's command reads prompts from the original bot's
`truth_or_dare.db` at the repository root and inserts missing prompts into the
configured Django database. It skips duplicates. Keep the source database
available locally; do not upload or commit a modified SQLite database just to
perform the import.

The command runs on your computer and connects directly to Neon. Render Shell
is not required. Check the printed imported/skipped/total counts before
proceeding.

## 6. Create the first Django admin account

After migrations succeed, create the account locally while Django is configured
with the intended Neon `DATABASE_URL`:

```bash
cd web
.venv/bin/python manage.py createsuperuser
```

Enter credentials in the terminal prompt; never put the password in a command
or commit it. The account is stored in the database, so creating it against a
local SQLite fallback does not create it on Neon.

Use the account to sign in at `/admin/` and the staff-only game records
dashboard at `/management/`.

## 7. Operational notes

- Keep Render and Neon secrets in their environment-variable settings.
- Render's free web services may spin down while idle, and their local
  filesystem is ephemeral. Neon is the persistent database; do not use a
  Render-local SQLite database for production records.
- PWA installation requires HTTPS, which Render provides for its service
  domain.
- If changing the Render service name, a manually configured service's name
  must be changed in the Render dashboard. Editing `render.yaml` affects
  Blueprint-managed resources, not necessarily an existing manual service.
- Review current Render and Neon plan limits before relying on free tiers for
  production. Free plans may have quotas, cold starts, and service limitations.
