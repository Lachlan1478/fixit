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
