import asyncio
import logging
import random
import re
import sys
from asyncio import sleep
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

from kover_bot.response_selector import select_svalko_response
from kover_bot.rx_utils import skip_some

logger = logging.getLogger("root")
logger.setLevel(logging.INFO)

MAX_RECENT_MESSAGES = 20
MESSAGE_CONTEXT_TTL = timedelta(hours=2)
KAMENT_ROUND_SIZE = 50
KAMENT_MAX_ROUNDS = 20
KAMENT_MIN_CONFIDENCE = 0.5
KAMENT_SOURCE_PAGE_COUNT = 3
KAMENT_MAX_FETCHES_PER_ROUND = 3
# Telegram clears a chat action after about 5 seconds.
CHAT_ACTION_REFRESH_PERIOD = 4
AUTOMATIC_KAMENT_MESSAGE_COUNT = 100
AUTOMATIC_KAMENT_PERIOD = 60 * 60
AUTO_MESSAGE_CHATS = ["svalo4ka"]

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
class RecentMessage:
    received_at: datetime
    speaker: str
    text: str


@dataclass
class Chat:
    chat_id: int
    username: str | None
    disposable: CompositeDisposable = field(default_factory=CompositeDisposable)
    svalko_pic_period: Subject = field(default_factory=partial(BehaviorSubject, value=None))
    is_configured: bool = False
    recent_messages: deque[RecentMessage] = field(
        default_factory=lambda: deque(maxlen=MAX_RECENT_MESSAGES)
    )

    def remember_message(
        self, text: str, speaker: str = "Unknown", *, received_at: datetime | None = None
    ):
        text = text.strip()
        if text:
            self.recent_messages.append(
                RecentMessage(received_at or datetime.now(UTC), speaker.strip() or "Unknown", text)
            )

    def recent_message_texts(self, *, now: datetime | None = None) -> list[str]:
        return [message["text"] for message in self.recent_message_context(now=now)]

    def recent_message_context(self, *, now: datetime | None = None) -> list[dict[str, str]]:
        cutoff = (now or datetime.now(UTC)) - MESSAGE_CONTEXT_TTL
        while self.recent_messages and self.recent_messages[0].received_at < cutoff:
            self.recent_messages.popleft()
        return [
            {"speaker": message.speaker, "text": message.text} for message in self.recent_messages
        ]


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
        self.jev_api_key: str | None = None
        self.disposable = CompositeDisposable()

    @classmethod
    async def create(cls, token: str, jev_api_key: str | None = None):
        self = cls()

        logger.info("Starting...")

        self.bot = telegram.Bot(token)
        await self.bot.initialize()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
        )
        self.jev_api_key = jev_api_key
        if not jev_api_key:
            logger.warning("JEV_API_KEY is not set; responses will be selected randomly")
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
        self.disposable.add(messages.subscribe(on_next=self.remember_message))

        # Only the main chat gets unsolicited comments. It needs both a quiet
        # enough interval and enough new conversation before Jev may comment.
        messages.pipe(
            op.filter(lambda update: self.is_auto_kament_chat(update.message.chat.id)),
            skip_some(
                # skip_some emits after its configured number of skipped values.
                AUTOMATIC_KAMENT_MESSAGE_COUNT - 1,
                AUTOMATIC_KAMENT_MESSAGE_COUNT - 1,
                AUTOMATIC_KAMENT_PERIOD,
                AUTOMATIC_KAMENT_PERIOD,
                partition=lambda update: update.message.chat.id,
            ),
            op.flat_map(
                lambda update: self._task(self.select_automatic_kament(update.message.chat.id))
            ),
            op.filter(lambda response: bool(response)),
            op.flat_map(
                lambda response: self._task(
                    self.send_message(chat_id=response[0], text=response[1])
                )
            ),
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
            op.flat_map(lambda update: self._task(self.handle_mention(update.message))),
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
            op.flat_map(lambda message: self._task(self.handle_kament(message))),
            op.retry(3),
        ).subscribe(on_next=lambda _: None, scheduler=scheduler)

    def handle_new_chat(self, chats: dict, chat_id: int, username: str | None):
        logger.info("handle new chat %s %s", chat_id, username)
        chats = {**chats, chat_id: Chat(chat_id, username)}
        if username == "svalo4ka":
            chats[chat_id].svalko_pic_period.on_next(7200)
        self.chats.on_next(chats)

    def is_auto_kament_chat(self, chat_id: int) -> bool:
        chat = self.chats.value.get(chat_id)
        return bool(chat and chat.username in AUTO_MESSAGE_CHATS)

    def remember_message(self, update):
        message = update.message
        chat = self.chats.value.get(message.chat.id)
        if chat is None:
            return
        if message.from_user and message.from_user.id == self.bot.id:
            return
        if any(
            entity.type == MessageEntity.BOT_COMMAND and entity.offset == 0
            for entity in message.entities or ()
        ):
            return
        chat.remember_message(message.text, self.message_context(message)["speaker"])

    @staticmethod
    def message_context(message) -> dict[str, str]:
        user = message.from_user
        sender_chat = getattr(message, "sender_chat", None)
        speaker = (
            getattr(user, "full_name", None)
            or getattr(user, "username", None)
            or getattr(sender_chat, "title", None)
            or "Unknown"
        )
        return {
            "speaker": speaker,
            "text": message.text or getattr(message, "caption", None) or "",
        }

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

    async def get_kament_candidates(self) -> list[str]:
        results = await asyncio.gather(
            *(
                self._get_soup(
                    f"https://svalko.org/random.html?rand={random.randint(0, 100_000_000)}"
                )
                for _ in range(KAMENT_SOURCE_PAGE_COUNT)
            ),
            return_exceptions=True,
        )
        comments = []
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Could not fetch candidate comments", exc_info=result)
                continue
            comments.extend(
                text.get_text(" ", strip=True)
                for comment in result.select("div.comment")
                if (text := comment.select_one("div.text")) is not None
                and 0 < len(text.get_text(strip=True)) < 500
                # Comment pagination is rendered as a comment: "насрано N раз: [0] [1] ..."
                and "[0]" not in text.get_text()
            )

        return list(dict.fromkeys(comments))

    async def _select_best_kament(
        self, context: list[dict[str, str]], message_to_answer: dict[str, str] | None = None
    ) -> tuple[str, float, list[str]]:
        """Run Jev passes over fresh candidates until one is confident enough.

        Returns the best response, its confidence, and every candidate seen.
        """
        seen: list[str] = []
        pool: list[str] = []
        best, best_confidence = "", 0.0
        jev_requests = page_fetches = tried = 0
        for _ in range(KAMENT_MAX_ROUNDS):
            for _ in range(KAMENT_MAX_FETCHES_PER_ROUND):
                if len(pool) >= KAMENT_ROUND_SIZE:
                    break
                page_fetches += 1
                fresh = [c for c in await self.get_kament_candidates() if c not in seen]
                seen.extend(fresh)
                pool.extend(fresh)
            if not pool:
                break

            group, pool = pool[:KAMENT_ROUND_SIZE], pool[KAMENT_ROUND_SIZE:]
            jev_requests += 1
            tried += len(group)
            response, confidence = await select_svalko_response(
                context,
                group,
                api_key=self.jev_api_key,
                session=self.session,
                message_to_answer=message_to_answer,
            )
            if response and (not best or confidence > best_confidence):
                best, best_confidence = response, confidence
            if best_confidence > KAMENT_MIN_CONFIDENCE:
                break
        if best:
            logger.info(
                "Picked kament: response=%r confidence=%s jev_requests=%s page_fetches=%s"
                " candidates_tried=%s",
                best,
                best_confidence,
                jev_requests,
                page_fetches * KAMENT_SOURCE_PAGE_COUNT,
                tried,
            )
        return best, best_confidence, seen

    async def select_kament(
        self, chat_id: int, *, message_to_answer: dict[str, str] | None = None
    ) -> str:
        if not self.jev_api_key:
            candidates = await self.get_kament_candidates()
            return random.choice(candidates) if candidates else ""

        chat = self.chats.value.get(chat_id)
        context = chat.recent_message_context() if chat else []
        if message_to_answer and context and context[-1] == message_to_answer:
            context = context[:-1]

        best, _, candidates = await self._select_best_kament(context, message_to_answer)
        if best:
            return best
        return random.choice(candidates) if candidates else ""

    async def select_automatic_kament(self, chat_id: int) -> tuple[int, str] | None:
        """Return an unsolicited comment only when Jev endorses it strongly."""
        if not self.jev_api_key:
            return None

        chat = self.chats.value.get(chat_id)
        context = chat.recent_message_context() if chat else []

        winner, confidence, _ = await self._select_best_kament(context)
        if winner and confidence > KAMENT_MIN_CONFIDENCE:
            return chat_id, winner
        return None

    async def send_reply(self, message, text):
        with handle_telegram_error():
            await message.reply_text(text)

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        with handle_telegram_error():
            await self.bot.send_message(
                chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id
            )

    @asynccontextmanager
    async def keep_chat_action(self, chat_id, action="typing"):
        """Keep showing a chat action until the block finishes."""

        async def refresh():
            while True:
                try:
                    await self.bot.send_chat_action(chat_id=chat_id, action=action)
                except NetworkError, RetryAfter:
                    logger.warning("Could not send chat action", exc_info=True)
                await sleep(CHAT_ACTION_REFRESH_PERIOD)

        task = asyncio.create_task(refresh())
        try:
            yield
        finally:
            task.cancel()

    async def handle_kament(self, message):
        chat_id = message.chat_id
        logger.info(f"send_kament, chat_id={chat_id}")

        target = (
            self.message_context(message.reply_to_message) if message.reply_to_message else None
        )
        async with self.keep_chat_action(chat_id):
            kament = await self.select_kament(chat_id, message_to_answer=target)
            if kament:
                await self.send_message(chat_id=chat_id, text=kament)

    async def handle_mention(self, message):
        chat_id = message.chat_id
        logger.info(f"reply_to_mention, chat_id={chat_id}")

        async with self.keep_chat_action(chat_id):
            kament = await self.select_kament(
                chat_id, message_to_answer=self.message_context(message)
            )
            if kament:
                await self.send_reply(message, kament)

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
