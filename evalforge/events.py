"""In-process async event bus.

The bus is an in-process, multi-subscriber async pub/sub: the engine is the
single publisher and storage / CLI / metrics are subscribers. Each subscriber
gets its own bounded ``anyio`` memory object stream, so a slow subscriber
throttles the publisher rather than dropping events. This is a *correctness*
property, not an optimization — an event that never reaches storage is state
that the run has forgotten.

Contract:

- ``publish`` is awaited; it completes only after every active subscriber has
  accepted the event (i.e. the event is committed to every subscriber queue).
- ``seq`` is assigned monotonically by the bus on publish, regardless of what
  the caller set. Subscribers observe events in ``seq`` order.
- The bus itself is the truth for ``seq`` — if a producer already set it we
  overwrite, because having two ``seq``-assignment paths is a bug farm.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from evalforge.errors import FatalError
from evalforge.types import Event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class EventBus:
    """A minimal in-process async pub/sub.

    Each call to :meth:`subscribe` opens a new bounded stream; callers iterate
    it to consume events. Closing the bus closes every subscriber stream so
    consumers' ``async for`` loops exit cleanly.
    """

    def __init__(self, *, buffer_size: int = 256) -> None:
        self._buffer_size = buffer_size
        self._subscribers: list[MemoryObjectSendStream[Event]] = []
        self._seq: int = 0
        self._closed: bool = False
        self._publish_lock = anyio.Lock()

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def last_seq(self) -> int:
        return self._seq

    def subscribe(self) -> MemoryObjectReceiveStream[Event]:
        """Open a new subscription stream.

        The caller owns the returned stream and must iterate it until it's
        closed (either by :meth:`close` or by closing the receive stream).
        """
        if self._closed:
            raise FatalError("EventBus is closed; cannot subscribe")
        send, recv = anyio.create_memory_object_stream[Event](self._buffer_size)
        self._subscribers.append(send)
        return recv

    async def publish(self, event: Event) -> Event:
        """Assign the next seq, fan out to every subscriber, return the
        committed event.

        Holds an internal lock so ``seq`` assignment is atomic across
        concurrent publishers (there is normally only one — the engine —
        but we do not rely on that).
        """
        async with self._publish_lock:
            if self._closed:
                raise FatalError("EventBus is closed; cannot publish")
            self._seq += 1
            stamped = event.model_copy(update={"seq": self._seq})
            # Snapshot the subscriber list under the lock so concurrent
            # subscribe()/close() calls don't mutate it mid-fan-out.
            subs = list(self._subscribers)
        # Back-pressure: if a subscriber's buffer is full, send() awaits.
        # Every live subscriber must accept before publish returns.
        for send in subs:
            try:
                await send.send(stamped)
            except anyio.BrokenResourceError:
                # Subscriber closed its receiver early — treat as unsubscribe.
                self._subscribers.remove(send)
        return stamped

    async def close(self) -> None:
        """Close the bus. Every subscriber's receive stream will raise
        ``EndOfStream`` on the next iteration.
        """
        async with self._publish_lock:
            if self._closed:
                return
            self._closed = True
            subs = list(self._subscribers)
            self._subscribers.clear()
        for send in subs:
            await send.aclose()


@asynccontextmanager
async def collect_events(bus: EventBus) -> AsyncIterator[list[Event]]:
    """Testing helper: collect every event the bus publishes while in-scope.

    Example::

        async with collect_events(bus) as seen:
            ...
        assert [e.kind for e in seen] == ["run_started", ...]
    """
    collected: list[Event] = []
    stream = bus.subscribe()

    async def _drain() -> None:
        async with stream:
            async for event in stream:
                collected.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_drain)
        try:
            yield collected
        finally:
            # Give the drain a tick to catch up before we tear down.
            await anyio.sleep(0)
            await stream.aclose()


def make_event(
    kind: str,
    *,
    run_id: str,
    task_id: str | None = None,
    node_id: str | None = None,
    attempt: int = 0,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Convenience constructor — thin wrapper around :class:`Event` that lets
    the engine write ``make_event("task_started", run_id=...)`` without
    hand-importing the kind literal type. The actual literal validation lives
    on :class:`Event` itself.
    """
    return Event(
        kind=kind,
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        attempt=attempt,
        payload=payload or {},
    )


__all__ = ["EventBus", "collect_events", "make_event"]
