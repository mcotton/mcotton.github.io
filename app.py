from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
import uuid

import click
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import wraps
from html import escape

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from import_data import import_auto, import_from_legacy_listing, import_from_rss
from models import (
    count_apps,
    count_posts,
    count_search_posts,
    create_app as create_app_record,
    create_post,
    create_user,
    delete_app as delete_app_record,
    delete_post,
    get_app_by_id,
    get_app_by_slug,
    get_post_by_id,
    get_post_by_legacy_key,
    get_post_by_slug,
    get_user_by_username,
    init_db,
    list_apps,
    list_posts,
    list_posts_by_category,
    search_posts,
    update_app as update_app_record,
    update_app_store_data,
    update_post,
)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}


def _parse_app_store_id(url: str) -> str | None:
    """Extract the numeric app ID from an App Store URL."""
    m = re.search(r"/id(\d+)", url or "")
    return m.group(1) if m else None


def fetch_app_store_data(app_store_url: str) -> dict | None:
    """Fetch app metadata from the iTunes Lookup API."""
    app_id = _parse_app_store_id(app_store_url)
    if not app_id:
        return None
    api_url = f"https://itunes.apple.com/lookup?id={app_id}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "mcottondesign-blog/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if not results:
            return None
        r = results[0]
        return {
            "trackName": r.get("trackName"),
            "description": r.get("description"),
            "artworkUrl512": r.get("artworkUrl512"),
            "screenshotUrls": r.get("screenshotUrls", []),
            "ipadScreenshotUrls": r.get("ipadScreenshotUrls", []),
            "averageUserRating": r.get("averageUserRating"),
            "userRatingCount": r.get("userRatingCount"),
            "price": r.get("price"),
            "formattedPrice": r.get("formattedPrice"),
            "sellerName": r.get("sellerName"),
            "genres": r.get("genres", []),
            "version": r.get("version"),
            "currentVersionReleaseDate": r.get("currentVersionReleaseDate"),
            "minimumOsVersion": r.get("minimumOsVersion"),
            "trackViewUrl": r.get("trackViewUrl"),
        }
    except Exception:
        return None


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def base_tag_escape(s: str) -> str:
    return s.replace('"', "&quot;")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-change-in-production"

    upload_folder = os.path.join(app.static_folder, "uploads")
    os.makedirs(upload_folder, exist_ok=True)
    app.config["UPLOAD_FOLDER"] = upload_folder
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

    init_db()

    # --- helpers ---

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("admin_login"))
            return f(*args, **kwargs)
        return decorated

    def save_upload(file):
        if file and file.filename and _allowed_file(file.filename):
            ext = file.filename.rsplit(".", 1)[1].lower()
            unique_name = f"{uuid.uuid4().hex}.{ext}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
            return url_for("static", filename=f"uploads/{unique_name}")
        return None

    @app.context_processor
    def _inject_globals():
        return {"current_year": datetime.now().year, "nav_apps": list_apps()}

    # --- public routes ---

    @app.route("/img/<path:filename>")
    def legacy_image(filename: str):
        return send_from_directory(os.path.join(app.static_folder, "img"), filename)

    @app.route("/")
    @app.route("/page/<int:page>/")
    def index(page: int = 1):
        # Support legacy ?page=N during transition
        page = request.args.get("page", page, type=int)
        per_page = 20
        posts = list_posts(limit=per_page + 1, offset=(page - 1) * per_page)
        has_next = len(posts) > per_page
        posts = posts[:per_page]
        return render_template("index.html", posts=posts, page=page, has_next=has_next)

    @app.route("/post/<path:key>")
    def post(key: str):
        row = get_post_by_legacy_key(key) or get_post_by_slug(key)
        if not row:
            abort(404)
        redirect_target = row["redirect_url"] if "redirect_url" in row.keys() else None
        if redirect_target:
            # External URL: redirect
            if redirect_target.startswith(("http://", "https://")):
                return redirect(redirect_target)
            # Local path: serve file inline so URL stays at /post/<slug>
            target = redirect_target.lstrip("/")
            full_path = os.path.normpath(os.path.join(app.root_path, target))
            # Sandbox to within the project root to prevent directory traversal
            if not full_path.startswith(app.root_path) or not os.path.isfile(full_path):
                abort(404)
            with open(full_path, "r", encoding="utf-8") as f:
                html = f.read()
            # Inject <base> tag so relative asset paths resolve against the file's directory
            base_dir = "/" + os.path.dirname(target).rstrip("/") + "/"
            base_tag = f'<base href="{base_tag_escape(base_dir)}">'
            if "<head>" in html:
                html = html.replace("<head>", "<head>\n  " + base_tag, 1)
            elif "<html" in html:
                html = html.replace("<html", "<head>" + base_tag + "</head><html", 1)
            else:
                html = base_tag + html
            return Response(html, mimetype="text/html")
        return render_template("post.html", post=row)

    @app.route("/apps")
    def apps():
        all_apps = list_apps()
        return render_template("apps.html", apps=all_apps)

    @app.route("/apps/<slug>")
    def app_detail(slug: str):
        app_row = get_app_by_slug(slug)
        if not app_row:
            abort(404)
        store = None
        raw = app_row["app_store_data"] if "app_store_data" in app_row.keys() else None
        if raw:
            store = json.loads(raw)
        posts = list_posts_by_category(app_row["category_tag"])
        return render_template("app_detail.html", app=app_row, store=store, posts=posts)

    def generate_feed(base_url: str = "https://mcottondesign.com") -> None:
        """Rebuild the static feed.xml from all posts."""
        base = base_url.rstrip("/")
        posts = list_posts(limit=10000, include_hidden=False)
        items = []
        for p in posts:
            slug = p["slug"]
            link = f"{base}/post/{slug}"
            try:
                raw_ts = p["published_at"].replace("Z", "+00:00")
                pub_dt = datetime.fromisoformat(raw_ts)
                pub = escape(format_datetime(pub_dt))
            except (TypeError, ValueError):
                pub = escape(p["published_at"])
            title = escape(p["title"])
            desc = escape((p["summary"] or "")[:400])
            items.append(
                f"""    <item>
      <title>{title}</title>
      <link>{escape(link)}</link>
      <guid isPermaLink="true">{escape(link)}</guid>
      <pubDate>{pub}</pubDate>
      <description>{desc}</description>
    </item>"""
            )
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>mcottondesign</title>
    <link>{escape(base + "/")}</link>
    <description>Blog</description>
{chr(10).join(items)}
  </channel>
</rss>"""
        feed_path = os.path.join(app.static_folder, "feed.xml")
        with open(feed_path, "w", encoding="utf-8") as f:
            f.write(xml)

    app.generate_feed = generate_feed

    @app.route("/feed.xml")
    def rss_feed():
        feed_path = os.path.join(app.static_folder, "feed.xml")
        if not os.path.exists(feed_path):
            generate_feed()
        return send_from_directory(app.static_folder, "feed.xml", mimetype="application/rss+xml")

    # --- auth ---

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            user = get_user_by_username(username)
            if user and check_password_hash(user["password_hash"], password):
                session["user_id"] = user["id"]
                return redirect(url_for("admin_dashboard"))
            flash("Invalid username or password.", "error")
        return render_template("admin/login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect(url_for("index"))

    # --- admin dashboard ---

    @app.route("/admin/")
    @login_required
    def admin_dashboard():
        return render_template(
            "admin/dashboard.html",
            post_count=count_posts(),
            app_count=count_apps(),
            recent_posts=list_posts(limit=5),
        )

    # --- admin posts ---

    @app.route("/admin/posts")
    @login_required
    def admin_posts():
        page = request.args.get("page", 1, type=int)
        q = request.args.get("q", "").strip()
        per_page = 25
        if q:
            posts = search_posts(q, limit=per_page + 1, offset=(page - 1) * per_page)
            total = count_search_posts(q)
        else:
            posts = list_posts(limit=per_page + 1, offset=(page - 1) * per_page, include_hidden=True)
            total = count_posts()
        has_next = len(posts) > per_page
        posts = posts[:per_page]
        return render_template("admin/posts.html", posts=posts, page=page, has_next=has_next, q=q, total=total)

    @app.route("/admin/posts/new", methods=["GET", "POST"])
    @login_required
    def admin_post_new():
        if request.method == "POST":
            slug = request.form.get("slug", "").strip()
            title = request.form.get("title", "").strip()
            body_html = request.form.get("body_html", "")
            summary = request.form.get("summary", "").strip() or None
            category = request.form.get("category", "").strip() or None
            published_at = request.form.get("published_at", "")
            hidden_from_index = "hidden_from_index" in request.form
            redirect_url = request.form.get("redirect_url", "").strip() or None
            if not published_at:
                published_at = datetime.now(timezone.utc).isoformat()

            if not slug or not title:
                flash("Title and slug are required.", "error")
                return render_template("admin/post_form.html", post=None, form=request.form)

            try:
                create_post(
                    slug=slug, title=title, body_html=body_html,
                    summary=summary, category=category, published_at=published_at,
                    hidden_from_index=hidden_from_index,
                    redirect_url=redirect_url,
                )
            except sqlite3.IntegrityError:
                flash("A post with that slug already exists.", "error")
                return render_template("admin/post_form.html", post=None, form=request.form)

            flash("Post created.", "success")
            generate_feed()
            return redirect(url_for("admin_posts"))
        return render_template("admin/post_form.html", post=None, form={})

    @app.route("/admin/posts/<int:post_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_post_edit(post_id: int):
        post_row = get_post_by_id(post_id)
        if not post_row:
            abort(404)

        if request.method == "POST":
            slug = request.form.get("slug", "").strip()
            title = request.form.get("title", "").strip()
            body_html = request.form.get("body_html", "")
            summary = request.form.get("summary", "").strip() or None
            category = request.form.get("category", "").strip() or None
            published_at = request.form.get("published_at", "")
            hidden_from_index = "hidden_from_index" in request.form
            redirect_url = request.form.get("redirect_url", "").strip() or None
            if not published_at:
                published_at = post_row["published_at"]

            if not slug or not title:
                flash("Title and slug are required.", "error")
                return render_template("admin/post_form.html", post=post_row, form=request.form)

            try:
                update_post(
                    post_id, slug=slug, title=title, body_html=body_html,
                    summary=summary, category=category, published_at=published_at,
                    hidden_from_index=hidden_from_index,
                    redirect_url=redirect_url,
                )
            except sqlite3.IntegrityError:
                flash("A post with that slug already exists.", "error")
                return render_template("admin/post_form.html", post=post_row, form=request.form)

            flash("Post updated.", "success")
            generate_feed()
            return redirect(url_for("admin_posts"))
        return render_template("admin/post_form.html", post=post_row, form={})

    @app.route("/admin/posts/<int:post_id>/delete", methods=["POST"])
    @login_required
    def admin_post_delete(post_id: int):
        delete_post(post_id)
        generate_feed()
        flash("Post deleted.", "success")
        return redirect(url_for("admin_posts"))

    # --- admin apps ---

    @app.route("/admin/apps")
    @login_required
    def admin_apps():
        all_apps = list_apps()
        return render_template("admin/apps.html", apps=all_apps)

    @app.route("/admin/apps/new", methods=["GET", "POST"])
    @login_required
    def admin_app_new():
        if request.method == "POST":
            slug = request.form.get("slug", "").strip()
            name = request.form.get("name", "").strip()
            tagline = request.form.get("tagline", "").strip() or None
            description = request.form.get("description", "").strip() or None
            app_store_url = request.form.get("app_store_url", "").strip() or None
            category_tag = request.form.get("category_tag", "").strip()

            icon_url = None
            icon_file = request.files.get("icon")
            if icon_file and icon_file.filename:
                icon_url = save_upload(icon_file)

            if not slug or not name or not category_tag:
                flash("Name, slug, and category tag are required.", "error")
                return render_template("admin/app_form.html", app=None, form=request.form)

            try:
                create_app_record(
                    slug=slug, name=name, tagline=tagline, description=description,
                    icon_url=icon_url, app_store_url=app_store_url, category_tag=category_tag,
                )
            except sqlite3.IntegrityError:
                flash("An app with that slug already exists.", "error")
                return render_template("admin/app_form.html", app=None, form=request.form)

            flash("App created.", "success")
            return redirect(url_for("admin_apps"))
        return render_template("admin/app_form.html", app=None, form={})

    @app.route("/admin/apps/<int:app_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_app_edit(app_id: int):
        app_row = get_app_by_id(app_id)
        if not app_row:
            abort(404)

        if request.method == "POST":
            slug = request.form.get("slug", "").strip()
            name = request.form.get("name", "").strip()
            tagline = request.form.get("tagline", "").strip() or None
            description = request.form.get("description", "").strip() or None
            app_store_url = request.form.get("app_store_url", "").strip() or None
            category_tag = request.form.get("category_tag", "").strip()

            icon_url = app_row["icon_url"]
            icon_file = request.files.get("icon")
            if icon_file and icon_file.filename:
                icon_url = save_upload(icon_file)

            if not slug or not name or not category_tag:
                flash("Name, slug, and category tag are required.", "error")
                return render_template("admin/app_form.html", app=app_row, form=request.form)

            try:
                update_app_record(
                    app_id, slug=slug, name=name, tagline=tagline, description=description,
                    icon_url=icon_url, app_store_url=app_store_url, category_tag=category_tag,
                )
            except sqlite3.IntegrityError:
                flash("An app with that slug already exists.", "error")
                return render_template("admin/app_form.html", app=app_row, form=request.form)

            flash("App updated.", "success")
            return redirect(url_for("admin_apps"))
        return render_template("admin/app_form.html", app=app_row, form={})

    @app.route("/admin/apps/<int:app_id>/delete", methods=["POST"])
    @login_required
    def admin_app_delete(app_id: int):
        delete_app_record(app_id)
        flash("App deleted.", "success")
        return redirect(url_for("admin_apps"))

    # --- media ---

    @app.route("/admin/media")
    @login_required
    def admin_media():
        media = []
        for subdir in ("uploads", "img"):
            base = os.path.join(app.static_folder, subdir)
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for fname in files:
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, app.static_folder)
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                    if ext not in ALLOWED_EXTENSIONS:
                        continue
                    # URL path for use in posts
                    if rel.startswith("img/"):
                        url_path = "/" + rel
                    else:
                        url_path = url_for("static", filename=rel)
                    stat = os.stat(full)
                    media.append({
                        "filename": fname,
                        "url": url_path,
                        "folder": subdir,
                        "size_kb": round(stat.st_size / 1024),
                        "mtime": stat.st_mtime,
                    })
        media.sort(key=lambda m: m["mtime"], reverse=True)
        return render_template("admin/media.html", media=media)

    @app.route("/admin/upload", methods=["POST"])
    @login_required
    def admin_upload():
        file = request.files.get("file")
        if not file or not file.filename:
            flash("No file selected.", "error")
            return redirect(request.referrer or url_for("admin_media"))
        image_url = save_upload(file)
        if not image_url:
            flash("Invalid file type.", "error")
            return redirect(request.referrer or url_for("admin_media"))
        flash(f"Image uploaded: {image_url}", "success")
        return redirect(request.referrer or url_for("admin_media"))

    # --- CLI commands ---

    @app.cli.command("import-posts")
    @click.option(
        "--url",
        default="https://mcottondesign.com/blog/feed",
        show_default=True,
        help="RSS/Atom URL, or legacy HTML listing URL.",
    )
    @click.option(
        "--mode",
        type=click.Choice(["auto", "rss", "legacy"]),
        default="auto",
        show_default=True,
    )
    def import_posts(url: str, mode: str) -> None:
        """Import posts from RSS/Atom or legacy mcottondesign HTML."""
        init_db()
        if mode == "rss":
            n = import_from_rss(url)
            used = "rss"
        elif mode == "legacy":
            n = import_from_legacy_listing(url)
            used = "legacy"
        else:
            used, n = import_auto(url)
        click.echo(f"Imported {n} posts via {used!r} ({url}).")
        generate_feed()
        click.echo("Feed regenerated.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.argument("password")
    def cli_create_admin(username: str, password: str) -> None:
        """Create an admin user: flask create-admin USERNAME PASSWORD"""
        init_db()
        create_user(username, password)
        click.echo(f"Admin user '{username}' created.")

    @app.cli.command("change-password")
    @click.argument("username")
    @click.argument("password")
    def cli_change_password(username: str, password: str) -> None:
        """Change a user's password: flask change-password USERNAME PASSWORD"""
        from models import change_password
        if change_password(username, password):
            click.echo(f"Password updated for '{username}'.")
        else:
            click.echo(f"User '{username}' not found.", err=True)

    @app.cli.command("sync-apps")
    def sync_apps() -> None:
        """Fetch App Store metadata for all apps with an app_store_url."""
        init_db()
        all_apps = list_apps()
        n = 0
        for a in all_apps:
            url = a["app_store_url"]
            if not url:
                click.echo(f"  Skipping {a['name']}: no App Store URL")
                continue
            click.echo(f"  Fetching {a['name']}...")
            data = fetch_app_store_data(url)
            if data:
                update_app_store_data(a["id"], json.dumps(data))
                # Download icon locally
                artwork_url = data.get("artworkUrl512")
                if artwork_url:
                    icon_dir = os.path.join(app.static_folder, "img", "app-icons")
                    os.makedirs(icon_dir, exist_ok=True)
                    icon_path = os.path.join(icon_dir, f"{a['slug']}.png")
                    try:
                        req = urllib.request.Request(artwork_url, headers={"User-Agent": "mcottondesign-blog/1.0"})
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            with open(icon_path, "wb") as f:
                                f.write(resp.read())
                        icon_url = f"/img/app-icons/{a['slug']}.png"
                        from models import get_connection
                        with get_connection() as conn:
                            conn.execute("UPDATE apps SET icon_url = ? WHERE id = ?", (icon_url, a["id"]))
                        click.echo(f"    Icon saved: {icon_url}")
                    except Exception as exc:
                        click.echo(f"    Icon download failed: {exc}")
                click.echo(f"    OK: {data.get('trackName')}")
                n += 1
            else:
                click.echo(f"    Failed to fetch data")
        click.echo(f"Synced {n} app(s).")

    @app.cli.command("generate-feed")
    @click.option("--base-url", default="https://mcottondesign.com", show_default=True)
    def cli_generate_feed(base_url: str) -> None:
        """Regenerate the static feed.xml."""
        generate_feed(base_url)
        click.echo("Feed generated.")

    @app.cli.command("build-static")
    @click.option("--dest", default="docs", show_default=True,
                  help="Output directory. GitHub Pages can serve this from main /docs.")
    @click.option("--clean/--no-clean", default=True,
                  help="Delete the output directory before building.")
    @click.option("--base-url", default="https://mcottondesign.com", show_default=True,
                  help="Used for the RSS feed.")
    def build_static(dest: str, clean: bool, base_url: str) -> None:
        """Generate a static version of the site for GitHub Pages."""
        import shutil
        from pathlib import Path

        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True)
        if clean:
            for child in out.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()

        # Regenerate feed with absolute URLs first
        generate_feed(base_url)

        pages = 0
        redirects = 0

        def write_page(url_path: str, content: bytes) -> None:
            nonlocal pages
            # Strip leading slash and handle directory-style URLs
            rel = url_path.lstrip("/")
            if rel == "" or rel.endswith("/"):
                target = out / rel / "index.html"
            else:
                target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            pages += 1

        def write_redirect(url_path: str, target_url: str) -> None:
            nonlocal redirects
            html = (
                '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0; url={target_url}">'
                f'<link rel="canonical" href="{target_url}">'
                '<title>Redirecting...</title></head>'
                f'<body>Redirecting to <a href="{target_url}">{target_url}</a></body></html>'
            )
            write_page(url_path, html.encode())
            redirects += 1

        with app.test_client() as client:
            # Index + paginated pages
            from models import list_posts as _list_posts, list_apps as _list_apps
            per_page = 20  # matches the index route
            total_posts = len(_list_posts(limit=100000, include_hidden=False))
            total_pages = max(1, (total_posts + per_page - 1) // per_page)
            for page_num in range(1, total_pages + 1):
                url = "/" if page_num == 1 else f"/page/{page_num}/"
                r = client.get(url)
                if r.status_code == 200:
                    write_page(url, r.data)

            # Each post (including hidden, since they're accessible by direct URL)
            for p in _list_posts(limit=100000, include_hidden=True):
                slug = p["slug"]
                url = f"/post/{slug}/"
                r = client.get(f"/post/{slug}", follow_redirects=False)
                if r.status_code == 200:
                    write_page(url, r.data)
                elif r.status_code in (301, 302):
                    write_redirect(url, r.headers.get("Location", "/"))
                # Legacy slug redirect to canonical
                if p["legacy_post_key"] and p["legacy_post_key"] != slug:
                    write_redirect(f"/post/{p['legacy_post_key']}/", f"/post/{slug}/")

            # Apps listing + each app page
            r = client.get("/apps")
            if r.status_code == 200:
                write_page("/apps/", r.data)
            for a in _list_apps():
                r = client.get(f"/apps/{a['slug']}")
                if r.status_code == 200:
                    write_page(f"/apps/{a['slug']}/", r.data)

            # RSS feed
            r = client.get("/feed.xml")
            if r.status_code == 200:
                write_page("/feed.xml", r.data)

        # Copy static assets to /static/
        static_src = Path(app.static_folder)
        static_dst = out / "static"
        if static_dst.exists():
            shutil.rmtree(static_dst)
        shutil.copytree(static_src, static_dst)

        # Mirror static/img/ to /img/ for post bodies that reference /img/...
        img_src = static_src / "img"
        if img_src.exists():
            img_dst = out / "img"
            if img_dst.exists():
                shutil.rmtree(img_dst)
            shutil.copytree(img_src, img_dst)

        # .nojekyll prevents GitHub Pages from running Jekyll
        (out / ".nojekyll").write_text("")

        click.echo(f"Wrote {pages} pages, {redirects} redirects to {dest}/")

    @app.cli.command("backfill-summaries")
    def backfill_summaries() -> None:
        """Generate summaries for posts that are missing them."""
        from bs4 import BeautifulSoup as BS
        from models import get_connection
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, body_html FROM posts WHERE summary IS NULL OR summary = ''"
            ).fetchall()
            n = 0
            for row in rows:
                plain = BS(row["body_html"], "html.parser").get_text(" ", strip=True)
                if not plain:
                    continue
                summary = plain[:500]
                conn.execute("UPDATE posts SET summary = ? WHERE id = ?", (summary, row["id"]))
                n += 1
            click.echo(f"Backfilled {n} summaries.")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
