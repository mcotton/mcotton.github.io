"""
Import posts from an RSS/Atom feed (feedparser) or, for mcottondesign.com,
from the legacy HTML listing at /blog/feed plus per-post pages.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from models import upsert_post

DEFAULT_UA = (
    "mcottondesign-blog-importer/1.0 (+https://mcottondesign.com; contact: rss-import)"
)

_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@contextmanager
def _without_proxy_env():
    saved = {}
    for k in _PROXY_KEYS:
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    try:
        yield
    finally:
        os.environ.update(saved)


def _fetch(url: str, timeout: int = 45) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_UA},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (OSError, urllib.error.URLError) as exc:
        err = str(exc).lower()
        if "tunnel connection failed" in err or "proxy" in err:
            direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with _without_proxy_env():
                with direct.open(req, timeout=timeout) as resp:
                    return resp.read()
        raise


def _slug_from_url(link: str) -> str:
    path = urlparse(link).path.rstrip("/")
    if not path:
        return "post"
    last = path.split("/")[-1]
    return last or "post"


def import_from_rss_bytes(raw: bytes) -> int:
    """
    Parse RSS or Atom XML from raw bytes. Returns number of entries stored.
    """
    parsed = feedparser.parse(raw)
    count = 0
    for entry in parsed.entries:
        title = (entry.get("title") or "Untitled").strip()
        link = entry.get("link") or ""
        guid = entry.get("id") or entry.get("guid") or link
        slug = _slug_from_url(link) if link else re.sub(r"[^\w-]+", "-", title.lower()).strip("-")[
            :80
        ]
        if not slug:
            slug = re.sub(r"\W+", "-", str(guid))[:80]

        published = None
        if entry.get("published_parsed"):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        elif entry.get("updated_parsed"):
            published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        elif entry.get("published"):
            published = date_parser.parse(entry["published"])
        if published is None:
            published = datetime.now(timezone.utc)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        body_html = ""
        if entry.get("content"):
            body_html = entry["content"][0].get("value") or ""
        if not body_html:
            body_html = entry.get("summary") or entry.get("description") or ""
        body_html = body_html.strip()
        if not body_html:
            continue

        summary = entry.get("summary") or ""
        if summary == body_html:
            summary = ""
        summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:500]

        tags = entry.get("tags") or []
        category = tags[0].get("term") if tags else None

        upsert_post(
            slug=slug,
            title=unescape(title),
            body_html=body_html,
            summary=summary or None,
            category=category,
            published_at=published,
            legacy_post_key=None,
            source_link=link or None,
        )
        count += 1
    return count


def import_from_rss(feed_url: str) -> int:
    return import_from_rss_bytes(_fetch(feed_url))


_POST_META_RE = re.compile(
    r"by\s+\w+\s+on\s+(.+?)(?:\s*$|\s*\n)",
    re.I,
)


def _parse_legacy_date(meta_text: str) -> datetime:
    m = _POST_META_RE.search(meta_text)
    chunk = (m.group(1) if m else meta_text).strip()
    dt = date_parser.parse(chunk, fuzzy=True)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_post_from_html(html: bytes, page_url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    art = soup.select_one("article.post")
    if not art:
        return None
    h2 = art.select_one("header.article-title h2")
    title = h2.get_text(strip=True) if h2 else "Untitled"
    meta_el = art.select_one("p.post-meta")
    meta_text = meta_el.get_text(" ", strip=True) if meta_el else ""
    category = None
    if meta_el:
        cat_a = meta_el.select_one("a[href^='/blog/']")
        if cat_a:
            category = cat_a.get_text(strip=True)
    published = _parse_legacy_date(meta_text)
    feat = art.select_one("div.article-featured-content")
    summ = art.select_one("div.post-summary")
    if not feat and not summ:
        return None
    body_parts: list[str] = []
    for container in (feat, summ):
        if not container:
            continue
        for noise in container.select("script, style"):
            noise.decompose()
        chunk = container.decode_contents().strip()
        if chunk:
            body_parts.append(chunk)
    body_html = "\n".join(body_parts)
    if not body_html:
        return None
    plain = BeautifulSoup(body_html, "html.parser").get_text(" ", strip=True)
    summary = plain[:500] if plain else None
    key = urlparse(page_url).path.rstrip("/").split("/")[-1]
    return {
        "slug": key,
        "title": title,
        "body_html": body_html,
        "summary": summary,
        "category": category,
        "published_at": published,
        "legacy_post_key": key,
        "source_link": page_url,
    }


def _download_images(body_html: str, base_url: str, static_dir: str) -> None:
    """Download images with relative src paths and save locally."""
    soup = BeautifulSoup(body_html, "html.parser")
    for img in soup.select("img[src]"):
        src = img["src"]
        if not src.startswith("/"):
            continue
        local_path = os.path.join(static_dir, src.lstrip("/"))
        if os.path.exists(local_path):
            continue
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            data = _fetch(base_url + src)
            with open(local_path, "wb") as f:
                f.write(data)
        except Exception:
            pass


def import_from_legacy_listing(
    listing_url: str = "https://mcottondesign.com/blog/feed",
    *,
    max_posts: int | None = None,
) -> int:
    """
    Discover /post/{key} links on the listing page and scrape each article.
    """
    base = f"{urlparse(listing_url).scheme}://{urlparse(listing_url).netloc}"
    html = _fetch(listing_url).decode("utf-8", errors="replace")
    hrefs = set(re.findall(r'href="(/post/[^"]+)"', html))
    urls = [urljoin(base, h) for h in sorted(hrefs)]
    if max_posts is not None:
        urls = urls[:max_posts]
    static_dir = str(Path(__file__).resolve().parent / "static")
    count = 0
    for url in urls:
        raw = _fetch(url)
        data = _extract_post_from_html(raw, url)
        if not data:
            continue
        _download_images(data["body_html"], base, static_dir)
        upsert_post(**data)
        count += 1
    return count


def import_auto(feed_url: str) -> tuple[str, int]:
    """
    If the URL returns RSS/Atom with entries, use feedparser; otherwise treat
    the page as the legacy HTML listing (as on mcottondesign.com today).
    """
    raw = _fetch(feed_url)
    head = raw.lstrip()[:12000].lower()
    looks_feed = b"<rss" in head or (b"<feed" in head and b"xmlns" in head)
    if looks_feed:
        n = import_from_rss_bytes(raw)
        if n > 0:
            return "rss", n
    return "legacy", import_from_legacy_listing(feed_url)
