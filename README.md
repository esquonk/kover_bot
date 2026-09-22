# kover-bot

A Telegram bot that posts random comments, anecdotes, and images from svalko.org.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```console
uv sync
uv run pytest
uv run ruff check .
```

Set the Telegram and Jev API keys in the environment or in a local `.env` file:

```dotenv
TELEGRAM_TOKEN=your-telegram-bot-token
TYPESAFE_API_KEY=your-typesafe-api-key
LOG_LEVEL=DEBUG
```

Then run:

```console
uv run python main.py
```

If `TYPESAFE_API_KEY` is omitted or Jev is unavailable, the bot falls back to
selecting a random svalko.org comment. The legacy `JEV_API_KEY` name is also accepted.

`LOG_LEVEL=DEBUG` logs the Jev request payload and response body. The authorization
header and API key are never logged. The default logging level is `INFO`.
