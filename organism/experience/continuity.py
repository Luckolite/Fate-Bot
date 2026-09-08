"""Step-local temporal continuity for recent workspace focus.

Continuity has no process-global storage and no identity key.  A caller may pass
the immediately preceding moment from its own isolation scope; after a gap or
scope change it must pass ``None``.  Each advance decays old traces, and the
same retention factor limits how much rapid successive calls can incorporate.
"""

from __future__ import annotations

from .models import AttendedCandidate, FocusTrace, TemporalContinuity
from ..models import clamp, unit


def advance_continuity(
    previous: TemporalContinuity | None,
    attended: tuple[AttendedCandidate, ...],
    *,
    retention: float = 0.60,
    incorporation: float = 0.70,
    limit: int = 4,
) -> TemporalContinuity:
    if previous is not None and not isinstance(previous, TemporalContinuity):
        raise ValueError("previous must be TemporalContinuity or None")
    if not isinstance(attended, tuple) or any(
        not isinstance(item, AttendedCandidate) for item in attended
    ):
        raise ValueError("attended must be a tuple of AttendedCandidate instances")
    if len(attended) > 4:
        raise ValueError("continuity accepts at most four attended candidates")
    domains = [item.domain for item in attended]
    if len(domains) != len(set(domains)):
        raise ValueError("attended focus domains must be unique")
    retained = unit(retention, name="continuity.retention")
    incorporated = unit(incorporation, name="continuity.incorporation")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("continuity limit must be between one and four")

    prior = previous or TemporalContinuity()
    strengths = {trace.domain: trace.strength * retained for trace in prior.traces}
    effective_incorporation = (
        incorporated if previous is None else incorporated * (1.0 - retained)
    )
    for item in attended:
        strengths[item.domain] = clamp(
            strengths.get(item.domain, 0.0) + effective_incorporation * item.share
        )
    traces = tuple(
        FocusTrace(domain=domain, strength=strength)
        for domain, strength in sorted(
            strengths.items(), key=lambda item: (-item[1], item[0].value)
        )[:limit]
        if strength >= 0.01
    )
    return TemporalContinuity(traces=traces, revision=prior.revision + 1)


__all__ = ["advance_continuity"]
