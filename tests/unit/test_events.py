"""EventBus unit tests."""

from __future__ import annotations

import anyio
import pytest
from evalforge.errors import FatalError
from evalforge.events import EventBus, make_event


async def test_publish_assigns_monotonic_seq() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    async with anyio.create_task_group() as tg:

        async def drain() -> list[int]:
            out: list[int] = []
            async with sub:
                async for e in sub:
                    out.append(e.seq)
                    if len(out) == 3:
                        return out
            return out

        result: list[list[int]] = []

        async def drain_capture() -> None:
            result.append(await drain())

        tg.start_soon(drain_capture)
        for i in range(3):
            await bus.publish(make_event("task_started", run_id=f"r{i}"))
        await bus.close()

    assert result[0] == [1, 2, 3]


async def test_backpressure_blocks_slow_subscriber() -> None:
    bus = EventBus(buffer_size=1)
    sub = bus.subscribe()
    try:
        await bus.publish(make_event("task_started", run_id="r"))
        with anyio.move_on_after(0.05) as scope:
            await bus.publish(make_event("task_started", run_id="r"))
        assert scope.cancel_called, "publish should block when subscriber is saturated"
    finally:
        # Drain the buffered event and close.
        async with sub:
            await sub.receive()
        await bus.close()


async def test_subscribe_after_close_raises() -> None:
    bus = EventBus()
    await bus.close()
    with pytest.raises(FatalError):
        bus.subscribe()


async def test_publish_after_close_raises() -> None:
    bus = EventBus()
    await bus.close()
    with pytest.raises(FatalError):
        await bus.publish(make_event("task_started", run_id="r"))


async def test_multiple_subscribers_each_see_all_events() -> None:
    bus = EventBus(buffer_size=8)
    s1 = bus.subscribe()
    s2 = bus.subscribe()

    async def drain(sub) -> list[str]:
        out: list[str] = []
        async with sub:
            async for e in sub:
                out.append(e.run_id)
        return out

    collected: dict[int, list[str]] = {}

    async def collect(idx: int, sub) -> None:
        collected[idx] = await drain(sub)

    async with anyio.create_task_group() as tg:
        tg.start_soon(collect, 1, s1)
        tg.start_soon(collect, 2, s2)
        for i in range(4):
            await bus.publish(make_event("task_started", run_id=f"r{i}"))
        await bus.close()

    assert collected[1] == ["r0", "r1", "r2", "r3"]
    assert collected[2] == ["r0", "r1", "r2", "r3"]
