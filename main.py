import asyncio
import logging
import os

from dotenv import load_dotenv

from kover_bot.bot2 import KoverBot


async def main():
    load_dotenv()
    logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    jev_api_key = os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_API_KEY")
    bot = await KoverBot.create(token=token, jev_api_key=jev_api_key)
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
