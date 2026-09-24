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

Requirements: python-telegram-bot>=20, aiohttp, python-dotenv
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

DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "pexels").lower()
DEFAULT_COUNT = 10
WEB_ENGINE_DEFAULT_COUNT = 15      # google/serpapi default to 10-20 range when count is omitted
MAX_COUNT = 30                     # hard cap per /search call
MIN_WIDTH_FOR_YOUTUBE = 1280       # HD-ish minimum
CONCURRENT_DOWNLOADS = 6
TELEGRAM_DOC_SIZE_LIMIT = 45 * 1024 * 1024  # keep under the 50MB API cap
DOWNLOAD_TIMEOUT = 30

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
}

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


def is_youtube_friendly(img: ImageResult) -> bool:
    """Landscape orientation and reasonably high resolution."""
    if img.width <= 0 or img.height <= 0:
        return True  # unknown dims, don't filter out
    landscape = img.width >= img.height
    hi_res = img.width >= MIN_WIDTH_FOR_YOUTUBE
    return landscape and hi_res


def url_extension(url: str) -> str:
    lower = url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"):
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
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": max(min(count * 2, 200), 3),  # pixabay minimum per_page is 3
        "safesearch": "true",
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
    params = {"query": query, "per_page": min(count * 2, 30), "orientation": "landscape"}
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


async def search_safebooru(session: aiohttp.ClientSession, query: str, count: int, **kwargs) -> List[ImageResult]:
    """
    Safebooru (safebooru.org) -- anime screenshots/fan art, safe-rated only,
    no API key required. `query` is treated as one or more booru tags
    (comma or space separated; spaces inside a tag become underscores, so
    "hatsune miku" -> "hatsune_miku"). Results are always requested sorted
    by score (highest first) so the character's most popular/cleanest art
    comes back first; pass minscore:N via the /search command to also
    hard-filter out anything below that score.
    """
    min_score = int(kwargs.get("minscore", 0) or 0)

    raw_tags = [t.strip() for t in re.split(r"[,\n]", query) if t.strip()]
    tags = [t.replace(" ", "_") for t in raw_tags] if raw_tags else []
    tags.append("sort:score:desc")  # prioritize by score, as requested
    tag_string = " ".join(tags)

    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": max(min(count * 3, 100), 10),
        "tags": tag_string,
    }
    async with session.get("https://safebooru.org/index.php", params=params) as resp:
        resp.raise_for_status()
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = []

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

    results.sort(key=lambda r: r.score or 0, reverse=True)
    return results


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
    """GIF search via Giphy (free API key: https://developers.giphy.com/)."""
    if not GIPHY_API_KEY:
        raise RuntimeError("GIPHY_API_KEY is not set.")
    params = {
        "api_key": GIPHY_API_KEY,
        "q": query,
        "limit": min(max(count, 1), 50),
        "rating": "pg-13",
    }
    async with session.get("https://api.giphy.com/v1/gifs/search", params=params) as resp:
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
    """GIF search via Tenor (free Google API key: https://tenor.com/gifapi/documentation)."""
    if not TENOR_API_KEY:
        raise RuntimeError("TENOR_API_KEY is not set.")
    params = {
        "key": TENOR_API_KEY,
        "q": query,
        "limit": min(max(count, 1), 50),
        "media_filter": "gif",
        "client_key": "telegram_photo_bot",
    }
    async with session.get("https://tenor.googleapis.com/v2/search", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    results = []
    for item in data.get("results", []):
        gif = item.get("media_formats", {}).get("gif", {})
        url = gif.get("url")
        if not url:
            continue
        dims = gif.get("dims", [0, 0])
        results.append(ImageResult(
            url=url,
            width=int(dims[0]) if dims else 0,
            height=int(dims[1]) if len(dims) > 1 else 0,
            source="tenor",
            ident=item.get("id", ""),
        ))
    return results


PROVIDERS = {
    "pexels": search_pexels,
    "pixabay": search_pixabay,
    "unsplash": search_unsplash,
    "safebooru": search_safebooru,
    "google": search_google_cse,
    "serpapi": search_serpapi,
    "giphy": search_giphy,
    "tenor": search_tenor,
}

# Providers whose art is often portrait-oriented (character shots, GIFs,
# overlay PNGs) -- skip the landscape/hi-res "youtube_friendly" filter for these.
SKIP_ORIENTATION_FILTER = {"safebooru", "giphy", "tenor"}


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
    if TENOR_API_KEY:
        avail.append("tenor")
    avail.append("safebooru")  # no API key required
    return avail


# ---------------------------------------------------------------------------
# Download + zip
# ---------------------------------------------------------------------------

async def download_one(session: aiohttp.ClientSession, img: ImageResult, dest_dir: str, sem: asyncio.Semaphore) -> Optional[str]:
    """
    Downloads img.url and writes it to dest_dir. The file extension is
    determined from the URL when it's recognizable, otherwise sniffed from
    the response's Content-Type header -- necessary for web-search results
    (Google/SerpApi) whose links often have no extension at all.
    """
    async with sem:
        try:
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with session.get(img.url, timeout=timeout) as resp:
                resp.raise_for_status()
                content = await resp.read()
                content_type = resp.headers.get("Content-Type", "")

            ext = url_extension(img.url) or ext_from_content_type(content_type) or "jpg"
            filename = f"{img.source}_{img.ident}.{ext}"
            path = os.path.join(dest_dir, filename)
            with open(path, "wb") as f:
                f.write(content)
            return path
        except Exception as e:
            logger.warning("Failed to download %s: %s", img.url, e)
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


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Just send me a word or phrase and I'll show you 4 buttons — "
        "Google, Stock photos, Safebooru (anime), and GIF — tap one (or "
        "\"All of the above\") and I'll batch-download and zip the results.\n\n"
        "For more control, use:\n"
        "/search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]\n\n"
        "Examples:\n"
        "cat wearing sunglasses          ← plain message, shows buttons\n"
        "/search \"drone shot of a forest\" 15 pexels\n"
        "/search hatsune_miku 20 safebooru type:png minscore:80\n\n"
        "Run /providers to see which sources are configured, /help for details."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Quick mode*\n"
        "Type a word or phrase (no command) and I'll show 4 inline buttons — "
        "Google, Stock, Safebooru, GIF — plus \"All of the above\". Tap one "
        "and I'll run just that category's search; tap \"All\" to get all 4 "
        "as separate zips.\n\n"
        "*Full control*\n"
        "/search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]\n"
        "  • count: 1-30 (web engines default 10-20, others default 10)\n"
        f"  • provider: one of {', '.join(PROVIDERS.keys())} (default: {DEFAULT_PROVIDER})\n"
        "  • type: keep only this file type — works for ANY provider, any image type "
        "(png/jpg/gif/webp/bmp/tiff/svg)\n"
        "  • minscore: (safebooru only) drop results below this score\n"
        "/providers — list which providers have API keys configured\n\n"
        "*Stock photos* (pexels/pixabay/unsplash): filtered to landscape, "
        f"≥{MIN_WIDTH_FOR_YOUTUBE}px wide.\n"
        "*Anime art* (safebooru): character/tag search, safe-rated only, no key needed, "
        "always sorted by score.\n"
        "  e.g. `/search hatsune_miku 20 safebooru type:png minscore:80`\n"
        "*GIFs* (giphy/tenor): free API key each.\n"
        "  e.g. `/search excited cat 15 giphy`\n"
        "*Real web images* (google/serpapi): google is a curated free-tier "
        "Custom Search; serpapi is paid, real Google Images results, use it "
        "when CSE's index doesn't have what you want.\n"
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
            else:
                extra["minscore"] = val
        else:
            remaining.append(tok)
    args = remaining

    if args and args[-1].lower() in PROVIDERS:
        provider = args.pop().lower()
    if args and re.fullmatch(r"\d+", args[-1]):
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
# Category-based search: a plain word/phrase gets 4 inline "glass" buttons
# (google, stock, safebooru, gif) instead of running automatically. Tapping
# one runs that category's search with whichever configured provider fits it.
# ---------------------------------------------------------------------------

CATEGORY_PROVIDER_PRIORITY = {
    "google": ["google", "serpapi"],
    "stock": ["pexels", "pixabay", "unsplash"],
    "safebooru": ["safebooru"],
    "gif": ["giphy", "tenor"],
}
CATEGORY_ORDER = ["google", "stock", "safebooru", "gif"]
CATEGORY_BUTTON_LABELS = {
    "google": "🔎 Google",
    "stock": "🖼 Stock photos",
    "safebooru": "🎌 Safebooru",
    "gif": "🎞 GIF",
}
PLAIN_TEXT_COUNT = 10  # per-category batch size when triggered via buttons

# Pending queries waiting on a button tap: search_id -> (query, created_at).
# callback_data has a 64-byte limit, so we pass a short id and look the
# actual query text up here rather than cramming it into callback_data.
PENDING_SEARCHES: dict = {}
PENDING_TTL_SECONDS = 30 * 60


def _remember_query(query: str) -> str:
    now = time.time()
    # lazy cleanup of stale entries whenever we add a new one
    stale = [k for k, (_, ts) in PENDING_SEARCHES.items() if now - ts > PENDING_TTL_SECONDS]
    for k in stale:
        PENDING_SEARCHES.pop(k, None)
    search_id = uuid.uuid4().hex[:10]
    PENDING_SEARCHES[search_id] = (query, now)
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


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A plain message with no leading slash shows 4 inline buttons -- one
    per category -- instead of searching immediately."""
    query = (update.message.text or "").strip()
    if not query:
        return

    plan, missing = build_category_plan()
    if not plan:
        await update.message.reply_text(
            "No providers are configured yet. Add at least one API key to your .env file — see /providers or the README."
        )
        return

    search_id = _remember_query(query)

    row = [
        InlineKeyboardButton(CATEGORY_BUTTON_LABELS[cat], callback_data=f"srch|{cat}|{search_id}")
        for cat, _ in plan
    ]
    keyboard = [row[i:i + 2] for i in range(0, len(row), 2)]  # 2 buttons per row
    if len(plan) > 1:
        keyboard.append([InlineKeyboardButton("📦 All of the above", callback_data=f"srch|all|{search_id}")])

    text = f"What would you like for “{query}”?"
    if missing:
        text += f"\n(not shown: {', '.join(missing)} — no API key configured)"

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_category_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    parts = (cb.data or "").split("|")
    if len(parts) != 3 or parts[0] != "srch":
        return
    _, category, search_id = parts

    entry = PENDING_SEARCHES.get(search_id)
    if not entry:
        await cb.edit_message_text("This search has expired — just send the word again.")
        return
    query, _ = entry

    plan, _ = build_category_plan()
    plan_dict = dict(plan)
    chat_id = cb.message.chat.id

    if category == "all":
        await cb.edit_message_text(f"🔎 Searching {', '.join(c for c, _ in plan)} for “{query}”…")

        async def run_one(cat: str, provider: str):
            status_msg = await context.bot.send_message(chat_id=chat_id, text=f"[{cat}] starting…")
            await run_search_job(context, chat_id, query, PLAIN_TEXT_COUNT, provider, {}, status_msg, label=cat)

        await asyncio.gather(*[run_one(cat, provider) for cat, provider in plan])
        PENDING_SEARCHES.pop(search_id, None)
        return

    provider = plan_dict.get(category)
    if not provider:
        await cb.edit_message_text(f"'{category}' isn't configured anymore. Try another option or /providers.")
        return

    status_msg = await cb.edit_message_text(f"[{category}] 🔎 Searching for “{query}”…")
    await run_search_job(context, chat_id, query, PLAIN_TEXT_COUNT, provider, {}, status_msg, label=category)
    PENDING_SEARCHES.pop(search_id, None)


async def run_search_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    query: str,
    count: int,
    provider: str,
    extra: dict,
    status_msg,
    label: Optional[str] = None,
):
    """
    Core search -> download -> zip -> send pipeline, shared by /search and
    the plain-text multi-category handler. Each call gets its own uniquely
    named job directory (chat id + provider/label + uuid) so concurrent
    jobs for the same chat can never collide or clean up each other's files
    mid-download/zip -- that collision was the cause of jobs silently
    stalling at the zipping step.
    """
    label = label or provider
    file_type = extra.get("file_type")
    job_dir = os.path.join(
        WORKDIR, f"job_{chat_id}_{label}_{uuid.uuid4().hex}"
    )
    os.makedirs(job_dir, exist_ok=True)

    try:
        job_timeout = 20 + count * 8  # scales with batch size; generous but finite

        async def _do_job():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as session:
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

                if provider in SKIP_ORIENTATION_FILTER:
                    filtered = typed
                else:
                    filtered = [r for r in typed if is_youtube_friendly(r)]
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

            await status_msg.edit_text(f"[{label}] 📦 Zipping {len(paths)} files…")

            zip_path = os.path.join(job_dir, "files.zip")
            try:
                zip_files(paths, zip_path)
            except Exception:
                logger.exception("[%s/%s] zipping failed (job_dir=%s)", label, provider, job_dir)
                await status_msg.edit_text(f"[{label}] ⚠️ Failed while zipping the downloaded files.")
                return

            safe_query = re.sub(r"[^a-zA-Z0-9]+", "_", query)[:30]

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
    app.add_handler(CallbackQueryHandler(handle_category_button, pattern=r"^srch\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    logger.info("Bot starting… configured providers: %s", configured_providers())
    app.run_polling()


if __name__ == "__main__":
    main()
