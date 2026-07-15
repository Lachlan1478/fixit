"""
Tests for pipeline.py cancellation — POST /pipeline/{id}/cancel must unblock
the pipeline while it waits in AWAITING_HUMAN and drive it to CANCELLED.
"""

import asyncio

import pytest

import pipeline as pl


def _make_pipeline() -> tuple[pl.PipelineSession, pl.Pipeline]:
    return pl.create_session(problem="test problem", mode="medium")


async def test_cancel_unblocks_await_helper():
    session, pipe = _make_pipeline()

    wait_task = asyncio.create_task(pipe._await_human_or_cancel())
    await asyncio.sleep(0.05)
    assert not wait_task.done()

    pipe.cancel()
    result = await asyncio.wait_for(wait_task, timeout=2)
    assert result == "cancel"


async def test_human_input_unblocks_await_helper():
    session, pipe = _make_pipeline()

    wait_task = asyncio.create_task(pipe._await_human_or_cancel())
    await asyncio.sleep(0.05)

    pipe.provide_human_input("keep going")
    result = await asyncio.wait_for(wait_task, timeout=2)
    assert result == "human"


async def test_cancel_during_awaiting_human_sets_cancelled():
    session, pipe = _make_pipeline()
    session.spec = {"product_name": "Widget"}

    review = {
        "verdict": "human_needed",
        "verdicts": [],
        "follow_up_notes": ["need a decision"],
    }
    verdict_task = asyncio.create_task(pipe._handle_verdict(review))
    await asyncio.sleep(0.05)
    assert session.state == pl.PipelineState.AWAITING_HUMAN

    pipe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(verdict_task, timeout=2)
    assert session.state == pl.PipelineState.CANCELLED


async def test_human_input_during_awaiting_human_resumes_iterating():
    session, pipe = _make_pipeline()
    session.spec = {"product_name": "Widget"}

    review = {
        "verdict": "human_needed",
        "verdicts": [],
        "follow_up_notes": ["need a decision"],
    }
    verdict_task = asyncio.create_task(pipe._handle_verdict(review))
    await asyncio.sleep(0.05)
    assert session.state == pl.PipelineState.AWAITING_HUMAN

    pipe.provide_human_input("use option B")
    await asyncio.wait_for(verdict_task, timeout=2)
    assert session.state == pl.PipelineState.ITERATING
