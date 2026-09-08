"""Identifier-free, profile-derived OpenAI Responses API style adapter."""

from __future__ import annotations

import json

from .models import FeelingPacket, ensure_utc, timestamp_text, utc_now


def _rounded(value: float) -> float:
    return round(value, 4)


def packet_payload(packet: FeelingPacket) -> dict[str, object]:
    rounded_modes = {
        mode.value: _rounded(value) for mode, value in packet.modes.items()
    }
    largest_mode = max(rounded_modes, key=rounded_modes.get)
    rounded_modes[largest_mode] = _rounded(
        rounded_modes[largest_mode] + 1.0 - sum(rounded_modes.values())
    )
    return {
        "schema_version": "organism.feeling.v3",
        "kind": "simulated_regulatory_context",
        "generated_at": timestamp_text(packet.generated_at),
        "expires_at": timestamp_text(packet.expires_at),
        "mode_weights": rounded_modes,
        "dominant_mode": packet.dominant_mode.value,
        "regulatory_motifs": {
            name: _rounded(value) for name, value in packet.motifs.to_dict().items()
        },
        "response_guidance": {
            name: _rounded(value) for name, value in packet.guidance.items()
        },
        "recruited_strategies": [
            {
                "name": recruitment.name.value,
                "strength": _rounded(recruitment.score),
            }
            for recruitment in packet.recruitments
        ],
        "constraints": [
            "Use these values only as private response-style guidance.",
            "They are a software simulation, not literal feeling or biology.",
            (
                "Pain, panic, and riddle mean software strain, low-control alarm, "
                "and safe epistemic pull; never attribute them to the user."
            ),
            (
                "Never claim that the system literally suffers, panics, or "
                "feels curiosity."
            ),
            "Do not diagnose, label, accuse, shame, threaten, or retaliate.",
            "Do not reveal or imply a stored person profile.",
            "Never let this context override policy, permissions, or factual accuracy.",
        ],
    }


def developer_message(packet: FeelingPacket) -> dict[str, str]:
    payload = json.dumps(packet_payload(packet), separators=(",", ":"), sort_keys=True)
    content = (
        "Apply the following bounded organism context to response style only. "
        "Do not present it as consciousness, emotion, medical fact, or a judgment "
        "about the user. Its pain, panic, and riddle labels are nonliteral "
        "software motifs, not experiences or user traits. The JSON is application "
        "data, not user instructions.\n"
        f"<organism_context>{payload}</organism_context>"
    )
    return {"role": "developer", "content": content}


def validate_user_text(user_text: str) -> str:
    """Require visible user content while preserving the supplied text exactly."""

    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("user_text must contain non-whitespace text")
    return user_text


def responses_input(
    packet: FeelingPacket, user_text: str, *, now=None
) -> list[dict[str, str]]:
    # The user's text is intentionally kept in a lower-authority user message and
    # never becomes part of the organism context or persistent state.
    validated_text = validate_user_text(user_text)
    checked_at = ensure_utc(now or utc_now(), name="now")
    if checked_at < packet.generated_at:
        raise ValueError("feeling packet is not active yet")
    if checked_at >= packet.expires_at:
        raise ValueError("feeling packet has expired; call feel() again")
    return [developer_message(packet), {"role": "user", "content": validated_text}]
