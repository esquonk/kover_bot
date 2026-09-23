import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
from telegram import MessageEntity

from kover_bot.bot2 import (
    KAMENT_MAX_ROUNDS,
    KAMENT_ROUND_SIZE,
    KAMENT_SOURCE_PAGE_COUNT,
    MAX_RECENT_MESSAGES,
    Chat,
    KoverBot,
    RecentMessage,
)


def test_reactive_state_is_not_shared_between_instances():
    first = KoverBot()
    second = KoverBot()

    first.handle_new_chat(first.chats.value, 123, "first")

    assert 123 in first.chats.value
    assert 123 not in second.chats.value


def test_hashtag_can_appear_anywhere_in_message():
    match = KoverBot.ptaag_re.search("please find #cats-and-dogs today")

    assert match is not None
    assert match.group(1) == "cats-and-dogs"


def test_hashtag_does_not_match_inside_a_word():
    assert KoverBot.ptaag_re.search("email#tag") is None


def test_chat_keeps_only_the_most_recent_messages():
    chat = Chat(123, "test")

    for index in range(MAX_RECENT_MESSAGES + 5):
        chat.remember_message(f"message {index}")

    assert chat.recent_message_texts() == [
        f"message {index}" for index in range(5, MAX_RECENT_MESSAGES + 5)
    ]


def test_chat_discards_expired_messages():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    chat = Chat(123, "test")
    chat.recent_messages.extend(
        [
            RecentMessage(now - timedelta(hours=3), "Alice", "old"),
            RecentMessage(now - timedelta(minutes=30), "Bob", "recent"),
        ]
    )

    assert chat.recent_message_texts(now=now) == ["recent"]


def test_only_svalo4ka_receives_automatic_kaments():
    bot = KoverBot()
    bot.handle_new_chat(bot.chats.value, 123, "other-chat")
    bot.handle_new_chat(bot.chats.value, 456, "svalo4ka")

    assert not bot.is_auto_kament_chat(123)
    assert bot.is_auto_kament_chat(456)


def test_remember_message_ignores_commands_and_bot_messages():
    bot = KoverBot()
    bot.bot = SimpleNamespace(id=999)
    bot.handle_new_chat(bot.chats.value, 123, "test")

    def update(text, user_id, entities=None, full_name="Alice"):
        return SimpleNamespace(
            message=SimpleNamespace(
                text=text,
                chat=SimpleNamespace(id=123),
                from_user=SimpleNamespace(id=user_id, full_name=full_name),
                entities=entities,
            )
        )

    bot.remember_message(update("hello", 1))
    bot.remember_message(update("from the bot", 999))
    bot.remember_message(
        update(
            "/kament",
            1,
            [SimpleNamespace(type=MessageEntity.BOT_COMMAND, offset=0)],
        )
    )

    assert bot.chats.value[123].recent_message_texts() == ["hello"]
    assert bot.chats.value[123].recent_message_context() == [{"speaker": "Alice", "text": "hello"}]


def test_get_kament_candidates_collects_unique_comments():
    bot = KoverBot()

    async def get_soup(_):
        return BeautifulSoup(
            """
            <div class="comment"><div class="text">first</div></div>
            <div class="comment"><div class="text">second</div></div>
            <div class="comment"><div class="text">first</div></div>
            """,
            "html.parser",
        )

    bot._get_soup = get_soup

    assert asyncio.run(bot.get_kament_candidates()) == ["first", "second"]


def test_get_kament_candidates_skips_comment_pagination():
    bot = KoverBot()

    async def get_soup(_):
        return BeautifulSoup(
            """
            <div class="comment"><div class="text">real comment [1]</div></div>
            <div class="comment"><div class="text">насрано 60 раз:<br>
            <a href="/1.html?page=0#c">[0]</a><a href="/1.html?page=1#c">[1]</a></div></div>
            """,
            "html.parser",
        )

    bot._get_soup = get_soup

    assert asyncio.run(bot.get_kament_candidates()) == ["real comment [1]"]


def test_select_kament_uses_chat_context_and_jev(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()
    bot.handle_new_chat(bot.chats.value, 123, "test")
    bot.chats.value[123].remember_message("recent conversation")

    async def get_candidates():
        return ["first", "second"]

    selected_with = None

    async def select_response(context, candidates, **kwargs):
        nonlocal selected_with
        selected_with = (context, candidates, kwargs)
        return "second", 0.8

    bot.get_kament_candidates = get_candidates
    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)

    assert asyncio.run(bot.select_kament(123)) == "second"
    assert selected_with[0] == [{"speaker": "Unknown", "text": "recent conversation"}]
    assert set(selected_with[1]) == {"first", "second"}
    assert selected_with[2] == {
        "api_key": "test-key",
        "session": bot.session,
        "message_to_answer": None,
    }


def _paged_candidates(bot, page_size):
    """Make every candidate fetch return a fresh page of candidates."""
    fetches = 0

    async def get_candidates():
        nonlocal fetches
        start = fetches * page_size
        fetches += 1
        return [f"candidate {index}" for index in range(start, start + page_size)]

    bot.get_kament_candidates = get_candidates
    return lambda: fetches


def test_select_kament_stops_after_a_confident_pass(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()
    fetches = _paged_candidates(bot, 20)
    calls = []

    async def select_response(context, round_candidates, **kwargs):
        calls.append(round_candidates)
        return round_candidates[0], 0.8

    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)

    assert asyncio.run(bot.select_kament(123)) == "candidate 0"
    assert [len(call) for call in calls] == [KAMENT_ROUND_SIZE]
    assert fetches() == 3


def test_select_kament_logs_the_picked_response(monkeypatch, caplog):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()
    _paged_candidates(bot, 30)
    confidences = [0.3, 0.7]

    async def select_response(context, round_candidates, **kwargs):
        return round_candidates[0], confidences.pop(0)

    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)

    with caplog.at_level(logging.INFO, logger="root"):
        asyncio.run(bot.select_kament(123))

    assert (
        "Picked kament: response='candidate 50' confidence=0.7 jev_requests=2"
        f" page_fetches={4 * KAMENT_SOURCE_PAGE_COUNT} candidates_tried=100"
    ) in caplog.text


def test_select_kament_repeats_passes_and_picks_the_best(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()
    _paged_candidates(bot, 20)
    calls = []
    confidences = [0.2, 0.4, 0.3] + [0.1] * (KAMENT_MAX_ROUNDS - 3)

    async def select_response(context, round_candidates, **kwargs):
        calls.append(round_candidates)
        return round_candidates[0], confidences[len(calls) - 1]

    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)

    selected = asyncio.run(bot.select_kament(123))

    assert len(calls) == KAMENT_MAX_ROUNDS
    assert all(len(call) == KAMENT_ROUND_SIZE for call in calls)
    # Leftover candidates carry over, so no candidate is offered twice.
    offered = [candidate for call in calls for candidate in call]
    assert len(offered) == len(set(offered))
    assert selected == calls[1][0]


def test_select_kament_falls_back_to_random_when_jev_fails(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()

    async def get_candidates():
        return ["only"]

    async def select_response(context, round_candidates, **kwargs):
        return "", 0.0

    bot.get_kament_candidates = get_candidates
    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)

    assert asyncio.run(bot.select_kament(123)) == "only"


def test_select_automatic_kament_uses_confident_finalist(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()
    candidates = [f"candidate {index}" for index in range(KAMENT_ROUND_SIZE * 2)]

    async def get_candidates():
        return candidates.copy()

    async def select_response(context, round_candidates, **kwargs):
        return round_candidates[0], 0.6

    bot.get_kament_candidates = get_candidates
    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)
    monkeypatch.setattr("kover_bot.bot2.random.shuffle", lambda _: None)

    assert asyncio.run(bot.select_automatic_kament(123)) == (123, "candidate 0")


def test_select_automatic_kament_keeps_quiet_at_or_below_confidence_threshold(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()

    async def get_candidates():
        return [f"candidate {index}" for index in range(KAMENT_ROUND_SIZE * 2)]

    async def select_response(context, round_candidates, **kwargs):
        return round_candidates[0], 0.5

    bot.get_kament_candidates = get_candidates
    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)
    monkeypatch.setattr("kover_bot.bot2.random.shuffle", lambda _: None)

    assert asyncio.run(bot.select_automatic_kament(123)) is None


def test_kament_command_uses_replied_to_message_as_target():
    bot = KoverBot()
    bot.bot = SimpleNamespace(send_chat_action=AsyncMock())
    bot.send_message = AsyncMock()
    selected_with = None

    async def select_kament(chat_id, *, message_to_answer=None):
        nonlocal selected_with
        selected_with = (chat_id, message_to_answer)
        return "selected response"

    bot.select_kament = select_kament
    replied_to = SimpleNamespace(
        text="the specific message",
        caption=None,
        from_user=SimpleNamespace(full_name="Alice", username="alice"),
        sender_chat=None,
    )
    command = SimpleNamespace(chat_id=123, reply_to_message=replied_to)

    asyncio.run(bot.handle_kament(command))

    assert selected_with == (
        123,
        {"speaker": "Alice", "text": "the specific message"},
    )
    bot.send_message.assert_awaited_once_with(chat_id=123, text="selected response")


def test_kament_command_keeps_typing_while_searching(monkeypatch):
    monkeypatch.setattr("kover_bot.bot2.CHAT_ACTION_REFRESH_PERIOD", 0.01)
    bot = KoverBot()
    bot.bot = SimpleNamespace(send_chat_action=AsyncMock())
    bot.send_message = AsyncMock()

    async def select_kament(chat_id, *, message_to_answer=None):
        await asyncio.sleep(0.05)
        return "selected response"

    bot.select_kament = select_kament

    async def run():
        await bot.handle_kament(SimpleNamespace(chat_id=123, reply_to_message=None))
        calls = bot.bot.send_chat_action.await_count
        await asyncio.sleep(0.05)
        return calls

    calls_during_search = asyncio.run(run())

    assert calls_during_search >= 3
    # Typing stops once the reply has been sent.
    assert bot.bot.send_chat_action.await_count == calls_during_search
    bot.bot.send_chat_action.assert_awaited_with(chat_id=123, action="typing")
    bot.send_message.assert_awaited_once_with(chat_id=123, text="selected response")


def test_mention_reply_keeps_typing_while_searching(monkeypatch):
    monkeypatch.setattr("kover_bot.bot2.CHAT_ACTION_REFRESH_PERIOD", 0.01)
    bot = KoverBot()
    bot.bot = SimpleNamespace(send_chat_action=AsyncMock())
    bot.send_reply = AsyncMock()
    selected_with = None

    async def select_kament(chat_id, *, message_to_answer=None):
        nonlocal selected_with
        selected_with = (chat_id, message_to_answer)
        await asyncio.sleep(0.05)
        return "selected response"

    bot.select_kament = select_kament
    message = SimpleNamespace(
        chat_id=123,
        text="ковробот, привет",
        caption=None,
        from_user=SimpleNamespace(full_name="Alice", username="alice"),
        sender_chat=None,
    )

    asyncio.run(bot.handle_mention(message))

    assert bot.bot.send_chat_action.await_count >= 3
    bot.bot.send_chat_action.assert_awaited_with(chat_id=123, action="typing")
    assert selected_with == (123, {"speaker": "Alice", "text": "ковробот, привет"})
    bot.send_reply.assert_awaited_once_with(message, "selected response")
