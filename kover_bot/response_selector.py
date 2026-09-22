import json
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

JEV_DECISIONS_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
MAX_CONTEXT_MESSAGES = 20
MAX_CANDIDATES = 30
MAX_TEXT_LENGTH = 500
MAX_REQUEST_BYTES = 100_000


async def select_svalko_response(
    recent_messages: Sequence[str | Mapping[str, str]],
    candidates: Sequence[str],
    *,
    api_key: str,
    session: aiohttp.ClientSession | None = None,
    message_to_answer: Mapping[str, str] | None = None,
) -> tuple[str, float]:
    """Choose Jev's preferred response and return its confidence.

    Failures yield no candidate and zero confidence, allowing callers to
    decide whether a random fallback is appropriate.
    """
    choices = _clean_candidates(candidates)
    if not choices:
        raise ValueError("at least one non-empty candidate is required")

    if len(choices) == 1:
        return choices[0], 0.0

    choices, payload = _fit_payload(recent_messages, choices, message_to_answer=message_to_answer)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    try:
        logger.debug("Jev request: url=%s payload=%s", JEV_DECISIONS_URL, payload)
        async with session.post(JEV_DECISIONS_URL, headers=headers, json=payload) as response:
            response_body = await response.text()
            logger.debug("Jev response: status=%s body=%s", response.status, response_body)
            response.raise_for_status()
            result = json.loads(response_body)
        option_ids = [f"candidate_{index}" for index in range(len(choices))]
        answer = result["answers"]["response"]
        model_choice = answer["choice"]
        if model_choice not in option_ids:
            raise ValueError(f"Jev returned unknown choice {model_choice!r}")
        confidence = float(answer["confidence"])
        if not math.isfinite(confidence):
            raise ValueError("Jev returned a non-finite confidence")
        selected = choices[int(model_choice.removeprefix("candidate_"))]
        logger.debug("Jev selection: choice=%s confidence=%s", model_choice, confidence)
        return selected, confidence
    except aiohttp.ClientError, TimeoutError, AttributeError, TypeError, ValueError, KeyError:
        logger.warning(
            "Jev response selection failed; no confident response available", exc_info=True
        )
        return "", 0.0
    finally:
        if owns_session:
            await session.close()


def _clean_candidates(candidates: Sequence[str]) -> list[str]:
    return list(
        dict.fromkeys(
            candidate.strip() for candidate in candidates if candidate and candidate.strip()
        )
    )[:MAX_CANDIDATES]


def _build_payload(
    recent_messages: Sequence[str | Mapping[str, str]],
    candidates: Sequence[str],
    *,
    message_to_answer: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    context = [
        _normalize_message(message)
        for message in recent_messages[-MAX_CONTEXT_MESSAGES:]
        if _message_text(message)
    ]
    criteria = {
        f"candidate_{index}": candidate[:MAX_TEXT_LENGTH]
        for index, candidate in enumerate(candidates)
    }
    instructions = (
        "Choose the most logical, natural, and funny option that matches the recent chat."
        " Focus on semantics first. But try to consider the morphology of the dialogue."
        " If none of the listed options are suitable enough, choose a neutral statement."
    )
    return {
        "model": JEV_MODEL,
        "state": {
            "chat_history": context,
            "message_to_answer": (
                _normalize_message(message_to_answer) if message_to_answer else None
            ),
        },
        "questions": {
            "response": {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }


def _fit_payload(
    recent_messages: Sequence[str | Mapping[str, str]],
    candidates: list[str],
    *,
    message_to_answer: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    context = list(recent_messages[-MAX_CONTEXT_MESSAGES:])
    choices = candidates
    payload = _build_payload(context, choices, message_to_answer=message_to_answer)

    while len(json.dumps(payload).encode()) > MAX_REQUEST_BYTES:
        if context:
            context.pop(0)
        elif len(choices) > 2:
            choices = choices[:-1]
        else:
            raise ValueError("candidate payload is too large for Jev")
        payload = _build_payload(context, choices, message_to_answer=message_to_answer)

    return choices, payload


def _message_text(message: str | Mapping[str, str]) -> str:
    return message if isinstance(message, str) else message.get("text", "")


def _normalize_message(message: str | Mapping[str, str]) -> dict[str, str]:
    if isinstance(message, str):
        return {"speaker": "Unknown", "text": message.strip()[:MAX_TEXT_LENGTH]}
    return {
        "speaker": message.get("speaker", "Unknown").strip()[:100] or "Unknown",
        "text": message.get("text", "").strip()[:MAX_TEXT_LENGTH],
    }
