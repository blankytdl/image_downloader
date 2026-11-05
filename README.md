# Telegram Media Search & Download Bot

Send the bot any word or phrase and pick from a 4-item menu:

| # | Menu | Sources |
|---|------|---------|
| 1 | **Stock** | Pexels, Pixabay, Unsplash, Openverse, Open Media (Wikimedia Commons), **🎲 Random from all** |
| 2 | **GIF** | Giphy |
| 3 | **Sound** | Freesound (CC0 / Attribution only) |
| 4 | **Anime** | Safebooru, Yande.re, Konachan, Danbooru, Gelbooru, **🎲 Random from all** |

After you pick a source the bot offers **suggestions** for your word — site tags for anime sources
(Safebooru/Yande.re/Konachan/Danbooru/Gelbooru, and a merged list for the anime mix), Giphy tag suggestions for GIF,
and related/completed terms (Datamuse, keyless) for stock and sound. Tap one, keep your own text, or type something else;
if nothing is found the search simply continues as typed. Then choose count (5–30), minimum resolution, and delivery (📦 one zip, or 🖼 albums — up to 10 files per message group instead of one message per file).
Anime resolution also offers **6K+ / 7K+ / 8K+ / 10K+ / custom (6000–30000 px)**, measured on the
longest side of the real downloaded file. Anime sources only return safe/general-rated art.

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python bot.py
```

Power commands: `/search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]`
(providers: pexels pixabay unsplash openverse openmedia stockmix safebooru yandere konachan danbooru
gelbooru animemix giphy freesound google serpapi), `/overlay`, `/providers`, `/help`.

## Keys

| Variable | Needed for |
|---|---|
| `TELEGRAM_BOT_TOKEN` | always (@BotFather) |
| `PEXELS_API_KEY`, `PIXABAY_API_KEY`, `UNSPLASH_ACCESS_KEY` | those stock sources |
| `GIPHY_API_KEY` | GIF |
| `FREESOUND_API_KEY` | Sound |
| `GELBOORU_API_KEY` + `GELBOORU_USER_ID` | Gelbooru (their API now requires login credentials) |
| — | Safebooru, Yande.re, Konachan, Danbooru, Openverse, Open Media: no key |

A button for an unconfigured source stays visible and tells you which variable to set. In the
**Random from all** modes, unconfigured or temporarily blocked sources are skipped and reported.
`GOOGLE_CSE_*` / `SERPAPI_KEY` remain available only through `/search` (not in the menu).
Tenor is dead (shut down 2026-06-30); Waifu.im was removed.

"Open Media" is implemented as Wikimedia Commons (JPEG/PNG/WebP, ≤40 MB). CC-BY / CC-BY-SA files still
need attribution — check each file's Commons page. Openverse forces commercial-use licenses.

## What was fixed in this version

- **Konachan `403 Forbidden`**: Yande.re/Konachan reject non-browser clients. Requests now use a browser
  identity and Konachan tries `konachan.net` then `konachan.com`. A source that keeps blocking is put on a
  5-minute cool-down so it doesn't spoil every mix. Error text no longer leaks full API URLs.
- **"Sent 1/1 files" / too few results**: the old code downloaded exactly `count` candidates, so every dead link or
  blocked file shrank the result. Now candidates are downloaded in waves until `count` *valid* files exist.
- **Bogus "json"/`.bin` files**: HTML/JSON error pages served as `application/octet-stream` were delivered as
  documents. Every file is now verified from its bytes (real image/audio, readable, requested type, real size).
- **6K+ images not working**: (1) Pillow rejected huge images (decompression-bomb limit), so they were
  dropped — limit raised; (2) sites are now asked server-side for `width:>=N` / `height:>=N` (both axes, merged),
  with automatic fallback; (3) files above Telegram's 50 MB bot limit are re-encoded as same-size JPEG instead of
  failing (a note tells you when this happened); (4) big images go out as documents, not photos.
- GIF: the "Trending" option ignored your word (returned unrelated GIFs) and the HD/Full HD filter would have
  rejected nearly every GIF (Giphy originals are ~480px) — both removed; oversized originals fall back to a smaller
  rendition so they fit Telegram's 50 MB limit.
- "Send individually" now sends albums of up to 10 files at once (photos, large/6K images as document albums, GIFs as
  animations); if an album is rejected it falls back to one-by-one, so nothing is lost.
- The bot no longer freezes for everyone while one long download runs (updates are now processed concurrently);
  expired buttons after a restart still work; an invalid `DEFAULT_PROVIDER` falls back to pexels.
- Telegram uploads now have a 5-minute timeout (the default ~20 s broke 40 MB zips and 6K files) and the connection
  pool was enlarged so parallel jobs don't hit "pool timeout".
- The "custom size" prompt no longer swallows your next message if it isn't a number.
- Booru tag lookups now try the underscore form (`shinobu_oshino`) first.
- Pixabay "Full HD" no longer passes 1280px files; Danbooru no longer requires a user ID; `/help` is plain text
  (Markdown choked on names like `GELBOORU_API_KEY`); edit/flood errors no longer crash jobs; the tag is looked up
  from your original message instead of asking you to type it again.

## Deploying (Docker / Deplexo)

`Dockerfile` + `deplexo.yaml` are included; set variables in the host's environment tab. A health endpoint
listens on `$PORT` (default 8080). Use `GLOBAL_CONCURRENT_DOWNLOADS=3` on 128 MB plans. Note that re-encoding
very large (10K+) images needs RAM; on tiny containers prefer a lower threshold.
