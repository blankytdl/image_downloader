# Telegram Photo Search & Batch-Download Bot

Send the bot a word or phrase and it searches **every configured
source at once — Google, Pexels, Pixabay, Unsplash, anime art
(Safebooru/Danbooru/Yande.re/Konachan/Waifu.im), GIFs, SFX, and Openverse** —
downloading each result set in batch and sending you back a zip per
category. You can also use `/search` directly for full control over
provider, count, and file type.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your keys, see below
python bot.py
```

In Telegram, just type something like:

```
cat wearing sunglasses
```

...and the bot replies with an inline button per configured source —
**Google**, **Pexels**, **Pixabay**, **Unsplash**, **Anime art**,
**GIF**, **SFX**, **Openverse** — plus **All of the above**.

- Tap **Anime art** and the bot first asks *which site* — Safebooru,
  Danbooru (safe), Yande.re, Konachan, or Waifu.im (each has its own tag
  vocabulary). Then it
  asks you to type part of the character name or tag, and looks that
  up against **that site's own tag-autocomplete API** (the same
  endpoint that backs the search box on the site itself) and shows
  you real, existing tags to pick from, instead of you having to
  guess exact underscore formatting yourself. If nothing matches, it
  falls back to using your text as-is. After picking a tag, you get
  sort options matching that site's own scoring system: 🔥 **Top
  (score)**, 🆕 **New**, 🎲 **Random**. Results are always
  safe-rated only — Yande.re and Konachan otherwise host non-safe
  content, so this bot forces a safe-rating filter on every query to
  those two, with no way to turn it off.
- Tap **Pixabay**, **Unsplash**, **GIF**, or **SFX** and you get
  their own sort options (Popular/Latest; Trending/Best match).
  **Pexels**, **Google**, and **Openverse** have no ordering concept
  in their APIs and skip straight to the next step.
- Every category then walks through: **count** (5/10/15/20/30),
  **minimum resolution** (Any / HD ≥1280px / Full HD ≥1920px —
  ignored for SFX, which has no dimensions), and **delivery** — 📦
  one zip file, or 🖼 sent as individual messages (photos inline,
  GIFs as animations, SFX as playable audio, anything else as a
  document).
- Tap **All of the above** to skip the wizard entirely and get every
  configured category at once with default settings (top/relevance
  sort, 10 results, any resolution, zipped) — anime art defaults to
  Safebooru in this mode.

Categories with no API key configured simply don't get a button (the
message tells you which ones were skipped). Safebooru, Yande.re,
Konachan, Danbooru (safe), Waifu.im, and Openverse need no key at all, so they always show up.

For full one-line control, use `/search` directly:

```
/search drone shot of a forest
/search "city traffic at night" 20 pixabay
/search hatsune_miku 20 yandere type:png minscore:80
/search sailor_moon, transformation 15 safebooru type:gif
/search excited cat celebration 15 giphy
/search explosion 10 freesound
/search mount fuji 15 openverse
/overlay arrow red up-right
/search "cyberpunk skyline" serpapi type:png
/providers
/help
```

- `count` (optional): how many files to fetch. Web engines
  (`google`/`serpapi`) default to **15** if omitted (the 10-20 range);
  other providers default to 10. Max 30. The button wizard lets you
  pick 5/10/15/20/30 explicitly per search.
- `provider` (optional): `pexels`, `pixabay`, `unsplash`, `safebooru`,
  `yandere`, `konachan`, `danbooru`, `waifuim`, `giphy`, `freesound`, `openverse`, `google`,
  or `serpapi`. Defaults to whatever `DEFAULT_PROVIDER` is set to in
  `.env`. (`tenor` still exists as a provider name for backward
  compatibility, but Google fully shut down the Tenor API on
  2026-06-30 — selecting it just returns an explanatory error. Use
  `giphy` for GIFs.)
- `type:png` / `type:jpg` / `type:gif` / `type:webp` / `type:bmp` /
  `type:tiff` / `type:svg` / `type:mp3` (optional): only keep results
  of that file type. Works with **any** provider and any image or
  audio type.
- `minscore:N` (optional, safebooru/yandere/konachan only): drop
  anything scoring below N.

## Why not just scrape Google Images?

Scraping Google Image search results directly (a) violates Google's
Terms of Service, and (b) mostly returns copyrighted photos that
aren't cleared for reuse — using them in a monetized YouTube video is
a common way to get a copyright claim or strike.

Instead:
- **Pexels, Pixabay, Unsplash** — free APIs whose licenses explicitly
  permit use in video content.
- **Safebooru, Yande.re, Konachan** — anime art/screenshots, no key
  needed. Safebooru is a safe-rated-only mirror by design; Yande.re
  and Konachan are general boorus that also host non-safe content, so
  this bot forces a `rating:safe` filter on every query to those two
  and there's no way to turn it off.
- **Danbooru (safe)** — `safebooru.donmai.us`, Danbooru's own official
  safe-rated mirror (a different site from safebooru.org, with much broader
  character/tag coverage). Anonymous access allows only 2 search tags per
  query, so the bot uses your one tag plus a forced `rating:g` (General).
- **Waifu.im** — curated anime art organised by a fixed list of category
  tags (waifu, maid, …) rather than free-text character search; the bot
  suggests matching tags from its SFW list and always sends `is_nsfw=false`.
- **Openverse** — hundreds of millions of Creative Commons / public-domain
  images (Flickr, Wikimedia Commons, museums…) via `api.openverse.org`, no
  key needed. `license_type=commercial` is always forced; CC-BY results still
  require crediting the creator.
- **`/overlay`** — transparent PNG arrows, circles, boxes and stars for
  thumbnails, *generated locally* with Pillow. The usual clipart sources
  (Flaticon, Icons8, Noun Project) require attribution or a paid plan for
  commercial use, so generating them avoids any licensing strings.
- **Giphy** — GIFs, free key. (Tenor used to be a second option here,
  but Google fully decommissioned the Tenor API on 2026-06-30 — it no
  longer works for anyone, existing key or not. Giphy is the only GIF
  source now.)
- **Freesound** (SFX) — sound effects, free key. Restricted to
  CC0/Attribution-licensed results only (Noncommercial-restricted
  results are filtered out) since this is meant for monetized video —
  still check the specific sound's page for attribution wording.
- **Google Custom Search** — a curated way to pull real image URLs
  from the web, scoped to a search engine you configure. **Heads up:
  as of 2026 this API is closed to new customers** — if you don't
  already have an existing Custom Search JSON API key/project from
  before the closure, you can't sign up for a new one, and it's
  fully shut down for everyone on **January 1, 2027** regardless. If
  that's you, skip straight to SerpApi below for the "google"
  category, or just rely on the other sources.
- **SerpApi** (optional, paid) — a real pass-through to actual Google
  Images results, and currently the only realistic way to get the
  "google" category working if you don't already have Custom Search
  JSON API access. Note: **Microsoft retired the Bing Image Search
  API in August 2025**, so there's currently no free/official Bing
  equivalent to offer here either.

Even with these sources, skim each site's specific license terms
before publishing — most allow commercial/video use but details
differ (e.g. around depicting recognizable people or trademarks), and
anything you pull via Google/SerpApi is an arbitrary web image whose
license you're responsible for checking yourself.

## Getting each API key

All keys go in your `.env` file (copied from `.env.example`). You
only need keys for the providers you actually want to use — the bot
skips any category with no key configured and tells you which ones
it skipped.

| Variable | Provider | Required? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram | **Yes**, always |
| `PEXELS_API_KEY` | Pexels (stock) | Powers its own "Pexels" category |
| `PIXABAY_API_KEY` | Pixabay (stock) | Powers its own "Pixabay" category |
| `UNSPLASH_ACCESS_KEY` | Unsplash (stock) | Powers its own "Unsplash" category |
| `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_ENGINE_ID` | Google Custom Search | For "google" -- **closed to new sign-ups as of 2026**; only usable if you already had access before the closure |
| `SERPAPI_KEY` | SerpApi | For the "google" category (paid, real Google Images) -- your practical option if signing up fresh |
| `GIPHY_API_KEY` | Giphy | For the "gif" category (Tenor is dead, see below -- Giphy is the only gif source now) |
| `TENOR_API_KEY` | ~~Tenor~~ | **Dead.** Google shut down the Tenor API entirely on 2026-06-30. Leave unset; the bot ignores it either way |
| `FREESOUND_API_KEY` | Freesound | For the "sfx" category (sound effects) |
| — | Safebooru | No key needed, works out of the box |
| — | Yande.re | No key needed, works out of the box (safe-rating forced) |
| — | Konachan | No key needed, works out of the box (safe-rating forced) |
| — | Danbooru (safe), Waifu.im, Openverse | No key needed, work out of the box |

### Telegram Bot Token
1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, follow the prompts (choose a name and a username
   ending in `bot`).
3. BotFather replies with a token like `123456:ABC-DEF...`. Put it in
   `TELEGRAM_BOT_TOKEN`.

### Pexels (stock photos)
1. Go to https://www.pexels.com/api/ and sign in / create a free account.
2. Click "Get Started" — you're given an API key immediately, no
   approval wait.
3. Put it in `PEXELS_API_KEY`. Free tier: 200 requests/hour, 20,000/month.

### Pixabay (stock photos)
1. Create a free account at https://pixabay.com/.
2. Go to https://pixabay.com/api/docs/ — your API key is shown at the
   top of that page once logged in.
3. Put it in `PIXABAY_API_KEY`. Free, generous rate limits.

### Unsplash (stock photos)
1. Create a developer account at https://unsplash.com/developers.
2. Click "New Application", accept the API terms, name it anything.
3. Your **Access Key** is shown on the app's page — put it in
   `UNSPLASH_ACCESS_KEY`.
4. Free ("Demo") tier is capped at 50 requests/hour — fine for
   personal use; apply for production access from the same dashboard
   if you need more.

### Google Custom Search ("google" option — existing accounts only)
> ⚠️ **As of 2026 this API is closed to new customers**, and it shuts
> down for everyone on January 1, 2027 regardless. If you don't
> already have an existing Custom Search JSON API key/project from
> before the closure, the steps below won't work for you — skip to
> **SerpApi** further down for the "google" category instead, or
> just leave `GOOGLE_CSE_API_KEY`/`GOOGLE_CSE_ENGINE_ID` unset and
> rely on the stock/Safebooru/Giphy categories. These steps are kept
> here for anyone who already has access from before the closure.

1. Create a Programmable Search Engine at
   https://programmablesearchengine.google.com/ — click "Add",
   give it any name.
2. For **"What to search"**, don't choose "Search the entire web" —
   for an anime/YouTube channel bot you'll get much more relevant,
   on-topic results (and fewer random stock-photo/unrelated hits) by
   scoping the engine to a curated list of anime/manga/fan-art sites
   instead. Choose "Search specific sites or pages" and add roughly
   15–20 of these to start (Google's docs note that engines with more
   than ~20 sites can sometimes return fewer/less complete results, so
   don't dump all 50 slots in at once):

   ```text
   aniplex.co.jp/*
   crunchyroll.com/*
   animenewsnetwork.com/*
   myanimelist.net/*
   anilist.co/*
   anime-planet.com/*
   fandom.com/*
   *.fandom.com/*
   wikipedia.org/*
   *.wikipedia.org/*
   wikimedia.org/*
   *.wikimedia.org/*
   pixiv.net/*
   *.pixiv.net/*
   deviantart.com/*
   *.deviantart.com/*
   artstation.com/*
   reddit.com/*
   *.reddit.com/*
   imgur.com/*
   ```

   You can add/remove sites later from the engine's **Sites** tab
   (up to 50 distinct domains total) without needing new API keys.
3. Open the new engine's settings and turn **"Image search"** ON.
4. Copy the **Search engine ID** (also called `cx`) from the setup
   page — put it in `GOOGLE_CSE_ENGINE_ID`.
5. Get an API key for the Custom Search JSON API at
   https://developers.google.com/custom-search/v1/introduction —
   click "Get a Key", create/select a Google Cloud project, and copy
   the generated key into `GOOGLE_CSE_API_KEY`.
6. Free tier: 100 queries/day; billing can be enabled on the same
   Cloud project for more, at $5 per 1,000 queries.

Both `GOOGLE_CSE_API_KEY` and `GOOGLE_CSE_ENGINE_ID` are already
listed in `.env.example` — no new environment variables are needed
for this.

### SerpApi (paid "google" option, real Google Images)
1. Sign up at https://serpapi.com/ (a small number of free searches
   are included on signup for testing).
2. Your API key is on your https://serpapi.com/dashboard — put it in
   `SERPAPI_KEY`.
3. Paid plans start around $50/month for 5,000 searches (~1¢ each);
   check their pricing page for current rates.

### Giphy (GIFs)
1. Create an account and an app at https://developers.giphy.com/dashboard/.
2. Click "Create an App" → choose "API" (not SDK) → give it a name.
3. Copy the generated API key into `GIPHY_API_KEY`. Free tier is
   generous for personal/bot use; Giphy may ask you to apply for
   "Production" access if you exceed the beta-key rate limit.

### Tenor (GIFs) — removed
Google fully shut down the Tenor API on 2026-06-30; there's no key
you can get and no successor endpoint to switch to. `TENOR_API_KEY`
is ignored even if set. Use **Giphy** (above) for GIFs — it's the
only GIF source this bot supports now.

### Safebooru, Yande.re, Konachan (anime art)
> Tip: booru tags use **Japanese name order** (`furudo_erika`, `hatsune_miku`,
> `kamado_tanjirou`). If you type a Western-order name, autocomplete now also
> tries the words reversed and individually, and offers whatever real tags it finds.

No signup, no key, for any of the three. They're all public,
unauthenticated APIs — just leave them as-is. Safebooru is a
safe-rated-only mirror by design; Yande.re and Konachan are general
boorus that also host non-safe content, so this bot always forces a
`rating:safe` filter into every query sent to those two (both in the
wizard and via `/search`) — there's no setting to turn that off.

### Danbooru (safe), Waifu.im, Openverse
No signup and no key for any of them. Openverse's anonymous tier is
rate-limited (limits are fairly low and it caps results at 20 per page, so
the bot fetches up to 2 pages) — if you hit 429 errors, wait a bit or avoid
spamming "All of the above", which spends one Openverse request per use. Waifu.im and Danbooru are used
anonymously too; both always send an explicit safe-content filter.

### /overlay (thumbnail arrows & shapes)
`/overlay arrow red up-right`, `/overlay circle yellow`, `/overlay box blue`,
`/overlay star #ff00aa`, or `/overlay` alone for a button menu. Shapes:
arrow, circle, box, star. Colors: red (default), yellow, green, blue, orange,
pink, white, black, or any `#rrggbb`. Arrows take 8 directions. Output is a
512×512 transparent PNG sent as a *document* (photos would lose the
transparency). Requires Pillow (already in `requirements.txt`).

### Freesound (SFX)
1. Create a free account at https://freesound.org/ and apply for an
   API key at https://freesound.org/apiv2/apply/ (usually approved
   instantly).
2. Put it in `FREESOUND_API_KEY`. No OAuth setup needed — this bot
   only downloads the "preview" encode (good-quality MP3), which
   simple API-key auth already allows; only fetching Freesound's
   original uncompressed files requires the full OAuth2 flow, which
   this bot doesn't implement.
3. Results are filtered server-side to CC0 and Attribution licenses
   only (Noncommercial/ShareAlike-restricted sounds are excluded),
   since this bot is meant for monetized video use. Attribution-
   licensed sounds still require crediting the creator — check the
   sound's Freesound page for the exact wording before publishing.

## Bugfix note: jobs stalling at the "zipping" step

If you hit an earlier version of this bot silently stopping partway
through — status message stuck at "Zipping…" with no file ever
arriving and no error shown — that was a real concurrency bug: each
search job wrote its downloads to a temp folder named using a
whole-second timestamp. Two jobs for the same chat landing in the
same second (which happens constantly once you're running 4
categories per message) could share that folder; one job's cleanup
would then delete files out from under the other mid-download or
mid-zip, with no exception raised to explain why nothing arrived.

This is fixed: every job now gets its own folder keyed by a random
UUID (plus the category name), so concurrent jobs can never collide.
On top of that, each job now:
- has an overall timeout (scales with how many files you asked for)
  so a hung request reports a clear timeout message instead of going
  quiet forever,
- logs and reports the specific failure if zipping itself throws
  (previously any such error was still caught, but generic messaging
  made it easy to mistake for a hang),
- tolerates individual download failures without aborting the whole
  batch (`return_exceptions=True`), and
- is fully independent per category, so one slow/broken provider
  can't block or corrupt the others' results.

## Deploying on Deplexo

This repo is ready to deploy on [Deplexo](https://deplexo.com) as-is —
`Dockerfile`, `deplexo.yaml`, and `.dockerignore` are all included.

1. Push this repo to GitHub (or wherever Deplexo pulls from).
2. In the Deplexo dashboard, create a new app from that repository.
   It picks up `deplexo.yaml`, which points at the included
   `Dockerfile` and declares port `8080`.
3. Add your environment variables in the app's **Environment
   Variables** tab (`TELEGRAM_BOT_TOKEN`, plus whichever provider keys
   you're using — see "Getting each API key" above). Deplexo's
   filesystem is read-only at runtime, so this is the only place these
   values should live; don't bake a `.env` file into the image.
4. Deploy. The bot runs as a long-polling background process — it has
   no real web UI — but the container still exposes a tiny health
   endpoint on port 8080 (`/` and `/health`, both return `ok`) purely
   so the platform can confirm it's alive; you don't need to do
   anything with it.

A couple of things specific to Deplexo's container model:

- **Read-only filesystem**: the bot already writes its temporary
  download/zip files under the OS temp directory (`/tmp` in the
  container), which stays writable even on a read-only root
  filesystem, and cleans them up after every job. No persistent
  volume is needed — nothing here needs to survive a redeploy.
- **Memory**: the **Free** plan gives 128 MB RAM / 0.25 CPU — very
  tight for a Python process (interpreter + `python-telegram-bot` +
  `aiohttp` alone typically use 40-60 MB before a single download
  happens). Downloads now stream straight to disk in small chunks
  rather than buffering whole files in RAM, and `GLOBAL_CONCURRENT_DOWNLOADS`
  (env var, default 12) caps how many downloads can run at once
  across *all* chats/categories combined — both fixes exist
  specifically because holding several full files in memory at once
  (e.g. via the "All of the above" 4-categories-at-once flow, or
  several users searching at the same moment) is what can push past
  a small container's limit and get it killed by the OOM killer. On
  the 128 MB Free plan, set `GLOBAL_CONCURRENT_DOWNLOADS=3` or `4` in
  the environment variables to stay well under the ceiling; the
  **Starter** plan ($5/mo, 512 MB RAM, 3 projects) has enough
  headroom to leave it at the default.
- Only one process may long-poll a given bot token at a time — don't
  run this bot locally and on Deplexo simultaneously with the same
  token, or Telegram will error on one of them.

## Notes / things you may want to tweak

- `MAX_COUNT` in `bot.py` caps how many files one `/search` can pull
  (default 30).
- `PLAIN_TEXT_COUNT` controls how many files each category fetches when
  triggered via the inline buttons (default 10).
- `CONCURRENT_DOWNLOADS` controls parallel downloads per job (default 6).
- `GLOBAL_CONCURRENT_DOWNLOADS` (env var, default 12) caps parallel
  downloads across *all* jobs/chats at once — the real memory-safety
  lever if you're on a small container (see Deplexo section above).
- Downloads stream to disk in `DOWNLOAD_CHUNK_BYTES` (64 KB) chunks
  rather than being held whole in memory, with a `MAX_DOWNLOAD_BYTES`
  (100 MB) safety cap against a single runaway file.
- Downloaded files live in a per-job temp folder and are deleted right
  after sending (or on error) — nothing is kept on disk long-term.
- For heavier use, run this on a small always-on server (a $5 VPS, a
  Raspberry Pi, etc.) rather than your laptop, since `run_polling()`
  needs to stay running to receive messages.
