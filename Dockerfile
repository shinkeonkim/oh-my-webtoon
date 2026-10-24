# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY --from=build --chown=app:app /app /app
ENV PATH=/app/.venv/bin:$PATH \
    DATABASE_PATH=/data/my-webtoon.db \
    PYTHONUNBUFFERED=1
USER 10001
VOLUME ["/data"]
EXPOSE 8000
CMD ["my-webtoon", "serve", "--port", "8000"]
