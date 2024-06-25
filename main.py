import asyncio
from kover_bot.bot2 import KoverBot


async def main():
    bot = await KoverBot.create(token='')
    await asyncio.gather(
        bot.run(),
    )


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.run(main())
