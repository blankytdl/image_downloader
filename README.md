# Telegram Photo Search & Batch-Download Bot

Searches stock-photo APIs for images suitable for YouTube editing
(landscape orientation, high resolution) and sends you a zip of the
results, downloaded in batch.

## Why not just scrape Google Images?

Scraping Google Image search results directly (a) violates Google's
Terms of Service, and (b) mostly returns copyrighted photos that
aren't cleared for reuse — using them in a monetized YouTube video is
a common way to get a copyright claim or strike.

Instead, this bot uses **Pexels, Pixabay, and Unsplash** — all free
APIs whose licenses explicitly permit use in video content, with no
attribution required (attribution is still appreciated, per each
site's terms). It also includes an *optional* Google Custom Search
image provider for cases where you specifically want to search the
open web — it's off unless you configure it, and you're responsible
for checking the license of anything you download that way before
publishing.

For anime screenshots/character art, it also includes **Safebooru**
(safe-rated only, no API key required) — search by character/tag,
results are always sorted by score, and you can filter to a specific
file type (PNG/JPG/GIF) or set a minimum score.

For GIFs, it includes **Giphy** and **Tenor** (both free, key-based).

For actual Google/web image results beyond what the free Custom
Search tier indexes, it includes **SerpApi** (paid, ~$0.01-0.015 per
search) as a `serpapi` provider — a real pass-through to Google
Images. Note: **Microsoft retired the Bing Image Search API in August
2025**, so there is currently no equivalent free/official "Bing"
option to add here; SerpApi (or a paid scraping service) is the
practical route to real web image search results now.

### A note on "type:" filtering for web search results

Google/SerpApi links are ordinary web-page URLs and often carry no
file extension at all, unlike stock/booru/GIF APIs. So for those two
providers, the bot over-fetches candidates, downloads them, sniffs
the *actual* file type from the HTTP response, and only then applies
your `type:` filter — this makes `type:png`/`type:gif`/etc. reliable
even though the source URLs don't advertise their type up front.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN` — create a bot via [@BotFather](https://t.me/BotFather) on Telegram, it will give you a token.
   - At least one of `PEXELS_API_KEY`, `PIXABAY_API_KEY`, `UNSPLASH_ACCESS_KEY` (all free, sign up on their developer pages, links in `.env.example`).
3. Run it:
   ```bash
   python bot.py
   ```

## Using the bot

In Telegram, message your bot:

```
/search drone shot of a forest
/search "city traffic at night" 20 pixabay
/search hatsune_miku 20 safebooru type:png minscore:80
/search sailor_moon, transformation 15 safebooru type:gif
/search excited cat celebration 15 giphy
/search "cyberpunk skyline" serpapi type:png
/providers
/help
```

- `count` (optional): how many images to fetch. Web engines (`google`/`serpapi`) default to **15** if you don't specify one (i.e. the 10-20 range); other providers default to 10. Max 30 either way.
- `provider` (optional): `pexels`, `pixabay`, `unsplash`, `safebooru`, `giphy`, `tenor`, `google`, or `serpapi` (whichever have keys configured). Defaults to whatever `DEFAULT_PROVIDER` is set to in `.env`.
- `type:png` / `type:jpg` / `type:gif` / `type:webp` / `type:bmp` / `type:tiff` / `type:svg` (optional): only keep results of that file type. Works with **any** provider and any image type — handy for grabbing transparent-PNG overlays or GIFs specifically.
- `minscore:N` (optional, Safebooru only): drop any result scoring below N, so you only get well-liked/curated art for that character rather than everything tagged with their name.

### Safebooru specifics

- Query = character name and/or tags. Multiple tags: separate with commas (`hatsune_miku, vocaloid`); spaces within a tag become underscores automatically (`hatsune miku` → `hatsune_miku`), matching booru tag conventions.
- Results are **always** requested sorted by score (highest first), so the character's most popular/cleanest art comes first — `minscore` just lets you hard-cut anything below a threshold instead of relying on sort order alone.
- Safebooru only hosts safe-rated content (no NSFW), so it's already filtered that way at the source.
- Character art is often portrait-oriented, so — unlike the stock providers — Safebooru results are *not* filtered to landscape/high-res; use `type:png` if you specifically want overlay-style transparent PNGs.

The bot filters results to landscape orientation and at least 1280px
wide (editable via `MIN_WIDTH_FOR_YOUTUBE` in `bot.py`), downloads
them concurrently, zips them, and sends the zip back. If the batch is
too big for a single Telegram message (>45MB), it automatically
splits the download into multiple zip parts.

## Deploying on Deplexo

This repo includes a `Dockerfile` and `deplexo.yaml` so it deploys as-is:

1. Push this repo to GitHub and connect it in the Deplexo dashboard.
2. Set your env vars (`TELEGRAM_BOT_TOKEN` and at least one provider key)
   under the app's Environment settings — don't commit `.env`.
3. Deploy. Deplexo builds the `Dockerfile` and starts the container.

A couple of things worth knowing:

- **The `port` in `deplexo.yaml` isn't for Telegram traffic.** This bot
  only does outbound long-polling to Telegram's API, so it never needs an
  inbound webhook or port. Deplexo's health check still expects the
  container to answer on a port, though, so `bot.py` runs a trivial
  one-route HTTP server (`GET /` → `200 ok`) alongside the bot on the
  `PORT` env var (default `8080`, matching `deplexo.yaml`). It does
  nothing else — no Telegram traffic goes through it.
- **Free plan fits, with headroom to watch.** The Free tier gives 128 MB
  RAM and 250 MB disk. The bot itself is light (no Pillow/image
  processing), but each `/search` downloads images into a temp folder
  before zipping, so a big `count` on a free instance can push memory/
  disk usage — lower `MAX_COUNT`/`CONCURRENT_DOWNLOADS` in `bot.py`, or
  move to Starter ($5/mo, 512 MB RAM), if you hit limits.
- **No persistent volume needed.** Downloaded files already live in a
  per-request temp dir and get deleted after sending (see below), which
  matches Deplexo's default read-only/ephemeral container filesystem —
  nothing here relies on data surviving a redeploy.

## Notes / things you may want to tweak

- `MAX_COUNT` in `bot.py` caps how many images one `/search` can pull (default 30) — raise it if you need bigger batches, but watch out for provider rate limits.
- `CONCURRENT_DOWNLOADS` controls parallel downloads (default 6).
- Downloaded files are stored in a temp folder per-request and deleted after sending — nothing is kept on disk long-term.
- For heavier use, run this on a small always-on server (a $5 VPS, a Raspberry Pi, etc.) rather than your laptop, since `run_polling()` needs to stay running to receive messages.
- Even with royalty-free sources, it's worth skimming each site's specific license terms (Pexels, Pixabay, and Unsplash all allow commercial/video use, but details differ, e.g. around depicting recognizable people or trademarked content).
