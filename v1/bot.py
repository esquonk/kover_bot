import datetime
import logging
import random
import re
from time import sleep

import pytz as pytz
from bs4 import BeautifulSoup
from io import BytesIO

import requests
from requests.exceptions import MissingSchema

from telegram.ext import Updater, CommandHandler, MessageHandler
from telegram.ext.filters import Filters
from telegram.inline.inlinekeyboardbutton import InlineKeyboardButton
from telegram.inline.inlinekeyboardmarkup import InlineKeyboardMarkup

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.DEBUG)

kover_query = ['persian rug', 'rug', 'beautiful carpet', 'фото с ковром', 'советский ковёр', 'ковёр']
fallback_query = ''
SVALKO_ID = -1001030074733


def now():
    return datetime.datetime.now(pytz.utc).timestamp()


class WebSearch:
    headers = {'Ocp-Apim-Subscription-Key': ""}

    def search(self, query):
        r = requests.get("https://api.cognitive.microsoft.com/bing/v7.0/images/search",
                         params={'q': query}, headers=self.headers)
        if not r.json()['value']:
            return
        thumb = random.choice(r.json()['value'][:10])['thumbnailUrl']
        imgr = requests.get(thumb)
        b = BytesIO(imgr.content)
        return b


class Bot:
    updater = Updater(token='')
    dispatcher = updater.dispatcher
    search = WebSearch()
    svalko_re = re.compile(r"^javascript: image_view\(\'svalko\.org\', \'(.*?)\', \d+, \d+\);$")
    anek_re = re.compile(r'^.*анекдот.*$', re.IGNORECASE)
    privet_re = re.compile(r'^о привет$', re.IGNORECASE)
    kovrobot_re = re.compile(r'^.*ковробот.*$', re.IGNORECASE)
    choice_re = re.compile(r'\bили\b', re.IGNORECASE)
    choice_starter = re.compile(r'^ковробот,', re.IGNORECASE)
    ptaag_re = re.compile(r'\B(\#\w+\b)(?!;.)')

    kament_message_skip = random.randint(40, 120)
    last_message_date = 0
    last_privet_date = 0

    def can_send_message(self):
        return self.last_message_date == 0 or \
               now() - self.last_message_date > 30

    def start(self, bot, update):
        bot.send_message(chat_id=update.message.chat_id, text="I'm a bot, please talk to me!")

    def get_kament(self):
        tries = 0
        comments = []
        while len(comments) == 0 and tries < 10:
            r = requests.get("https://svalko.org/random.html")
            soup = BeautifulSoup(r.content, "html.parser")
            comments = [x for x in soup.find_all('div', {'class': 'comment'}) if len(x.text) < 500]
            tries += 1

        comment = random.choice(comments).find('div', {'class': 'text'})
        return comment.text

    def zoebis_kover(self, bot, update):
        q = update.message.text.replace('/zoebis_kover', '').strip() or random.choice(kover_query)
        res = self.search.search(q)
        if res:
            bot.send_photo(chat_id=update.message.chat_id, photo=res)
        else:
            bot.send_message(chat_id=update.message.chat_id,
                             text='Сам ищи подобную хуетень!',
                             reply_to_message_id=update.message.message_id)

    def tags(self, bot, update):
        r = requests.get("https://svalko.org/tags.html")
        soup = BeautifulSoup(r.content, "html.parser")
        tagtags = soup.find(attrs={
            'href': re.compile(r'^/tag/.*', re.IGNORECASE)
        })

        tagtags = random.choices(tagtags, k=5)
        buttons = []
        for tagtag in tagtags:
            buttons.append(InlineKeyboardButton(tagtag.tag))

        keyboard = InlineKeyboardMarkup(buttons)

        bot.send_message(chat_id=update.message.chat_id,
                         reply_to_message_id=update.message.message_id,
                         text='',
                         reply_markup=keyboard)

    def send_svalkopic(self, bot, tag_query, chat_id, reply_to_id, fail_silent=False):
        if tag_query:
            r = requests.get("https://svalko.org/tags.html")
            soup = BeautifulSoup(r.content, "html.parser")
            tagtag = soup.find(attrs={
                'href': re.compile(r'^/tag/.*', re.IGNORECASE)
            }, text=re.compile(r'{}.*'.format(re.escape(tag_query)), re.IGNORECASE))
            if tagtag:
                tag_id = tagtag.attrs['href'].replace('/tag/', '')
                r = requests.get("https://svalko.org{}".format(tagtag.attrs['href']))
                soup = BeautifulSoup(r.content, "html.parser")
                pages = int(soup.find('div', attrs={'class': 'paging'}).find('b').text.strip('[]'))
                success = False
                while not success:
                    page = random.randint(0, pages)
                    r = requests.get("https://svalko.org/page/{}?tag_id={}".format(page, tag_id))
                    soup = BeautifulSoup(r.content, "html.parser")
                    post = random.choice([x.find('div', attrs={'class': 'text'}) for x in
                                          soup.find_all('div', attrs={'class': 'posting'})])
                    tags = post.find('div', attrs={'class': 'tags'})
                    tags.decompose()
                    pictag = post.find('img')
                    if pictag:
                        try:
                            img_r = requests.get(pictag.attrs['src'])
                            b = BytesIO(img_r.content)
                            bot.send_photo(chat_id=chat_id, photo=b, caption=post.text[:200])
                            success = True
                        except MissingSchema:
                            pass
                    else:
                        bot.send_message(chat_id=chat_id, text=post.text)
                        success = True

            else:
                if not fail_silent:
                    bot.send_message(chat_id=chat_id,
                                     reply_to_message_id=reply_to_id,
                                     text='Нет такого тага')

        else:

            r = requests.get("https://svalko.org/images.html", params={'rand': str(random.randint(0, 100000000))})
            soup = BeautifulSoup(r.content, "html.parser")
            pictag = random.choice(soup.find_all("a", {"href": self.svalko_re}))
            name = self.svalko_re.match(pictag.attrs['href']).group(1)

            img_r = requests.get("https://svalko.org/data/{}".format(name))
            b = BytesIO(img_r.content)
            bot.send_photo(chat_id=chat_id, photo=b)

    def svalkopic(self, bot, update):
        query = ''.join(update.message.text.split(maxsplit=1)[1:]).lower()
        self.send_svalkopic(bot, query, update.message.chat_id, update.message.message_id)

    def kament(self, bot, update):
        bot.send_message(chat_id=update.message.chat_id,
                         text=self.get_kament())

    def private_message_handler(self, bot, update, chat_data):
        name = update.message.from_user.username
        if name:
            name = '@' + name
        else:
            name = update.message.from_user.full_name
        name = name.upper()

        if update.message.text.startswith('sv ') or update.message.text.startswith('св '):
            text = update.message.text[3:]
            text += " (ЭТО НАПИСАЛ {})".format(name)
            bot.send_message(chat_id=SVALKO_ID,
                             text=text)

        elif not update.message.text.startswith('/'):
            bot.send_message(chat_id=update.message.chat_id,
                             text='Пиши "sv сообщение" или "св сообщение", чтобы зослать на свалко')

    def chat_message_handler(self, bot, update, chat_data):
        if update.message.from_user.id == bot.id:
            return

        tags = self.ptaag_re.findall(update.message.text or '')
        if tags and self.can_send_message():
            self.send_svalkopic(bot, tags[0].strip('#'), update.message.chat_id, update.message.message_id, True)

        elif update.message.text and self.anek_re.match(update.message.text):
            r = requests.get('http://baneks.ru/random')
            soup = BeautifulSoup(r.content, "html.parser")
            anek = soup.find('section', {'class': 'anek-view'}).find('p').text
            bot.send_message(chat_id=update.message.chat_id,
                             reply_to_message_id=update.message.message_id,
                             text=anek)
            self.last_message_date = now()

        elif update.message.text and self.privet_re.match(update.message.text) and self.can_send_message():
            if now() - self.last_privet_date > 300:
                sleep(2)
                bot.send_message(chat_id=update.message.chat_id,
                                 text='о привет')
                self.last_privet_date = now()
                self.last_message_date = now()

        # handle choices
        elif update.message.text and \
                self.choice_re.findall(update.message.text) and \
                (
                        (update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot.id)
                        or self.choice_starter.match(update.message.text)
                ):

            text = update.message.text.rstrip('?!')
            text = self.choice_starter.sub('', text)
            choice = random.choice(self.choice_re.split(text)).strip()
            choice = choice[0].upper() + choice[1:]
            bot.send_message(chat_id=update.message.chat_id,
                             reply_to_message_id=update.message.message_id,
                             text=choice)

            self.last_message_date = now()

        # handle my name mention and replies to me
        elif (update.message.text and self.kovrobot_re.match(update.message.text)) or \
                update.message.reply_to_message and \
                update.message.reply_to_message.from_user.id == bot.id:

            bot.send_message(chat_id=update.message.chat_id,
                             reply_to_message_id=update.message.message_id,
                             text=self.get_kament())

            self.last_message_date = now()

        elif self.kament_message_skip == 0:
            if self.can_send_message():
                bot.send_message(chat_id=update.message.chat_id,
                                 reply_to_message_id=update.message.message_id,
                                 text=self.get_kament())
                self.kament_message_skip = random.randint(40, 120)
                self.last_message_date = now()
        else:
            self.kament_message_skip -= 1

    def run(self):
        start_handler = CommandHandler('start', self.start)
        self.dispatcher.add_handler(start_handler)
        svalkopic_handler = CommandHandler('svalkopic', self.svalkopic)
        self.dispatcher.add_handler(svalkopic_handler)
        tags_handler = CommandHandler('tags', self.tags)
        self.dispatcher.add_handler(tags_handler)
        zoebis_kover_handler = CommandHandler('zoebis_kover', self.zoebis_kover)
        self.dispatcher.add_handler(zoebis_kover_handler)
        kament_handler = CommandHandler('kament', self.kament)
        self.dispatcher.add_handler(kament_handler)

        chat_message_handler = MessageHandler(Filters.group, self.chat_message_handler, pass_chat_data=True)
        self.dispatcher.add_handler(chat_message_handler)
        private_message_handler = MessageHandler(Filters.private, self.private_message_handler, pass_chat_data=True)
        self.dispatcher.add_handler(private_message_handler)

        self.updater.start_polling()


if __name__ == "__main__":
    bot = Bot()
    bot.run()
