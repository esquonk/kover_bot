import asyncio
import os

from dotenv import load_dotenv

from kover_bot.bot2 import KoverBot


async def main():
    load_dotenv()
    bot = await KoverBot.create(token=os.getenv("TELEGRAM_TOKEN"))
    await asyncio.gather(
        bot.run(),
    )


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.run(main())
