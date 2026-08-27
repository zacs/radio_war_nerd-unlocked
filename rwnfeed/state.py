"""The manifest of episodes already published.

State lives in the bucket next to the feed, so the container itself stays
disposable: a fresh cron run in a fresh container picks up exactly where the
last one left off.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class StoredEpisode:
    post_id: str
    title: str
    published_at: str
    page_url: str
    audio_key: str
    audio_url: str
    audio_size: int
    audio_type: str
    duration_seconds: int | None = None
    art_key: str | None = None
    art_url: str | None = None
    summary: str = ""
    description_html: str = ""
    added_at: str = ""

    @property
    def published_datetime(self) -> datetime:
        try:
            parsed = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


@dataclass
class State:
    version: int = STATE_VERSION
    updated_at: str = ""
    episodes: dict[str, StoredEpisode] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "State":
        if not payload:
            return cls()
        episodes: dict[str, StoredEpisode] = {}
        raw_episodes = payload.get("episodes") or {}
        if isinstance(raw_episodes, list):  # tolerate an older list shape
            raw_episodes = {item.get("post_id"): item for item in raw_episodes}
        for post_id, item in raw_episodes.items():
            if not isinstance(item, dict):
                continue
            known = {k: v for k, v in item.items() if k in StoredEpisode.__annotations__}
            try:
                episodes[str(post_id)] = StoredEpisode(**known)
            except TypeError as exc:
                log.warning("dropping malformed state entry %s: %s", post_id, exc)
        return cls(
            version=int(payload.get("version") or STATE_VERSION),
            updated_at=payload.get("updated_at") or "",
            episodes=episodes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "episodes": {k: asdict(v) for k, v in self.episodes.items()},
        }

    def has(self, post_id: str) -> bool:
        return str(post_id) in self.episodes

    def add(self, episode: StoredEpisode) -> None:
        episode.added_at = episode.added_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        self.episodes[str(episode.post_id)] = episode

    def sorted_episodes(self) -> list[StoredEpisode]:
        return sorted(
            self.episodes.values(), key=lambda e: e.published_datetime, reverse=True
        )

    def extend(self, episodes: Iterable[StoredEpisode]) -> None:
        for episode in episodes:
            self.add(episode)
