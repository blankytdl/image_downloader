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
if nothing is found the search simply continues as typed. Then choose count (5–30), minimum resolution, and delivery (📦 one zip, or 🖼 🖼 one file at a time).
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
- **Server crashes / disk and RAM use**: "send one by one" now downloads ONE file, sends it, DELETES it, then fetches the
  next (peak disk = one file). Zip mode deletes the loose files before uploading the archive. At most
  `MAX_CONCURRENT_JOBS` (default 2) jobs run at once, others queue. Re-encoding of oversized images is capped
  (`REENCODE_MAX_PIXELS`) because decoding a huge image needs gigabytes of RAM. Leftover temp folders are deleted at
  startup, and jobs refuse to start when the disk is nearly full.
- **Pexels `HTTP 401`**: the key was rejected. Keys are now trimmed of stray spaces/quotes/newlines (a very common cause);
  if it still fails, the key itself is wrong or not activated — the error message names the variable to check.
- **Openverse `HTTP 403`**: supports free OAuth credentials (`OPENVERSE_CLIENT_ID` / `OPENVERSE_CLIENT_SECRET`, see
  `.env.example`) which avoid the anonymous block.
- **Konachan**: tries both hosts, both API paths and a second header set, logs what the site answered, and supports
  `BOORU_PROXY` for hosting-IP bans. Error messages now say what to do.
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
