import asyncio
import logging
import random
import re
import sys
from asyncio import sleep
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from io import BytesIO
from urllib.parse import urljoin

import aiohttp
import reactivex as rx
import reactivex.operators as op
import telegram
from bs4 import BeautifulSoup
from reactivex import Observable
from reactivex.disposable import CompositeDisposable
from reactivex.scheduler.eventloop import AsyncIOThreadSafeScheduler
from reactivex.subject import BehaviorSubject, Subject
from telegram import MessageEntity
from telegram.error import NetworkError, RetryAfter, TimedOut

from kover_bot.rx_utils import skip_some

logger = logging.getLogger("root")
logger.setLevel(logging.INFO)

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)


@contextmanager
def handle_telegram_error():
    try:
        yield
    except TimedOut:
        logger.exception("Telegram error")


@dataclass
class Chat:
    chat_id: int
    username: str | None
    disposable: CompositeDisposable = field(default_factory=CompositeDisposable)
    svalko_pic_period: Subject = field(default_factory=partial(BehaviorSubject, value=None))
    is_configured: bool = False


class KoverBot:
    kovrobot_re = re.compile(r"^.*ковробот.*$", re.IGNORECASE)
    ptaag_re = re.compile(r"(?<!\w)#([\w-]+)", re.UNICODE)
    svalko_re = re.compile(r"^javascript: image_view\(\'svalko\.org\', \'(.*?)\', \d+, \d+\);$")
    anek_re = re.compile(r"^.*анекдот.*$", re.IGNORECASE)
    privet_re = re.compile(r"^о привет$", re.IGNORECASE)

    def __init__(self):
        self.chats = BehaviorSubject({})
        self.updates = Subject()
        self.update_id: int | None = None
        self.bot: telegram.Bot
        self.session: aiohttp.ClientSession
        self.disposable = CompositeDisposable()

    @classmethod
    async def create(cls, token: str):
        self = cls()

        logger.info("Starting...")

        self.bot = telegram.Bot(token)
        await self.bot.initialize()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
        )
        return self

    async def close(self):
        self.updates.on_completed()
        self.disposable.dispose()
        for chat in self.chats.value.values():
            chat.disposable.dispose()
        if hasattr(self, "session") and not self.session.closed:
            await self.session.close()
        if hasattr(self, "bot"):
            await self.bot.shutdown()

    async def _run_handler(self, awaitable):
        try:
            return await awaitable
        except Exception:
            logger.exception("Unhandled error in update handler")
            return None

    def _task(self, awaitable):
        return asyncio.create_task(self._run_handler(awaitable))

    async def _get_soup(self, url: str) -> BeautifulSoup:
        async with self.session.get(url) as response:
            response.raise_for_status()
            return BeautifulSoup(await response.text(), "html.parser")

    async def _get_bytes(self, url: str) -> bytes:
        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.read()

    def command_obs(self, command: str) -> Observable:
        return self.updates.pipe(
            op.filter(lambda update: update.effective_message),
            op.map(lambda update: update.effective_message),
            op.filter(
                lambda message: (
                    message.entities
                    and message.entities[0].type == MessageEntity.BOT_COMMAND
                    and message.entities[0].offset == 0
                    and message.text
                    and message.text[1 : message.entities[0].length].split("@")[0].lower()
                    == command
                )
            ),
            op.share(),
        )

    async def _setup(self):
        scheduler = AsyncIOThreadSafeScheduler(asyncio.get_running_loop())

        def _setup_chat(chat: Chat):
            chat.is_configured = True
            auto_svalko_pic = chat.svalko_pic_period.pipe(
                op.map(
                    lambda period: (
                        rx.never() if not period else rx.timer(period, period, scheduler=scheduler)
                    )
                ),
                op.switch_latest(),
                op.flat_map(lambda _: self._task(self.handle_svalkopic(None, chat.chat_id, None))),
            ).subscribe(on_next=lambda _: None, scheduler=scheduler)
            chat.disposable.add(auto_svalko_pic)

        self.disposable.add(
            self.chats.pipe(
                op.flat_map(lambda chats: chats.values()),
                op.filter(lambda chat: not chat.is_configured),
                op.do_action(on_next=logger.info),
            ).subscribe(on_next=_setup_chat)
        )

        self.disposable.add(
            self.updates.pipe(
                op.with_latest_from(self.chats),
                op.filter(
                    lambda update_and_chats: (
                        update_and_chats[0]
                        and update_and_chats[0].message
                        and update_and_chats[0].message.chat.id
                        and update_and_chats[0].message.chat.type in ("group", "supergroup")
                        and update_and_chats[0].message.chat.id not in update_and_chats[1]
                    )
                ),
            ).subscribe(
                on_next=lambda update_and_chats: self.handle_new_chat(
                    chats=update_and_chats[1],
                    chat_id=update_and_chats[0].message.chat.id,
                    username=update_and_chats[0].message.chat.username,
                )
            )
        )

        messages = self.updates.pipe(
            op.filter(lambda update: bool(update.message and update.message.text)), op.share()
        )

        async def get_response(update):
            return update, await self.get_kament()

        # random response
        messages.pipe(
            skip_some(
                300,
                1000,
                30 * 60,
                3 * 60 * 60,
                partition=lambda update: update.message.chat.id,
            ),
            op.flat_map(lambda update: self._task(get_response(update))),
            op.filter(lambda args: bool(args and args[1])),
            op.flat_map(lambda args: self._task(self.send_reply(args[0].message, args[1]))),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

        # respond to me or to replies on my posts
        messages.pipe(
            op.filter(
                lambda update: bool(
                    self.kovrobot_re.match(update.message.text)
                    or (
                        update.message.reply_to_message
                        and update.message.reply_to_message.from_user.id == self.bot.id
                    )
                )
            ),
            op.flat_map(lambda update: self._task(get_response(update))),
            op.filter(lambda args: bool(args and args[1])),
            op.flat_map(lambda args: self._task(self.send_reply(args[0].message, args[1]))),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

        # respond to #ptaag picture
        messages.pipe(
            op.filter(lambda update: bool(self.ptaag_re.search(update.message.text))),
            op.flat_map(
                lambda update: self._task(
                    self.handle_svalkopic(
                        self.ptaag_re.search(update.message.text).group(1),
                        update.message.chat_id,
                        update.message.message_id,
                        True,
                    )
                )
            ),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

        # respond to anek
        messages.pipe(
            op.filter(lambda update: bool(self.anek_re.match(update.message.text))),
            op.flat_map(
                lambda update: self._task(
                    self.handle_anek(update.message.chat_id, update.message.message_id)
                )
            ),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

        # respond to o privet
        messages.pipe(
            op.filter(lambda update: bool(self.privet_re.match(update.message.text))),
            op.debounce(20),
            op.flat_map(
                lambda update: self._task(
                    self.send_message(chat_id=update.message.chat_id, text="о привет")
                )
            ),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

        # /svalkopic
        self.command_obs("svalkopic").pipe(
            op.flat_map(
                lambda message: self._task(
                    self.handle_svalkopic(
                        "".join(message.text.split(maxsplit=1)[1:]).lower(),
                        message.chat_id,
                        message.message_id,
                    )
                )
            ),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

        # /kament
        self.command_obs("kament").pipe(
            op.flat_map(lambda message: self._task(self.handle_kament(message.chat_id))),
            op.retry(3),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

    def handle_new_chat(self, chats: dict, chat_id: int, username: str | None):
        logger.info("handle new chat %s %s", chat_id, username)
        chats = {**chats, chat_id: Chat(chat_id, username)}
        if username == "svalo4ka":
            chats[chat_id].svalko_pic_period.on_next(7200)
        self.chats.on_next(chats)

    async def get_updates_async(self):
        while True:
            try:
                for update in await self.bot.get_updates(offset=self.update_id, timeout=10):
                    logger.debug(f"got update {update.update_id}")
                    self.update_id = update.update_id + 1
                    yield update
            except NetworkError:
                logger.exception("Error when calling bot.get_updates")
                await asyncio.sleep(10)
            except RetryAfter as e:
                logger.exception("RetryAfter when calling bot.get_updates")
                retry_after = e.retry_after
                if hasattr(retry_after, "total_seconds"):
                    retry_after = retry_after.total_seconds()
                await asyncio.sleep(float(retry_after) + 1)

    async def run(self):
        await self._setup()
        async for update in self.get_updates_async():
            await sleep(0.1)
            self.updates.on_next(update)

    async def get_kament(self) -> str:
        for attempt in range(10):
            try:
                soup = await self._get_soup("https://svalko.org/random.html")
                comments = [
                    text.get_text(" ", strip=True)
                    for comment in soup.select("div.comment")
                    if (text := comment.select_one("div.text")) is not None
                    and 0 < len(text.get_text(strip=True)) < 500
                ]
                if comments:
                    return random.choice(comments)
            except aiohttp.ClientError:
                logger.warning("Could not fetch a comment (attempt %s/10)", attempt + 1)
            await asyncio.sleep(1)
        return ""

    async def send_reply(self, message, text):
        with handle_telegram_error():
            await message.reply_text(text)

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        with handle_telegram_error():
            await self.bot.send_message(
                chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id
            )

    async def handle_kament(self, chat_id):
        logger.info(f"send_kament, chat_id={chat_id}")

        await self.bot.send_chat_action(chat_id=chat_id, action="typing")

        kament = await self.get_kament()
        if kament:
            await self.send_message(chat_id=chat_id, text=kament)

    async def handle_anek(self, chat_id, reply_to_id):
        logger.info(f"send_anek, chat_id={chat_id}, reply_to_id={reply_to_id}")

        await self.bot.send_chat_action(chat_id=chat_id, action="typing")

        soup = await self._get_soup("https://baneks.ru/random")
        paragraph = soup.select_one("section.anek-view p")
        if paragraph is None:
            raise ValueError("baneks.ru response did not contain an anecdote")
        anek = paragraph.get_text(" ", strip=True)

        await self.send_message(chat_id=chat_id, reply_to_message_id=reply_to_id, text=anek)

    async def handle_svalkopic(self, tag_query, chat_id, reply_to_id, fail_silent=False):
        logger.info(f"send_svalkopic, chat_id={chat_id}, reply_to_id={reply_to_id}")

        await self.bot.send_chat_action(chat_id=chat_id, action="upload_photo")

        if tag_query:
            soup = await self._get_soup("https://svalko.org/tags.html")

            tagtag = soup.find(
                attrs={"href": re.compile(r"^/tag/.*", re.IGNORECASE)},
                string=re.compile(rf"{re.escape(tag_query)}.*", re.IGNORECASE),
            )

            if not tagtag:
                if not fail_silent:
                    await self.send_message(
                        chat_id=chat_id, reply_to_message_id=reply_to_id, text="Нет такого тага"
                    )
                return

            tag_id = tagtag.attrs["href"].replace("/tag/", "")
            soup = await self._get_soup(urljoin("https://svalko.org", tagtag.attrs["href"]))

            paging = soup.select_one("div.paging b")
            pages = int(paging.get_text(strip=True).strip("[]")) if paging else 0
            for _ in range(10):
                page = random.randint(0, pages)
                soup = await self._get_soup(f"https://svalko.org/page/{page}?tag_id={tag_id}")

                posts = [
                    post
                    for posting in soup.select("div.posting")
                    if (post := posting.select_one("div.text")) is not None
                ]
                if not posts:
                    continue
                post = random.choice(posts)
                tags = post.select_one("div.tags")
                if tags is not None:
                    tags.decompose()
                pictag = post.find("img", src=True)
                if pictag:
                    try:
                        data = await self._get_bytes(urljoin("https://svalko.org", pictag["src"]))
                    except aiohttp.ClientError:
                        continue
                    with handle_telegram_error():
                        await self.bot.send_photo(
                            chat_id=chat_id,
                            photo=BytesIO(data),
                            caption=post.get_text(" ", strip=True)[:200] or None,
                        )
                else:
                    await self.send_message(chat_id=chat_id, text=post.get_text(" ", strip=True))
                return
            raise RuntimeError("Could not find a post for the requested tag")

        soup = await self._get_soup(
            f"https://svalko.org/images.html?rand={random.randint(0, 100000000)}"
        )
        pictures = soup.find_all("a", {"href": self.svalko_re})
        if not pictures:
            raise ValueError("svalko.org response did not contain any pictures")
        href = random.choice(pictures)["href"]
        match = self.svalko_re.match(href)
        if match is None:
            raise ValueError("svalko.org returned an invalid picture link")
        data = await self._get_bytes(f"https://svalko.org/data/{match.group(1)}")

        with handle_telegram_error():
            await self.bot.send_photo(chat_id=chat_id, photo=BytesIO(data))
