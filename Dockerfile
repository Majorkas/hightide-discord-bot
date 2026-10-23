FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Dependency layer: installs production deps only (no dev extras).
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy app code; runs from /app/src on sys.path (not pip-installed),
# so `pip show hightide-discord-bot` will not list modules.
COPY src/ ./src/

CMD ["python", "-m", "src.bot"]
