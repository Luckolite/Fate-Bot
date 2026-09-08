"""Owner-only runtime memory telemetry for the web dashboard."""

from __future__ import annotations

import gc
import os
import sys
from datetime import datetime, timezone
from time import time
from typing import Any

import psutil


def _optional_nonnegative(value: Any) -> int | None:
    if type(value) not in {int, float}:
        return None
    return max(0, int(value))


def _process_memory(process: psutil.Process) -> dict[str, Any] | None:
    try:
        with process.oneshot():
            info = process.memory_info()
            try:
                full_info = process.memory_full_info()
            except (psutil.AccessDenied, NotImplementedError, OSError):
                full_info = info
            return {
                "pid": process.pid,
                "name": process.name(),
                "rss_bytes": _optional_nonnegative(getattr(info, "rss", None)),
                "uss_bytes": _optional_nonnegative(getattr(full_info, "uss", None)),
                "vms_bytes": _optional_nonnegative(getattr(info, "vms", None)),
                "shared_bytes": _optional_nonnegative(getattr(info, "shared", None)),
                "private_bytes": _optional_nonnegative(getattr(info, "private", None)),
                "peak_rss_bytes": _optional_nonnegative(
                    getattr(info, "peak_wset", None)
                ),
                "percent": round(max(0.0, process.memory_percent()), 2),
                "started_at": datetime.fromtimestamp(
                    process.create_time(), timezone.utc
                ).isoformat(),
            }
    except (psutil.Error, OSError, ValueError):
        return None


def _optional_memory_fields(memory: Any, names: tuple[str, ...]) -> dict[str, int | None]:
    return {
        f"{name}_bytes": _optional_nonnegative(getattr(memory, name, None))
        for name in names
    }


def _normalise_message_cache(message_cache: dict[str, Any] | None) -> dict[str, Any]:
    if not message_cache or not message_cache.get("available"):
        return {
            "available": False,
            "current": None,
            "capacity": None,
            "fill_percent": None,
        }
    current = _optional_nonnegative(message_cache.get("current")) or 0
    capacity = _optional_nonnegative(message_cache.get("capacity"))
    fill_percent = None
    if capacity:
        fill_percent = round(min(100.0, current / capacity * 100), 1)
    return {
        "available": True,
        "current": current,
        "capacity": capacity,
        "fill_percent": fill_percent,
    }


def collect_memory_snapshot(
    message_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect a JSON-safe snapshot without blocking the dashboard event loop."""
    observed_at = datetime.now(timezone.utc).isoformat()
    process = psutil.Process(os.getpid())
    process_row = _process_memory(process)

    try:
        children = process.children(recursive=True)
    except (psutil.Error, OSError):
        children = []
    process_rows = [row for child in children if (row := _process_memory(child))]
    process_rows.sort(key=lambda row: row.get("rss_bytes") or 0, reverse=True)

    main_rss = (process_row or {}).get("rss_bytes") or 0
    children_rss = sum(row.get("rss_bytes") or 0 for row in process_rows)
    try:
        uptime_seconds = max(0, round(time() - process.create_time()))
    except (psutil.Error, OSError, ValueError):
        uptime_seconds = None

    virtual_memory = psutil.virtual_memory()
    total_bytes = max(0, int(virtual_memory.total))
    available_bytes = max(0, int(virtual_memory.available))
    used_bytes = max(0, total_bytes - available_bytes)
    used_percent = round(max(0.0, min(100.0, float(virtual_memory.percent))), 1)
    pressure = "high" if used_percent >= 90 else "elevated" if used_percent >= 75 else "normal"

    swap = psutil.swap_memory()
    gc_stats = gc.get_stats()
    generation_counts = gc.get_count()
    allocator_blocks = getattr(sys, "getallocatedblocks", None)

    return {
        "observed_at": observed_at,
        "process": {
            **(process_row or {}),
            "uptime_seconds": uptime_seconds,
            "children_rss_bytes": children_rss,
            "tree_rss_bytes": main_rss + children_rss,
            "child_count": len(process_rows),
            "children": process_rows[:12],
        },
        "system": {
            "total_bytes": total_bytes,
            "available_bytes": available_bytes,
            "used_bytes": used_bytes,
            "used_percent": used_percent,
            "pressure": pressure,
            **_optional_memory_fields(
                virtual_memory,
                ("free", "cached", "buffers", "active", "inactive", "shared", "slab"),
            ),
        },
        "swap": {
            "total_bytes": max(0, int(swap.total)),
            "used_bytes": max(0, int(swap.used)),
            "free_bytes": max(0, int(swap.free)),
            "used_percent": round(max(0.0, min(100.0, float(swap.percent))), 1),
            "paged_in_bytes": _optional_nonnegative(getattr(swap, "sin", None)),
            "paged_out_bytes": _optional_nonnegative(getattr(swap, "sout", None)),
        },
        "python": {
            "version": (
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
            "gc_enabled": gc.isenabled(),
            "tracked_objects": len(gc.get_objects()),
            "allocated_blocks": allocator_blocks() if allocator_blocks else None,
            "generation_counts": list(generation_counts),
            "collections": [stat.get("collections", 0) for stat in gc_stats],
            "collected_objects": sum(stat.get("collected", 0) for stat in gc_stats),
            "uncollectable_objects": sum(
                stat.get("uncollectable", 0) for stat in gc_stats
            ),
        },
        "message_cache": _normalise_message_cache(message_cache),
    }
