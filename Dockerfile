FROM python:3.11-slim

# ffmpeg/ffprobe are required by core/video_facts.py and core/watermark.py —
# not just a nice-to-have, the pipeline can't extract facts or render the
# posting copy without them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "telegram_bot.py"]
