FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Dependency layer: installs production deps only (no dev extras).
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY src/ ./src/

CMD ["python", "-m", "src.bot"]
