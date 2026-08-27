"""Download episode audio and artwork to a local working directory."""

from __future__ import annotations

import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}


@dataclass
class DownloadedFile:
    path: Path
    size: int
    content_type: str

    @property
    def extension(self) -> str:
        return self.path.suffix


GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream", "text/plain"}


def guess_extension(url: str, content_type: str | None, fallback: str) -> str:
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base in AUDIO_TYPES:
            return AUDIO_TYPES[base]
        # A generic type tells us nothing; the URL usually still carries a suffix.
        if base not in GENERIC_TYPES:
            guessed = mimetypes.guess_extension(base)
            if guessed:
                return ".jpg" if guessed == ".jpe" else guessed
    path = url.split("?", 1)[0]
    _, ext = os.path.splitext(path)
    if ext and len(ext) <= 6:
        return ext.lower()
    return fallback


def content_type_for(path: Path, sniffed: str | None) -> str:
    if sniffed and sniffed.split(";", 1)[0].strip().lower() not in {
        "",
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return sniffed.split(";", 1)[0].strip()
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def download(
    session: requests.Session,
    url: str,
    destination: Path,
    *,
    timeout: int = 60,
    max_retries: int = 4,
    fallback_extension: str = ".bin",
) -> DownloadedFile:
    """Stream ``url`` to ``destination`` (extension appended from the response)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(max_retries):
        if attempt:
            delay = 2**attempt
            log.warning("retrying download of %s in %ss (%s)", url, delay, last_error)
            time.sleep(delay)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with session.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                extension = guess_extension(
                    response.url or url, content_type, fallback_extension
                )
                final = destination.with_suffix(extension)
                written = 0
                with open(temporary, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            handle.write(chunk)
                            written += len(chunk)
            if written == 0:
                raise IOError(f"empty download from {url}")
            expected = response.headers.get("Content-Length")
            if expected and expected.isdigit() and int(expected) != written:
                raise IOError(
                    f"short download from {url}: got {written} of {expected} bytes"
                )
            temporary.replace(final)
            return DownloadedFile(
                path=final,
                size=written,
                content_type=content_type_for(final, content_type),
            )
        except (requests.RequestException, IOError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)

    raise RuntimeError(f"failed to download {url}: {last_error}")


def audio_duration_seconds(path: Path) -> int | None:
    """Best-effort duration probe; never fatal."""
    try:
        from mutagen import File as MutagenFile
    except ImportError:  # pragma: no cover - mutagen is a declared dependency
        return None
    try:
        audio = MutagenFile(str(path))
        if audio is not None and audio.info is not None:
            length = getattr(audio.info, "length", None)
            if length:
                return int(round(length))
    except Exception as exc:  # noqa: BLE001 - a bad tag must not fail the run
        log.debug("could not read duration from %s: %s", path.name, exc)
    return None
