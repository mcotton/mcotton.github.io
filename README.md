# mcottondesign blog

A minimal Flask blog with an admin interface, iOS app landing pages with App Store integration, and image management. Runs in Docker with SQLite.

## Quick start

```bash
docker compose up --build -d
docker compose exec web flask create-admin USERNAME PASSWORD
```

The site is available at `http://localhost:5001`. Log in at `/admin/login`.

## CLI commands

All commands run inside the container:

```bash
# Import posts from mcottondesign.com
docker compose exec web flask import-posts

# Import from a specific URL or mode
docker compose exec web flask import-posts --url https://example.com/feed.xml
docker compose exec web flask import-posts --mode legacy

# Create an admin user
docker compose exec web flask create-admin USERNAME PASSWORD

# Change a user's password
docker compose exec web flask change-password USERNAME PASSWORD

# Fetch App Store metadata and icons for all apps
docker compose exec web flask sync-apps

# Regenerate the static RSS feed
docker compose exec web flask generate-feed

# Generate summaries for posts missing them
docker compose exec web flask backfill-summaries

# Build the static site (for GitHub Pages)
docker compose exec web flask build-static --base-url https://mcottondesign.com
```

`flask sync-apps` and `flask generate-feed` also run automatically on container startup (see `entrypoint.sh`).

## Static site & GitHub Pages

The Flask app is also a static-site generator. Run `flask build-static` to write the whole public site into `docs/`. The output works as-is on GitHub Pages.

Author flow:
1. Run the local Flask app, edit posts/apps in `/admin`.
2. `docker compose exec web flask build-static`
3. `git add docs && git commit -m "rebuild" && git push`
4. In repo settings, set GitHub Pages source to **main / docs**.

What the build produces:
- `docs/index.html` and `docs/page/N/index.html` for pagination
- `docs/post/<slug>/index.html` for every post (including hidden ones, accessible by direct link)
- `docs/post/<legacy_post_key>/index.html` as a meta-refresh redirect when a post's slug differs from its legacy key (preserves old URLs)
- `docs/apps/index.html` and `docs/apps/<slug>/index.html`
- `docs/feed.xml` (with absolute URLs from `--base-url`)
- `docs/static/` (CSS, uploads, app icons) and `docs/img/` (post images)
- `.nojekyll` to skip Jekyll processing
- Custom-page posts are pre-baked with the `<base>` tag injected
- External-redirect posts become meta-refresh redirect pages

## Admin interface

Navigate to `/admin/login` and sign in with the credentials you created.

From the admin dashboard you can:

- **Posts** -- Create, edit, and delete blog posts. Paginated, with full-text search across title, summary, body, and category. Each post has:
  - Title, slug, category, summary, body HTML, publish date
  - **Hide from blog index** -- post still appears on app pages and is accessible by direct link
  - **Custom page** -- if set to a path inside `static/` (e.g. `/static/my-infographic.html`), that file is served at the post's URL while the URL stays at `/post/<slug>`. A `<base>` tag is auto-injected so relative asset paths resolve. External URLs (http(s)://) redirect instead.
- **Apps** -- Create, edit, and delete iOS app entries. Each app has a name, slug, tagline, description, icon, App Store URL, and a category tag that links it to blog posts.
- **Media** -- Library of every image in `static/uploads/` and `static/img/` with thumbnails and copyable URLs for pasting into post HTML.

## Custom page posts (interactive infographics)

Drop the HTML file (and any associated assets) into `static/`, e.g. `static/my-infographic.html`. Create a post in the admin and set the **Custom page** field to `/static/my-infographic.html`. Visiting `/post/<slug>` serves your HTML inline -- the URL stays put, the post still appears in the blog index, RSS feed, and app pages with its title and summary.

A `<base href="/static/">` (or matching directory) tag is auto-injected, so the HTML can use relative paths for its CSS, JS, and images.

## App pages

Each app gets a marketing-style landing page at `/apps/<slug>` that displays:

- App icon, name, and tagline
- App Store metadata (rating, price, version, screenshots) pulled from the iTunes Lookup API via `flask sync-apps`
- A "Download on the App Store" button
- Blog posts whose category matches the app's `category_tag` (case-insensitive)

Apps appear in a dropdown menu in the site header (hover on desktop, tap on mobile).

## RSS feed

`/feed.xml` is a static file at `static/feed.xml`, regenerated automatically when:

- A post is created, edited, or deleted in the admin
- Posts are imported via `flask import-posts`
- The container starts (via `entrypoint.sh`)

To regenerate manually: `flask generate-feed`.

## Project structure

```
app.py              Flask application, routes, and CLI commands
models.py           SQLite schema, migrations, and data access functions
import_data.py      RSS/Atom and legacy HTML post importer (downloads images locally)
entrypoint.sh       Runs sync-apps and generate-feed on container start
templates/          Jinja2 templates
  base.html         Site layout with nav dropdown (hover desktop, tap mobile)
  index.html        Blog index with pagination
  post.html         Single post view
  apps.html         App listing grid
  app_detail.html   App marketing page (App Store data + tagged posts)
  admin/            Admin interface templates
static/
  style.css         All styles (public + admin)
  img/              Downloaded post images and app icons
  uploads/          Admin-uploaded images
  feed.xml          Static RSS feed (regenerated on post changes)
data/
  blog.db           SQLite database (bind-mounted to host)
Dockerfile          Python 3.12-slim, gunicorn with gthread workers
docker-compose.yml  Service definition with bind mounts
```

## Data storage

Everything stateful lives in bind mounts so it can be backed up and rsync'd:

- **SQLite database** at `data/blog.db`
- **All static files** (`static/`) including downloaded post images, uploaded images, app icons, and the static RSS feed

## Importing posts

The importer (`flask import-posts`) fetches from `https://mcottondesign.com/blog/feed` by default. It auto-detects whether the URL returns RSS/Atom or a legacy HTML listing page. The legacy scraper:

- Discovers `/post/{key}` links on the listing page
- Scrapes each post's title, body HTML, category, and publish date
- Downloads referenced images to `static/img/`
- Preserves embedded content (YouTube iframes, etc.)

Re-running the import updates existing posts (upsert on slug).
