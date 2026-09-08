"""Pseudonymous, scope-bound identity helpers.

The HMAC key is deliberately external to persisted organism state.  Hashing is
pseudonymization, not anonymization, and the resulting records remain personal
data that callers must protect and delete on request.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from .models import bounded_text


class IdentityKeyError(ValueError):
    pass


class IdentityPseudonymizer:
    def __init__(self, key: bytes | str | None) -> None:
        if key is None:
            self._key = None
            return
        encoded = key.encode("utf-8") if isinstance(key, str) else key
        if not isinstance(encoded, bytes) or len(encoded) < 32:
            raise IdentityKeyError("identity_key must contain at least 32 bytes")
        self._key = encoded

    @property
    def enabled(self) -> bool:
        return self._key is not None

    @property
    def key_id(self) -> str | None:
        """Return a non-secret identifier used to detect accidental key changes."""
        if self._key is None:
            return None
        return hmac.new(
            self._key,
            b"organism.identity-key-id.v1",
            hashlib.sha256,
        ).hexdigest()[:32]

    def _digest(self, domain: str, *parts: str) -> str | None:
        if self._key is None:
            return None
        message = json.dumps(
            [domain, *parts], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def subject_key(self, scope: str, actor_ref: str) -> str | None:
        return self._digest(
            "organism.subject.v1",
            bounded_text(scope, name="scope"),
            bounded_text(actor_ref, name="actor_ref", maximum=512),
        )

    def scope_key(self, scope: str) -> str:
        normalized = bounded_text(scope, name="scope")
        return (
            self._digest("organism.scope.v1", normalized)
            or hashlib.sha256(
                json.dumps(
                    ["organism.scope.v1", normalized],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )

    def episode_key(
        self,
        scope: str,
        actor_ref: str,
        capability: str,
        episode_id: str,
    ) -> str | None:
        return self._digest(
            "organism.episode.v1",
            bounded_text(scope, name="scope"),
            bounded_text(actor_ref, name="actor_ref", maximum=512),
            bounded_text(capability, name="capability"),
            bounded_text(episode_id, name="episode_id"),
        )

    def event_key(self, scope: str, event_id: str) -> str:
        normalized_scope = bounded_text(scope, name="scope")
        normalized_event = bounded_text(event_id, name="event_id")
        return self._digest(
            "organism.event.v1", normalized_scope, normalized_event
        ) or opaque_event_key(normalized_event, normalized_scope)

    def content_key(self, domain: str, *parts: str) -> str:
        """Key arbitrary transient content without persisting the content itself."""
        normalized_domain = bounded_text(domain, name="domain")
        if not all(isinstance(part, str) for part in parts):
            raise ValueError("content key parts must be strings")
        digest = self._digest(normalized_domain, *parts)
        if digest is not None:
            return digest
        message = json.dumps(
            [normalized_domain, *parts],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(message.encode("utf-8")).hexdigest()


def opaque_event_key(event_id: str, scope: str = "local") -> str:
    normalized = bounded_text(event_id, name="event_id")
    normalized_scope = bounded_text(scope, name="scope")
    message = json.dumps(
        ["organism.event.v1", normalized_scope, normalized],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(message.encode("utf-8")).hexdigest()
