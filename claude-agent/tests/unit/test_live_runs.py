"""Re-attaching to an in-flight run."""

import asyncio

import claude_session as cs


def test_live_run_records_events_and_expires(monkeypatch):
    async def fake_impl(prompt, agent_id, model, mode, cwd, source="phone"):
        yield {"type": "status", "message": "Ready"}
        await asyncio.sleep(0.05)
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "_stream_task_impl", fake_impl)

    async def run():
        gen = cs.stream_task("hi", "live-test", "haiku", "auto")
        first = await gen.__anext__()
        live = cs.get_live_run("live-test")
        assert live is not None and live.events[0] == first and not live.done
        await gen.aclose()  # consumer gone; the run carries on
        await asyncio.sleep(0.2)
        events, done = cs.get_live_run("live-test").snapshot()
        return events, done

    events, done = asyncio.run(run())
    assert done and [e["type"] for e in events] == ["status", "done"]
    cs._live_runs["live-test"].finished_at -= cs.LIVE_RUN_TTL_S + 1
    assert cs.get_live_run("live-test") is None


def test_second_prompt_announces_it_is_waiting_for_the_lock(monkeypatch):
    async def slow_impl(prompt, agent_id, model, mode, cwd, source="phone"):
        await asyncio.sleep(0.2)
        yield {"type": "done", "result": prompt}

    monkeypatch.setattr(cs, "_stream_task_impl", slow_impl)

    async def run():
        first = cs.stream_task("one", "lock-test", "haiku", "auto")
        task = asyncio.ensure_future(first.__anext__())
        await asyncio.sleep(0.05)
        second = cs.stream_task("two", "lock-test", "haiku", "auto")
        head = await second.__anext__()
        await task
        rest = [e async for e in second]
        return head, rest

    head, rest = asyncio.run(run())
    assert head["type"] == "status" and "Waiting for the previous run" in head["message"]
    assert rest[-1] == {"type": "done", "result": "two"}


class _BlockingProc:
    """A claude process that stays silent until killed."""

    pid = 4242

    def __init__(self):
        self.returncode = None
        self._killed = asyncio.Event()
        outer = self

        class Stdin:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        class Stdout:
            async def readline(self):
                await outer._killed.wait()
                return b""

            async def read(self, n=-1):
                await outer._killed.wait()
                return b""

        self.stdin, self.stdout, self.stderr = Stdin(), Stdout(), Stdout()

    def kill(self):
        self.returncode = -9
        self._killed.set()

    async def wait(self):
        await self._killed.wait()
        return self.returncode


def test_stop_kills_the_process_and_records_an_interrupted_run(monkeypatch):
    import json
    import os

    async def fake_exec(*argv, **kwargs):
        return _BlockingProc()

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cs.analytics, "record_session", lambda *a, **k: None)

    async def run():
        assert cs.stop_run("stop-test") is False
        gen = cs.stream_task("long job", "stop-test", "haiku", "auto", "/tmp")
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)
        assert cs.is_busy("stop-test")
        assert cs.stop_run("stop-test") is True
        events = [await task] + [e async for e in gen]
        return events

    events = asyncio.run(run())
    assert events[-1] == {"type": "error", "message": "Stopped by user"}
    assert not cs.is_busy("stop-test") and cs.stop_run("stop-test") is False
    assert cs.get_live_run("stop-test").done
    with open(os.path.join(cs.LOGS_DIR, "sessions.jsonl"), encoding="utf-8") as fh:
        row = [json.loads(l) for l in fh][-1]
    assert row["prompt"] == "long job" and row["interrupted"] is True and row["exit_code"] == -9
    assert cs.get_history("stop-test")[-1]["content"] == "long job"
