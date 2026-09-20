"""Recovering from a wedged write without a person.

DuckDB takes one writer, so a write that stops making progress stops every later write
too. This has now happened twice on the live deployment, and both times it needed someone
to notice and a volume to be wiped - which is not a fix, it is a chore that loses data.

A thread blocked inside DuckDB's C code cannot be killed from Python. Its *query* can be
interrupted, though, which makes it raise, which runs the `finally` that releases the
lock. That is the recovery. Exiting so the platform restarts us is the fallback for when
even that does not land.
"""
import asyncio
import time

import pytest

from talaia.config import settings
from talaia.store import Store, StoreBusy


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "watchdog.duckdb")
    s.connect()
    yield s
    s.close()


async def test_interrupting_frees_the_single_writer(store, monkeypatch):
    """The whole recovery in one test: a write wedges, later writes are refused, the
    watchdog interrupts, and the queue drains."""
    monkeypatch.setattr(settings, "write_lock_timeout_s", 0.2)

    release = asyncio.Event()

    async def wedged():
        async with store._writing("upsert_assets"):
            await release.wait()

    task = asyncio.create_task(wedged())
    await asyncio.sleep(0.05)

    with pytest.raises(StoreBusy) as exc:
        await store.execute_write("SELECT 1")
    assert "upsert_assets" in str(exc.value)

    # What the watchdog does. (The real wedge is inside DuckDB; here the stand-in is an
    # event, because a genuinely blocked C call cannot be created on demand.)
    release.set()
    await task

    await store.execute_write("SELECT 1")
    assert store.write_health()["holder"] is None


async def test_the_watchdog_interrupts_before_it_gives_up(store, monkeypatch):
    """Order matters: interrupting costs one write, exiting costs the process."""
    from talaia import main

    monkeypatch.setattr(settings, "write_watchdog_interval_s", 0.01)
    monkeypatch.setattr(settings, "write_stuck_after_s", 0.0)
    monkeypatch.setattr(settings, "write_fatal_after_s", 10_000.0)

    calls = {"interrupt": 0, "exit": 0}
    monkeypatch.setattr(store, "interrupt_live_queries",
                        lambda: calls.__setitem__("interrupt", calls["interrupt"] + 1) or 1)
    monkeypatch.setattr(main.os, "_exit", lambda code: calls.__setitem__("exit", code))
    monkeypatch.setattr(store, "write_health",
                        lambda: {"holder": "upsert_assets", "held_for_s": 500.0})

    task = asyncio.create_task(main._write_watchdog(store))
    await asyncio.sleep(0.2)
    task.cancel()

    assert calls["interrupt"] >= 1, "it must try interrupting first"
    assert calls["exit"] == 0, "and must not exit while the deadline has not passed"


async def test_the_watchdog_exits_only_after_interrupting_failed(store, monkeypatch):
    from talaia import main

    monkeypatch.setattr(settings, "write_watchdog_interval_s", 0.01)
    monkeypatch.setattr(settings, "write_stuck_after_s", 0.0)
    monkeypatch.setattr(settings, "write_fatal_after_s", 1.0)
    monkeypatch.setattr(settings, "write_watchdog_may_exit", True)

    exited: list[int] = []
    monkeypatch.setattr(store, "interrupt_live_queries", lambda: 0)
    monkeypatch.setattr(main.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(store, "write_health",
                        lambda: {"holder": "upsert_assets", "held_for_s": 900.0})

    task = asyncio.create_task(main._write_watchdog(store))
    await asyncio.sleep(0.2)
    task.cancel()
    assert exited and exited[0] == 70


async def test_a_healthy_write_never_trips_the_watchdog(store, monkeypatch):
    """It must not interrupt a large ingest that is simply taking its time."""
    from talaia import main

    monkeypatch.setattr(settings, "write_watchdog_interval_s", 0.01)
    monkeypatch.setattr(settings, "write_stuck_after_s", 90.0)

    calls: list[int] = []
    monkeypatch.setattr(store, "interrupt_live_queries", lambda: calls.append(1) or 1)
    monkeypatch.setattr(store, "write_health",
                        lambda: {"holder": "upsert_assets", "held_for_s": 30.0})

    task = asyncio.create_task(main._write_watchdog(store))
    await asyncio.sleep(0.15)
    task.cancel()
    assert not calls, "30s of honest work is not a wedge"


async def test_exiting_can_be_turned_off(store, monkeypatch):
    """Somewhere without a supervisor, exiting is worse than staying up degraded."""
    from talaia import main

    monkeypatch.setattr(settings, "write_watchdog_interval_s", 0.01)
    monkeypatch.setattr(settings, "write_stuck_after_s", 0.0)
    monkeypatch.setattr(settings, "write_fatal_after_s", 1.0)
    monkeypatch.setattr(settings, "write_watchdog_may_exit", False)

    exited: list[int] = []
    monkeypatch.setattr(store, "interrupt_live_queries", lambda: 0)
    monkeypatch.setattr(main.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(store, "write_health",
                        lambda: {"holder": "x", "held_for_s": 900.0})

    task = asyncio.create_task(main._write_watchdog(store))
    await asyncio.sleep(0.15)
    task.cancel()
    assert not exited


def test_live_cursors_are_tracked_so_they_can_be_interrupted(store):
    """Interrupting needs a handle on what is running; an untracked cursor is
    unreachable from the watchdog's thread."""
    assert not store._cursors
    with store.cursor():
        assert len(store._cursors) == 1
    assert not store._cursors
    assert store.interrupt_live_queries() == 0
