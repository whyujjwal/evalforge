"""Storage protocol + helpers.

Storage is a **subscriber** of the :class:`~evalforge.events.EventBus`. The
engine does not import storage; it emits events that encode the full state
transition (``task_finished`` carries the full :class:`TaskResult`), and a
storage implementation attaches itself to the bus to persist them.

This protocol is narrow on purpose. Implementations:

- :class:`~evalforge.storage.sqlite.SQLiteStorage` — default.

Custom implementations (Postgres, Redis, in-memory) only need to satisfy
:class:`Storage`; they attach to a bus via :meth:`Storage.attach` which spawns
a subscriber task and returns an ``AsyncContextManager`` that drains it.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from evalforge.events import EventBus
from evalforge.types import Run, TaskResult


@runtime_checkable
class Storage(Protocol):
    """Persistent backing store.

    All methods are ``async`` so implementations can wrap sync drivers
    (sqlite3) with ``anyio.to_thread`` without leaking that choice into
    the interface.
    """

    async def initialize(self) -> None:
        """Run migrations / set up schema. Idempotent."""

    async def close(self) -> None:
        """Release resources. Idempotent."""

    async def save_run(self, run: Run) -> None:
        """Upsert a :class:`Run` header (no results).

        Storage writes results per task as ``task_finished`` events arrive;
        this method handles create/update of the run row itself.
        """

    async def save_task_result(self, run_id: str, result: TaskResult) -> None:
        """Persist a :class:`TaskResult` and its scores in one transaction."""

    async def load_run(self, run_id: str) -> Run:
        """Return the fully materialized :class:`Run`, including results."""

    async def completed_task_ids(self, run_id: str) -> set[str]:
        """Return task ids already persisted as ``ok`` for this run."""

    async def list_runs(self, *, suite_name: str | None = None, limit: int = 50) -> list[Run]:
        """Return recent runs (headers only, no results for cheapness)."""

    def attach(self, bus: EventBus) -> AbstractAsyncContextManager[None]:
        """Return a context manager that drains events from ``bus`` and
        persists them for as long as the context is active.
        """


__all__ = ["Storage"]
