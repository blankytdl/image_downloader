"""
Telegram Photo Search & Batch-Download Bot

Searches Pexels, Pixabay, Unsplash, Google CSE, SerpApi, Safebooru and
Giphy, then downloads and returns images as ZIPs or individual files.
"""

import asyncio
import logging
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ENGINE_ID = os.getenv("GOOGLE_CSE_ENGINE_ID", "")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "")
TENOR_API_KEY = os.getenv("TENOR_API_KEY", "")

DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "pexels").lower()
DEFAULT_COUNT = 10
WEB_ENGINE_DEFAULT_COUNT = 15
MAX_COUNT = 30
MIN_WIDTH_FOR_YOUTUBE = 1280
CONCURRENT_DOWNLOADS = 6
GLOBAL_CONCURRENT_DOWNLOADS = int(os.getenv("GLOBAL_CONCURRENT_DOWNLOADS", "12"))
GLOBAL_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(GLOBAL_CONCURRENT_DOWNLOADS)
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
TELEGRAM_DOC_SIZE_LIMIT = 45 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30

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

SIZE_THRESHOLDS = {"any": 0, "hd": 1280, "fhd": 1920}


@dataclass
class ImageResult:
    url: str
    width: int
    height: int
    source: str
    ident: str
    score: Optional[int] = None
    thumbnail_url: Optional[str] = None
    source_page: Optional[str] = None


def passes_size_filter(img: ImageResult, size: str, require_landscape: bool) -> bool:
    threshold = SIZE_THRESHOLDS.get(size, 0)
    if img.width <= 0 or img.height <= 0:
        return True
    if threshold and img.width < threshold:
        return False
    if require_landscape and img.width < img.height:
        return False
    return True


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
    if not file_type:
        return True
    wanted = file_type.lower().lstrip(".")
    wanted_set = {"jpg", "jpeg"} if wanted in ("jpg", "jpeg") else {wanted}
    ext = ext_or_img if isinstance(ext_or_img, str) else url_extension(ext_or_img.url)
    return ext.lower() in wanted_set


async def search_pexels(session, query, count, **kwargs):
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY is not set.")
    headers = {"Authorization": PEXELS_API_KEY}
    params = {
        "query": query,
        "per_page": min(count * 2, 80),
        "orientation": "landscape",
    }
    async with session.get("https://api.pexels.com/v1/search", headers=headers, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    return [
        ImageResult(
            url=p["src"]["original"],
            width=p.get("width", 0),
            height=p.get("height", 0),
            source="pexels",
            ident=str(p["id"]),
        )
        for p in data.get("photos", [])
    ]


async def search_pixabay(session, query, count, **kwargs):
    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY is not set.")
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "image_type": "photo",
        "orientation": "horizontal",
        "per_page": max(min(count * 2, 200), 3),
        "safesearch": "true",
        "order": "latest" if kwargs.get("sort") == "latest" else "popular",
    }
    async with session.get("https://pixabay.com/api/", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    return [
        ImageResult(
            url=h.get("largeImageURL") or h.get("webformatURL"),
            width=h.get("imageWidth", 0),
            height=h.get("imageHeight", 0),
            source="pixabay",
            ident=str(h["id"]),
        )
        for h in data.get("hits", [])
    ]


async def search_unsplash(session, query, count, **kwargs):
    if not UNSPLASH_ACCESS_KEY:
        raise RuntimeError("UNSPLASH_ACCESS_KEY is not set.")
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {
        "query": query,
        "per_page": min(count * 2, 30),
        "orientation": "landscape",
        "order_by": "latest" if kwargs.get("sort") == "latest" else "relevant",
    }
    async with session.get("https://api.unsplash.com/search/photos", headers=headers, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    return [
        ImageResult(
            url=p["urls"]["full"],
            width=p.get("width", 0),
            height=p.get("height", 0),
            source="unsplash",
            ident=p["id"],
        )
        for p in data.get("results", [])
    ]


SAFEBOORU_SORT_TAGS = {
    "score": "sort:score:desc",
    "new": "sort:id:desc",
    "random": "sort:random",
}


async def search_safebooru(session, query, count, **kwargs):
    min_score = int(kwargs.get("minscore", 0) or 0)
    sort_choice = kwargs.get("sort", "score")

    raw_tags = [t.strip() for t in re.split(r"[,\n]", query) if t.strip()]
    tags = [t.replace(" ", "_") for t in raw_tags]
    tags.append(SAFEBOORU_SORT_TAGS.get(sort_choice, SAFEBOORU_SORT_TAGS["score"]))

    params = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "limit": max(min(count * 3, 100), 10),
        "tags": " ".join(tags),
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

        file_url = post.get("file_url")
        if not file_url:
            directory = post.get("directory")
            image = post.get("image")
            if directory and image:
                file_url = f"https://safebooru.org/images/{directory}/{image}"
        if not file_url:
            continue

        results.append(
            ImageResult(
                url=file_url,
                width=int(post.get("width") or 0),
                height=int(post.get("height") or 0),
                source="safebooru",
                ident=str(post.get("id")),
                score=score,
            )
        )

    if sort_choice == "score":
        results.sort(key=lambda r: r.score or 0, reverse=True)

    return results


async def safebooru_autocomplete(partial: str, limit: int = 8) -> List[str]:
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://safebooru.org/autocomplete.php",
                params={"q": partial},
            ) as resp:
                resp.raise_for_status()
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = []
    except Exception:
        logger.warning("Safebooru autocomplete failed for %r", partial, exc_info=True)
        return []

    tags = []
    for item in data or []:
        if isinstance(item, str):
            tag = item
        elif isinstance(item, dict):
            tag = item.get("value") or item.get("label") or ""
            tag = re.sub(r"\s*\(\d+\)\s*$", "", tag).strip()
        else:
            continue
        if tag:
            tags.append(tag)

    seen = set()
    return [x for x in tags if not (x in seen or seen.add(x))][:limit]


async def search_google_cse(session, query, count, **kwargs):
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_ENGINE_ID:
        raise RuntimeError("GOOGLE_CSE_API_KEY / GOOGLE_CSE_ENGINE_ID not set.")

    results = []
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
        async with session.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
        ) as resp:
            if resp.status != 200:
                break
            data = await resp.json()

        for i, item in enumerate(data.get("items", [])):
            meta = item.get("image", {})
            if not item.get("link"):
                continue
            results.append(
                ImageResult(
                    url=item["link"],
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    source="google",
                    ident=f"{start}_{i}",
                    thumbnail_url=meta.get("thumbnailLink"),
                    source_page=meta.get("contextLink"),
                )
            )
        if len(results) >= count:
            break

    return results


async def search_serpapi(session, query, count, **kwargs):
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
        results.append(
            ImageResult(
                url=url,
                width=item.get("original_width", 0),
                height=item.get("original_height", 0),
                source="serpapi",
                ident=str(item.get("position", i)),
            )
        )
    return results


async def search_giphy(session, query, count, **kwargs):
    if not GIPHY_API_KEY:
        raise RuntimeError("GIPHY_API_KEY is not set.")

    limit = min(max(count, 1), 50)
    if kwargs.get("sort") == "trending":
        url = "https://api.giphy.com/v1/gifs/trending"
        params = {"api_key": GIPHY_API_KEY, "limit": limit, "rating": "pg-13"}
    else:
        url = "https://api.giphy.com/v1/gifs/search"
        params = {
            "api_key": GIPHY_API_KEY,
            "q": query,
            "limit": limit,
            "rating": "pg-13",
        }

    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    results = []
    for item in data.get("data", []):
        img = item.get("images", {}).get("original", {})
        if not img.get("url"):
            continue
        results.append(
            ImageResult(
                url=img["url"],
                width=int(img.get("width") or 0),
                height=int(img.get("height") or 0),
                source="giphy",
                ident=item.get("id", ""),
            )
        )
    return results


DEPRECATED_PROVIDERS = {
    "tenor": (
        "Tenor's GIF API was fully shut down by Google on 2026-06-30. "
        "Use Giphy instead."
    )
}


async def search_tenor(session, query, count, **kwargs):
    raise RuntimeError(DEPRECATED_PROVIDERS["tenor"])


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

SKIP_ORIENTATION_FILTER = {"safebooru", "giphy", "tenor"}


def configured_providers():
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
    avail.append("safebooru")
    return avail


async def download_one(session, img, dest_dir, sem):
    async with sem, GLOBAL_DOWNLOAD_SEMAPHORE:
        tmp_path = None
        try:
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with session.get(img.url, timeout=timeout) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")

                if img.source in ("google", "serpapi") and "html" in content_type.lower():
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
                            raise ValueError(
                                f"exceeded {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB safety cap"
                            )
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


def zip_files(paths, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=os.path.basename(p))
    return zip_path


def chunk_by_size(paths, limit_bytes):
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


async def send_single_file(context, chat_id, path, caption):
    ext = path.rsplit(".", 1)[-1].lower()
    with open(path, "rb") as f:
        if ext == "gif":
            await context.bot.send_animation(
                chat_id=chat_id, animation=f, caption=caption
            )
        elif ext in PHOTO_EXTS:
            await context.bot.send_photo(
                chat_id=chat_id, photo=f, caption=caption
            )
        else:
            await context.bot.send_document(
                chat_id=chat_id, document=f, caption=caption
            )


async def start_cmd(update, context):
    await update.message.reply_text(
        "👋 Send a word or phrase and I'll show buttons for Google, Stock "
        "photos, Safebooru and GIF. You can also use /search for direct control.\n\n"
        "Examples:\n"
        "/search mountain sunset\n"
        "/search hatsune_miku 20 safebooru type:png minscore:80\n\n"
        "Use /providers to see configured sources and /help for details."
    )


async def help_cmd(update, context):
    await update.message.reply_text(
        "*Quick mode*\n"
        "Send a plain phrase to open the search wizard.\n\n"
        "*Direct mode*\n"
        "/search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]\n\n"
        f"Providers: {', '.join(PROVIDERS.keys())}\n"
        f"Stock photos use landscape filtering and ≥{MIN_WIDTH_FOR_YOUTUBE}px by default.\n"
        "Safebooru requires no API key.\n"
        "Google CSE and SerpApi are web-image sources; check licensing before reuse.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def providers_cmd(update, context):
    avail = configured_providers()
    if not avail:
        await update.message.reply_text(
            "No providers are configured. Add API keys to .env."
        )
        return
    await update.message.reply_text("Configured providers: " + ", ".join(avail))


def parse_search_args(raw_args):
    args = list(raw_args)
    provider = DEFAULT_PROVIDER
    count = None
    extra = {}

    remaining = []
    for tok in args:
        m = re.fullmatch(
            r"(type|filetype|minscore|min_score|score)\s*:\s*(\S+)",
            tok,
            re.IGNORECASE,
        )
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

    return " ".join(args).strip(), count, provider, extra


async def search_cmd(update, context):
    query, count, provider, extra = parse_search_args(context.args)
    chat_id = update.effective_chat.id

    if not query:
        await update.message.reply_text(
            "Usage: /search <query> [count] [provider]"
        )
        return

    if provider not in PROVIDERS:
        await update.message.reply_text(
            f"Unknown provider '{provider}'. Choose from: {', '.join(PROVIDERS)}"
        )
        return

    if provider in DEPRECATED_PROVIDERS:
        await update.message.reply_text(
            f"⚠️ {DEPRECATED_PROVIDERS[provider]}"
        )
        return

    if provider not in configured_providers():
        await update.message.reply_text(
            f"'{provider}' has no API key configured. Run /providers."
        )
        return

    if count is None:
        count = WEB_ENGINE_DEFAULT_COUNT if provider in WEB_ENGINE_PROVIDERS else DEFAULT_COUNT

    status = await update.message.reply_text(
        f"🔎 Searching {provider} for “{query}”…"
    )

    await run_search_job(
        context,
        chat_id,
        query,
        count,
        provider,
        extra,
        status,
        label=provider,
    )


CATEGORY_PROVIDER_PRIORITY = {
    "google": ["google", "serpapi"],
    "stock": ["pexels", "pixabay", "unsplash"],
    "safebooru": ["safebooru"],
    "gif": ["giphy"],
}

STOCK_PROVIDERS_SUPPORTING_LATEST = ("pixabay", "unsplash")
CATEGORY_ORDER = ["google", "stock", "safebooru", "gif"]

CATEGORY_BUTTON_LABELS = {
    "google": "🔎 Google",
    "stock": "🖼 Stock photos",
    "safebooru": "🎌 Safebooru",
    "gif": "🎞 GIF",
}

PLAIN_TEXT_COUNT = 10

SORT_OPTIONS = {
    "safebooru": [
        ("🔥 Top (score)", "score"),
        ("🆕 New", "new"),
        ("🎲 Random", "random"),
    ],
    "stock": [
        ("🔥 Popular", "popular"),
        ("🆕 Latest", "latest"),
    ],
    "gif": [
        ("🔥 Trending", "trending"),
        ("🔍 Best match", "relevance"),
    ],
}

COUNT_OPTIONS = [5, 10, 15, 20, 30]
SIZE_OPTIONS = [
    ("Any size", "any"),
    ("HD (≥1280px)", "hd"),
    ("Full HD (≥1920px)", "fhd"),
]
DELIVERY_OPTIONS = [
    ("📦 Zip file", "zip"),
    ("🖼 Send individually", "individual"),
]

PENDING_SEARCHES = {}
PENDING_TTL_SECONDS = 30 * 60


def _new_pending(query):
    now = time.time()
    stale = [
        k for k, v in PENDING_SEARCHES.items()
        if now - v.get("created", 0) > PENDING_TTL_SECONDS
    ]
    for k in stale:
        PENDING_SEARCHES.pop(k, None)

    search_id = uuid.uuid4().hex[:10]
    PENDING_SEARCHES[search_id] = {
        "query": query,
        "created": now,
    }
    return search_id


def build_category_plan():
    avail = set(configured_providers())
    plan, missing = [], []

    for category in CATEGORY_ORDER:
        chosen = next(
            (p for p in CATEGORY_PROVIDER_PRIORITY[category] if p in avail),
            None,
        )
        if chosen:
            plan.append((category, chosen))
        else:
            missing.append(category)

    return plan, missing


def _wiz_kb(rows):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=cd) for label, cd in row]
            for row in rows
        ]
    )


async def handle_plain_text(update, context):
    text = (update.message.text or "").strip()
    if not text:
        return

    awaiting_id = context.chat_data.get("awaiting_safebooru_tag")

    if awaiting_id:
        context.chat_data.pop("awaiting_safebooru_tag", None)
        entry = PENDING_SEARCHES.get(awaiting_id)

        if not entry:
            return

        typed_fallback = (
            text if "," in text else text.replace(" ", "_")
        )
        suggestions = await safebooru_autocomplete(text)

        if suggestions:
            entry["tag_suggestions"] = suggestions
            entry["typed_fallback"] = typed_fallback

            rows = [
                [(f"🏷 {tag}", f"wz|tagpick|{i}|{awaiting_id}")]
                for i, tag in enumerate(suggestions)
            ]
            rows.append(
                [
                    (
                        f"✅ Use “{typed_fallback}” as typed",
                        f"wz|tagpick|-1|{awaiting_id}",
                    )
                ]
            )

            await update.message.reply_text(
                f"Safebooru tags matching “{text}”:",
                reply_markup=_wiz_kb(rows),
            )
        else:
            entry["query"] = typed_fallback
            keyboard = _wiz_kb(
                [
                    [
                        (
                            label,
                            f"wz|sort|{val}|{awaiting_id}",
                        )
                        for label, val in SORT_OPTIONS["safebooru"]
                    ]
                ]
            )
            await update.message.reply_text(
                f"No Safebooru tag matches for “{text}” — using “{typed_fallback}”.",
                reply_markup=keyboard,
            )
        return

    plan, missing = build_category_plan()

    if not plan:
        await update.message.reply_text(
            "No providers are configured. Add at least one API key to .env."
        )
        return

    search_id = _new_pending(text)
    row = [
        (CATEGORY_BUTTON_LABELS[cat], f"wz|cat|{cat}|{search_id}")
        for cat, _ in plan
    ]

    rows = [row[i:i + 2] for i in range(0, len(row), 2)]

    if len(plan) > 1:
        rows.append(
            [("📦 All of the above", f"wz|cat|all|{search_id}")]
        )

    msg = f"What would you like for “{text}”?"

    if missing:
        msg += f"\n(not shown: {', '.join(missing)} — no API key configured)"

    await update.message.reply_text(
        msg,
        reply_markup=_wiz_kb(rows),
    )


async def handle_wizard_button(update, context):
    cb = update.callback_query
    await cb.answer()

    parts = (cb.data or "").split("|")

    if len(parts) != 4 or parts[0] != "wz":
        return

    _, step, value, search_id = parts
    entry = PENDING_SEARCHES.get(search_id)

    if not entry:
        await cb.edit_message_text(
            "This search has expired — send the word again."
        )
        return

    chat_id = cb.message.chat.id
    query = entry["query"]

    if step == "cat":
        plan, _ = build_category_plan()
        plan_dict = dict(plan)

        if value == "all":
            await cb.edit_message_text(
                f"🔎 Searching {', '.join(c for c, _ in plan)} for “{query}”…"
            )

            async def run_one(cat, provider):
                status = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"[{cat}] starting…",
                )
                await run_search_job(
                    context,
                    chat_id,
                    query,
                    PLAIN_TEXT_COUNT,
                    provider,
                    {},
                    status,
                    label=cat,
                )

            await asyncio.gather(
                *[run_one(cat, provider) for cat, provider in plan]
            )
            PENDING_SEARCHES.pop(search_id, None)
            return

        provider = plan_dict.get(value)

        if not provider:
            await cb.edit_message_text(
                f"'{value}' isn't configured anymore. Try another option."
            )
            return

        entry["category"] = value
        entry["provider"] = provider

        if value == "safebooru":
            context.chat_data["awaiting_safebooru_tag"] = search_id
            await cb.edit_message_text(
                f"Type part of the character name or tag. "
                f"I'll look it up against Safebooru's tag list."
            )
            return

        if value in SORT_OPTIONS:
            keyboard = _wiz_kb(
                [
                    [
                        (
                            label,
                            f"wz|sort|{val}|{search_id}",
                        )
                        for label, val in SORT_OPTIONS[value]
                    ]
                ]
            )
            await cb.edit_message_text(
                f"[{value}] Sort by:",
                reply_markup=keyboard,
            )
        else:
            entry["sort"] = "relevance"
            keyboard = _wiz_kb(
                [
                    [
                        (
                            str(n),
                            f"wz|count|{n}|{search_id}",
                        )
                        for n in COUNT_OPTIONS
                    ]
                ]
            )
            await cb.edit_message_text(
                f"[{value}] How many results?",
                reply_markup=keyboard,
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

        keyboard = _wiz_kb(
            [
                [
                    (
                        label,
                        f"wz|sort|{val}|{search_id}",
                    )
                    for label, val in SORT_OPTIONS["safebooru"]
                ]
            ]
        )

        await cb.edit_message_text(
            f"Tag: “{chosen_tag}”. Sort by:",
            reply_markup=keyboard,
        )
        return

    if step == "sort":
        entry["sort"] = value
        note = ""

        if (
            entry["category"] == "stock"
            and value == "latest"
            and entry.get("provider") == "pexels"
        ):
            avail = set(configured_providers())
            fallback = next(
                (
                    p
                    for p in STOCK_PROVIDERS_SUPPORTING_LATEST
                    if p in avail
                ),
                None,
            )
            if fallback:
                entry["provider"] = fallback
                note = (
                    f" (using {fallback} — Pexels has no latest ordering)"
                )

        keyboard = _wiz_kb(
            [
                [
                    (
                        str(n),
                        f"wz|count|{n}|{search_id}",
                    )
                    for n in COUNT_OPTIONS
                ]
            ]
        )

        await cb.edit_message_text(
            f"[{entry['category']}] How many results?{note}",
            reply_markup=keyboard,
        )
        return

    if step == "count":
        entry["count"] = int(value)

        keyboard = _wiz_kb(
            [
                [
                    (
                        label,
                        f"wz|size|{val}|{search_id}",
                    )
                    for label, val in SIZE_OPTIONS
                ]
            ]
        )

        await cb.edit_message_text(
            f"[{entry['category']}] Minimum resolution?",
            reply_markup=keyboard,
        )
        return

    if step == "size":
        entry["size"] = value

        keyboard = _wiz_kb(
            [
                [
                    (
                        label,
                        f"wz|deliver|{val}|{search_id}",
                    )
                    for label, val in DELIVERY_OPTIONS
                ]
            ]
        )

        await cb.edit_message_text(
            f"[{entry['category']}] Deliver as:",
            reply_markup=keyboard,
        )
        return

    if step == "deliver":
        category = entry["category"]
        provider = entry["provider"]
        extra = {
            "sort": entry.get("sort"),
            "size": entry.get("size", "any"),
        }
        count = entry.get("count", PLAIN_TEXT_COUNT)

        status = await cb.edit_message_text(
            f"[{category}] 🔎 Searching for “{query}”…"
        )

        await run_search_job(
            context,
            chat_id,
            query,
            count,
            provider,
            extra,
            status,
            label=category,
            delivery=value,
        )

        PENDING_SEARCHES.pop(search_id, None)


async def run_search_job(
    context,
    chat_id,
    query,
    count,
    provider,
    extra,
    status_msg,
    label=None,
    delivery="zip",
):
    label = label or provider
    file_type = extra.get("file_type")
    size = extra.get("size") or (
        "hd" if provider not in SKIP_ORIENTATION_FILTER else "any"
    )

    job_dir = os.path.join(
        WORKDIR,
        f"job_{chat_id}_{label}_{uuid.uuid4().hex}",
    )
    os.makedirs(job_dir, exist_ok=True)

    try:
        needs_post_filter = (
            bool(file_type)
            and provider in WEB_ENGINE_PROVIDERS
        )
        effective_count = count * 3 if needs_post_filter else count
        job_timeout = 20 + effective_count * 8

        async def _do_job():
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                search_fn = PROVIDERS[provider]

                fetch_n = count * 3 if needs_post_filter else count
                raw_results = await search_fn(
                    session,
                    query,
                    fetch_n,
                    **extra,
                )

                logger.info(
                    "[%s/%s] %d raw results for %r",
                    label,
                    provider,
                    len(raw_results),
                    query,
                )

                if needs_post_filter:
                    typed = raw_results
                else:
                    typed = [
                        r
                        for r in raw_results
                        if matches_file_type(r, file_type)
                    ]

                require_landscape = (
                    provider not in SKIP_ORIENTATION_FILTER
                )

                filtered = [
                    r
                    for r in typed
                    if passes_size_filter(
                        r,
                        size,
                        require_landscape,
                    )
                ]

                if not filtered:
                    filtered = typed

                chosen = filtered[: (
                    count * 3 if needs_post_filter else count
                )]

                if not chosen:
                    msg = (
                        f"[{label}] No results found for “{query}”"
                    )
                    if file_type:
                        msg += f" (type:{file_type})"
                    await status_msg.edit_text(msg + ".")
                    return

                await status_msg.edit_text(
                    f"[{label}] ⬇️ Downloading {len(chosen)} files…"
                )

                sem = asyncio.Semaphore(CONCURRENT_DOWNLOADS)

                downloaded = await asyncio.gather(
                    *[
                        download_one(
                            session,
                            img,
                            job_dir,
                            sem,
                        )
                        for img in chosen
                    ],
                    return_exceptions=True,
                )

                paths = [
                    p for p in downloaded
                    if isinstance(p, str)
                ]

                failures = len(downloaded) - len(paths)

                if needs_post_filter:
                    wanted = file_type.lower().lstrip(".")
                    wanted_set = (
                        {"jpg", "jpeg"}
                        if wanted in ("jpg", "jpeg")
                        else {wanted}
                    )
                    paths = [
                        p
                        for p in paths
                        if p.rsplit(".", 1)[-1].lower()
                        in wanted_set
                    ]

                paths = paths[:count]

            if not paths:
                if needs_post_filter:
                    msg = (
                        f"[{label}] None of the results were actually "
                        f"{file_type} files after downloading."
                    )
                else:
                    msg = (
                        f"[{label}] All {failures or len(chosen)} "
                        f"downloads failed."
                    )

                await status_msg.edit_text(
                    msg + " Try another query or provider."
                )
                return

            safe_query = re.sub(
                r"[^a-zA-Z0-9]+",
                "_",
                query,
            )[:30]

            if delivery == "individual":
                await status_msg.edit_text(
                    f"[{label}] 🖼 Sending {len(paths)} files…"
                )

                sent = 0

                for i, path in enumerate(paths, start=1):
                    try:
                        await send_single_file(
                            context,
                            chat_id,
                            path,
                            caption=(
                                f"[{label}] {i}/{len(paths)} "
                                f"“{query}”"
                            ),
                        )
                        sent += 1
                    except Exception:
                        logger.exception(
                            "[%s/%s] failed to send %s",
                            label,
                            provider,
                            path,
                        )

                await status_msg.edit_text(
                    f"[{label}] ✅ Done — sent {sent}/{len(paths)} files."
                )
                return

            await status_msg.edit_text(
                f"[{label}] 📦 Zipping {len(paths)} files…"
            )

            zip_path = os.path.join(
                job_dir,
                "files.zip",
            )

            zip_files(paths, zip_path)

            if os.path.getsize(zip_path) <= TELEGRAM_DOC_SIZE_LIMIT:
                with open(zip_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=f"{label}_{safe_query}.zip",
                        caption=(
                            f"[{label}] {len(paths)} files "
                            f"for “{query}”"
                        ),
                    )
            else:
                os.remove(zip_path)

                batches = chunk_by_size(
                    paths,
                    TELEGRAM_DOC_SIZE_LIMIT,
                )

                for i, batch in enumerate(
                    batches,
                    start=1,
                ):
                    batch_zip = os.path.join(
                        job_dir,
                        f"files_part{i}.zip",
                    )

                    zip_files(batch, batch_zip)

                    with open(batch_zip, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=(
                                f"{label}_{safe_query}_part{i}.zip"
                            ),
                            caption=(
                                f"[{label}] Part "
                                f"{i}/{len(batches)}"
                            ),
                        )

            await status_msg.edit_text(
                f"[{label}] ✅ Done — sent {len(paths)} files."
            )

        await asyncio.wait_for(
            _do_job(),
            timeout=job_timeout,
        )

    except asyncio.TimeoutError:
        await status_msg.edit_text(
            f"[{label}] ⚠️ Timed out after {job_timeout}s."
        )

    except RuntimeError as e:
        await status_msg.edit_text(
            f"[{label}] ⚠️ {e}"
        )

    except aiohttp.ClientError as e:
        await status_msg.edit_text(
            f"[{label}] ⚠️ Network/API error: {e}"
        )

    except Exception as e:
        logger.exception(
            "[%s/%s] run_search_job failed",
            label,
            provider,
        )
        await status_msg.edit_text(
            f"[{label}] ⚠️ Unexpected error: {e}"
        )

    finally:
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )


async def start_health_server(app):
    from aiohttp import web

    port = int(os.getenv("PORT", "8080"))

    async def health(_request):
        return web.Response(text="ok")

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        "Health check server listening on 0.0.0.0:%d",
        port,
    )


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN in your environment or .env file."
        )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(start_health_server)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start_cmd)
    )
    app.add_handler(
        CommandHandler("help", help_cmd)
    )
    app.add_handler(
        CommandHandler("providers", providers_cmd)
    )
    app.add_handler(
        CommandHandler("search", search_cmd)
    )
    app.add_handler(
        CallbackQueryHandler(
            handle_wizard_button,
            pattern=r"^wz\|",
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_plain_text,
        )
    )

    logger.info(
        "Bot starting… configured providers: %s",
        configured_providers(),
    )

    app.run_polling()


if __name__ == "__main__":
    main()
