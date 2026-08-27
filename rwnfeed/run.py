"""One pass: fetch unlocked posts, mirror new ones to R2, rebuild the feed."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import requests

from .config import AppConfig
from .feed import build_feed
from .media import audio_duration_seconds, download
from .patreon import Episode, PatreonClient
from .state import State, StoredEpisode
from .storage import R2Storage

log = logging.getLogger(__name__)


def _session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    return session


def _store_episode(
    episode: Episode,
    config: AppConfig,
    storage: R2Storage,
    session: requests.Session,
    work_dir: Path,
) -> StoredEpisode:
    """Download one episode (plus art) and push it to the bucket."""
    audio_key_base = config.storage.key("episodes", episode.slug)
    staging = work_dir / episode.slug
    staging.mkdir(parents=True, exist_ok=True)

    log.info("downloading audio for %s", episode.title)
    audio = download(
        session,
        episode.audio_url,
        staging / "audio",
        timeout=config.patreon.request_timeout,
        max_retries=config.patreon.max_retries,
        fallback_extension=".mp3",
    )
    audio_key = f"{audio_key_base}{audio.extension}"
    audio_url = storage.upload_file(audio.path, audio_key, audio.content_type)

    duration = episode.declared_duration or audio_duration_seconds(audio.path)

    art_key: str | None = None
    art_url: str | None = None
    if config.download_art and episode.art_url:
        try:
            art = download(
                session,
                episode.art_url,
                staging / "art",
                timeout=config.patreon.request_timeout,
                max_retries=2,
                fallback_extension=".jpg",
            )
            art_key = f"{config.storage.key('art', episode.slug)}{art.extension}"
            art_url = storage.upload_file(art.path, art_key, art.content_type)
        except Exception as exc:  # noqa: BLE001 - art is optional, audio is not
            log.warning("could not mirror art for %s: %s", episode.title, exc)

    shutil.rmtree(staging, ignore_errors=True)

    return StoredEpisode(
        post_id=episode.post_id,
        title=episode.title,
        published_at=episode.published_at.isoformat(timespec="seconds"),
        page_url=episode.page_url,
        audio_key=audio_key,
        audio_url=audio_url,
        audio_size=audio.size,
        audio_type=audio.content_type,
        duration_seconds=duration,
        art_key=art_key,
        art_url=art_url,
        summary=episode.summary,
        description_html=episode.description_html,
    )


def run_once(config: AppConfig) -> int:
    """Returns the number of newly published episodes."""
    storage = R2Storage(config.storage, dry_run=config.dry_run)
    state_key = config.storage.key(config.storage.state_key)
    feed_key = config.storage.key(config.storage.feed_key)

    state = State.from_dict(storage.get_json(state_key))
    log.info("state has %d episode(s)", len(state.episodes))

    client = PatreonClient(config.patreon)
    episodes = client.fetch_unlocked_episodes()

    fresh = [e for e in episodes if not state.has(e.post_id)]
    if config.max_new_per_run > 0 and len(fresh) > config.max_new_per_run:
        log.info(
            "limiting this run to %d of %d new episodes",
            config.max_new_per_run,
            len(fresh),
        )
        fresh = fresh[: config.max_new_per_run]

    log.info("%d new episode(s) to mirror", len(fresh))

    work_dir = Path(config.work_dir) / "staging"
    work_dir.mkdir(parents=True, exist_ok=True)
    session = _session(config.patreon.user_agent)

    added = 0
    # Oldest first, so an interrupted run leaves a contiguous back catalogue.
    for episode in sorted(fresh, key=lambda e: e.published_at):
        if config.dry_run:
            log.info(
                "[dry-run] would mirror %s (%s) from %s",
                episode.title,
                episode.published_at.date(),
                episode.audio_url.split("?", 1)[0],
            )
            added += 1
            continue
        try:
            state.add(_store_episode(episode, config, storage, session, work_dir))
            added += 1
        except Exception as exc:  # noqa: BLE001 - one bad episode must not stop the run
            log.error("failed to mirror %s: %s", episode.title, exc)

    if config.dry_run:
        log.info("[dry-run] %d episode(s) would be added; feed not written", added)
        return added

    default_image = next(
        (e.art_url for e in state.sorted_episodes() if e.art_url), None
    )
    xml = build_feed(
        config.feed,
        state.sorted_episodes(),
        feed_url=config.storage.public_url(feed_key),
        default_image=default_image,
    )
    storage.upload_bytes(
        xml,
        feed_key,
        "application/rss+xml; charset=utf-8",
        cache_control=config.storage.feed_cache_control,
    )
    storage.put_json(state_key, state.to_dict())

    log.info(
        "done: %d new, %d total, feed at %s",
        added,
        len(state.episodes),
        config.storage.public_url(feed_key),
    )
    return added
