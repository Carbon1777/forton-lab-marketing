"""Проверка старта publish.yml после dispatch (инцидент 2026-11-16: запуск завис в очереди)."""
import datetime as dt

from src import plan_writer

T0 = dt.datetime(2026, 11, 16, 17, 0, tzinfo=dt.timezone.utc)


def _run(status, minutes=1):
    return {"status": status,
            "created_at": (T0 + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")}


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_started_run_needs_no_redispatch():
    clk, dispatched, notified = Clock(), [], []
    ok = plan_writer.ensure_publish_started(
        "slug", T0, list_runs=lambda: [_run("in_progress")],
        dispatch=dispatched.append, notify=notified.append,
        sleep=clk.sleep, monotonic=clk.monotonic)
    assert ok and not dispatched and not notified


def test_queued_run_is_redispatched_then_started():
    clk, dispatched, notified = Clock(), [], []
    calls = {"n": 0}

    def list_runs():
        calls["n"] += 1
        # первые ~опросы: только queued; после повторного dispatch — in_progress
        return [_run("queued")] if not dispatched else [_run("in_progress", minutes=10)]

    ok = plan_writer.ensure_publish_started(
        "lucea-x", T0, start_timeout_s=60, poll_s=20, attempts=3,
        list_runs=list_runs, dispatch=dispatched.append, notify=notified.append,
        sleep=clk.sleep, monotonic=clk.monotonic)
    assert ok
    assert dispatched == ["lucea-x"]          # перезапуск был ровно один раз
    assert not notified


def test_never_started_notifies_owner_after_all_attempts():
    clk, dispatched, notified = Clock(), [], []
    ok = plan_writer.ensure_publish_started(
        "lucea-x", T0, start_timeout_s=60, poll_s=20, attempts=3,
        list_runs=lambda: [_run("queued")], dispatch=dispatched.append,
        notify=notified.append, sleep=clk.sleep, monotonic=clk.monotonic)
    assert not ok
    assert dispatched == ["lucea-x", "lucea-x"]   # 3 попытки = 2 перезапуска
    assert notified and "lucea-x" in notified[0]


def test_polling_errors_do_not_crash():
    clk, notified = Clock(), []

    def boom():
        raise RuntimeError("github down")

    ok = plan_writer.ensure_publish_started(
        "s", T0, start_timeout_s=40, poll_s=20, attempts=1,
        list_runs=boom, dispatch=lambda s: None, notify=notified.append,
        sleep=clk.sleep, monotonic=clk.monotonic)
    assert not ok and notified


def test_old_runs_before_dispatch_are_ignored():
    clk, notified = Clock(), []
    old = {"status": "completed", "created_at": "2026-11-16T10:00:00Z"}
    ok = plan_writer.ensure_publish_started(
        "s", T0, start_timeout_s=40, poll_s=20, attempts=1,
        list_runs=lambda: [old], dispatch=lambda s: None, notify=notified.append,
        sleep=clk.sleep, monotonic=clk.monotonic)
    assert not ok
