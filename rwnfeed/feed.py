"""Render the podcast RSS document."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import format_datetime
from typing import Iterable

from .config import FeedConfig
from .state import StoredEpisode

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)


def _itunes(tag: str) -> str:
    return f"{{{ITUNES_NS}}}{tag}"


def _text(parent: ET.Element, tag: str, value: str | None) -> ET.Element | None:
    if value is None:
        return None
    element = ET.SubElement(parent, tag)
    element.text = value
    return element


def format_duration(seconds: int | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_feed(
    config: FeedConfig,
    episodes: Iterable[StoredEpisode],
    *,
    feed_url: str,
    default_image: str | None = None,
) -> bytes:
    episodes = list(episodes)
    if config.max_items > 0:
        episodes = episodes[: config.max_items]

    # Namespace declarations come from register_namespace() above; setting
    # xmlns:* by hand here would emit them twice.
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    _text(channel, "title", config.title)
    _text(channel, "link", config.link)
    _text(channel, "description", config.description)
    _text(channel, "language", config.language)
    _text(channel, "generator", "rwnfeed")
    if config.copyright:
        _text(channel, "copyright", config.copyright)
    if episodes:
        _text(channel, "lastBuildDate", format_datetime(episodes[0].published_datetime))

    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    _text(channel, _itunes("author"), config.author)
    _text(channel, _itunes("summary"), config.description)
    _text(channel, _itunes("explicit"), "true" if config.explicit else "false")
    _text(channel, _itunes("type"), "episodic")

    owner = ET.SubElement(channel, _itunes("owner"))
    _text(owner, _itunes("name"), config.owner_name)
    _text(owner, _itunes("email"), config.owner_email)

    channel_image = config.image_url or default_image
    if channel_image:
        ET.SubElement(channel, _itunes("image"), {"href": channel_image})
        image = ET.SubElement(channel, "image")
        _text(image, "url", channel_image)
        _text(image, "title", config.title)
        _text(image, "link", config.link)

    for category in config.categories:
        ET.SubElement(channel, _itunes("category"), {"text": category})

    for episode in episodes:
        item = ET.SubElement(channel, "item")
        _text(item, "title", episode.title)
        _text(item, "link", episode.page_url)
        _text(item, "pubDate", format_datetime(episode.published_datetime))

        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = f"patreon-post-{episode.post_id}"

        summary = episode.summary or _strip_html(episode.description_html)
        _text(item, "description", summary or episode.title)
        if episode.description_html:
            content = ET.SubElement(item, f"{{{CONTENT_NS}}}encoded")
            content.text = episode.description_html
        _text(item, _itunes("summary"), summary or episode.title)
        _text(item, _itunes("author"), config.author)
        _text(item, _itunes("explicit"), "true" if config.explicit else "false")
        _text(item, _itunes("episodeType"), "full")

        duration = format_duration(episode.duration_seconds)
        if duration:
            _text(item, _itunes("duration"), duration)

        art = episode.art_url or channel_image
        if art:
            ET.SubElement(item, _itunes("image"), {"href": art})

        ET.SubElement(
            item,
            "enclosure",
            {
                "url": episode.audio_url,
                "length": str(episode.audio_size or 0),
                "type": episode.audio_type or "audio/mpeg",
            },
        )

    ET.indent(rss, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="utf-8", xml_declaration=False
    )


def _strip_html(html: str) -> str:
    import re
    from html import unescape

    text = re.sub(r"<br\s*/?>", "\n", html or "")
    text = re.sub(r"</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()
