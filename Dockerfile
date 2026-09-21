FROM ghcr.io/astral-sh/uv:0.11.14 AS uv

FROM python:3.14-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY main.py ./
COPY kover_bot ./kover_bot
RUN uv sync --locked --no-dev

CMD ["/app/.venv/bin/python", "main.py"]
