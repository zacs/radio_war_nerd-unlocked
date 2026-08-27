# radio_war_nerd-unlocked

A small, self-contained cron job that turns the **publicly unlocked** posts of a
Patreon campaign into a real podcast feed.

On each run it:

1. Reads the campaign's public post list from Patreon's JSON API — **no login,
   no cookies, no OAuth**. Only posts Patreon serves to anonymous visitors are
   visible, which is exactly the set of unlocked episodes.
2. Downloads any episode it hasn't seen before, plus that episode's artwork.
3. Uploads audio and art to Cloudflare R2.
4. Rebuilds `feed.xml` (RSS 2.0 + iTunes tags) and uploads it too.

Point any podcast app at the public URL of `feed.xml` and it behaves like a
normal show.

Defaults target [Radio War Nerd](https://www.patreon.com/radiowarnerd), but the
campaign is just an environment variable — it works for any Patreon page.

## Quick start

```bash
cp .env.example .env
$EDITOR .env                 # fill in the R2 credentials and PUBLIC_BASE_URL
docker compose build

# See what the first run would pick up, without downloading or uploading:
docker compose run --rm rwnfeed --once --dry-run

# Start the scheduler (runs on CRON_SCHEDULE, restarts with the daemon):
docker compose up -d
docker compose logs -f
```

The first run backfills the whole unlocked archive, so it takes a while and
moves a lot of bytes. To ease into it, set `MAX_NEW_EPISODES_PER_RUN=10` for the
first few runs and then remove it.

## Configuration

Everything is configured through environment variables — no credentials live in
the image or in any config file. `.env.example` documents the full set; these
are the ones you must supply:

| Variable | Purpose |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare account id (used to derive the S3 endpoint) |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 API token credentials |
| `R2_BUCKET` | Bucket that holds the episodes, art, feed and state |
| `PUBLIC_BASE_URL` | Public origin serving that bucket — the `r2.dev` subdomain or your own domain |

Useful optional ones:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PATREON_VANITY` | `radiowarnerd` | The `patreon.com/<vanity>` slug to follow |
| `PATREON_CAMPAIGN_ID` | *(auto)* | Skip campaign-id discovery if the page layout changes |
| `CRON_SCHEDULE` | `17 */6 * * *` | Standard 5-field cron expression, evaluated in **UTC** |
| `RUN_ONCE` | `false` | Run a single pass and exit, for an external scheduler |
| `MAX_NEW_EPISODES_PER_RUN` | `0` (unlimited) | Throttle a large backfill |
| `FEED_MAX_ITEMS` | `0` (all) | Keep only the N newest items in the feed |
| `R2_PREFIX` | *(none)* | Store everything under a folder inside the bucket |
| `DRY_RUN` | `false` | Report what would happen, change nothing |

Feed metadata (`FEED_TITLE`, `FEED_AUTHOR`, `FEED_OWNER_EMAIL`,
`FEED_CATEGORIES`, `FEED_IMAGE_URL`, …) is configurable the same way. Set a real
`FEED_OWNER_EMAIL` if you plan to submit the feed to a directory.

## Cloudflare R2 setup

1. Create an R2 bucket (**R2 → Create bucket**).
2. Give it public read access: either enable the managed `r2.dev` subdomain, or
   connect a custom domain. Copy that hostname into `PUBLIC_BASE_URL`.
3. Create an API token with **Object Read & Write** scoped to the bucket
   (**R2 → Manage API Tokens**), and copy the access key id and secret.

The job writes only under the keys it owns:

```
episodes/2026-08-20-ep-300-a-public-episode-98765432.mp3
art/2026-08-20-ep-300-a-public-episode-98765432.jpg
feed.xml
state.json
```

Audio and art are uploaded with a long, immutable `Cache-Control`; `feed.xml`
gets a short one so new episodes show up promptly.

## How state works

`state.json` — the manifest of everything already published — lives **in the
bucket**, not in the container. The container is disposable: a fresh run in a
fresh container picks up exactly where the last one left off, and nothing is
downloaded twice. The `/data` volume is scratch space for in-flight downloads
only, and is safe to delete.

If you want to force a re-download of everything, delete `state.json` from the
bucket.

## Scheduling

The container runs its own cron-expression scheduler as PID 1 (via `tini`), so
`docker compose up -d` is all you need, and logs go to stdout where
`docker compose logs` can see them.

If you'd rather drive it from an external scheduler, run a single pass instead:

```bash
# host crontab
17 */6 * * * docker run --rm --env-file /etc/rwnfeed.env rwnfeed:latest --once
```

`RUN_ONCE=true` does the same thing via the environment, which is the shape a
Kubernetes `CronJob` or a systemd timer wants.

## Robustness notes

- **Anonymous only.** The client never sends credentials to Patreon. A post that
  isn't public simply doesn't come back with media, and is skipped.
- **Idempotent.** Episodes are keyed by Patreon post id, and the RSS `guid` is
  derived from it, so re-runs never duplicate an episode in the feed.
- **Per-episode isolation.** One episode failing to download is logged and
  skipped; the rest of the run and the feed rebuild still happen.
- **Retries.** HTTP requests and downloads retry with exponential backoff, and
  truncated downloads are detected via `Content-Length` and retried rather than
  published.
- **Interrupted backfills.** New episodes are mirrored oldest-first, so a run
  that dies partway leaves a contiguous back catalogue.
- **Duration.** Taken from Patreon's metadata when present, otherwise probed
  from the downloaded file with `mutagen`.

Patreon's API is not a documented public contract. If they change the payload
shape, the symptom will be "0 unlocked audio posts" in the logs; the extraction
logic lives in `rwnfeed/patreon.py` and the fixture in `tests/` shows the shape
it expects.

## Development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m rwnfeed --once --dry-run
```

The test suite is fully offline — it drives the parser, state manifest and feed
renderer from a recorded API fixture, so it doesn't touch the network.

## Layout

```
rwnfeed/
  __main__.py   CLI, signal handling, the cron scheduler loop
  run.py        one pass: fetch -> download -> upload -> rebuild feed
  config.py     every setting, read from the environment
  patreon.py    anonymous Patreon API client and post normalisation
  media.py      streaming downloads, extension/type sniffing, duration probe
  storage.py    Cloudflare R2 (S3-compatible) uploads
  state.py      the published-episode manifest
  feed.py       RSS 2.0 + iTunes rendering
```

## Scope

This mirrors episodes their creators have deliberately made public, for personal
podcast-app use. It has no ability to reach paywalled posts, and adding one
isn't the point of the project.
