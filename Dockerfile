# Deplexo (and any Docker-based host) builds from this file.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so this layer is cached across code-only changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1

# No secrets baked in -- TELEGRAM_BOT_TOKEN and all provider API keys are
# read from the environment at runtime (set them in Deplexo's Environment
# Variables tab, not in this image).
CMD ["python", "bot.py"]
