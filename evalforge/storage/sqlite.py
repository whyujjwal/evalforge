"""SQLite-backed :class:`Storage`.

Design notes:

- Uses stdlib :mod:`sqlite3` wrapped in :mod:`anyio.to_thread` calls. SQLite
  is synchronous; the async façade is real, not cosmetic — every ``await``
  actually hands control back to the event loop while the DB does its work.
- One connection, accessed under a single lock. For our workloads (one
  writer: the engine's event subscriber; many point reads: resume / CLI)
  this is simpler and more correct than a pool. We set
  ``PRAGMA journal_mode=WAL`` so reads never block the writer.
- Migrations live in ``evalforge/storage/migrations/`` as numbered SQL
  files. ``initialize()`` applies any file whose version is greater than
  the current ``schema_version``.
- Transactional writes: ``save_task_result`` inserts the ``task_results``
  row and all its ``scores`` rows in the same ``BEGIN ... COMMIT`` block.
  Partial writes are impossible.
- The event subscriber is implemented in :meth:`SQLiteStorage.attach`. It
  drains the bus and persists ``run_started`` / ``task_finished`` /
  ``run_finished`` events. Other kinds go to the ``events`` table as
  audit records.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from evalforge.errors import FatalError
from evalforge.events import EventBus
from evalforge.types import (
    Event,
    Run,
    RunConfig,
    RunStatus,
    Score,
    TaskResult,
    TaskStatus,
    TokenUsage,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


class SQLiteStorage:
    """SQLite :class:`~evalforge.storage.Storage` implementation."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = anyio.Lock()

    # ---- sync helpers (run via anyio.to_thread) ------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    def _migrate_sync(self) -> None:
        conn = self._connect()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            try:
                version = int(path.name.split("_", 1)[0])
            except ValueError as e:
                raise FatalError(f"Migration file has non-numeric prefix: {path.name}") from e
            if version <= current:
                continue
            sql = path.read_text()
            conn.executescript(sql)

    def _save_run_sync(self, run: Run) -> None:
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO runs (id, suite_name, status, config_json, created_at, started_at, finished_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                error=excluded.error
            """,
            (
                run.id,
                run.suite_name,
                run.status.value,
                json.dumps(run.config.model_dump(mode="json")),
                _iso(run.created_at),
                _iso(run.started_at),
                _iso(run.finished_at),
                run.error,
            ),
        )

    def _save_task_result_sync(self, run_id: str, result: TaskResult) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO task_results (
                    run_id, task_id, status, outputs_json,
                    tokens_input, tokens_output, cost_usd, error,
                    started_at, finished_at, attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, task_id) DO UPDATE SET
                    status=excluded.status,
                    outputs_json=excluded.outputs_json,
                    tokens_input=excluded.tokens_input,
                    tokens_output=excluded.tokens_output,
                    cost_usd=excluded.cost_usd,
                    error=excluded.error,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    attempts=excluded.attempts
                """,
                (
                    run_id,
                    result.task_id,
                    result.status.value,
                    json.dumps(result.outputs),
                    result.tokens.input_tokens,
                    result.tokens.output_tokens,
                    result.tokens.cost_usd,
                    result.error,
                    _iso(result.started_at),
                    _iso(result.finished_at),
                    result.attempts,
                ),
            )
            # Scores: replace-all for this task so resume-on-replay is idempotent.
            conn.execute(
                "DELETE FROM scores WHERE run_id = ? AND task_id = ?",
                (run_id, result.task_id),
            )
            for s in result.scores:
                conn.execute(
                    """
                    INSERT INTO scores (run_id, task_id, rubric_name, value, rationale, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result.task_id,
                        s.rubric_name,
                        s.value,
                        s.rationale,
                        json.dumps(s.metadata),
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _save_event_sync(self, event: Event) -> None:
        conn = self._connect()
        with suppress(sqlite3.IntegrityError):
            # Duplicate (run_id, seq) on idempotent replay is a no-op.
            conn.execute(
                """
                INSERT INTO events (seq, run_id, kind, ts, task_id, node_id, attempt, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.seq,
                    event.run_id,
                    event.kind,
                    _iso(event.ts),
                    event.task_id,
                    event.node_id,
                    event.attempt,
                    json.dumps(event.payload),
                ),
            )

    def _load_run_sync(self, run_id: str) -> Run:
        conn = self._connect()
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise FatalError(f"No such run: {run_id!r}", context={"run_id": run_id})
        config = RunConfig.model_validate(json.loads(row["config_json"]))

        result_rows = conn.execute(
            "SELECT * FROM task_results WHERE run_id = ? ORDER BY started_at", (run_id,)
        ).fetchall()
        results: list[TaskResult] = []
        for rr in result_rows:
            score_rows = conn.execute(
                "SELECT * FROM scores WHERE run_id = ? AND task_id = ? ORDER BY rubric_name",
                (run_id, rr["task_id"]),
            ).fetchall()
            scores = tuple(
                Score(
                    rubric_name=s["rubric_name"],
                    value=s["value"],
                    rationale=s["rationale"],
                    metadata=json.loads(s["metadata_json"]),
                )
                for s in score_rows
            )
            started = _dt(rr["started_at"])
            finished = _dt(rr["finished_at"])
            assert started is not None and finished is not None
            results.append(
                TaskResult(
                    task_id=rr["task_id"],
                    status=TaskStatus(rr["status"]),
                    outputs=json.loads(rr["outputs_json"]),
                    scores=scores,
                    tokens=TokenUsage(
                        input_tokens=rr["tokens_input"],
                        output_tokens=rr["tokens_output"],
                        cost_usd=rr["cost_usd"],
                    ),
                    error=rr["error"],
                    started_at=started,
                    finished_at=finished,
                    attempts=rr["attempts"],
                )
            )

        created = _dt(row["created_at"])
        assert created is not None
        return Run(
            id=row["id"],
            suite_name=row["suite_name"],
            status=RunStatus(row["status"]),
            config=config,
            created_at=created,
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            results=tuple(results),
            error=row["error"],
        )

    def _completed_task_ids_sync(self, run_id: str) -> set[str]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT task_id FROM task_results WHERE run_id = ? AND status = ?",
            (run_id, TaskStatus.OK.value),
        ).fetchall()
        return {r["task_id"] for r in rows}

    def _list_runs_sync(self, suite_name: str | None, limit: int) -> list[Run]:
        conn = self._connect()
        if suite_name is not None:
            rows = conn.execute(
                "SELECT id FROM runs WHERE suite_name = ? ORDER BY created_at DESC LIMIT ?",
                (suite_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._load_run_sync(r["id"]) for r in rows]

    # ---- async surface -----------------------------------------------

    async def initialize(self) -> None:
        async with self._lock:
            await anyio.to_thread.run_sync(self._migrate_sync)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                conn = self._conn
                self._conn = None
                await anyio.to_thread.run_sync(conn.close)

    async def save_run(self, run: Run) -> None:
        async with self._lock:
            await anyio.to_thread.run_sync(self._save_run_sync, run)

    async def save_task_result(self, run_id: str, result: TaskResult) -> None:
        async with self._lock:
            await anyio.to_thread.run_sync(self._save_task_result_sync, run_id, result)

    async def save_event(self, event: Event) -> None:
        async with self._lock:
            await anyio.to_thread.run_sync(self._save_event_sync, event)

    async def load_run(self, run_id: str) -> Run:
        async with self._lock:
            return await anyio.to_thread.run_sync(self._load_run_sync, run_id)

    async def completed_task_ids(self, run_id: str) -> set[str]:
        async with self._lock:
            return await anyio.to_thread.run_sync(self._completed_task_ids_sync, run_id)

    async def list_runs(self, *, suite_name: str | None = None, limit: int = 50) -> list[Run]:
        async with self._lock:
            return await anyio.to_thread.run_sync(self._list_runs_sync, suite_name, limit)

    @asynccontextmanager
    async def attach(self, bus: EventBus) -> AsyncIterator[None]:
        """Drain events from ``bus`` and persist them for the context's lifetime.

        Applies:
        - ``run_started`` / ``run_finished`` → upsert the ``runs`` row.
        - ``task_finished`` → persist the full :class:`TaskResult` + scores
          in one transaction (from the event's ``result`` payload).
        - All other kinds → appended to ``events`` table as audit records.

        **Caller contract:** close the bus before exiting this context. The
        context's ``__aexit__`` waits for the drain task to consume every
        remaining buffered event, which only happens once the bus is closed
        (``EndOfStream``). If the bus is still open at exit, the context
        closes it on your behalf to keep shutdown deterministic.
        """
        stream = bus.subscribe()

        async def _drain() -> None:
            try:
                async with stream:
                    async for event in stream:
                        await self._apply_event(event)
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return

        async with anyio.create_task_group() as tg:
            tg.start_soon(_drain)
            try:
                yield
            finally:
                if not bus.is_closed:
                    await bus.close()
                # Exiting the task group waits for _drain to finish, which
                # it will once EndOfStream is seen.

    async def _apply_event(self, event: Event) -> None:
        # Always audit-log the event itself.
        await self.save_event(event)

        if event.kind == "run_started":
            run_blob = event.payload.get("run")
            if isinstance(run_blob, dict):
                run = Run.model_validate(run_blob)
                await self.save_run(run)
        elif event.kind == "run_finished":
            # Re-load and update status/finished_at from the payload.
            try:
                current = await self.load_run(event.run_id)
            except FatalError:
                return
            status = RunStatus(event.payload.get("status", current.status.value))
            updated = current.model_copy(
                update={
                    "status": status,
                    "finished_at": _dt(event.payload.get("finished_at")) or current.finished_at,
                    "error": event.payload.get("error") or current.error,
                }
            )
            await self.save_run(updated)
        elif event.kind == "task_finished":
            result_blob = event.payload.get("result")
            if isinstance(result_blob, dict):
                tr = TaskResult.model_validate(result_blob)
                await self.save_task_result(event.run_id, tr)


def attach_storage(storage: SQLiteStorage, bus: EventBus) -> AbstractAsyncContextManager[None]:
    """Shim so callers can write ``async with attach_storage(store, bus):``.

    Thin wrapper; prefer ``storage.attach(bus)`` directly.
    """
    return storage.attach(bus)


__all__ = ["SQLiteStorage", "attach_storage"]
