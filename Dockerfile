FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY updater.py .

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=10m --timeout=5s --start-period=1m --retries=3 \
  CMD /app/.venv/bin/python -c "from pathlib import Path; import time; assert time.time() - Path('/tmp/healthcheck').stat().st_mtime < 900"

ENTRYPOINT ["/app/.venv/bin/python", "-u", "updater.py"]
