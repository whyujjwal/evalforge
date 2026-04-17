"""The ``evalforge`` CLI.

Thin adapter over the library. Every command here translates to one or two
library calls — no business logic. If you find yourself writing logic in
this file, it belongs in the core.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import typer

from evalforge._loader import load_suite
from evalforge._logging import configure, get_logger
from evalforge.engine import Engine
from evalforge.events import EventBus
from evalforge.storage.sqlite import SQLiteStorage
from evalforge.types import Run, TaskResult

app = typer.Typer(
    name="evalforge",
    help="Multi-agent evaluation harness.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_DB = Path("evalforge.sqlite")


@app.command("run")
def run_cmd(
    suite_path: Path = typer.Argument(..., exists=True, readable=True, help="Path to suite.py"),
    attr: str = typer.Option("suite", help="Name of the Suite variable in the file"),
    db: Path = typer.Option(DEFAULT_DB, help="SQLite DB path"),
    resume: str | None = typer.Option(None, help="Run id to resume"),
    log_level: str = typer.Option("INFO", help="Log level"),
) -> None:
    """Execute a Suite and persist the results."""
    configure(level=log_level)
    log = get_logger()

    async def _run() -> None:
        suite = load_suite(suite_path, attr=attr)
        store = SQLiteStorage(db)
        await store.initialize()
        resume_from: Run | None = None
        if resume is not None:
            resume_from = await store.load_run(resume)
        bus = EventBus(buffer_size=1024)
        engine = Engine(bus=bus, own_bus=False)
        log.info("run.start", suite=suite.name, db=str(db))
        async with store.attach(bus):
            result = await engine.run(suite, resume_from=resume_from)
            await bus.close()
        log.info(
            "run.done",
            run_id=result.run.id,
            status=result.run.status.value,
            completed=len(result.run.results),
            total_tokens=result.run.total_tokens.total_tokens,
            mean_scores=result.run.mean_score_per_rubric,
        )
        typer.echo(result.run.id)
        await store.close()

    anyio.run(_run)


@app.command("show")
def show_cmd(
    run_id: str = typer.Argument(...),
    db: Path = typer.Option(DEFAULT_DB, help="SQLite DB path"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON"),
) -> None:
    """Print a persisted run."""

    async def _show() -> None:
        store = SQLiteStorage(db)
        await store.initialize()
        run = await store.load_run(run_id)
        indent = 2 if pretty else None
        typer.echo(json.dumps(run.model_dump(mode="json"), indent=indent, default=str))
        await store.close()

    anyio.run(_show)


@app.command("list")
def list_cmd(
    db: Path = typer.Option(DEFAULT_DB, help="SQLite DB path"),
    suite_name: str | None = typer.Option(None, help="Filter by suite name"),
    limit: int = typer.Option(20, help="Max rows to list"),
) -> None:
    """List recent runs (id, suite_name, status, task counts)."""

    async def _list() -> None:
        store = SQLiteStorage(db)
        await store.initialize()
        runs = await store.list_runs(suite_name=suite_name, limit=limit)
        for r in runs:
            ok = sum(1 for tr in r.results if tr.status.value == "ok")
            failed = sum(1 for tr in r.results if tr.status.value == "failed")
            typer.echo(f"{r.id}\t{r.suite_name}\t{r.status.value}\tok={ok}\tfailed={failed}")
        await store.close()

    anyio.run(_list)


@app.command("diff")
def diff_cmd(
    run_a: str = typer.Argument(...),
    run_b: str = typer.Argument(...),
    db: Path = typer.Option(DEFAULT_DB, help="SQLite DB path"),
) -> None:
    """Side-by-side score deltas between two runs."""

    async def _diff() -> None:
        store = SQLiteStorage(db)
        await store.initialize()
        a = await store.load_run(run_a)
        b = await store.load_run(run_b)
        _emit_diff(a.results, b.results)
        await store.close()

    anyio.run(_diff)


def _emit_diff(a: tuple[TaskResult, ...], b: tuple[TaskResult, ...]) -> None:
    a_by_id = {r.task_id: r for r in a}
    b_by_id = {r.task_id: r for r in b}
    all_ids = sorted(set(a_by_id.keys()) | set(b_by_id.keys()))
    for tid in all_ids:
        ar = a_by_id.get(tid)
        br = b_by_id.get(tid)
        a_scores = {s.rubric_name: s.value for s in (ar.scores if ar else ())}
        b_scores = {s.rubric_name: s.value for s in (br.scores if br else ())}
        rubrics = sorted(set(a_scores) | set(b_scores))
        parts: list[str] = []
        for rub in rubrics:
            av = a_scores.get(rub)
            bv = b_scores.get(rub)
            delta = (bv - av) if (av is not None and bv is not None) else None
            parts.append(
                f"{rub}: {av if av is not None else '-'} -> {bv if bv is not None else '-'}"
                + (f" (Δ{delta:+.3f})" if delta is not None else "")
            )
        typer.echo(f"{tid}\t" + " | ".join(parts))


if __name__ == "__main__":  # pragma: no cover
    app()
