"""Authenticate durable feedback before it can affect learned state."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Mapping

from .models import Feedback, bounded_text, parse_timestamp, timestamp_text, utc_now


class LearningKeyError(ValueError):
    pass


def _key_bytes(key: bytes | str | None) -> bytes | None:
    if key is None:
        return None
    encoded = key.encode("utf-8") if isinstance(key, str) else key
    if not isinstance(encoded, bytes) or len(encoded) < 32:
        raise LearningKeyError("learning_key must contain at least 32 bytes")
    return encoded


def feedback_payload(feedback: Feedback) -> bytes:
    if not isinstance(feedback, Feedback):
        raise ValueError("feedback must be a Feedback instance")
    payload = {
        "schema_version": "organism.feedback.authorization.v2",
        "event_id": feedback.event_id,
        "kind": feedback.kind.value,
        "reason": feedback.reason.value,
        "cues": feedback.cues.to_dict(),
        "scope": feedback.scope,
        "capability": feedback.capability.value,
        "actor_ref": feedback.actor_ref,
        "episode_id": feedback.episode_id,
        "severity": feedback.severity,
        "confidence": feedback.confidence,
        "source": feedback.source.value,
        "verified": feedback.verified,
        "reward": feedback.reward,
        "strategies": [strategy.value for strategy in feedback.strategies],
        "occurred_at": (
            timestamp_text(feedback.occurred_at) if feedback.occurred_at else None
        ),
        "retracts_event_id": feedback.retracts_event_id,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


AUTHORIZATION_VERSION = "organism.feedback.authorization.v3"


def _authorization_payload(
    feedback: Feedback, issued_at: str, nonce: str, audience: str
) -> bytes:
    return (
        AUTHORIZATION_VERSION.encode("ascii")
        + b"\0"
        + issued_at.encode("ascii")
        + b"\0"
        + nonce.encode("utf-8")
        + b"\0"
        + audience.encode("utf-8")
        + b"\0"
        + feedback_payload(feedback)
    )


def sign_feedback(
    feedback: Feedback,
    key: bytes | str,
    *,
    audience: str,
    issued_at: datetime | None = None,
    nonce: str | None = None,
) -> str:
    encoded = _key_bytes(key)
    if encoded is None:  # pragma: no cover - the type contract excludes None
        raise LearningKeyError("learning_key is required")
    issued_text = timestamp_text(issued_at or utc_now())
    normalized_audience = bounded_text(audience, name="audience", maximum=128)
    normalized_nonce = bounded_text(
        nonce or secrets.token_urlsafe(18), name="nonce", maximum=128
    )
    if "|" in normalized_nonce:
        raise ValueError("nonce cannot contain '|'")
    signature = hmac.new(
        encoded,
        _authorization_payload(
            feedback, issued_text, normalized_nonce, normalized_audience
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"{AUTHORIZATION_VERSION}|{issued_text}|{normalized_nonce}|{signature}"


class LearningAuthority:
    def __init__(self, key: bytes | str | None) -> None:
        self._key = _key_bytes(key)

    @property
    def enabled(self) -> bool:
        return self._key is not None

    @property
    def key_id(self) -> str | None:
        if self._key is None:
            return None
        return hmac.new(
            self._key,
            b"organism.learning-key-id.v1",
            hashlib.sha256,
        ).hexdigest()[:32]

    def authorization_time(
        self, feedback: Feedback, authorization: str | None, *, audience: str
    ) -> datetime | None:
        if (
            self._key is None
            or not isinstance(authorization, str)
            or len(authorization) > 512
        ):
            return None
        try:
            version, issued_text, nonce, supplied = authorization.split("|", 3)
            issued_at = parse_timestamp(issued_text)
        except (ValueError, TypeError):
            return None
        if (
            version != AUTHORIZATION_VERSION
            or not nonce
            or len(nonce) > 128
            or len(supplied) != 64
        ):
            return None
        expected = hmac.new(
            self._key,
            _authorization_payload(feedback, issued_text, nonce, audience),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return None
        return issued_at

    def authorization_token(self, authorization: str) -> str:
        if self._key is None:
            raise LearningKeyError("learning_key is required")
        return hmac.new(
            self._key,
            b"organism.authorization-token.v1\0" + authorization.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _record_payload(record: Mapping[str, object]) -> bytes:
        payload = {
            key: value
            for key, value in record.items()
            if key != "record_authentication"
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def authenticate_record(self, record: Mapping[str, object]) -> str:
        if self._key is None:
            raise LearningKeyError("learning_key is required")
        return hmac.new(
            self._key,
            self._record_payload(record),
            hashlib.sha256,
        ).hexdigest()

    def verifies_record(self, record: Mapping[str, object]) -> bool:
        authentication = record.get("record_authentication")
        if self._key is None or not isinstance(authentication, str):
            return False
        expected = hmac.new(
            self._key,
            self._record_payload(record),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, authentication)
