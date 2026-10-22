"""
Telegram Photo Search & Batch-Download Bot
===========================================

Searches royalty-free stock photo APIs (Pexels, Pixabay, Unsplash) for
images suitable for YouTube video editing (landscape orientation,
high resolution), plus Safebooru for anime screenshots/art (character
tag search, sorted by score, safe-rated only), and batch-downloads the
results, zipping them up and sending the archive back to the user.

An optional Google Custom Search (CSE) provider is included, but is
OFF by default -- see the README for why (licensing / copyright risk
for reuse in monetized video).

Usage inside Telegram, once the bot is running:

    /search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]

    /search mountain sunset
    /search "city traffic at night" 20 pixabay
    /search hatsune_miku 20 safebooru type:png minscore:50
    /providers            -> lists which providers are configured
    /help

Requirements: python-telegram-bot>=21.10,<22, aiohttp, python-dotenv
See requirements.txt and README.md.
"""

import asyncio
import io
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageDraw
from telegram import Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("photo_bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ENGINE_ID = os.getenv("GOOGLE_CSE_ENGINE_ID", "")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")           # paid, real Google Images results
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "")
TENOR_API_KEY = os.getenv("TENOR_API_KEY", "")
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY", "")

DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "pexels").lower()
DEFAULT_COUNT = 10
WEB_ENGINE_DEFAULT_COUNT = 15      # google/serpapi default to 10-20 range when count is omitted
MAX_COUNT = 30                     # hard cap per /search call
MIN_WIDTH_FOR_YOUTUBE = 1280       # HD-ish minimum
CONCURRENT_DOWNLOADS = 6
# Process-wide cap on simultaneous downloads across ALL jobs/chats -- without
# this, e.g. "All of the above" (4 categories at once) or several users
# searching at the same moment could stack up 6 concurrent downloads *per
# job* with no shared ceiling. Configurable since containers vary in size.
GLOBAL_CONCURRENT_DOWNLOADS = int(os.getenv("GLOBAL_CONCURRENT_DOWNLOADS", "12"))
GLOBAL_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_CONCURRENT_DOWNLOADS)
DOWNLOAD_CHUNK_BYTES = 64 * 1024   # streamed to disk, never buffered whole in RAM
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # safety cap against a single runaway file
TELEGRAM_DOC_SIZE_LIMIT = 45 * 1024 * 1024  # keep under the 50MB API cap
DOWNLOAD_TIMEOUT = 30
# Some sites (Danbooru/Cloudflare-fronted boorus, image CDNs) reject or throttle the
# bare default aiohttp User-Agent, so every session identifies itself.
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AnimeYouTubeImageBot/1.0)"}

# Providers where the search result URL doesn't reliably carry a file
# extension (arbitrary web pages), so we over-fetch candidates and sniff
# the real file type from the HTTP response after downloading.
WEB_ENGINE_PROVIDERS = {"google", "serpapi"}

CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}
AUDIO_EXTS = {"mp3", "wav", "ogg", "flac", "m4a"}

WORKDIR = os.path.join(tempfile.gettempdir(), "photo_bot_jobs")
os.makedirs(WORKDIR, exist_ok=True)


@dataclass
class ImageResult:
    url: str          # direct downloadable URL
    width: int
    height: int
    source: str        # provider name, used for the filename prefix
    ident: str          # provider-side id, used for the filename
    score: Optional[int] = None       # booru-style score, None for stock providers
    thumbnail_url: Optional[str] = None  # small preview image, when the provider gives one
    source_page: Optional[str] = None    # page the image was found on, when the provider gives one


SIZE_THRESHOLDS = {"any": 0, "hd": 1280, "fhd": 1920}


def passes_size_filter(img: ImageResult, size: str, require_landscape: bool) -> bool:
    """
    `size` is one of SIZE_THRESHOLDS's keys and sets a minimum width.
    `require_landscape` additionally requires width >= height -- used for
    stock-photo providers (video-editing B-roll), but skipped for booru/GIF
    sources where portrait art and square/odd-aspect GIFs are normal.
    Unknown dimensions (0) always pass, since we can't judge them.
    """
    threshold = SIZE_THRESHOLDS.get(size, 0)
    if img.width <= 0 or img.height <= 0:
        return True
    if threshold and img.width < threshold:
        return False
    if require_landscape and img.width < img.height:
        return False
    return True


def is_youtube_friendly(img: ImageResult) -> bool:
    """Landscape orientation and reasonably high resolution (legacy helper,
    equivalent to passes_size_filter(img, "hd", require_landscape=True))."""
    return passes_size_filter(img, "hd", require_landscape=True)


def url_extension(url: str) -> str:
    lower = url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg",
                ".mp3", ".wav", ".ogg", ".flac", ".m4a"):
        if lower.endswith(ext):
            return ext.lstrip(".")
    return ""


def ext_from_content_type(content_type: str) -> str:
    if not content_type:
        return ""
    main = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_TO_EXT.get(main, "")


def matches_file_type(ext_or_img, file_type: Optional[str]) -> bool:
    """Accepts either a bare extension string or an ImageResult."""
    if not file_type:
        return True
    wanted = file_type.lower().lstrip(".")
    wanted_set = {"jpg", "jpeg"} if wanted in ("jpg", "jpeg") else {wanted}
    ext = ext_or_img if isinstance(ext_or_img, str) else url_extension(ext_or_img.url)
    return ext.lower() in wanted_set


# ---------------------------------------------------------------------------
# Providers -- each returns a list[ImageResult]
# ---------------------------------------------------------------------------

async def search_pexels(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY is not set.")
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": min(count * 2, 80), "orientation": "landscape"}
    async with session.get("https://api.pexels.com/v1/search", headers=headers, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = []
    for photo in data.get("photos", []):
        results.append(ImageResult(
            url=photo["src"]["original"],
            width=photo.get("width", 0),
            height=photo.get("height", 0),
            source="pexels",
            ident=str(photo["id"]),
        ))
    return results


async def search_pixabay(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY is not set.")
    order_map = {"popular": "popular", "latest": "latest"}
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": max(min(count * 2, 200), 3),  # pixabay minimum per_page is 3
        "safesearch": "true",
        "order": order_map.get(kwargs.get("sort"), "popular"),
    }
    async with session.get("https://pixabay.com/api/", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = []
    for hit in data.get("hits", []):
        results.append(ImageResult(
            url=hit.get("largeImageURL") or hit.get("webformatURL"),
            width=hit.get("imageWidth", 0),
            height=hit.get("imageHeight", 0),
            source="pixabay",
            ident=str(hit["id"]),
        ))
    return results


async def search_unsplash(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    if not UNSPLASH_ACCESS_KEY:
        raise RuntimeError("UNSPLASH_ACCESS_KEY is not set.")
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    order_map = {"popular": "relevant", "latest": "latest"}
    params = {
        "query": query,
        "per_page": min(count * 2, 30),
        "orientation": "landscape",
        "order_by": order_map.get(kwargs.get("sort"), "relevant"),
    }
    async with session.get("https://api.unsplash.com/search/photos", headers=headers, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = []
    for photo in data.get("results", []):
        results.append(ImageResult(
            url=photo["urls"]["full"],
            width=photo.get("width", 0),
            height=photo.get("height", 0),
            source="unsplash",
            ident=photo["id"],
        ))
    return results


# Safebooru sort options, mapped to the site's own booru-tag sort syntax.
SAFEBOORU_SORT_TAGS = {
    "score": "sort:score:desc",   # top / most popular (default)
    "new": "sort:id:desc",        # most recently posted
    "random": "sort:random",      # random pick
}


async def search_safebooru(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    Safebooru (safebooru.org) -- anime screenshots/fan art, safe-rated only,
    no API key required. `query` must be one or more exact booru tags
    (comma or space separated; spaces inside a tag become underscores, so
    "hatsune miku" -> "hatsune_miku" -- Safebooru's tag matching is strict
    and won't fuzzy-match natural-language phrasing).

    `sort` selects Safebooru's own ordering: "score" (top, default), "new"
    (most recent), or "random". `minscore` (any sort mode) additionally
    hard-filters out anything below that score.
    """
    min_score = int(kwargs.get("minscore", 0) or 0)
    sort_choice = kwargs.get("sort", "score")

    raw_tags = [t.strip() for t in re.split(r"[,\n]", query) if t.strip()]
    tags = [t.replace(" ", "_") for t in raw_tags] if raw_tags else []
    tags.append(SAFEBOORU_SORT_TAGS.get(sort_choice, SAFEBOORU_SORT_TAGS["score"]))
    tag_string = " ".join(tags)

    async def _fetch(tag_str: str):
        params = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": "1",
            "limit": max(min(count * 3, 100), 10),
            "tags": tag_str,
        }
        async with session.get("https://safebooru.org/index.php", params=params) as resp:
            resp.raise_for_status()
            try:
                return await resp.json(content_type=None)
            except Exception:
                return []

    data = await _fetch(tag_string)
    if not data and raw_tags:
        # If the server-side sort: meta-tag is what tripped the query up,
        # retry once with just the plain tag(s); ordering is then done
        # client-side below instead of failing with a false "no results".
        data = await _fetch(" ".join(t.replace(" ", "_") for t in raw_tags))

    results = []
    for post in data or []:
        score = int(post.get("score") or 0)
        if score < min_score:
            continue
        # Safebooru's JSON gives directory + image filename rather than a
        # ready-made URL in some API responses; build it defensively.
        file_url = post.get("file_url")
        if not file_url:
            directory = post.get("directory")
            image = post.get("image")
            if directory and image:
                file_url = f"https://safebooru.org/images/{directory}/{image}"
        if not file_url:
            continue
        results.append(ImageResult(
            url=file_url,
            width=int(post.get("width") or 0),
            height=int(post.get("height") or 0),
            source="safebooru",
            ident=str(post.get("id")),
            score=score,
        ))

    # The API is already asked to sort server-side via the tag above; only
    # re-sort client-side for "score" as a safety net, and never for
    # "random" (that would defeat the point).
    if sort_choice == "score":
        results.sort(key=lambda r: r.score or 0, reverse=True)
    return results


async def safebooru_autocomplete(partial: str, limit: int = 8) -> List[str]:
    """
    Queries Safebooru's own tag-autocomplete endpoint (the same one that
    backs the search box on safebooru.org) so the bot can offer real,
    existing tags instead of guessing at underscore/spelling formatting.
    Returns a plain list of tag names, best matches first. Returns an empty
    list on any error or if there's simply no match -- callers should treat
    that as "fall back to the user's raw text" rather than as a hard failure.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=HTTP_HEADERS) as session:
            async with session.get(
                "https://safebooru.org/autocomplete.php", params={"q": partial}
            ) as resp:
                resp.raise_for_status()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = []
    except Exception:
        logger.warning("Safebooru autocomplete request failed for %r", partial, exc_info=True)
        return []

    tags: List[str] = []
    for item in data or []:
        if isinstance(item, str):
            tag = item
        elif isinstance(item, dict):
            tag = item.get("value") or item.get("label") or ""
            # Gelbooru-style autocomplete labels often look like
            # "tag_name (1234)" with a post count -- strip that suffix.
            tag = re.sub(r"\s*\(\d+\)\s*$", "", tag).strip()
        else:
            continue
        if tag:
            tags.append(tag)

    seen = set()
    deduped = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:limit]


# Yande.re and Konachan both run the "Moebooru" board software (a different
# codebase/API family from Safebooru's Gelbooru-style API), and share an
# identical API shape, so one implementation backs both. Unlike Safebooru --
# which is a safe-rated-only mirror by design -- these two ARE general
# boorus that also host non-safe-rated content, so "rating:safe" is always
# force-appended to every query below (plus a client-side re-check on the
# response) and there is no way to override that from the wizard or /search.
MOEBOORU_SORT_TAGS = {
    "score": "order:score",   # top / most popular (default)
    "new": "order:date",      # most recently posted
    "random": "order:random", # random pick
}


async def _moebooru_search(base_url: str, source: str, session: aiohttp.ClientSession,
                            query: str, count: int, **kwargs) -> List[ImageResult]:
    min_score = int(kwargs.get("minscore", 0) or 0)
    sort_choice = kwargs.get("sort", "score")

    raw_tags = [t.strip() for t in re.split(r"[,\n]", query) if t.strip()]
    tags = [t.replace(" ", "_") for t in raw_tags] if raw_tags else []
    tags.append("rating:safe")  # not user-overridable -- see module note above
    tags.append(MOEBOORU_SORT_TAGS.get(sort_choice, MOEBOORU_SORT_TAGS["score"]))
    tag_string = " ".join(tags)

    params = {"tags": tag_string, "limit": max(min(count * 3, 100), 10)}
    async with session.get(f"{base_url}/post.json", params=params) as resp:
        resp.raise_for_status()
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = []

    results = []
    for post in data or []:
        if post.get("rating") not in (None, "s"):
            continue  # belt-and-suspenders on top of the rating:safe tag above
        score = int(post.get("score") or 0)
        if score < min_score:
            continue
        file_url = post.get("file_url")
        if not file_url:
            continue
        results.append(ImageResult(
            url=file_url,
            width=int(post.get("width") or 0),
            height=int(post.get("height") or 0),
            source=source,
            ident=str(post.get("id")),
            score=score,
        ))

    if sort_choice == "score":
        results.sort(key=lambda r: r.score or 0, reverse=True)
    return results


async def search_yandere(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """Yande.re -- anime art/screenshots, safe-rated results only (forced), no API key required."""
    return await _moebooru_search("https://yande.re", "yandere", session, query, count, **kwargs)


async def search_konachan(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """Konachan -- anime art/wallpapers, safe-rated results only (forced), no API key required."""
    return await _moebooru_search("https://konachan.com", "konachan", session, query, count, **kwargs)


async def _moebooru_autocomplete(base_url: str, partial: str, limit: int = 8) -> List[str]:
    """Shared tag-autocomplete for both Moebooru sites, mirroring safebooru_autocomplete's contract."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=HTTP_HEADERS) as session:
            async with session.get(
                f"{base_url}/tag.json",
                params={"name_pattern": f"*{partial}*", "limit": limit, "order": "count"},
            ) as resp:
                resp.raise_for_status()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = []
    except Exception:
        logger.warning("Moebooru autocomplete request failed for %r (%s)", partial, base_url, exc_info=True)
        return []

    tags: List[str] = []
    for item in data or []:
        name = (item or {}).get("name") if isinstance(item, dict) else None
        if name:
            tags.append(name)

    seen = set()
    deduped = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped[:limit]


async def yandere_autocomplete(partial: str, limit: int = 8) -> List[str]:
    return await _moebooru_autocomplete("https://yande.re", partial, limit)


async def konachan_autocomplete(partial: str, limit: int = 8) -> List[str]:
    return await _moebooru_autocomplete("https://konachan.com", partial, limit)


# safebooru.donmai.us is Danbooru's own official safe-rated mirror (a
# different site from safebooru.org) running the real Danbooru API, with much
# broader character/tag coverage. Anonymous Danbooru access is capped at 2
# search tags per query, so exactly one user tag + "rating:g" (General, the
# strictest tier -- not "sensitive") is used; sorting is done via params /
# client-side rather than order: meta-tags to stay under that cap. As with
# the other boorus, the safe rating is forced and re-checked client-side.
async def search_danbooru_safe(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    min_score = int(kwargs.get("minscore", 0) or 0)
    sort_choice = kwargs.get("sort", "score")

    raw_tags = [t.strip() for t in re.split(r"[,\n]", query) if t.strip()]
    user_tag = raw_tags[0].replace(" ", "_") if raw_tags else ""
    tags = " ".join(t for t in (user_tag, "rating:g") if t)

    params = {"tags": tags, "limit": max(min(count * 3, 100), 10)}
    if sort_choice == "random":
        params["random"] = "true"
    async with session.get("https://safebooru.donmai.us/posts.json", params=params) as resp:
        resp.raise_for_status()
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = []

    results = []
    for post in data or []:
        if not isinstance(post, dict) or post.get("rating") not in (None, "g"):
            continue
        score = int(post.get("score") or 0)
        if score < min_score:
            continue
        file_url = post.get("file_url") or post.get("large_file_url")
        if not file_url:
            continue
        results.append(ImageResult(
            url=file_url,
            width=int(post.get("image_width") or 0),
            height=int(post.get("image_height") or 0),
            source="danbooru",
            ident=str(post.get("id")),
            score=score,
            thumbnail_url=post.get("preview_file_url"),
        ))

    if sort_choice == "score":
        results.sort(key=lambda r: r.score or 0, reverse=True)
    return results


async def danbooru_safe_autocomplete(partial: str, limit: int = 8) -> List[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=HTTP_HEADERS) as session:
            async with session.get(
                "https://safebooru.donmai.us/tags.json",
                params={"search[name_matches]": f"*{partial}*", "search[order]": "count", "limit": limit},
            ) as resp:
                resp.raise_for_status()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = []
    except Exception:
        logger.warning("Danbooru autocomplete request failed for %r", partial, exc_info=True)
        return []
    names = []
    for item in data or []:
        if isinstance(item, dict) and item.get("name") and int(item.get("post_count") or 0) > 0:
            names.append(item["name"])
    return names[:limit]


# Waifu.im is a curated anime-art API organised around a fixed list of
# category tags (waifu, maid, ...) rather than free-text character search, so
# "autocomplete" here means matching what the user typed against that list.
# is_nsfw=false is always sent explicitly (never relying on the default) and
# only the SFW ("versatile") tag list is ever offered as suggestions.
async def search_waifu_im(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    sort_choice = kwargs.get("sort", "favorites")
    tag = re.split(r"[,\n]", query)[0].strip().replace(" ", "-").replace("_", "-").lower()
    params = {"is_nsfw": "false", "many": "true"}
    if tag:
        params["included_tags"] = tag
    if sort_choice == "favorites":
        params["order_by"] = "FAVORITES"
    async with session.get("https://api.waifu.im/search", params=params) as resp:
        resp.raise_for_status()
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = {}

    items = data.get("images") or data.get("items") or [] if isinstance(data, dict) else []
    results = []
    for item in items:
        if not isinstance(item, dict) or item.get("is_nsfw"):
            continue
        url = item.get("url")
        if not url:
            continue
        results.append(ImageResult(
            url=url,
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            source="waifuim",
            ident=str(item.get("image_id") or item.get("id") or len(results)),
            score=int(item.get("favorites") or 0),
        ))
    return results[:max(count, 1) * 3]


async def waifu_im_autocomplete(partial: str, limit: int = 8) -> List[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=HTTP_HEADERS) as session:
            async with session.get("https://api.waifu.im/tags") as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception:
        logger.warning("Waifu.im tag list request failed", exc_info=True)
        return []
    sfw = data.get("versatile", []) if isinstance(data, dict) else []
    names = []
    for t in sfw:
        name = t.get("name") if isinstance(t, dict) else t
        if isinstance(name, str):
            names.append(name)
    needle = partial.lower().replace(" ", "-").replace("_", "-")
    return [n for n in names if needle in n.lower()][:limit]


async def search_google_cse(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    Optional. Requires GOOGLE_CSE_API_KEY + GOOGLE_CSE_ENGINE_ID (a Programmable
    Search Engine configured for image search). Free tier is capped at 100
    queries/day and results are NOT guaranteed to be reusable -- you are
    responsible for checking the license of anything downloaded this way
    before using it in a published video.
    """
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_ENGINE_ID:
        raise RuntimeError("GOOGLE_CSE_API_KEY / GOOGLE_CSE_ENGINE_ID not set.")
    results = []
    # Google CSE returns max 10 per page, up to 100 total (start=1..91)
    for start in range(1, min(count, 50) + 1, 10):
        params = {
            "key": GOOGLE_CSE_API_KEY,
            "cx": GOOGLE_CSE_ENGINE_ID,
            "q": query,
            "searchType": "image",
            "imgSize": "large",
            "safe": "active",
            "num": 10,
            "start": start,
        }
        async with session.get("https://www.googleapis.com/customsearch/v1", params=params) as resp:
            if resp.status != 200:
                break
            data = await resp.json()
        for i, item in enumerate(data.get("items", [])):
            img_meta = item.get("image", {})
            results.append(ImageResult(
                url=item.get("link"),
                width=img_meta.get("width", 0),
                height=img_meta.get("height", 0),
                source="google",
                ident=f"{start}_{i}",
                thumbnail_url=img_meta.get("thumbnailLink"),
                source_page=img_meta.get("contextLink"),
            ))
        if len(results) >= count:
            break
    return results


async def search_serpapi(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    Real Google Images results via SerpApi (https://serpapi.com/) -- a paid
    pass-through API (no free tier beyond a small trial), used when Google
    CSE's curated/limited index isn't turning up what you need. Requires
    SERPAPI_KEY.
    """
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY is not set.")
    params = {
        "engine": "google_images",
        "q": query,
        "api_key": SERPAPI_KEY,
        "ijn": "0",
    }
    async with session.get("https://serpapi.com/search", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = []
    for i, item in enumerate(data.get("images_results", [])[: max(count * 2, count)]):
        url = item.get("original") or item.get("thumbnail")
        if not url:
            continue
        results.append(ImageResult(
            url=url,
            width=item.get("original_width", 0),
            height=item.get("original_height", 0),
            source="serpapi",
            ident=str(item.get("position", i)),
        ))
    return results


async def search_giphy(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """GIF search via Giphy (free API key: https://developers.giphy.com/).
    sort="trending" pulls Giphy's trending feed instead of searching (query is ignored in that mode)."""
    if not GIPHY_API_KEY:
        raise RuntimeError("GIPHY_API_KEY is not set.")
    limit = min(max(count, 1), 50)
    if kwargs.get("sort") == "trending":
        url = "https://api.giphy.com/v1/gifs/trending"
        params = {"api_key": GIPHY_API_KEY, "limit": limit, "rating": "pg-13"}
    else:
        url = "https://api.giphy.com/v1/gifs/search"
        params = {"api_key": GIPHY_API_KEY, "q": query, "limit": limit, "rating": "pg-13"}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = []
    for item in data.get("data", []):
        img = item.get("images", {}).get("original", {})
        url = img.get("url")
        if not url:
            continue
        results.append(ImageResult(
            url=url,
            width=int(img.get("width") or 0),
            height=int(img.get("height") or 0),
            source="giphy",
            ident=item.get("id", ""),
        ))
    return results


async def search_tenor(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    DEAD: Google fully decommissioned the Tenor API on 2026-06-30 -- every
    integration, including this one, stopped working that day. There is no
    successor endpoint; Google's own guidance is to migrate to GIPHY (or a
    third party like Klipy). Kept here (raising immediately, before any
    network call) so /search tenor and old TENOR_API_KEY configs fail with
    an accurate explanation instead of a confusing network error.
    """
    raise RuntimeError(DEPRECATED_PROVIDERS["tenor"])


async def search_openverse(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    Openverse (api.openverse.org) -- a free, keyless search over hundreds of
    millions of Creative Commons / public-domain images pulled from Flickr,
    Wikimedia Commons, museums and more. license_type=commercial is always
    forced, so every result permits commercial use (this bot is meant for
    monetized video); note that CC-BY results still require crediting the
    creator -- the source page for each image is on Openverse itself.
    """
    # Anonymous requests are capped at 20 results per page (larger page_size
    # gets rejected), so fetch up to 2 pages to have headroom for the
    # resolution/type filters that run afterwards.
    wanted = max(min(count * 2, 40), 10)
    results: List[ImageResult] = []
    for page in range(1, (wanted + 19) // 20 + 1):
        params = {
            "q": query,
            "license_type": "commercial",
            "page_size": 20,
            "page": page,
        }
        wanted_ext = (kwargs.get("file_type") or "").lower().lstrip(".")
        if wanted_ext:
            params["extension"] = "jpg" if wanted_ext == "jpeg" else wanted_ext
        async with session.get("https://api.openverse.org/v1/images/", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        page_items = data.get("results", [])
        for item in page_items:
            file_url = item.get("url")
            if not file_url:
                continue
            results.append(ImageResult(
                url=file_url,
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                source="openverse",
                ident=str(item.get("id", "")),
                thumbnail_url=item.get("thumbnail"),
                source_page=item.get("foreign_landing_url"),
            ))
        if len(results) >= wanted or len(page_items) < 20:
            break
    return results


# Excludes Noncommercial/ShareAlike-only licenses -- this bot is meant for
# monetized video use, and the free-provider picks throughout this file are
# all chosen for licenses that permit that. Still check the specific sound's
# page for attribution wording before publishing; CC-BY requires credit.
FREESOUND_LICENSE_FILTER = 'license:("Creative Commons 0" OR "Attribution")'
FREESOUND_SORT_MAP = {"popular": "downloads_desc", "new": "created_desc", "relevance": "score"}


async def search_freesound(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    Freesound.org sound-effects search. Needs a free FREESOUND_API_KEY --
    simple API-key auth is enough for text search + downloading the
    "preview" encode (good-quality MP3); Freesound's original uncompressed
    file requires a full OAuth2 flow, which this bot doesn't implement, so
    the preview is what gets downloaded/sent.
    """
    if not FREESOUND_API_KEY:
        raise RuntimeError("FREESOUND_API_KEY is not set.")
    sort = FREESOUND_SORT_MAP.get(kwargs.get("sort"), "score")
    params = {
        "query": query,
        "token": FREESOUND_API_KEY,
        "page_size": min(max(count, 1), 150),
        "sort": sort,
        "filter": FREESOUND_LICENSE_FILTER,
        "fields": "id,name,previews,duration,license",
    }
    async with session.get("https://freesound.org/apiv2/search/text/", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    results = []
    for item in data.get("results", []):
        previews = item.get("previews", {})
        file_url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
        if not file_url:
            continue
        # Belt-and-suspenders on top of the server-side license filter: never
        # pass along anything that looks Noncommercial / Sampling+.
        lic = (item.get("license") or "").lower()
        if "-nc" in lic or "noncommercial" in lic or "sampling" in lic:
            continue
        # width/height are meaningless for audio -- ImageResult's size/
        # orientation filters already treat 0 as "always passes".
        results.append(ImageResult(
            url=file_url,
            width=0,
            height=0,
            source="freesound",
            ident=str(item.get("id", "")),
        ))
    return results


PROVIDERS = {
    "pexels": search_pexels,
    "pixabay": search_pixabay,
    "unsplash": search_unsplash,
    "safebooru": search_safebooru,
    "yandere": search_yandere,
    "konachan": search_konachan,
    "google": search_google_cse,
    "serpapi": search_serpapi,
    "giphy": search_giphy,
    "tenor": search_tenor,
    "openverse": search_openverse,
    "danbooru": search_danbooru_safe,
    "waifuim": search_waifu_im,
    "freesound": search_freesound,
}

# Providers that are still selectable (so /search gives an accurate reason
# instead of "unknown provider") but no longer functional at all, regardless
# of whether an API key is configured for them.
DEPRECATED_PROVIDERS = {
    "tenor": (
        "Tenor's GIF API was fully shut down by Google on 2026-06-30 and no "
        "longer works for anyone, key or no key. Use giphy instead."
    ),
}

# Providers whose art is often portrait-oriented (character shots, GIFs,
# overlay PNGs) -- skip the landscape/hi-res "youtube_friendly" filter for these.
SKIP_ORIENTATION_FILTER = {"safebooru", "yandere", "konachan", "danbooru", "waifuim", "giphy", "tenor", "freesound"}


def configured_providers() -> List[str]:
    avail = []
    if PEXELS_API_KEY:
        avail.append("pexels")
    if PIXABAY_API_KEY:
        avail.append("pixabay")
    if UNSPLASH_ACCESS_KEY:
        avail.append("unsplash")
    if GOOGLE_CSE_API_KEY and GOOGLE_CSE_ENGINE_ID:
        avail.append("google")
    if SERPAPI_KEY:
        avail.append("serpapi")
    if GIPHY_API_KEY:
        avail.append("giphy")
    # Tenor's API is fully decommissioned (see DEPRECATED_PROVIDERS) -- never
    # list it as usable, even if TENOR_API_KEY is still set in .env.
    if FREESOUND_API_KEY:
        avail.append("freesound")
    avail.append("safebooru")  # no API key required
    avail.append("yandere")    # no API key required (rating:safe forced)
    avail.append("konachan")   # no API key required (rating:safe forced)
    avail.append("danbooru")   # no API key required (rating:g forced)
    avail.append("waifuim")    # no API key required (is_nsfw=false forced)
    avail.append("openverse")  # no API key required (commercial licenses only)
    return avail


# ---------------------------------------------------------------------------
# Download + zip
# ---------------------------------------------------------------------------

async def download_one(session: aiohttp.ClientSession, img: ImageResult, dest_dir: str, sem: asyncio.Semaphore) -> Optional[str]:
    """
    Downloads img.url and writes it to dest_dir, streaming to disk in fixed
    chunks rather than buffering the whole response in memory -- with several
    concurrent downloads per job (and no cross-job cap before this), holding
    full files (multi-MB photos, sometimes much larger GIFs) in RAM at once
    is what actually blows past a container's memory limit. The file
    extension is determined from the URL when it's recognizable, otherwise
    sniffed from the response's Content-Type header -- necessary for
    web-search results (Google/SerpApi) whose links often have no extension
    at all.
    """
    async with sem, GLOBAL_DOWNLOAD_SEMAPHORE:
        tmp_path = None
        try:
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with session.get(img.url, timeout=timeout) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")

                # Links from any provider (most often web-search ones) can resolve to
                # an HTML page instead of the actual image (dead link, hotlink
                # protection, a redirect to a viewer page, etc). Catch the obvious
                # case so we don't save/send a webpage as if it were a photo.
                if "html" in content_type.lower():
                    logger.warning("Skipping non-image response for %s (Content-Type: %s)", img.url, content_type)
                    return None

                ext = url_extension(img.url) or ext_from_content_type(content_type) or "jpg"
                filename = f"{img.source}_{img.ident}.{ext}"
                path = os.path.join(dest_dir, filename)
                tmp_path = path + ".part"

                total = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError(f"exceeded {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB safety cap")
                        f.write(chunk)

            os.replace(tmp_path, path)
            return path
        except Exception as e:
            logger.warning("Failed to download %s: %s", img.url, e)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None


def zip_files(paths: List[str], zip_path: str) -> str:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=os.path.basename(p))
    return zip_path


def chunk_by_size(paths: List[str], limit_bytes: int) -> List[List[str]]:
    """Group files into batches that stay under the Telegram doc size limit."""
    batches, current, current_size = [], [], 0
    for p in paths:
        size = os.path.getsize(p)
        if current and current_size + size > limit_bytes:
            batches.append(current)
            current, current_size = [], 0
        current.append(p)
        current_size += size
    if current:
        batches.append(current)
    return batches


PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}


async def send_single_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int, path: str, caption: str):
    """Send one downloaded file as its own message, picking the Telegram
    send method that gives the best inline preview for its type: animated
    playback for GIFs, an inline image for common photo formats, an inline
    audio player for sound effects, and a plain document for anything else
    (bmp/tiff/svg/etc)."""
    ext = path.rsplit(".", 1)[-1].lower()
    with open(path, "rb") as f:
        if ext == "gif":
            await context.bot.send_animation(chat_id=chat_id, animation=f, caption=caption)
        elif ext in PHOTO_EXTS:
            await context.bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
        elif ext in AUDIO_EXTS:
            await context.bot.send_audio(chat_id=chat_id, audio=f, caption=caption)
        else:
            await context.bot.send_document(chat_id=chat_id, document=f, caption=caption)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Just send me a word or phrase and I'll show you 4 buttons — "
        "Google, Stock photos, Safebooru (anime), and GIF. Tap one and I'll "
        "walk you through sort order, count, resolution, and delivery "
        "(zip or send individually) — or tap \"All of the above\" for a "
        "quick run with defaults.\n\n"
        "For more control, use:\n"
        "/search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]\n\n"
        "Examples:\n"
        "cat wearing sunglasses          ← plain message, shows buttons\n"
        "/search \"drone shot of a forest\" 15 pexels\n"
        "/search hatsune_miku 20 safebooru type:png minscore:80\n\n"
        "Run /providers to see which sources are configured, /help for details."
    )


# ---------------------------------------------------------------------------
# /overlay -- locally generated thumbnail elements (arrows, circles, boxes,
# stars) as transparent PNGs. Generated rather than fetched: the free
# icon/clipart sites that carry these (Flaticon, Icons8, Noun Project) all
# require attribution or a paid plan for commercial use, whereas a shape this
# simple has no licensing strings at all when drawn here.
# ---------------------------------------------------------------------------
OVERLAY_SHAPES = ["arrow", "circle", "box", "star"]
OVERLAY_COLORS = {
    "red": (255, 32, 32),
    "yellow": (255, 214, 0),
    "green": (40, 200, 60),
    "blue": (30, 120, 255),
    "orange": (255, 140, 0),
    "pink": (255, 60, 160),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
}
# Arrow directions -> counter-clockwise rotation in degrees from "pointing right".
OVERLAY_DIRECTIONS = {
    "right": 0, "up-right": 45, "up": 90, "up-left": 135,
    "left": 180, "down-left": 225, "down": 270, "down-right": 315,
}
_OVERLAY_CANVAS = 1024   # drawn at this size, then downscaled 2x for smooth edges
_OVERLAY_OUT = 512


def _hex_to_rgb(token: str):
    t = token.lstrip("#")
    if len(t) == 6:
        try:
            return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None
    return None


def render_overlay(shape: str, color, direction: str = "right", out_path: str = "overlay.png") -> str:
    """Draws one shape as a transparent RGBA PNG at out_path. Blocking (Pillow) -- call via a thread."""
    S = _OVERLAY_CANVAS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = tuple(color) + (255,)
    outline = (0, 0, 0, 255) if sum(color) > 90 else (255, 255, 255, 255)
    ow = 22  # outline width so the shape stays visible on any background

    if shape == "arrow":
        pts = [(100, 420), (560, 420), (560, 250), (940, 512), (560, 774), (560, 604), (100, 604)]
        d.polygon(pts, fill=fill)
        d.line(pts + [pts[0]], fill=outline, width=ow, joint="curve")
    elif shape == "circle":
        box = (90, 150, 934, 874)
        d.ellipse(box, outline=outline, width=96)
        d.ellipse((box[0] + 13, box[1] + 13, box[2] - 13, box[3] - 13), outline=fill, width=70)
    elif shape == "box":
        box = (100, 180, 924, 844)
        d.rectangle(box, outline=outline, width=96)
        d.rectangle((box[0] + 13, box[1] + 13, box[2] - 13, box[3] - 13), outline=fill, width=70)
    elif shape == "star":
        cx = cy = S / 2
        outer, inner = 430, 180
        pts = []
        for i in range(10):
            r = outer if i % 2 == 0 else inner
            a = math.radians(-90 + i * 36)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=fill)
        d.line(pts + [pts[0]], fill=outline, width=ow, joint="curve")
    else:
        raise ValueError(f"unknown shape {shape!r}")

    if shape == "arrow" and OVERLAY_DIRECTIONS.get(direction, 0):
        img = img.rotate(OVERLAY_DIRECTIONS[direction], resample=Image.BICUBIC, expand=False)
    img = img.resize((_OVERLAY_OUT, _OVERLAY_OUT), Image.LANCZOS)
    img.save(out_path, "PNG")
    return out_path


async def _send_overlay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, shape: str, color_name: str,
                        rgb, direction: str):
    job_dir = os.path.join(WORKDIR, f"overlay_{chat_id}_{uuid.uuid4().hex}")
    os.makedirs(job_dir, exist_ok=True)
    try:
        suffix = f"_{direction}" if shape == "arrow" else ""
        path = os.path.join(job_dir, f"{shape}_{color_name}{suffix}.png")
        await asyncio.to_thread(render_overlay, shape, rgb, direction, path)
        # send_document (not send_photo) so Telegram doesn't flatten the transparency.
        with open(path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, caption=f"{shape} · {color_name}{suffix}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def overlay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/overlay [shape] [color|#hex] [direction] -- tokens in any order. No args opens a button menu."""
    args = [a.lower() for a in (context.args or [])]
    if not args:
        rows = [[(s.title(), f"ov|shape|{s}") for s in OVERLAY_SHAPES[:2]],
                [(s.title(), f"ov|shape|{s}") for s in OVERLAY_SHAPES[2:]]]
        await update.message.reply_text("What kind of overlay?", reply_markup=_wiz_kb(rows))
        return

    shape, color_name, rgb, direction = None, "red", OVERLAY_COLORS["red"], "right"
    for a in args:
        if a in OVERLAY_SHAPES:
            shape = a
        elif a in OVERLAY_COLORS:
            color_name, rgb = a, OVERLAY_COLORS[a]
        elif a in OVERLAY_DIRECTIONS:
            direction = a
        elif _hex_to_rgb(a):
            color_name, rgb = a.lstrip("#"), _hex_to_rgb(a)
        else:
            await update.message.reply_text(
                f"Didn't understand “{a}”. Usage: /overlay <{'|'.join(OVERLAY_SHAPES)}> "
                f"[{'|'.join(OVERLAY_COLORS)}|#hex] [{'|'.join(OVERLAY_DIRECTIONS)}]"
            )
            return
    if not shape:
        await update.message.reply_text(f"Pick a shape: {', '.join(OVERLAY_SHAPES)}")
        return
    await _send_overlay(context, update.effective_chat.id, shape, color_name, rgb, direction)


async def handle_overlay_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()
    parts = (cb.data or "").split("|")
    if len(parts) < 3 or parts[0] != "ov":
        return
    step, value = parts[1], parts[2]
    chat_id = cb.message.chat_id

    if step == "shape":
        names = list(OVERLAY_COLORS)
        rows = [[(n.title(), f"ov|color|{n}|{value}") for n in names[i:i + 4]] for i in range(0, len(names), 4)]
        await cb.edit_message_text(f"{value.title()} — which color?", reply_markup=_wiz_kb(rows))
        return

    if step == "color" and len(parts) >= 4:
        shape = parts[3]
        if shape == "arrow":
            dirs = list(OVERLAY_DIRECTIONS)
            rows = [[(n, f"ov|dir|{n}|{value}") for n in dirs[i:i + 4]] for i in range(0, len(dirs), 4)]
            await cb.edit_message_text(f"{value.title()} arrow — which direction?", reply_markup=_wiz_kb(rows))
            return
        await cb.edit_message_text(f"Generating {value} {shape}…")
        await _send_overlay(context, chat_id, shape, value, OVERLAY_COLORS[value], "right")
        return

    if step == "dir" and len(parts) >= 4:
        color_name = parts[3]
        await cb.edit_message_text(f"Generating {color_name} arrow ({value})…")
        await _send_overlay(context, chat_id, "arrow", color_name, OVERLAY_COLORS[color_name], value)
        return


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Quick mode*\n"
        "Type a word or phrase (no command) and I'll show inline buttons for "
        "each configured source — Google, Pexels, Pixabay, Unsplash, Anime "
        "art, GIF, SFX, Openverse.\n"
        "• Tap *Anime art* and pick a site — Safebooru, Danbooru (safe), "
        "Yande.re, Konachan, or Waifu.im. Then type part of the character name; I look it up "
        "against that site's own tag-autocomplete API and show you real "
        "matching tags to pick from, so you don't have to guess underscore "
        "formatting yourself. Results are always safe-rated only, even on "
        "Yande.re/Konachan which otherwise host non-safe content. Sort "
        "options: 🔥 Top (score), 🆕 New, 🎲 Random.\n"
        "• Tap *Pixabay*, *Unsplash*, *GIF*, or *SFX* for their own sort "
        "options (Popular/Latest, Trending/Best match); *Pexels*, *Google*, "
        "and *Openverse* skip straight to count since those APIs don't "
        "support ordering results.\n"
        "• Every category then asks for count (5-30), minimum resolution "
        "(Any / HD / Full HD -- ignored for SFX), and delivery — 📦 one zip "
        "file, or 🖼 sent as individual messages.\n"
        "• Tap *All of the above* to skip the wizard and get every "
        "configured category at once with default settings.\n\n"
        "*Full control*\n"
        "/search <query> [count] [provider] [type:png|jpg|gif|mp3] [minscore:N]\n"
        "  • count: 1-30 (web engines default 10-20, others default 10)\n"
        f"  • provider: one of {', '.join(PROVIDERS.keys())} (default: {DEFAULT_PROVIDER})\n"
        "  • type: keep only this file type — works for ANY provider, any "
        "image or audio type (png/jpg/gif/webp/bmp/tiff/svg/mp3)\n"
        "  • minscore: (booru sites only) drop results below "
        "this score\n"
        "  (/search always zips its results; use the button wizard for "
        "individual-upload delivery or sort/resolution choices)\n"
        "/providers — list which providers have API keys configured\n\n"
        "*Stock photos* (pexels/pixabay/unsplash): filtered to landscape, "
        f"≥{MIN_WIDTH_FOR_YOUTUBE}px wide by default.\n"
        "*Anime art* (safebooru/danbooru/yandere/konachan/waifuim): tag "
        "search, safe-rated only (forced, not optional), no key needed.\n"
        "  e.g. `/search hatsune_miku 20 yandere type:png minscore:80`\n"
        "*GIFs* (giphy): free API key. Tenor's API was fully shut down by "
        "Google on 2026-06-30 and no longer works.\n"
        "  e.g. `/search excited cat 15 giphy`\n"
        "*SFX* (freesound): sound effects, CC0/Attribution-licensed only "
        "(no Noncommercial-restricted results) since this is meant for "
        "monetized video -- still check the sound's page for attribution "
        "wording before publishing.\n"
        "  e.g. `/search explosion 10 freesound`\n"
        "*Openverse* (openverse): Creative Commons images from Flickr, "
        "Wikimedia, museums etc., commercial-use licenses only, no key "
        "needed. CC-BY results still need crediting the creator.\n"
        "  e.g. `/search mount fuji 15 openverse`\n"
        "*Thumbnail elements*: `/overlay` makes transparent PNG arrows, "
        "circles, boxes and stars locally (no download, no licensing) — "
        "e.g. `/overlay arrow red up-right`, `/overlay circle yellow`, "
        "`/overlay star #ff00aa`, or just `/overlay` for buttons.\n"
        "*Real web images* (google/serpapi): google is a curated free-tier "
        "Custom Search (closed to new sign-ups as of 2026 -- see README); "
        "serpapi is paid, real Google Images results.\n"
        "  e.g. `/search cyberpunk skyline 15 serpapi type:png`\n\n"
        "For google/serpapi, the file type is sniffed from the actual "
        "downloaded file (not guessed from the URL), since web links often "
        "don't carry an extension — so `type:` still works reliably there.\n\n"
        "All API keys live in your .env file — see the README for how to get each one.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def providers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    avail = configured_providers()
    if not avail:
        await update.message.reply_text(
            "No providers are configured yet. Add at least one API key "
            "(PEXELS_API_KEY, PIXABAY_API_KEY, or UNSPLASH_ACCESS_KEY) to your .env file."
        )
        return
    await update.message.reply_text("Configured providers: " + ", ".join(avail))


def parse_search_args(raw_args: List[str]):
    """
    Accepts, in any order after the query: [count] [provider] [type:png|jpg|gif] [minscore:N]

    Examples:
        mountain sunset 15 pexels
        hatsune_miku 20 safebooru type:png minscore:80
    """
    args = list(raw_args)
    provider = DEFAULT_PROVIDER
    count = None  # resolved later: provider-specific default if not given explicitly
    extra = {}

    # pull out key:value option tokens (order-independent, can appear anywhere)
    remaining = []
    for tok in args:
        m = re.fullmatch(r"(type|filetype|minscore|min_score|score)\s*:\s*(\S+)", tok, re.IGNORECASE)
        if m:
            key, val = m.group(1).lower(), m.group(2)
            if key in ("type", "filetype"):
                extra["file_type"] = val.lower().lstrip(".")
            elif re.fullmatch(r"-?\d+", val):
                extra["minscore"] = val
        else:
            remaining.append(tok)
    args = remaining

    provider_set = False
    for _ in range(2):  # trailing [count] and [provider], accepted in either order
        if args and not provider_set and args[-1].lower() in PROVIDERS:
            provider = args.pop().lower()
            provider_set = True
        elif args and count is None and re.fullmatch(r"\d+", args[-1]):
            count = max(1, min(int(args.pop()), MAX_COUNT))

    query = " ".join(args).strip()
    return query, count, provider, extra


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, count, provider, extra = parse_search_args(context.args)
    chat_id = update.effective_chat.id

    if not query:
        await update.message.reply_text("Usage: /search <query> [count] [provider]")
        return

    if provider not in PROVIDERS:
        await update.message.reply_text(f"Unknown provider '{provider}'. Choose from: {', '.join(PROVIDERS)}")
        return

    if provider in DEPRECATED_PROVIDERS:
        await update.message.reply_text(f"⚠️ {DEPRECATED_PROVIDERS[provider]}")
        return

    if provider not in configured_providers():
        await update.message.reply_text(
            f"'{provider}' has no API key configured. Run /providers to see what's available."
        )
        return

    if count is None:
        count = WEB_ENGINE_DEFAULT_COUNT if provider in WEB_ENGINE_PROVIDERS else DEFAULT_COUNT

    status_bits = f"🔎 Searching {provider} for “{query}”"
    file_type = extra.get("file_type")
    if file_type:
        status_bits += f" (type:{file_type})"
    if extra.get("minscore"):
        status_bits += f" (minscore:{extra['minscore']})"
    status_msg = await update.message.reply_text(status_bits + "…")

    await run_search_job(context, chat_id, query, count, provider, extra, status_msg, label=provider)


# ---------------------------------------------------------------------------
# Category-based search wizard: a plain word/phrase shows 4 inline "glass"
# buttons (google, stock, safebooru, gif) plus "All of the above". Tapping a
# single category walks through a short wizard -- for Safebooru specifically,
# an exact-tag prompt first (its tag matching is strict, not natural
# language), then sort, then count, then resolution, then delivery mode --
# before running that one search. "All" skips the wizard and runs all
# configured categories immediately with sensible defaults.
# ---------------------------------------------------------------------------

CATEGORY_PROVIDER_PRIORITY = {
    "google": ["google", "serpapi"],
    "pexels": ["pexels"],
    "pixabay": ["pixabay"],
    "unsplash": ["unsplash"],
    # Used only for "All of the above"'s default pick and button-visibility
    # checks -- the interactive per-tap flow instead offers an explicit site
    # picker (see ANIME_SITES below) since all three need no API key and so
    # would otherwise always resolve to the first one, silently.
    "anime": ["safebooru", "danbooru", "yandere", "konachan", "waifuim"],
    "gif": ["giphy"],  # tenor removed: its API is fully decommissioned, see DEPRECATED_PROVIDERS
    "sfx": ["freesound"],
    "openverse": ["openverse"],
}
CATEGORY_ORDER = ["google", "pexels", "pixabay", "unsplash", "openverse", "anime", "gif", "sfx"]
CATEGORY_BUTTON_LABELS = {
    "google": "🔎 Google",
    "pexels": "📷 Pexels",
    "pixabay": "🖼 Pixabay",
    "unsplash": "📸 Unsplash",
    "anime": "🎌 Anime art",
    "gif": "🎞 GIF",
    "sfx": "🔊 SFX",
    "openverse": "🌐 Openverse",
}
PLAIN_TEXT_COUNT = 10  # default per-category batch size (overridden by the count step)

# Sort options offered per category, as (button label, value) pairs. A
# category with no entry here skips the sort step entirely -- Pexels' API
# has no ordering parameter at all (previously this bot silently re-routed
# a "Latest" pick to Pixabay/Unsplash to work around that; splitting Pexels
# into its own category makes that unnecessary -- it just never offers
# "Latest"), and google/openverse are relevance-only. "anime"'s sort options
# depend on which site the user picks and live in ANIME_SITE_SORT_OPTIONS
# below instead, since Safebooru (Gelbooru-style) and Yande.re/Konachan
# (Moebooru-style) don't share a sort vocabulary.
SORT_OPTIONS = {
    "pixabay": [("🔥 Popular", "popular"), ("🆕 Latest", "latest")],
    "unsplash": [("🔥 Popular", "popular"), ("🆕 Latest", "latest")],
    "gif": [("🔥 Trending", "trending"), ("🔍 Best match", "relevance")],
    "sfx": [("🔥 Popular", "popular"), ("🆕 Newest", "new"), ("🔍 Best match", "relevance")],
}

# The "anime" category asks which site before asking for a tag, since each
# has its own tag-autocomplete API (Yande.re/Konachan are safe-rated-forced
# here regardless of sort -- see _moebooru_search).
ANIME_SITES = [
    ("🎌 Safebooru", "safebooru"),
    ("🅳 Danbooru (safe)", "danbooru"),
    ("🌸 Yande.re", "yandere"),
    ("🎨 Konachan", "konachan"),
    ("💫 Waifu.im", "waifuim"),
]
ANIME_SITE_AUTOCOMPLETE = {
    "safebooru": safebooru_autocomplete,
    "yandere": yandere_autocomplete,
    "konachan": konachan_autocomplete,
    "danbooru": danbooru_safe_autocomplete,
    "waifuim": waifu_im_autocomplete,
}
ANIME_SITE_SORT_OPTIONS = {
    "safebooru": [("🔥 Top (score)", "score"), ("🆕 New", "new"), ("🎲 Random", "random")],
    "yandere": [("🔥 Top (score)", "score"), ("🆕 New", "new"), ("🎲 Random", "random")],
    "konachan": [("🔥 Top (score)", "score"), ("🆕 New", "new"), ("🎲 Random", "random")],
    "danbooru": [("🔥 Top (score)", "score"), ("🆕 New", "new"), ("🎲 Random", "random")],
    "waifuim": [("⭐ Most favorited", "favorites"), ("🎲 Random", "random")],
}

COUNT_OPTIONS = [5, 10, 15, 20, 30]
SIZE_OPTIONS = [("Any size", "any"), ("HD (≥1280px)", "hd"), ("Full HD (≥1920px)", "fhd")]
DELIVERY_OPTIONS = [("📦 Zip file", "zip"), ("🖼 Send individually", "individual")]

# Pending searches waiting on the next button tap or text reply:
# search_id -> dict(query, category, provider, sort, count, size, created).
# callback_data has a 64-byte limit, so we pass a short id and keep the
# actual state here rather than cramming it into callback_data.
PENDING_SEARCHES: dict = {}
PENDING_TTL_SECONDS = 30 * 60


def _new_pending(query: str) -> str:
    now = time.time()
    stale = [k for k, v in PENDING_SEARCHES.items() if now - v.get("created", 0) > PENDING_TTL_SECONDS]
    for k in stale:
        PENDING_SEARCHES.pop(k, None)
    search_id = uuid.uuid4().hex[:10]
    PENDING_SEARCHES[search_id] = {"query": query, "created": now}
    return search_id


def build_category_plan():
    """Pick one configured provider per category. Returns (available, missing)."""
    avail_providers = set(configured_providers())
    plan, missing = [], []
    for category in CATEGORY_ORDER:
        chosen_provider = next((p for p in CATEGORY_PROVIDER_PRIORITY[category] if p in avail_providers), None)
        if chosen_provider:
            plan.append((category, chosen_provider))
        else:
            missing.append(category)
    return plan, missing


async def autocomplete_with_fallbacks(fn, text: str, limit: int = 8) -> List[str]:
    """
    Runs a site's tag-autocomplete on what the user typed; if that finds
    nothing, also tries the words in reverse order and each word on its own.
    Booru tags follow Japanese name order ("furudo_erika", not
    "erika_furudo"), so a Western-order name typed as-is matches nothing.
    """
    found = await fn(text, limit)
    if found:
        return found
    words = [w for w in re.split(r"[\s_\-]+", text.strip()) if len(w) >= 3]
    attempts = []
    if len(words) >= 2:
        attempts.append("_".join(reversed(words)))
    attempts += sorted(set(words), key=len, reverse=True)[:2]
    attempts = [a for a in dict.fromkeys(attempts) if a.lower() != text.strip().lower()]
    if not attempts:
        return []
    batches = await asyncio.gather(*[fn(a, limit) for a in attempts], return_exceptions=True)
    merged: List[str] = []
    for batch in batches:
        if isinstance(batch, list):
            for t in batch:
                if t not in merged:
                    merged.append(t)
    return merged[:limit]


def _wiz_kb(rows):
    """rows: list of lists of (label, callback_data) -> InlineKeyboardMarkup."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=cd) for label, cd in row] for row in rows])


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    A plain message with no leading slash normally shows the category
    buttons. The one exception: if this chat is mid-wizard waiting on an
    anime tag (after tapping Anime art -> picking Safebooru/Yande.re/
    Konachan), this message is looked up against that site's own
    tag-autocomplete API instead of being used as the tag verbatim.
    """
    text = (update.message.text or "").strip()
    if not text:
        return

    awaiting_id = context.chat_data.get("awaiting_anime_tag")
    if awaiting_id:
        context.chat_data.pop("awaiting_anime_tag", None)
        entry = PENDING_SEARCHES.get(awaiting_id)
        if not entry:
            return

        site = entry.get("provider", "safebooru")
        site_label = dict(ANIME_SITES).get(site, site)
        autocomplete_fn = ANIME_SITE_AUTOCOMPLETE.get(site, safebooru_autocomplete)
        sort_options = ANIME_SITE_SORT_OPTIONS.get(site, ANIME_SITE_SORT_OPTIONS["safebooru"])

        typed_fallback = text if "," in text else text.replace(" ", "_")
        suggestions = await autocomplete_with_fallbacks(autocomplete_fn, text)

        if suggestions:
            entry["tag_suggestions"] = suggestions
            entry["typed_fallback"] = typed_fallback
            rows = [[(f"🏷 {tag}", f"wz|tagpick|{i}|{awaiting_id}")] for i, tag in enumerate(suggestions)]
            rows.append([(f"✅ Use “{typed_fallback}” as typed", f"wz|tagpick|-1|{awaiting_id}")])
            await update.message.reply_text(
                f"{site_label} tags matching “{text}”:", reply_markup=_wiz_kb(rows)
            )
        else:
            entry["query"] = typed_fallback
            keyboard = _wiz_kb([[
                (label, f"wz|sort|{val}|{awaiting_id}") for label, val in sort_options
            ]])
            await update.message.reply_text(
                f"No {site_label} tag matches for “{text}” — using it as-is: "
                f"“{typed_fallback}”. Sort by:",
                reply_markup=keyboard,
            )
        return

    plan, missing = build_category_plan()
    if not plan:
        await update.message.reply_text(
            "No providers are configured yet. Add at least one API key to your .env file — see /providers or the README."
        )
        return

    search_id = _new_pending(text)
    row = [(CATEGORY_BUTTON_LABELS[cat], f"wz|cat|{cat}|{search_id}") for cat, _ in plan]
    rows = [row[i:i + 2] for i in range(0, len(row), 2)]  # 2 buttons per row
    if len(plan) > 1:
        rows.append([("📦 All of the above", f"wz|cat|all|{search_id}")])

    msg = f"What would you like for “{text}”?"
    if missing:
        msg += f"\n(not shown: {', '.join(missing)} — no API key configured)"

    await update.message.reply_text(msg, reply_markup=_wiz_kb(rows))


async def handle_wizard_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    parts = (cb.data or "").split("|")
    if len(parts) != 4 or parts[0] != "wz":
        return
    _, step, value, search_id = parts

    entry = PENDING_SEARCHES.get(search_id)
    if not entry:
        await cb.edit_message_text("This search has expired — just send the word again.")
        return
    chat_id = cb.message.chat.id
    query = entry["query"]

    if step == "cat":
        plan, _ = build_category_plan()
        plan_dict = dict(plan)

        if value == "all":
            await cb.edit_message_text(f"🔎 Searching {', '.join(c for c, _ in plan)} for “{query}”…")

            async def run_one(cat: str, provider: str):
                status_msg = await context.bot.send_message(chat_id=chat_id, text=f"[{cat}] starting…")
                await run_search_job(context, chat_id, query, PLAIN_TEXT_COUNT, provider, {}, status_msg, label=cat)

            await asyncio.gather(*[run_one(cat, provider) for cat, provider in plan])
            PENDING_SEARCHES.pop(search_id, None)
            return

        provider = plan_dict.get(value)
        if not provider:
            await cb.edit_message_text(f"'{value}' isn't configured anymore. Try another option or /providers.")
            return
        entry["category"] = value
        entry["provider"] = provider

        if value == "anime":
            # Which site? Each has its own tag-autocomplete API and sort
            # vocabulary (see ANIME_SITE_AUTOCOMPLETE / ANIME_SITE_SORT_OPTIONS).
            site_btns = [(label, f"wz|animesite|{val}|{search_id}") for label, val in ANIME_SITES]
            keyboard = _wiz_kb([site_btns[i:i + 2] for i in range(0, len(site_btns), 2)])
            await cb.edit_message_text("Which anime site?", reply_markup=keyboard)
            return

        if value in SORT_OPTIONS:
            keyboard = _wiz_kb([[(label, f"wz|sort|{val}|{search_id}") for label, val in SORT_OPTIONS[value]]])
            await cb.edit_message_text(f"[{value}] Sort by:", reply_markup=keyboard)
        else:
            # no sort concept for this category (e.g. google/serpapi) -- skip to count
            entry["sort"] = "relevance"
            keyboard = _wiz_kb([[(str(n), f"wz|count|{n}|{search_id}") for n in COUNT_OPTIONS]])
            await cb.edit_message_text(f"[{value}] How many results?", reply_markup=keyboard)
        return

    if step == "animesite":
        site = value
        entry["provider"] = site
        # All three sites use strict/literal booru tag matching (no
        # natural-language fuzzy matching), so look up real tags via that
        # site's own autocomplete API before running anything.
        context.chat_data["awaiting_anime_tag"] = search_id
        site_label = dict(ANIME_SITES).get(site, site)
        await cb.edit_message_text(
            f"Type part of the character name or tag — I'll look it up "
            f"against {site_label}'s own tag list (you searched “{query}”).\n"
            f"Tip: booru tags use Japanese name order, e.g. furudo_erika, not erika_furudo."
        )
        return

    if step == "tagpick":
        idx = int(value)
        suggestions = entry.get("tag_suggestions", [])
        if idx == -1 or not (0 <= idx < len(suggestions)):
            chosen_tag = entry.get("typed_fallback", query)
        else:
            chosen_tag = suggestions[idx]
        entry["query"] = chosen_tag
        query = chosen_tag
        site = entry.get("provider", "safebooru")
        sort_options = ANIME_SITE_SORT_OPTIONS.get(site, ANIME_SITE_SORT_OPTIONS["safebooru"])
        keyboard = _wiz_kb([[(label, f"wz|sort|{val}|{search_id}") for label, val in sort_options]])
        await cb.edit_message_text(f"Tag: “{chosen_tag}”. Sort by:", reply_markup=keyboard)
        return

    if step == "sort":
        entry["sort"] = value
        keyboard = _wiz_kb([[(str(n), f"wz|count|{n}|{search_id}") for n in COUNT_OPTIONS]])
        await cb.edit_message_text(f"[{entry['category']}] How many results?", reply_markup=keyboard)
        return

    if step == "count":
        entry["count"] = int(value)
        if entry["category"] == "sfx":
            # audio has no resolution -- skip straight to delivery
            entry["size"] = "any"
            keyboard = _wiz_kb([[(label, f"wz|deliver|{val}|{search_id}") for label, val in DELIVERY_OPTIONS]])
            await cb.edit_message_text(f"[{entry['category']}] Deliver as:", reply_markup=keyboard)
            return
        keyboard = _wiz_kb([[(label, f"wz|size|{val}|{search_id}") for label, val in SIZE_OPTIONS]])
        await cb.edit_message_text(f"[{entry['category']}] Minimum resolution?", reply_markup=keyboard)
        return

    if step == "size":
        entry["size"] = value
        keyboard = _wiz_kb([[(label, f"wz|deliver|{val}|{search_id}") for label, val in DELIVERY_OPTIONS]])
        await cb.edit_message_text(f"[{entry['category']}] Deliver as:", reply_markup=keyboard)
        return

    if step == "deliver":
        category = entry["category"]
        provider = entry["provider"]
        label = provider if category == "anime" else category
        extra = {"sort": entry.get("sort"), "size": entry.get("size", "any")}
        count = entry.get("count", PLAIN_TEXT_COUNT)

        status_msg = await cb.edit_message_text(f"[{label}] 🔎 Searching for “{query}”…")
        await run_search_job(
            context, chat_id, query, count, provider, extra, status_msg,
            label=label, delivery=value,
        )
        PENDING_SEARCHES.pop(search_id, None)
        return


async def run_search_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    query: str,
    count: int,
    provider: str,
    extra: dict,
    status_msg,
    label: Optional[str] = None,
    delivery: str = "zip",
):
    """
    Core search -> download -> (zip or send individually) pipeline, shared
    by /search and the button wizard. Each call gets its own uniquely named
    job directory (chat id + provider/label + uuid) so concurrent jobs for
    the same chat can never collide or clean up each other's files
    mid-download/zip -- that collision was the cause of jobs silently
    stalling at the zipping step.
    """
    label = label or provider
    file_type = extra.get("file_type")
    size = extra.get("size") or ("hd" if provider not in SKIP_ORIENTATION_FILTER else "any")
    job_dir = os.path.join(
        WORKDIR, f"job_{chat_id}_{label}_{uuid.uuid4().hex}"
    )
    os.makedirs(job_dir, exist_ok=True)

    try:
        # google/serpapi + a type: filter over-fetches (up to 3x) and downloads
        # every candidate before filtering by real sniffed type, so pad the
        # timeout for that case instead of sizing it off the requested count.
        needs_post_filter_estimate = bool(file_type) and provider in WEB_ENGINE_PROVIDERS
        effective_count = count * 3 if needs_post_filter_estimate else count
        job_timeout = 20 + effective_count * 8  # scales with batch size; generous but finite

        async def _do_job():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT), headers=HTTP_HEADERS) as session:
                search_fn = PROVIDERS[provider]

                # Web search engines (google/serpapi) return arbitrary web-page
                # URLs that often carry no file extension, so we can't reliably
                # pre-filter by type from the URL alone -- overfetch candidates
                # and filter by the real, sniffed type after downloading instead.
                needs_post_filter = bool(file_type) and provider in WEB_ENGINE_PROVIDERS
                fetch_n = count * 3 if needs_post_filter else count
                raw_results = await search_fn(session, query, fetch_n, **extra)
                logger.info("[%s/%s] %d raw results for %r", label, provider, len(raw_results), query)

                if needs_post_filter:
                    typed = raw_results  # can't judge type yet; filter after download
                else:
                    typed = [r for r in raw_results if matches_file_type(r, file_type)]

                require_landscape = provider not in SKIP_ORIENTATION_FILTER
                filtered = [r for r in typed if passes_size_filter(r, size, require_landscape)]
                if not filtered:
                    filtered = typed

                chosen = filtered[: (count * 3 if needs_post_filter else count)]

                if not chosen:
                    msg = f"[{label}] No results found for “{query}”"
                    if file_type:
                        msg += f" (type:{file_type})"
                    await status_msg.edit_text(msg + ".")
                    return

                await status_msg.edit_text(f"[{label}] ⬇️ Downloading {len(chosen)} files…")

                sem = asyncio.Semaphore(CONCURRENT_DOWNLOADS)
                downloaded = await asyncio.gather(
                    *[download_one(session, img, job_dir, sem) for img in chosen],
                    return_exceptions=True,
                )
                paths = [p for p in downloaded if isinstance(p, str)]
                failures = len(downloaded) - len(paths)
                if failures:
                    logger.warning("[%s/%s] %d/%d downloads failed", label, provider, failures, len(downloaded))

                if needs_post_filter:
                    wanted_ext = file_type.lower().lstrip(".")
                    wanted_set = {"jpg", "jpeg"} if wanted_ext in ("jpg", "jpeg") else {wanted_ext}
                    paths = [p for p in paths if p.rsplit(".", 1)[-1].lower() in wanted_set]

                paths = paths[:count]

            if not paths:
                msg = f"[{label}] All {failures or len(chosen)} downloads failed."
                if needs_post_filter:
                    msg = f"[{label}] None of the results were actually {file_type} files after downloading."
                await status_msg.edit_text(msg + " Try a different query or provider.")
                return

            safe_query = re.sub(r"[^a-zA-Z0-9]+", "_", query)[:30]

            if delivery == "individual":
                await status_msg.edit_text(f"[{label}] 🖼 Sending {len(paths)} files…")
                sent = 0
                for i, path in enumerate(paths, start=1):
                    try:
                        await send_single_file(context, chat_id, path, caption=f"[{label}] {i}/{len(paths)} “{query}”")
                        sent += 1
                    except Exception:
                        logger.exception("[%s/%s] failed to send %s individually", label, provider, path)
                await status_msg.edit_text(f"[{label}] ✅ Done — sent {sent}/{len(paths)} files.")
                return

            await status_msg.edit_text(f"[{label}] 📦 Zipping {len(paths)} files…")

            zip_path = os.path.join(job_dir, "files.zip")
            try:
                zip_files(paths, zip_path)
            except Exception:
                logger.exception("[%s/%s] zipping failed (job_dir=%s)", label, provider, job_dir)
                await status_msg.edit_text(f"[{label}] ⚠️ Failed while zipping the downloaded files.")
                return

            if os.path.getsize(zip_path) <= TELEGRAM_DOC_SIZE_LIMIT:
                with open(zip_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=f"{label}_{safe_query}.zip",
                        caption=f"[{label}] {len(paths)} files for “{query}”",
                    )
            else:
                # zip too big for one message -- split the raw files into
                # size-safe batches and zip+send each batch separately
                os.remove(zip_path)
                batches = chunk_by_size(paths, TELEGRAM_DOC_SIZE_LIMIT)
                for i, batch in enumerate(batches, start=1):
                    batch_zip = os.path.join(job_dir, f"files_part{i}.zip")
                    zip_files(batch, batch_zip)
                    with open(batch_zip, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=f"{label}_{safe_query}_part{i}.zip",
                            caption=f"[{label}] Part {i}/{len(batches)}",
                        )

            await status_msg.edit_text(f"[{label}] ✅ Done — sent {len(paths)} files.")

        await asyncio.wait_for(_do_job(), timeout=job_timeout)

    except asyncio.TimeoutError:
        await status_msg.edit_text(f"[{label}] ⚠️ Timed out after {job_timeout}s. Try a smaller count or a different provider.")
    except RuntimeError as e:
        await status_msg.edit_text(f"[{label}] ⚠️ {e}")
    except aiohttp.ClientError as e:
        await status_msg.edit_text(f"[{label}] ⚠️ Network/API error: {e}")
    except Exception as e:
        logger.exception("[%s/%s] run_search_job failed", label, provider)
        await status_msg.edit_text(f"[{label}] ⚠️ Unexpected error: {e}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Deplexo compatibility: the platform's default deplexo.yaml expects an app
# to listen on a port (see deplexo.yaml in this repo), even though this bot
# is a pure long-polling background worker with no real HTTP surface. This
# spins up a tiny health-check endpoint on $PORT alongside the bot so the
# platform can see the container is alive. It's a no-op with no downside if
# your Deplexo plan/app type doesn't require it.
# ---------------------------------------------------------------------------

async def start_health_server(app: Application):
    from aiohttp import web

    port = int(os.getenv("PORT", "8080"))

    async def health(_request):
        return web.Response(text="ok")

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health check server listening on 0.0.0.0:%d", port)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment or .env file before running.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(start_health_server).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("providers", providers_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("overlay", overlay_cmd))
    app.add_handler(CallbackQueryHandler(handle_overlay_button, pattern=r"^ov\|"))
    app.add_handler(CallbackQueryHandler(handle_wizard_button, pattern=r"^wz\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    logger.info("Bot starting… configured providers: %s", configured_providers())
    app.run_polling()


if __name__ == "__main__":
    main()
