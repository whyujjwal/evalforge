"""FastAPI server — thin adapter over the library.

Endpoints:

- ``POST /runs``          — start a new run from a Suite file path.
- ``GET  /runs``          — list recent runs (headers only).
- ``GET  /runs/{id}``     — full run JSON.
- ``GET  /runs/{id}/events`` — SSE stream of stored events for the run.

The server is a *client* of the library — it never reaches into storage
directly from handler bodies. Everything goes through the :class:`Storage`
and :class:`Engine` abstractions. If you find yourself writing business
logic here, it belongs in the core.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from evalforge._loader import load_suite
from evalforge.engine import Engine
from evalforge.errors import ConfigurationError, EvalforgeError, FatalError
from evalforge.events import EventBus
from evalforge.storage.sqlite import SQLiteStorage


class RunRequest(BaseModel):
    suite_path: str = Field(..., description="Absolute path to a Suite file")
    attr: str = Field("suite", description="Suite variable name in the file")
    resume: str | None = None


def create_app(*, db_path: str | Path = "evalforge.sqlite") -> FastAPI:
    """Build a FastAPI app bound to ``db_path``.

    Factory style (rather than a module-level ``app``) so tests can stand up
    isolated instances and so the same server can run against multiple DBs.
    """
    store = SQLiteStorage(db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await store.initialize()
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="evalforge", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(EvalforgeError)
    async def _evalforge_error(_request: Any, exc: EvalforgeError) -> Any:
        status = 500 if isinstance(exc, FatalError) else 400
        return _err(status, type(exc).__name__, exc.message, exc.context)

    @app.post("/runs")
    async def post_runs(body: RunRequest) -> dict[str, Any]:
        try:
            suite = load_suite(body.suite_path, attr=body.attr)
        except ConfigurationError as e:
            raise HTTPException(status_code=400, detail=e.message) from e
        resume_from = None
        if body.resume is not None:
            resume_from = await store.load_run(body.resume)
        bus = EventBus(buffer_size=1024)
        engine = Engine(bus=bus, own_bus=False)
        async with store.attach(bus):
            result = await engine.run(suite, resume_from=resume_from)
            await bus.close()
        return {
            "run_id": result.run.id,
            "status": result.run.status.value,
            "results": len(result.run.results),
            "mean_scores": result.run.mean_score_per_rubric,
        }

    @app.get("/runs")
    async def get_runs(suite_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        runs = await store.list_runs(suite_name=suite_name, limit=limit)
        return [
            {
                "id": r.id,
                "suite_name": r.suite_name,
                "status": r.status.value,
                "created_at": r.created_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "n_results": len(r.results),
            }
            for r in runs
        ]

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            run = await store.load_run(run_id)
        except FatalError as e:
            raise HTTPException(status_code=404, detail=e.message) from e
        return run.model_dump(mode="json")

    @app.get("/runs/{run_id}/events")
    async def get_run_events(run_id: str) -> EventSourceResponse:
        async def generate() -> Any:
            # Drain stored events for this run from disk. This is historical
            # replay; live streaming of a running run would fan out off the
            # bus and is a follow-up.
            for event in await _load_events_for_run(store, run_id):
                yield {
                    "event": event["kind"],
                    "id": str(event["seq"]),
                    "data": json.dumps(event),
                }

        return EventSourceResponse(generate())

    return app


async def _load_events_for_run(store: SQLiteStorage, run_id: str) -> list[dict[str, Any]]:
    def _sync() -> list[dict[str, Any]]:
        conn = store._connect()
        rows = conn.execute(
            "SELECT seq, kind, ts, task_id, node_id, attempt, payload_json "
            "FROM events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "ts": r["ts"],
                "task_id": r["task_id"],
                "node_id": r["node_id"],
                "attempt": r["attempt"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]

    try:
        async with store._lock:
            return await anyio.to_thread.run_sync(_sync)
    except sqlite3.Error as e:  # pragma: no cover — defensive
        raise HTTPException(status_code=500, detail=str(e)) from e


def _err(status: int, kind: str, message: str, context: dict[str, Any]) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status,
        content={"error": {"kind": kind, "message": message, "context": context}},
    )


# Module-level default app for `uvicorn evalforge.server:app`.
app = create_app()
