"""What the process can see about the box it is running on.

Written because a deployment wedged in a way nothing in the application could explain:
reads answered in 0.3s, every write hung forever, and the only way to tell why was to be
inside the container. None of this is secret - limits, counters and free space - so it is
served without a credential, which is the point: diagnosing a stuck deployment should not
require handing anyone the keys to it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import settings


def _read_int(path: str) -> int | None:
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    if text in ("max", ""):
        return None
    try:
        return int(text.split()[0])
    except ValueError:
        return None


def container_limits() -> dict[str, Any]:
    """cgroup v2 first, then v1. Absent on a normal machine, which is fine."""
    mem_max = _read_int("/sys/fs/cgroup/memory.max") or _read_int(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    mem_used = _read_int("/sys/fs/cgroup/memory.current") or _read_int(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes")
    # cpu.max is "<quota> <period>"; quota/period is the fraction of a core allowed.
    cpu_quota = None
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if len(raw) == 2 and raw[0] != "max":
            cpu_quota = round(int(raw[0]) / int(raw[1]), 2)
    except (OSError, ValueError, ZeroDivisionError):
        pass

    mb = lambda v: round(v / 1_048_576) if v else None   # noqa: E731
    return {
        "memory_limit_mb": mb(mem_max),
        "memory_used_mb": mb(mem_used),
        "memory_headroom_mb": mb(mem_max - mem_used) if mem_max and mem_used else None,
        "cpu_count": os.cpu_count(),
        "cpu_quota_cores": cpu_quota,
    }


def disk() -> dict[str, Any]:
    """Free space where the database lives. A full volume stops writes dead."""
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError:
        return {}
    mb = lambda v: round(v / 1_048_576)    # noqa: E731
    db = settings.db_path
    return {
        "path": str(settings.data_dir),
        "total_mb": mb(usage.total),
        "free_mb": mb(usage.free),
        "used_pct": round(100 * usage.used / usage.total, 1) if usage.total else None,
        "db_file_mb": round(db.stat().st_size / 1_048_576, 1) if db.exists() else None,
    }


def versions(store) -> dict[str, Any]:
    """What is actually installed here.

    ``duckdb>=1.1`` is not a version, it is a range, so the image can carry a different
    engine from the one anything was tested against - which is exactly the shape of
    "works on my machine, hangs in production".
    """
    import duckdb

    out: dict[str, Any] = {"duckdb": getattr(duckdb, "__version__", "?")}
    try:
        rows = store.fetch_sync(
            "SELECT extension_name, extension_version, installed, loaded "
            "FROM duckdb_extensions() WHERE extension_name IN ('spatial', 'json')")
        out["extensions"] = {r[0]: {"version": r[1], "installed": r[2], "loaded": r[3]}
                             for r in rows}
    except Exception as exc:  # pragma: no cover - defensive
        out["extensions"] = {"error": str(exc)[:200]}
    return out


def duckdb_settings(store) -> dict[str, Any]:
    """What DuckDB thinks it may use - which is the thing that has to match the box."""
    wanted = ("memory_limit", "threads", "temp_directory", "max_temp_directory_size")
    out: dict[str, Any] = {}
    try:
        rows = store.fetch_sync(
            "SELECT name, value FROM duckdb_settings() WHERE name IN "
            f"({', '.join(repr(w) for w in wanted)})")
        out = {name: value for name, value in rows}
    except Exception as exc:  # pragma: no cover - defensive
        out = {"error": str(exc)[:200]}
    return out


def thread_stacks(limit: int = 12) -> list[dict[str, Any]]:
    """Where every thread currently is.

    A write that holds the single writer for minutes while using no memory and writing
    no bytes is not working slowly, it is blocked, and nothing short of the stack says
    on what. This reports file, line and function - the same information a traceback in
    the log would carry, and no more.
    """
    import sys
    import threading
    import traceback

    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        stack = traceback.extract_stack(frame)[-limit:]
        out.append({
            "thread": names.get(ident, str(ident)),
            "frames": [f"{f.filename.split('/')[-1]}:{f.lineno} {f.name}" for f in stack],
        })
    return out


def report(store) -> dict[str, Any]:
    limits = container_limits()
    d = duckdb_settings(store)
    notes: list[str] = []
    # The comparison that matters: DuckDB will happily use what it was told it may use.
    if limits.get("memory_limit_mb") and isinstance(d.get("memory_limit"), str):
        notes.append(
            f"DuckDB is allowed {d['memory_limit']} on a container limited to "
            f"{limits['memory_limit_mb']}MB.")
    if limits.get("cpu_quota_cores") and d.get("threads"):
        notes.append(
            f"DuckDB is using {d['threads']} threads on "
            f"{limits['cpu_quota_cores']} of a core.")
    health = store.write_health()
    out = {"container": limits, "disk": disk(), "duckdb": d,
           "versions": versions(store),
           "write_health": health, "notes": notes}
    # Only when something is actually stuck: it is cheap, but it is noise otherwise.
    if health.get("held_for_s", 0) > 10:
        out["threads"] = thread_stacks()
    return out
