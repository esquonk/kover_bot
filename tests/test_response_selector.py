import asyncio
import json
import logging

import pytest

from kover_bot.response_selector import (
    JEV_DECISIONS_URL,
    MAX_REQUEST_BYTES,
    _build_payload,
    _fit_payload,
    select_svalko_response,
)


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def raise_for_status(self):
        return None

    async def text(self):
        return json.dumps(self.body)


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse(self.body)


def test_build_payload_limits_context_and_numbers_candidates():
    payload = _build_payload(
        [f"message {index}" for index in range(25)],
        ["first", "second"],
    )

    assert payload["state"]["chat_history"][0] == {
        "speaker": "Unknown",
        "text": "message 5",
    }
    assert payload["state"]["message_to_answer"] is None
    assert payload["questions"]["response"]["criteria"] == {
        "candidate_0": "first",
        "candidate_1": "second",
    }


def test_build_payload_includes_speakers_and_message_to_answer():
    payload = _build_payload(
        [{"speaker": "Alice", "text": "What happened?"}],
        ["first", "second"],
        message_to_answer={"speaker": "Bob", "text": "Nobody knows."},
    )

    assert payload["state"] == {
        "chat_history": [{"speaker": "Alice", "text": "What happened?"}],
        "message_to_answer": {"speaker": "Bob", "text": "Nobody knows."},
    }


def test_fit_payload_obeys_jev_request_limit():
    candidates, payload = _fit_payload(
        ["🚀" * 500 for _ in range(20)],
        [f"{index}:" + "🚀" * 500 for index in range(20)],
    )

    assert len(json.dumps(payload).encode()) <= MAX_REQUEST_BYTES
    assert len(candidates) >= 2


def test_fit_payload_drops_candidates_before_chat_history():
    history = ["🚀" * 500 for _ in range(10)]
    candidates, payload = _fit_payload(
        history,
        [f"{index}:" + "🚀" * 500 for index in range(50)],
    )

    assert len(json.dumps(payload).encode()) <= MAX_REQUEST_BYTES
    assert 2 < len(candidates) < 50
    assert len(payload["state"]["chat_history"]) == len(history)


def test_selects_using_jev_choice_and_confidence():
    session = FakeSession(
        {
            "model": "jev-1.13.0",
            "answers": {
                "response": {
                    "type": "choice",
                    "choice": "candidate_1",
                    "probabilities": {"candidate_0": 0, "candidate_1": 1},
                    "confidence": 1,
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
    )

    selected = asyncio.run(
        select_svalko_response(
            ["What a day"],
            ["first", "second"],
            api_key="test-key",
            session=session,
        )
    )

    assert selected == ("second", 1.0)
    assert session.request[0] == JEV_DECISIONS_URL
    assert session.request[1]["headers"]["Authorization"] == "Bearer test-key"


def test_logs_jev_exchange_without_api_key(caplog):
    session = FakeSession(
        {
            "model": "jev-1.13.0",
            "answers": {
                "response": {
                    "type": "choice",
                    "choice": "candidate_0",
                    "probabilities": {"candidate_0": 1, "candidate_1": 0},
                    "confidence": 1,
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
    )

    with caplog.at_level(logging.DEBUG, logger="kover_bot.response_selector"):
        asyncio.run(
            select_svalko_response(
                ["context"],
                ["first", "second"],
                api_key="secret-test-key",
                session=session,
            )
        )

    assert "Jev request:" in caplog.text
    assert "Jev response: status=200" in caplog.text
    assert "secret-test-key" not in caplog.text


def test_returns_no_confident_candidate_for_an_invalid_response():
    selected = asyncio.run(
        select_svalko_response(
            [],
            ["first", "second"],
            api_key="test-key",
            session=FakeSession({"model": "jev-1.13.0", "answers": {}}),
        )
    )

    assert selected == ("", 0.0)


def test_rejects_an_empty_candidate_list():
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(select_svalko_response([], ["", "  "], api_key="test-key"))


def test_returns_the_model_confidence():
    selected, confidence = asyncio.run(
        select_svalko_response(
            ["context"],
            ["first", "second"],
            api_key="test-key",
            session=FakeSession(
                {
                    "answers": {
                        "response": {
                            "choice": "candidate_1",
                            "confidence": 0.7,
                        }
                    }
                }
            ),
        )
    )

    assert (selected, confidence) == ("second", 0.7)
