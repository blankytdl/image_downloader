# Telegram Photo Search & Batch-Download Bot

## 1. Files

- `main.py` — bot code
- `.env.example` — environment-variable template
- `requirements.txt` — Python dependencies

## 2. Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the API keys you actually use.

At minimum:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
```

Safebooru works without an API key.

## 3. Run

```bash
python main.py
```

## 4. Commands

```text
/start
/help
/providers
/search <query> [count] [provider] [type:png|jpg|gif] [minscore:N]
```

Plain text searches open the button wizard.

## 5. Providers

- Pexels
- Pixabay
- Unsplash
- Google CSE
- SerpApi
- Safebooru
- Giphy

Tenor is intentionally disabled because its API was decommissioned.

## 6. Hosting

The bot includes a small HTTP health endpoint on `$PORT`, which is useful for hosts that require a listening port even when the Telegram bot itself uses polling.
