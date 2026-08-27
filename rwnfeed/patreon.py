"""Read publicly-visible posts from Patreon's JSON API.

Everything here is anonymous: no session cookie, no OAuth token. Patreon's web
front end fetches ``/api/posts`` for logged-out visitors, and posts that have
been unlocked for the public come back with ``current_user_can_view: true`` and
a usable media URL. Locked posts come back with the metadata but no media, and
we drop them.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlencode

import requests

from .config import PatreonConfig

log = logging.getLogger(__name__)

API_ROOT = "https://www.patreon.com/api"

# The field set Patreon's own web client requests. Asking for less tends to make
# the API omit post_file entirely, so we mirror the browser.
POST_FIELDS = (
    "content,current_user_can_view,embed,image,is_paid,meta_image_url,"
    "min_cents_pledged_to_view,post_file,post_metadata,post_type,published_at,"
    "patreon_url,teaser_text,thumbnail,thumbnail_url,title,url,video_preview"
)
MEDIA_FIELDS = "id,image_urls,download_url,metadata,file_name,mimetype,size_bytes,state"
CAMPAIGN_FIELDS = "avatar_photo_url,name,url,is_nsfw,summary"

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")


class PatreonError(RuntimeError):
    pass


@dataclass
class Episode:
    """One unlocked audio post, normalised out of the JSON:API soup."""

    post_id: str
    title: str
    published_at: datetime
    audio_url: str
    audio_filename: str
    page_url: str
    description_html: str = ""
    summary: str = ""
    art_url: str | None = None
    declared_size: int | None = None
    declared_duration: int | None = None
    mimetype: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def slug(self) -> str:
        """Stable, filesystem- and URL-safe basename for stored objects."""
        base = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")[:80]
        stamp = self.published_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        return f"{stamp}-{base or 'episode'}-{self.post_id}"


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _looks_like_audio(url: str | None, mimetype: str | None = None) -> bool:
    if mimetype and mimetype.lower().startswith("audio/"):
        return True
    if not url:
        return False
    path = url.split("?", 1)[0].lower()
    return path.endswith(AUDIO_EXTENSIONS)


class PatreonClient:
    def __init__(self, config: PatreonConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    # ---------------------------------------------------------------- HTTP --

    def _get(self, url: str, *, as_json: bool = True) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            if attempt:
                delay = 2**attempt
                log.warning("retrying %s in %ss (%s)", url, delay, last_error)
                time.sleep(delay)
            try:
                response = self.session.get(url, timeout=self.config.request_timeout)
            except requests.RequestException as exc:
                last_error = exc
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = PatreonError(f"HTTP {response.status_code} from {url}")
                continue
            if response.status_code >= 400:
                raise PatreonError(
                    f"HTTP {response.status_code} from {url}: {response.text[:200]}"
                )
            if not as_json:
                return response.text
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
        raise PatreonError(f"giving up on {url}: {last_error}")

    # ------------------------------------------------------------ campaign --

    def resolve_campaign_id(self) -> str:
        """Find the numeric campaign id behind the vanity URL."""
        if self.config.campaign_id:
            return self.config.campaign_id

        page_url = f"https://www.patreon.com/{self.config.vanity}"
        html = self._get(page_url, as_json=False)
        patterns = (
            r'"campaign_id"\s*:\s*"?(\d{3,})',
            r'/api/campaigns/(\d{3,})',
            r'"campaign"\s*:\s*\{[^{}]*?"id"\s*:\s*"(\d{3,})"',
            r'"id"\s*:\s*"(\d{3,})"\s*,\s*"type"\s*:\s*"campaign"',
            r'"type"\s*:\s*"campaign"\s*,\s*"id"\s*:\s*"(\d{3,})"',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                campaign_id = match.group(1)
                log.info("resolved campaign id %s for %s", campaign_id, self.config.vanity)
                return campaign_id
        raise PatreonError(
            f"could not find a campaign id on {page_url}; "
            "set PATREON_CAMPAIGN_ID explicitly"
        )

    # --------------------------------------------------------------- posts --

    def _posts_url(self, campaign_id: str, cursor: str | None) -> str:
        params = {
            "include": "campaign,attachments,audio,images,media,user",
            "fields[post]": POST_FIELDS,
            "fields[media]": MEDIA_FIELDS,
            "fields[campaign]": CAMPAIGN_FIELDS,
            "filter[campaign_id]": campaign_id,
            "filter[contains_exclusive_posts]": "true",
            "filter[is_draft]": "false",
            "sort": "-published_at",
            "page[count]": str(self.config.page_size),
            "json-api-version": "1.0",
        }
        if cursor:
            params["page[cursor]"] = cursor
        return f"{API_ROOT}/posts?{urlencode(params)}"

    def iter_posts(self, campaign_id: str) -> Iterator[tuple[dict, dict[str, dict]]]:
        """Yield ``(post, included_by_id)`` pairs, newest first."""
        cursor: str | None = None
        seen = 0
        while True:
            payload = self._get(self._posts_url(campaign_id, cursor))
            included = {
                f"{item.get('type')}:{item.get('id')}": item
                for item in payload.get("included") or []
            }
            data = payload.get("data") or []
            if not data:
                return
            for post in data:
                yield post, included
                seen += 1
                if self.config.max_posts and seen >= self.config.max_posts:
                    log.info("reached PATREON_MAX_POSTS=%s", self.config.max_posts)
                    return
            cursor = (
                (payload.get("meta") or {})
                .get("pagination", {})
                .get("cursors", {})
                .get("next")
            )
            if not cursor:
                return

    def fetch_unlocked_episodes(self, campaign_id: str | None = None) -> list[Episode]:
        campaign_id = campaign_id or self.resolve_campaign_id()
        episodes: list[Episode] = []
        seen_ids: set[str] = set()
        skipped_locked = 0
        skipped_no_audio = 0
        for post, included in self.iter_posts(campaign_id):
            post_id = str(post.get("id") or "")
            if not post_id or post_id in seen_ids:
                continue  # cursor pages can overlap
            seen_ids.add(post_id)
            attributes = post.get("attributes") or {}
            if not attributes.get("current_user_can_view", False):
                skipped_locked += 1
                continue
            episode = build_episode(post, included)
            if episode is None:
                skipped_no_audio += 1
                continue
            episodes.append(episode)
        log.info(
            "found %d unlocked audio posts (skipped %d locked, %d without audio)",
            len(episodes),
            skipped_locked,
            skipped_no_audio,
        )
        episodes.sort(key=lambda e: e.published_at, reverse=True)
        return episodes


# ------------------------------------------------------------ normalising --


def _related_media(post: dict, included: dict[str, dict]) -> list[dict]:
    """Collect the media objects a post points at, in relationship order."""
    media: list[dict] = []
    relationships = post.get("relationships") or {}
    for name in ("audio", "media", "attachments", "images"):
        rel = relationships.get(name) or {}
        data = rel.get("data")
        if data is None:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            key = f"{entry.get('type')}:{entry.get('id')}"
            item = included.get(key)
            if item is not None and item not in media:
                media.append(item)
    return media


def _extract_audio(attributes: dict, media: list[dict]) -> tuple[str, str, dict] | None:
    """Return ``(url, filename, source_attributes)`` for the episode audio."""
    post_file = attributes.get("post_file") or {}
    url = post_file.get("url")
    if _looks_like_audio(url, post_file.get("mimetype")) or (
        url and attributes.get("post_type") in {"audio_file", "podcast"}
    ):
        name = post_file.get("name") or ""
        return url, name, post_file

    for item in media:
        item_attributes = item.get("attributes") or item
        candidate = item_attributes.get("download_url") or item_attributes.get("url")
        mimetype = item_attributes.get("mimetype")
        filename = item_attributes.get("file_name") or ""
        if _looks_like_audio(candidate, mimetype) or _looks_like_audio(filename):
            return candidate, filename, item_attributes
    return None


def _extract_art(attributes: dict, media: list[dict]) -> str | None:
    image = attributes.get("image") or {}
    thumbnail = attributes.get("thumbnail") or {}
    candidates = [
        image.get("large_url"),
        image.get("url"),
        thumbnail.get("large2x"),
        thumbnail.get("large"),
        thumbnail.get("url"),
        attributes.get("thumbnail_url"),
        attributes.get("meta_image_url"),
        image.get("thumb_url"),
    ]
    for item in media:
        item_attributes = item.get("attributes") or item
        image_urls = item_attributes.get("image_urls") or {}
        candidates.extend(
            [image_urls.get("original"), image_urls.get("default"), image_urls.get("thumbnail")]
        )
    for candidate in candidates:
        if candidate:
            return candidate
    return None


def _duration_seconds(attributes: dict, source: dict) -> int | None:
    for container in (source, source.get("metadata") or {}, attributes.get("post_metadata") or {}):
        if not isinstance(container, dict):
            continue
        for key in ("duration", "duration_s", "length", "audio_duration"):
            value = container.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
    return None


def build_episode(post: dict, included: dict[str, dict]) -> Episode | None:
    """Turn one JSON:API post into an :class:`Episode`, or ``None`` if it has no audio."""
    attributes = post.get("attributes") or {}
    media = _related_media(post, included)
    audio = _extract_audio(attributes, media)
    if audio is None:
        return None
    audio_url, filename, source = audio
    if not audio_url:
        return None

    post_id = str(post.get("id") or "")
    published_at = _parse_ts(attributes.get("published_at"))
    title = (attributes.get("title") or f"Episode {post_id}").strip()
    page_url = (
        attributes.get("url")
        or attributes.get("patreon_url")
        or f"https://www.patreon.com/posts/{post_id}"
    )
    if page_url.startswith("/"):
        page_url = f"https://www.patreon.com{page_url}"

    size = source.get("size_bytes") or (source.get("metadata") or {}).get("size_bytes")

    return Episode(
        post_id=post_id,
        title=title,
        published_at=published_at,
        audio_url=audio_url,
        audio_filename=filename or f"{post_id}.mp3",
        page_url=page_url,
        description_html=attributes.get("content") or "",
        summary=(attributes.get("teaser_text") or "").strip(),
        art_url=_extract_art(attributes, media),
        declared_size=int(size) if isinstance(size, (int, float)) and size > 0 else None,
        declared_duration=_duration_seconds(attributes, source),
        mimetype=source.get("mimetype"),
        raw=post,
    )
