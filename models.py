"""SQLite persistence for blog posts, apps, and users."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body_html TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    published_at TEXT NOT NULL,
    legacy_post_key TEXT UNIQUE,
    source_link TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_published ON posts (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_category ON posts (category);

CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    tagline TEXT,
    description TEXT,
    icon_url TEXT,
    app_store_url TEXT,
    category_tag TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_MIGRATIONS = [
    "ALTER TABLE apps ADD COLUMN app_store_data TEXT",
    "ALTER TABLE posts ADD COLUMN hidden_from_index INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE posts ADD COLUMN redirect_url TEXT",
]


def get_db_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "blog.db"


@contextmanager
def get_connection():
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass


def upsert_post(
    *,
    slug: str,
    title: str,
    body_html: str,
    summary: str | None,
    category: str | None,
    published_at: datetime,
    legacy_post_key: str | None,
    source_link: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    pub = published_at.astimezone(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO posts (
                slug, title, body_html, summary, category, published_at,
                legacy_post_key, source_link, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                title = excluded.title,
                body_html = excluded.body_html,
                summary = excluded.summary,
                category = excluded.category,
                published_at = excluded.published_at,
                legacy_post_key = COALESCE(excluded.legacy_post_key, posts.legacy_post_key),
                source_link = COALESCE(excluded.source_link, posts.source_link),
                updated_at = excluded.updated_at
            """,
            (
                slug,
                title,
                body_html,
                summary,
                category,
                pub,
                legacy_post_key,
                source_link,
                now,
            ),
        )


def list_posts(limit: int = 200, offset: int = 0, include_hidden: bool = False):
    with get_connection() as conn:
        where = "" if include_hidden else "WHERE COALESCE(hidden_from_index, 0) = 0"
        cur = conn.execute(
            f"""
            SELECT id, slug, title, summary, category, published_at, legacy_post_key
            FROM posts
            {where}
            ORDER BY datetime(published_at) DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return cur.fetchall()


def search_posts(query: str, limit: int = 200, offset: int = 0):
    with get_connection() as conn:
        pattern = f"%{query}%"
        cur = conn.execute(
            """
            SELECT id, slug, title, summary, category, published_at, legacy_post_key
            FROM posts
            WHERE title LIKE ? OR summary LIKE ? OR body_html LIKE ? OR category LIKE ?
            ORDER BY datetime(published_at) DESC
            LIMIT ? OFFSET ?
            """,
            (pattern, pattern, pattern, pattern, limit, offset),
        )
        return cur.fetchall()


def count_search_posts(query: str) -> int:
    with get_connection() as conn:
        pattern = f"%{query}%"
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM posts
            WHERE title LIKE ? OR summary LIKE ? OR body_html LIKE ? OR category LIKE ?
            """,
            (pattern, pattern, pattern, pattern),
        )
        return cur.fetchone()[0]


def get_post_by_slug(slug: str):
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM posts WHERE slug = ?",
            (slug,),
        )
        return cur.fetchone()


def get_post_by_legacy_key(key: str):
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM posts WHERE legacy_post_key = ? OR slug = ?",
            (key, key),
        )
        return cur.fetchone()


def list_posts_by_category(category: str, limit: int = 200, offset: int = 0):
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT slug, title, summary, category, published_at, legacy_post_key
            FROM posts
            WHERE LOWER(category) = LOWER(?)
            ORDER BY datetime(published_at) DESC
            LIMIT ? OFFSET ?
            """,
            (category, limit, offset),
        )
        return cur.fetchall()


def upsert_app(
    *,
    slug: str,
    name: str,
    tagline: str | None,
    description: str | None,
    icon_url: str | None,
    app_store_url: str | None,
    category_tag: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO apps (slug, name, tagline, description, icon_url, app_store_url, category_tag, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name,
                tagline = excluded.tagline,
                description = excluded.description,
                icon_url = excluded.icon_url,
                app_store_url = excluded.app_store_url,
                category_tag = excluded.category_tag,
                updated_at = excluded.updated_at
            """,
            (slug, name, tagline, description, icon_url, app_store_url, category_tag, now),
        )


def update_app_store_data(app_id: int, data_json: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE apps SET app_store_data = ?, updated_at = ? WHERE id = ?",
            (data_json, now, app_id),
        )


def list_apps():
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM apps ORDER BY name")
        return cur.fetchall()


def get_app_by_slug(slug: str):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM apps WHERE slug = ?", (slug,))
        return cur.fetchone()


def get_app_by_id(app_id: int):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,))
        return cur.fetchone()


def create_app(
    *,
    slug: str,
    name: str,
    tagline: str | None,
    description: str | None,
    icon_url: str | None,
    app_store_url: str | None,
    category_tag: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO apps (slug, name, tagline, description, icon_url, app_store_url, category_tag, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (slug, name, tagline, description, icon_url, app_store_url, category_tag, now),
        )
        return cur.lastrowid


def update_app(
    app_id: int,
    *,
    slug: str,
    name: str,
    tagline: str | None,
    description: str | None,
    icon_url: str | None,
    app_store_url: str | None,
    category_tag: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE apps SET slug=?, name=?, tagline=?, description=?, icon_url=?,
                app_store_url=?, category_tag=?, updated_at=?
            WHERE id=?
            """,
            (slug, name, tagline, description, icon_url, app_store_url, category_tag, now, app_id),
        )


def delete_app(app_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


# --- Post CRUD ---

def get_post_by_id(post_id: int):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return cur.fetchone()


def create_post(
    *,
    slug: str,
    title: str,
    body_html: str,
    summary: str | None,
    category: str | None,
    published_at: str,
    hidden_from_index: bool = False,
    redirect_url: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO posts (slug, title, body_html, summary, category, published_at, hidden_from_index, redirect_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (slug, title, body_html, summary, category, published_at, int(hidden_from_index), redirect_url, now),
        )
        return cur.lastrowid


def update_post(
    post_id: int,
    *,
    slug: str,
    title: str,
    body_html: str,
    summary: str | None,
    category: str | None,
    published_at: str,
    hidden_from_index: bool = False,
    redirect_url: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE posts SET slug=?, title=?, body_html=?, summary=?, category=?,
                published_at=?, hidden_from_index=?, redirect_url=?, updated_at=?
            WHERE id=?
            """,
            (slug, title, body_html, summary, category, published_at, int(hidden_from_index), redirect_url, now, post_id),
        )


def delete_post(post_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))


def count_posts() -> int:
    with get_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM posts")
        return cur.fetchone()[0]


def count_apps() -> int:
    with get_connection() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM apps")
        return cur.fetchone()[0]


# --- Users ---

def create_user(username: str, password: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), now),
        )
        return cur.lastrowid


def change_password(username: str, password: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(password), username),
        )
        return cur.rowcount > 0


def get_user_by_username(username: str):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        return cur.fetchone()


def get_user_by_id(user_id: int):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cur.fetchone()
