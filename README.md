# Telegram Photo Search & Batch-Download Bot

Send the bot a word or phrase and it searches **4 categories at once —
google, stock photos, safebooru (anime), and gif** — downloading each
result set in batch and sending you back a zip per category. You can
also use `/search` directly for full control over provider, count,
and file type.

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

...and the bot replies with 4 inline "glass" buttons — **Google**,
**Stock photos**, **Safebooru**, **GIF** — plus **All of the above**.

- Tap **Safebooru** and the bot asks you to type part of the
  character name or tag. It looks that up against **Safebooru's own
  tag-autocomplete API** (`autocomplete.php` — the same endpoint that
  backs the search box on safebooru.org) and shows you real, existing
  tags to pick from, instead of you having to guess exact underscore
  formatting yourself. If nothing matches, it falls back to using your
  text as-is. After picking a tag, you get sort options matching
  Safebooru's own scoring system: 🔥 **Top (score)**, 🆕 **New**,
  🎲 **Random**.
- Tap **Stock** or **GIF** and you get their own sort options
  (Popular/Latest for stock; Trending/Best match for GIFs).
  **Google** has no sort concept and skips straight to the next step.
- Every category then walks through: **count** (5/10/15/20/30),
  **minimum resolution** (Any / HD ≥1280px / Full HD ≥1920px), and
  **delivery** — 📦 one zip file, or 🖼 sent as individual messages
  (photos inline, GIFs as animations, anything else as a document).
- Tap **All of the above** to skip the wizard entirely and get all 4
  categories at once with default settings (top/relevance sort, 10
  results, any resolution, zipped).

Categories with no API key configured simply don't get a button (the
message tells you which ones were skipped).

For full one-line control, use `/search` directly:

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

- `count` (optional): how many files to fetch. Web engines
  (`google`/`serpapi`) default to **15** if omitted (the 10-20 range);
  other providers default to 10. Max 30. The button wizard lets you
  pick 5/10/15/20/30 explicitly per search.
- `provider` (optional): `pexels`, `pixabay`, `unsplash`, `safebooru`,
  `giphy`, `tenor`, `google`, or `serpapi`. Defaults to whatever
  `DEFAULT_PROVIDER` is set to in `.env`.
- `type:png` / `type:jpg` / `type:gif` / `type:webp` / `type:bmp` /
  `type:tiff` / `type:svg` (optional): only keep results of that file
  type. Works with **any** provider and any image type.
- `minscore:N` (optional, Safebooru only): drop anything scoring
  below N.

## Why not just scrape Google Images?

Scraping Google Image search results directly (a) violates Google's
Terms of Service, and (b) mostly returns copyrighted photos that
aren't cleared for reuse — using them in a monetized YouTube video is
a common way to get a copyright claim or strike.

Instead:
- **Pexels, Pixabay, Unsplash** — free APIs whose licenses explicitly
  permit use in video content.
- **Safebooru** — safe-rated anime art/screenshots, no key needed.
- **Giphy / Tenor** — GIFs, free keys.
- **Google Custom Search** — a curated, official, free-tier (100
  queries/day) way to pull real image URLs from the web, scoped to a
  search engine you configure.
- **SerpApi** (optional, paid) — a real pass-through to actual Google
  Images results, for when the CSE's curated index doesn't have what
  you want. Note: **Microsoft retired the Bing Image Search API in
  August 2025**, so there's currently no free/official Bing
  equivalent to offer here.

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
| `PEXELS_API_KEY` | Pexels (stock) | One of the 3 stock keys, for the "stock" category |
| `PIXABAY_API_KEY` | Pixabay (stock) | " |
| `UNSPLASH_ACCESS_KEY` | Unsplash (stock) | " |
| `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_ENGINE_ID` | Google Custom Search | For the "google" category (free option) |
| `SERPAPI_KEY` | SerpApi | For the "google" category (paid, real Google Images) |
| `GIPHY_API_KEY` | Giphy | One of the 2 gif keys |
| `TENOR_API_KEY` | Tenor | " |
| — | Safebooru | No key needed, works out of the box |

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

### Google Custom Search (free "google" option)
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

### Tenor (GIFs)
1. Tenor's API uses a standard Google API key. Go to
   https://console.cloud.google.com/apis/library and enable the
   "Tenor API" (formerly listed under Google APIs) for a project.
2. Create credentials → API key, from
   https://console.cloud.google.com/apis/credentials.
3. Put it in `TENOR_API_KEY`. Alternatively, Tenor's own docs at
   https://tenor.com/gifapi/documentation list a quick-start test key
   for development — swap in your own before any real usage.

### Safebooru (anime)
No signup, no key. It's a public, unauthenticated API — just leave it
configured as-is.

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
- **Memory**: the **Free** plan gives 128 MB RAM / 0.25 CPU. That's
  workable for light use of `/search`, but the plain-text "4 buttons"
  flow can run 4 searches concurrently (each with its own downloads),
  which can spike memory on a busy request. If you plan to use that
  feature regularly, the **Starter** plan ($5/mo, 512 MB RAM, 3
  projects) is a safer fit; drop `CONCURRENT_DOWNLOADS` and
  `PLAIN_TEXT_COUNT` in `bot.py` if you want to stay on Free.
- Only one process may long-poll a given bot token at a time — don't
  run this bot locally and on Deplexo simultaneously with the same
  token, or Telegram will error on one of them.

## Notes / things you may want to tweak

- `MAX_COUNT` in `bot.py` caps how many files one `/search` can pull
  (default 30).
- `PLAIN_TEXT_COUNT` controls how many files each category fetches when
  triggered via the inline buttons (default 10).
- `CONCURRENT_DOWNLOADS` controls parallel downloads per job (default 6).
- Downloaded files live in a per-job temp folder and are deleted right
  after sending (or on error) — nothing is kept on disk long-term.
- For heavier use, run this on a small always-on server (a $5 VPS, a
  Raspberry Pi, etc.) rather than your laptop, since `run_polling()`
  needs to stay running to receive messages.
