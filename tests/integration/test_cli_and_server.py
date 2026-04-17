"""End-to-end smoke: the example suite runs via the CLI and the server."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from evalforge._loader import load_suite
from evalforge.engine import Engine
from evalforge.events import EventBus
from evalforge.providers import clear_registry
from evalforge.storage.sqlite import SQLiteStorage


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


def _example_path() -> Path:
    return Path(__file__).parents[2] / "examples" / "math_suite.py"


async def test_example_suite_runs(tmp_path: Path) -> None:
    suite = load_suite(_example_path())
    store = SQLiteStorage(tmp_path / "db.sqlite")
    await store.initialize()
    bus = EventBus(buffer_size=512)
    engine = Engine(bus=bus, own_bus=False)
    async with store.attach(bus):
        result = await engine.run(suite)
        await bus.close()
    assert result.run.status.value == "completed"
    assert len(result.run.results) == 4
    await store.close()


def test_cli_run_command(tmp_path: Path) -> None:
    db = tmp_path / "cli.sqlite"
    env_cmd = [
        sys.executable,
        "-m",
        "evalforge.cli",
        "run",
        str(_example_path()),
        "--db",
        str(db),
    ]
    proc = subprocess.run(env_cmd, capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    # CLI echoes the run id on success.
    run_id = proc.stdout.strip().splitlines()[-1]
    assert run_id.startswith("run_")

    show = subprocess.run(
        [sys.executable, "-m", "evalforge.cli", "show", run_id, "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert show.returncode == 0, show.stderr
    assert run_id in show.stdout

    lst = subprocess.run(
        [sys.executable, "-m", "evalforge.cli", "list", "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert lst.returncode == 0, lst.stderr
    assert run_id in lst.stdout


async def test_server_lifecycle(tmp_path: Path) -> None:
    pytest.importorskip("httpx")
    from evalforge.server import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(db_path=tmp_path / "server.sqlite")
    # Drive ASGI lifespan manually so startup/shutdown run without external deps.
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
            r = await client.post(
                "/runs",
                json={"suite_path": str(_example_path())},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            run_id = data["run_id"]
            assert data["status"] == "completed"

            got = await client.get(f"/runs/{run_id}")
            assert got.status_code == 200
            assert got.json()["id"] == run_id

            lst = await client.get("/runs")
            assert lst.status_code == 200
            assert any(x["id"] == run_id for x in lst.json())
