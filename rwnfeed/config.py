"""Configuration, loaded entirely from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise ConfigError(f"missing required environment variable {name}")
    return value


def _pattern(name: str, default: str) -> str:
    """Like _env, but an explicitly empty value disables the pattern."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


@dataclass(frozen=True)
class StorageConfig:
    """Cloudflare R2 (S3-compatible) target for episodes, art and the feed."""

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    endpoint_url: str
    public_base_url: str
    prefix: str
    feed_key: str
    state_key: str
    feed_cache_control: str
    media_cache_control: str

    @classmethod
    def from_env(cls) -> "StorageConfig":
        account_id = _required("R2_ACCOUNT_ID")
        endpoint = _env(
            "R2_ENDPOINT_URL", f"https://{account_id}.r2.cloudflarestorage.com"
        )
        prefix = (_env("R2_PREFIX", "") or "").strip("/")
        return cls(
            account_id=account_id,
            access_key_id=_required("R2_ACCESS_KEY_ID"),
            secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
            bucket=_required("R2_BUCKET"),
            endpoint_url=endpoint,
            public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
            prefix=prefix,
            feed_key=_env("FEED_KEY", "feed.xml"),
            state_key=_env("STATE_KEY", "state.json"),
            feed_cache_control=_env("FEED_CACHE_CONTROL", "public, max-age=300"),
            media_cache_control=_env(
                "MEDIA_CACHE_CONTROL", "public, max-age=31536000, immutable"
            ),
        )

    def key(self, *parts: str) -> str:
        """Join an object key, honouring the optional bucket prefix."""
        pieces = [p.strip("/") for p in parts if p and p.strip("/")]
        if self.prefix:
            pieces.insert(0, self.prefix)
        return "/".join(pieces)

    def public_url(self, key: str) -> str:
        from urllib.parse import quote

        return f"{self.public_base_url}/{quote(key)}"


@dataclass(frozen=True)
class FeedConfig:
    """Channel-level metadata for the generated RSS document."""

    title: str
    description: str
    link: str
    language: str
    author: str
    owner_name: str
    owner_email: str
    categories: tuple[str, ...]
    explicit: bool
    image_url: str | None
    copyright: str | None
    max_items: int

    @classmethod
    def from_env(cls) -> "FeedConfig":
        categories = tuple(
            c.strip()
            for c in (_env("FEED_CATEGORIES", "News,Politics") or "").split(",")
            if c.strip()
        )
        return cls(
            title=_env("FEED_TITLE", "Radio War Nerd (Unlocked)"),
            description=_env(
                "FEED_DESCRIPTION",
                "Publicly unlocked episodes of Radio War Nerd, collected from Patreon.",
            ),
            link=_env("FEED_LINK", "https://www.patreon.com/radiowarnerd"),
            language=_env("FEED_LANGUAGE", "en-us"),
            author=_env("FEED_AUTHOR", "Radio War Nerd"),
            owner_name=_env("FEED_OWNER_NAME", _env("FEED_AUTHOR", "Radio War Nerd")),
            owner_email=_env("FEED_OWNER_EMAIL", "nobody@example.com"),
            categories=categories or ("News",),
            explicit=_bool("FEED_EXPLICIT", True),
            image_url=_env("FEED_IMAGE_URL"),
            copyright=_env("FEED_COPYRIGHT"),
            max_items=_int("FEED_MAX_ITEMS", 0),
        )


# Teaser clips, not episodes: the campaign posts a short "FREE PREVIEW" of an
# otherwise paywalled episode. Deliberately narrow -- a title like
# "FREE REPOST ... [UNLOCKED]" is a full episode and must survive this.
DEFAULT_EXCLUDE_TITLE_PATTERN = r"\bfree\s+preview\b|^\s*preview\b"


@dataclass(frozen=True)
class PatreonConfig:
    """How to reach Patreon's public JSON API. No login, no cookies."""

    vanity: str
    campaign_id: str | None
    user_agent: str
    max_posts: int
    page_size: int
    request_timeout: int
    max_retries: int
    exclude_title_pattern: str

    @classmethod
    def from_env(cls) -> "PatreonConfig":
        return cls(
            vanity=_env("PATREON_VANITY", "radiowarnerd"),
            campaign_id=_env("PATREON_CAMPAIGN_ID"),
            user_agent=_env(
                "PATREON_USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            ),
            max_posts=_int("PATREON_MAX_POSTS", 500),
            page_size=_int("PATREON_PAGE_SIZE", 20),
            request_timeout=_int("HTTP_TIMEOUT_SECONDS", 60),
            max_retries=_int("HTTP_MAX_RETRIES", 4),
            exclude_title_pattern=_pattern(
                "EXCLUDE_TITLE_PATTERN", DEFAULT_EXCLUDE_TITLE_PATTERN
            ),
        )


@dataclass(frozen=True)
class AppConfig:
    patreon: PatreonConfig
    storage: StorageConfig
    feed: FeedConfig
    work_dir: str
    schedule: str
    run_once: bool
    dry_run: bool
    log_level: str
    download_art: bool
    max_new_per_run: int
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, require_storage: bool = True) -> "AppConfig":
        storage = StorageConfig.from_env() if require_storage else None
        return cls(
            patreon=PatreonConfig.from_env(),
            storage=storage,
            feed=FeedConfig.from_env(),
            work_dir=_env("WORK_DIR", "/data"),
            schedule=_env("CRON_SCHEDULE", "17 */6 * * *"),
            run_once=_bool("RUN_ONCE", False),
            dry_run=_bool("DRY_RUN", False),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            download_art=_bool("DOWNLOAD_ART", True),
            max_new_per_run=_int("MAX_NEW_EPISODES_PER_RUN", 0),
        )
