FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# ffmpeg for pydub audio chunking (meeting recordings over the 25MB Whisper cap).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer: installs production deps only (no dev extras).
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy app code; runs from /app/src on sys.path (not pip-installed),
# so `pip show hightide-discord-bot` will not list modules.
COPY src/ ./src/

# The launcher runs the events bot and (when DECISION_BOT_TOKEN is set) the
# decision bot in the same process.
CMD ["python", "-m", "src"]
