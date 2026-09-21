import asyncio
import os

from dotenv import load_dotenv

from kover_bot.bot2 import KoverBot


async def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    bot = await KoverBot.create(token=token)
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
