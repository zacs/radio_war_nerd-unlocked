"""Offline checks for post parsing, state handling and feed rendering."""

from __future__ import annotations

import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rwnfeed.config import FeedConfig  # noqa: E402
from rwnfeed.feed import ITUNES_NS, build_feed, format_duration  # noqa: E402
from rwnfeed.media import guess_extension  # noqa: E402
from rwnfeed.config import PatreonConfig  # noqa: E402
from rwnfeed.patreon import (  # noqa: E402
    PatreonClient,
    PatreonError,
    _compile_exclude,
    build_episode,
)
from rwnfeed.state import State, StoredEpisode  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixture_posts.json").read_text())


def parse_fixture():
    included = {
        f"{item['type']}:{item['id']}": item for item in FIXTURE.get("included", [])
    }
    episodes = []
    for post in FIXTURE["data"]:
        if not post["attributes"].get("current_user_can_view", False):
            continue
        episode = build_episode(post, included)
        if episode is not None:
            episodes.append(episode)
    return episodes


class TestPatreonParsing(unittest.TestCase):
    def setUp(self):
        self.episodes = parse_fixture()

    def test_only_unlocked_audio_posts_survive(self):
        ids = sorted(e.post_id for e in self.episodes)
        self.assertEqual(ids, ["98764000", "98765432"])

    def test_post_file_audio_is_preferred(self):
        episode = next(e for e in self.episodes if e.post_id == "98765432")
        self.assertTrue(episode.audio_url.endswith("ep300.mp3?token=abc"))
        self.assertEqual(episode.mimetype, "audio/mpeg")
        self.assertEqual(episode.declared_size, 51200000)
        self.assertEqual(episode.declared_duration, 4230)
        self.assertIn("large.jpg", episode.art_url)

    def test_media_relationship_is_the_fallback(self):
        episode = next(e for e in self.episodes if e.post_id == "98764000")
        self.assertTrue(episode.audio_url.endswith("ep298.m4a?token=xyz"))
        self.assertEqual(episode.declared_duration, 3600)
        self.assertIn("large2x.jpg", episode.art_url)

    def test_relative_post_urls_are_absolute(self):
        episode = next(e for e in self.episodes if e.post_id == "98765432")
        self.assertEqual(
            episode.page_url, "https://www.patreon.com/posts/ep-300-98765432"
        )

    def test_slug_is_stable_and_safe(self):
        episode = next(e for e in self.episodes if e.post_id == "98765432")
        self.assertEqual(episode.slug, "2026-08-20-ep-300-a-public-episode-98765432")


class TestOverlappingPages(unittest.TestCase):
    """Cursor pages can repeat a post; it must not be mirrored twice."""

    def test_duplicate_posts_are_collapsed(self):
        included = {
            f"{i['type']}:{i['id']}": i for i in FIXTURE.get("included", [])
        }
        posts = FIXTURE["data"] + FIXTURE["data"]  # same page served twice

        class FakeClient(PatreonClient):
            def iter_posts(self, campaign_id):
                for post in posts:
                    yield post, included

        client = FakeClient(PatreonConfig.from_env())
        episodes = client.fetch_unlocked_episodes(campaign_id="1")
        self.assertEqual(len(episodes), 2)
        self.assertEqual(len({e.post_id for e in episodes}), 2)

    def test_episodes_are_newest_first(self):
        class FakeClient(PatreonClient):
            def iter_posts(self, campaign_id):
                for post in reversed(FIXTURE["data"]):
                    yield post, {f"{i['type']}:{i['id']}": i for i in FIXTURE["included"]}

        episodes = FakeClient(PatreonConfig.from_env()).fetch_unlocked_episodes("1")
        self.assertEqual([e.post_id for e in episodes], ["98765432", "98764000"])


class TestTitleExclusion(unittest.TestCase):
    """Titles taken verbatim from a real run against the campaign."""

    DROPPED = [
        "FREE PREVIEW: Radio War Nerd #187 \u2014 Climate Change & Wars, with Christian Parenti",
        "FREE PREVIEW: Radio War Nerd #238 \u2014 Vigilante Mobs & Antifa Freakout",
        "FREE PREVIEW: Radio War Nerd #296 \u2014 The Spoils of War, with Andrew Cockburn",
        "Preview: something",
        "free preview: lowercase variant",
    ]
    KEPT = [
        # The trap: starts with FREE, but it is a full episode.
        "FREE REPOST Radio War Nerd EP #628 [UNLOCKED] \u2014 The Israel Lobby",
        "REPOST: Radio War Nerd #375 [UNLOCKED] \u2014 Sudan Crisis, feat. Joshua Craze",
        "Radio War Nerd EP #366 \u2014 Seymour Hersh on US Bombing Nord Stream Pipelines",
        "UNLOCKED: Radio War Nerd EP #384 [REPOST] \u2014 Prigozhin's Mutiny",
        "Radio War Nerd EP #250 [UNLOCKED] \u2014 Second Nagorno-Karabakh War",
    ]

    def setUp(self):
        os.environ.pop("EXCLUDE_TITLE_PATTERN", None)
        self.pattern = _compile_exclude(PatreonConfig.from_env().exclude_title_pattern)

    def test_free_previews_are_dropped(self):
        for title in self.DROPPED:
            with self.subTest(title=title):
                self.assertIsNotNone(self.pattern.search(title))

    def test_full_episodes_survive(self):
        for title in self.KEPT:
            with self.subTest(title=title):
                self.assertIsNone(self.pattern.search(title))

    def test_client_filters_the_fetch(self):
        included = {f"{i['type']}:{i['id']}": i for i in FIXTURE["included"]}
        posts = json.loads(json.dumps(FIXTURE["data"]))
        posts[0]["attributes"]["title"] = "FREE PREVIEW: Radio War Nerd #187"

        class FakeClient(PatreonClient):
            def iter_posts(self, campaign_id):
                for post in posts:
                    yield post, included

        episodes = FakeClient(PatreonConfig.from_env()).fetch_unlocked_episodes("1")
        self.assertEqual([e.post_id for e in episodes], ["98764000"])

    def test_empty_pattern_disables_filtering(self):
        os.environ["EXCLUDE_TITLE_PATTERN"] = ""
        try:
            config = PatreonConfig.from_env()
        finally:
            del os.environ["EXCLUDE_TITLE_PATTERN"]
        self.assertEqual(config.exclude_title_pattern, "")
        self.assertIsNone(_compile_exclude(config.exclude_title_pattern))

    def test_custom_pattern_is_honoured(self):
        os.environ["EXCLUDE_TITLE_PATTERN"] = r"\[REPOST\]"
        try:
            pattern = _compile_exclude(PatreonConfig.from_env().exclude_title_pattern)
        finally:
            del os.environ["EXCLUDE_TITLE_PATTERN"]
        self.assertIsNotNone(pattern.search("Radio War Nerd #131* [REPOST]"))
        self.assertIsNone(pattern.search("Radio War Nerd #366"))

    def test_invalid_pattern_is_reported_clearly(self):
        with self.assertRaises(PatreonError):
            _compile_exclude("([unclosed")


class TestMediaHelpers(unittest.TestCase):
    def test_extension_from_content_type(self):
        self.assertEqual(guess_extension("https://x/y", "audio/mpeg", ".bin"), ".mp3")
        self.assertEqual(guess_extension("https://x/y", "audio/mp4", ".bin"), ".m4a")

    def test_extension_from_url_when_type_is_generic(self):
        self.assertEqual(
            guess_extension("https://x/ep.m4a?t=1", "application/octet-stream", ".bin"),
            ".m4a",
        )

    def test_extension_falls_back(self):
        self.assertEqual(guess_extension("https://x/stream", None, ".mp3"), ".mp3")


class TestState(unittest.TestCase):
    def test_round_trip_and_dedupe(self):
        state = State()
        state.add(_stored("1", "2026-01-01T00:00:00+00:00"))
        state.add(_stored("2", "2026-02-01T00:00:00+00:00"))
        state.add(_stored("1", "2026-01-01T00:00:00+00:00"))
        self.assertEqual(len(state.episodes), 2)
        self.assertTrue(state.has("1"))

        revived = State.from_dict(state.to_dict())
        self.assertEqual(len(revived.episodes), 2)
        self.assertEqual([e.post_id for e in revived.sorted_episodes()], ["2", "1"])

    def test_malformed_entries_are_dropped(self):
        state = State.from_dict({"episodes": {"9": "not-a-dict", "8": {}}})
        self.assertEqual(len(state.episodes), 0)

    def test_unknown_fields_are_ignored(self):
        payload = {"episodes": {"1": {**_stored("1", "2026-01-01T00:00:00+00:00").__dict__, "future_field": 1}}}
        self.assertEqual(len(State.from_dict(payload).episodes), 1)


class TestFeed(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FEED_IMAGE_URL", None)
        self.config = FeedConfig.from_env()
        self.episodes = [
            _stored("98765432", "2026-08-20T15:04:05+00:00", duration=4230),
            _stored("98764000", "2026-08-06T12:00:00+00:00", duration=3600),
        ]
        self.xml = build_feed(
            self.config,
            self.episodes,
            feed_url="https://cdn.example.com/feed.xml",
            default_image="https://cdn.example.com/art/cover.jpg",
        )
        self.root = ET.fromstring(self.xml)

    def test_is_wellformed_rss(self):
        self.assertTrue(self.xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertEqual(self.root.tag, "rss")
        self.assertEqual(self.root.get("version"), "2.0")

    def test_items_and_enclosures(self):
        items = self.root.findall("./channel/item")
        self.assertEqual(len(items), 2)
        enclosure = items[0].find("enclosure")
        self.assertEqual(enclosure.get("type"), "audio/mpeg")
        self.assertEqual(enclosure.get("length"), "51200000")
        self.assertTrue(enclosure.get("url").startswith("https://cdn.example.com/"))

    def test_guid_is_stable_and_not_a_permalink(self):
        guid = self.root.find("./channel/item/guid")
        self.assertEqual(guid.get("isPermaLink"), "false")
        self.assertEqual(guid.text, "patreon-post-98765432")

    def test_itunes_metadata(self):
        channel = self.root.find("channel")
        self.assertEqual(channel.find(f"{{{ITUNES_NS}}}author").text, self.config.author)
        self.assertEqual(
            channel.find(f"{{{ITUNES_NS}}}image").get("href"),
            "https://cdn.example.com/art/cover.jpg",
        )
        duration = self.root.find(f"./channel/item/{{{ITUNES_NS}}}duration")
        self.assertEqual(duration.text, "1:10:30")

    def test_pubdate_is_rfc2822(self):
        pub_date = self.root.find("./channel/item/pubDate").text
        self.assertEqual(pub_date, "Thu, 20 Aug 2026 15:04:05 +0000")

    def test_newest_first(self):
        titles = [i.find("title").text for i in self.root.findall("./channel/item")]
        self.assertEqual(titles[0], "Episode 98765432")

    def test_atom_self_link(self):
        link = self.root.find("./channel/{http://www.w3.org/2005/Atom}link")
        self.assertEqual(link.get("href"), "https://cdn.example.com/feed.xml")

    def test_max_items_is_respected(self):
        os.environ["FEED_MAX_ITEMS"] = "1"
        try:
            xml = build_feed(
                FeedConfig.from_env(), self.episodes, feed_url="https://x/feed.xml"
            )
        finally:
            del os.environ["FEED_MAX_ITEMS"]
        self.assertEqual(len(ET.fromstring(xml).findall("./channel/item")), 1)

    def test_html_in_titles_is_escaped(self):
        episode = _stored("1", "2026-01-01T00:00:00+00:00")
        episode.title = 'Ep <b>1</b> & "friends"'
        xml = build_feed(self.config, [episode], feed_url="https://x/feed.xml")
        self.assertIn(b"&lt;b&gt;", xml)
        parsed = ET.fromstring(xml)
        self.assertEqual(
            parsed.find("./channel/item/title").text, 'Ep <b>1</b> & "friends"'
        )


class TestDurationFormat(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_duration(59), "0:59")
        self.assertEqual(format_duration(605), "10:05")
        self.assertEqual(format_duration(3600), "1:00:00")
        self.assertIsNone(format_duration(0))
        self.assertIsNone(format_duration(None))


def _stored(post_id: str, published: str, duration: int | None = None) -> StoredEpisode:
    return StoredEpisode(
        post_id=post_id,
        title=f"Episode {post_id}",
        published_at=published,
        page_url=f"https://www.patreon.com/posts/{post_id}",
        audio_key=f"episodes/{post_id}.mp3",
        audio_url=f"https://cdn.example.com/episodes/{post_id}.mp3",
        audio_size=51200000,
        audio_type="audio/mpeg",
        duration_seconds=duration,
        art_url="https://cdn.example.com/art/cover.jpg",
        summary="A summary.",
        description_html="<p>A summary.</p>",
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
