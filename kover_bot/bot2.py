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

import aiohttp
import reactivex as rx
import reactivex.operators as op
import telegram
from bs4 import BeautifulSoup
from reactivex import Observable
from reactivex.disposable import CompositeDisposable
from reactivex.scheduler.eventloop import AsyncIOThreadSafeScheduler
from reactivex.subject import Subject, BehaviorSubject
from telegram import MessageEntity
from telegram.error import NetworkError, RetryAfter, TimedOut

from kover_bot.rx_utils import skip_some

UPDATE_ID = None

logger = logging.getLogger('root')
logger.setLevel(logging.INFO)

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO,
    stream=sys.stdout
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
    username: str
    disposable: CompositeDisposable = field(default_factory=CompositeDisposable)
    svalko_pic_period: Subject = field(default_factory=partial(BehaviorSubject, value=None))


class KoverBot:
    kovrobot_re = re.compile(r'^.*ковробот.*$', re.IGNORECASE)
    ptaag_re = re.compile(r'\B(\#\w+\b)(?!;.)')
    svalko_re = re.compile(r"^javascript: image_view\(\'svalko\.org\', \'(.*?)\', \d+, \d+\);$")
    anek_re = re.compile(r'^.*анекдот.*$', re.IGNORECASE)
    privet_re = re.compile(r'^о привет$', re.IGNORECASE)

    chats = BehaviorSubject({})
    updates = Subject()
    update_id: int | None
    bot: telegram.Bot

    @classmethod
    async def create(cls, token: str):
        self = cls()

        logger.info("Starting...")

        self.bot = telegram.Bot(token)
        await self.bot.initialize()

        self.update_id = None
        return self

    def command_obs(self, command: str) -> Observable:
        return self.updates.pipe(
            op.filter(lambda update: update.effective_message),
            op.map(lambda update: update.effective_message),
            op.filter(lambda message: (
                    message.entities
                    and message.entities[0].type == MessageEntity.BOT_COMMAND
                    and message.entities[0].offset == 0
                    and message.text
                    and message.text[1: message.entities[0].length].split('@')[0].lower()
                    == command
            )),
            op.share(),
        )

    async def _setup(self):
        scheduler = AsyncIOThreadSafeScheduler(asyncio.get_running_loop())

        def _setup_chat(chat: Chat):
            auto_svalko_pic = chat.svalko_pic_period.pipe(
                op.map(lambda period: (
                    rx.never()
                    if not period else
                    rx.timer(period, period,
                             scheduler=scheduler)
                )),
                op.switch_latest(),
                op.flat_map(
                    lambda message: asyncio.create_task(self.send_svalkopic(
                        None, chat.chat_id, None
                    ))
                ),
            ).subscribe(
                on_next=lambda _: None,
                scheduler=scheduler
            )
            chat.disposable.add(auto_svalko_pic)

        self.chats.pipe(
            op.flat_map(lambda chats: chats.values()),
            op.filter(lambda chat: not chat.disposable.disposable),
            op.do_action(on_next=logger.info)
        ).subscribe(on_next=_setup_chat)

        self.updates.pipe(
            op.with_latest_from(self.chats),
            op.filter(lambda update_and_chats: (
                    update_and_chats[0]
                    and update_and_chats[0].message
                    and update_and_chats[0].message.chat.id
                    and update_and_chats[0].message.chat.type in ('group', 'supergroup')
                    and update_and_chats[0].message.chat.id not in update_and_chats[1]
            ))
        ).subscribe(
            on_next=lambda update_and_chats: self.handle_new_chat(
                chats=update_and_chats[1],
                chat_id=update_and_chats[0].message.chat.id,
                username=update_and_chats[0].message.chat.username)
        )

        messages = self.updates.pipe(
            op.filter(lambda update: bool(update.message and update.message.text)),
            op.share()
        )

        async def get_response(update):
            return update, await self.get_kament()

        # random response
        messages.pipe(
            skip_some(60, 180, 60, 120, partition=lambda update: update.message.chat.id),
            op.flat_map(lambda update: asyncio.create_task(get_response(update))),
            op.flat_map(lambda args: asyncio.create_task(self.send_reply(args[0].message, args[1]))),
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

        # respond to me or to replies on my posts
        messages.pipe(
            op.filter(lambda update: bool(
                self.kovrobot_re.match(update.message.text) or
                (
                        update.message.reply_to_message and
                        update.message.reply_to_message.from_user.id == self.bot.id
                )
            )),
            op.flat_map(lambda update: asyncio.create_task(get_response(update))),
            op.flat_map(lambda args: asyncio.create_task(self.send_reply(args[0].message, args[1]))),
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

        # respond to #ptaag picture
        messages.pipe(
            op.filter(lambda update: bool(self.ptaag_re.match(update.message.text))),
            op.flat_map(lambda update: asyncio.create_task(self.send_svalkopic(
                self.ptaag_re.findall(update.message.text or '')[0].strip('#'),
                update.message.chat_id, update.message.message_id, True
            ))),
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

        # respond to anek
        messages.pipe(
            op.filter(lambda update: bool(self.anek_re.match(update.message.text))),
            op.flat_map(lambda update: asyncio.create_task(self.send_anek(
                update.message.chat_id, update.message.message_id
            ))),
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

        # respond to o privet
        messages.pipe(
            op.filter(lambda update: bool(self.privet_re.match(update.message.text))),
            op.debounce(20),
            op.flat_map(lambda update: asyncio.create_task(self.send_message(
                chat_id=update.message.chat_id,
                text="о привет")
            )),
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

        # /svalkopic
        self.command_obs('svalkopic').pipe(
            op.flat_map(
                lambda message: asyncio.create_task(self.send_svalkopic(
                    ''.join(message.text.split(maxsplit=1)[1:]).lower(),
                    message.chat_id, message.message_id
                ))
            ),
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

        # /kament
        self.command_obs('kament').pipe(
            op.flat_map(
                lambda message: asyncio.create_task(self.send_kament(message.chat_id))
            ),
            op.retry(3)
        ).subscribe(
            on_next=lambda _: None,
            scheduler=scheduler
        )

    def handle_new_chat(self, chats: dict, chat_id, username):
        logger.info(f'handle new chat {chat_id} {username}')
        chats[chat_id] = Chat(chat_id, username)
        if username == 'svalo4ka':
            chats[chat_id].svalko_pic_period.on_next(3600)
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
                await asyncio.sleep(e.retry_after + 1)

    async def run(self):
        await self._setup()
        # get the first pending update_id, this is so we can skip over it in case
        # we get an "Unauthorized" exception.
        async for update in self.get_updates_async():
            await sleep(0.1)
            self.updates.on_next(update)

    async def get_kament(self) -> str:
        tries = 0
        comments = []
        while len(comments) == 0 and tries < 10:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get("https://svalko.org/random.html") as response:
                        content = await response.read()
                        soup = BeautifulSoup(content, "html.parser")
                        comments = [x for x in soup.find_all('div', {'class': 'comment'}) if
                                    len(x.text) < 500 and x.text.strip()]
                        tries += 1
            except aiohttp.ClientError:
                await asyncio.sleep(1)
                tries += 1

        if not comments:
            return ""

        comment = random.choice(comments).find('div', {'class': 'text'})
        return comment.text

    async def send_kament(self, chat_id):
        logger.info(f"send_kament, chat_id={chat_id}")

        kament = await self.get_kament()
        await self.send_message(chat_id=chat_id, text=kament)

    async def send_reply(self, message, text):
        with handle_telegram_error():
            await message.reply_text(text)

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        with handle_telegram_error():
            self.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)

    async def send_anek(self, chat_id, reply_to_id):
        logger.info(f"send_anek, chat_id={chat_id}, reply_to_id={reply_to_id}")

        async with aiohttp.ClientSession() as session:
            async with session.get('http://baneks.ru/random') as response:
                soup = BeautifulSoup(await response.read(), "html.parser")

        anek = soup.find('section', {'class': 'anek-view'}).find('p').text

        await self.send_message(chat_id=chat_id,
                                reply_to_message_id=reply_to_id,
                                text=anek)

    async def send_svalkopic(self, tag_query, chat_id, reply_to_id, fail_silent=False):
        logger.info(f"send_svalkopic, chat_id={chat_id}, reply_to_id={reply_to_id}")

        async with aiohttp.ClientSession() as session:
            if tag_query:
                async with session.get("https://svalko.org/tags.html") as response:
                    soup = BeautifulSoup(await response.read(), "html.parser")

                tagtag = soup.find(attrs={
                    'href': re.compile(r'^/tag/.*', re.IGNORECASE)
                }, text=re.compile(r'{}.*'.format(re.escape(tag_query)), re.IGNORECASE))

                if not tagtag:
                    if not fail_silent:
                        await self.send_message(chat_id=chat_id,
                                                reply_to_message_id=reply_to_id,
                                                text='Нет такого тага')
                    return

                tag_id = tagtag.attrs['href'].replace('/tag/', '')
                async with session.get(f"https://svalko.org{tagtag.attrs['href']}") as response:
                    soup = BeautifulSoup(await response.read(), "html.parser")

                pages = int(
                    soup.find('div', attrs={'class': 'paging'}).find('b').text.strip('[]'))
                success = False
                while not success:
                    page = random.randint(0, pages)
                    async with session.get("https://svalko.org/page/{}?tag_id={}".format(page,
                                                                                         tag_id)) as response:
                        soup = BeautifulSoup(await response.read(), "html.parser")

                    post = random.choice([x.find('div', attrs={'class': 'text'}) for x in
                                          soup.find_all('div', attrs={'class': 'posting'})])
                    tags = post.find('div', attrs={'class': 'tags'})
                    tags.decompose()
                    pictag = post.find('img')
                    if pictag:
                        try:
                            async with session.get(pictag.attrs['src']) as response:
                                b = BytesIO(await response.read())

                            with handle_telegram_error():
                                await self.bot.send_photo(chat_id=chat_id, photo=b,
                                                          caption=post.text[:200])
                            success = True
                        except aiohttp.ClientError:
                            pass
                    else:
                        await self.send_message(chat_id=chat_id, text=post.text)
                        success = True

            else:

                async with session.get(
                        f"https://svalko.org/images.html?rand={random.randint(0, 100000000)}"
                ) as response:
                    soup = BeautifulSoup(await response.read(), "html.parser")
                pictag = random.choice(soup.find_all("a", {"href": self.svalko_re}))
                name = self.svalko_re.match(pictag.attrs['href']).group(1)

                async with session.get(
                        f"https://svalko.org/data/{name}"
                ) as response:
                    b = BytesIO(await response.read())

                with handle_telegram_error():
                    await self.bot.send_photo(chat_id=chat_id, photo=b)
