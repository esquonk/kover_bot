import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
from telegram import MessageEntity

from kover_bot.bot2 import (
    KAMENT_CANDIDATE_COUNT,
    KAMENT_ROUND_COUNT,
    KAMENT_ROUND_SIZE,
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


def test_select_kament_uses_the_highest_confidence_bracket_winner(monkeypatch):
    bot = KoverBot()
    bot.jev_api_key = "test-key"
    bot.session = object()
    candidates = [f"candidate {index}" for index in range(KAMENT_CANDIDATE_COUNT)]

    async def get_candidates():
        return candidates.copy()

    calls = []

    async def select_response(context, round_candidates, **kwargs):
        calls.append((context, round_candidates, kwargs))
        return round_candidates[0], len(calls) / 10

    bot.get_kament_candidates = get_candidates
    monkeypatch.setattr("kover_bot.bot2.select_svalko_response", select_response)
    monkeypatch.setattr("kover_bot.bot2.random.shuffle", lambda _: None)

    selected = asyncio.run(bot.select_kament(123))

    assert selected == f"candidate {KAMENT_ROUND_SIZE * (KAMENT_ROUND_COUNT - 1)}"
    assert [len(call[1]) for call in calls] == [KAMENT_ROUND_SIZE] * KAMENT_ROUND_COUNT
    assert all(call[2]["message_to_answer"] is None for call in calls)


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
