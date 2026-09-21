# kover-bot

A Telegram bot that posts random comments, anecdotes, and images from svalko.org.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```console
uv sync
uv run pytest
uv run ruff check .
```

Set `TELEGRAM_TOKEN` in the environment or in a local `.env` file, then run:

```console
uv run python main.py
```
