from kover_bot.bot2 import KoverBot


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
