FROM python:3.11-slim

WORKDIR /app

# Install deps first so they're cached across rebuilds when only bot.py changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "bot.py"]
