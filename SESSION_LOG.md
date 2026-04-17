# Session Log — evalforge

Real-time narrative of how this library was built in one session. Each milestone ends with a recap of what was built, key decisions, and what's next.

---

## M0 — Tooling and CI skeleton

**Built:** `pyproject.toml` as the single source of truth (uv-managed, hatchling backend). Ruff (E/F/I/N/UP/B/A/C4/SIM/TID/RUF), `mypy --strict` on `evalforge/`, `import-linter` contracts enforcing the prescribed dependency direction, `pre-commit` config wiring all three, and a GitHub Actions workflow that runs ruff → format → mypy → import-linter → pytest → core-coverage-floor-at-90%. Package skeleton created with empty modules so the import-linter layers contract resolves.

**Key decisions:**
- Layered import contract is expressed as `tool.importlinter` *layers* rather than a pile of *forbidden* contracts. One declarative list, reads top-down, matches the spec's module order exactly. A second *forbidden* contract is a belt-and-suspenders check that pins `types`/`errors` as true leaves (no intra-package imports, and no `httpx`/`sqlalchemy`/`fastapi`/`typer`). This makes it structurally impossible for a future edit to slip an HTTP client into `types.py`.
- Coverage floor is enforced by a small script (`scripts/check_core_coverage.py`) rather than inline shell. It is explicit about which files count as "core" — exactly the ones the spec names — and can be run locally.
- GitHub Actions: single job, 3.11 + 3.12 matrix, uv for speed. Workflow passes user-controlled values via env vars, not inline expansion, because the security hook (correctly) flags the other pattern.
- Secret-redaction test will live in M4 alongside the real providers. Decided not to pre-stub it in M0 because we'd have nothing meaningful to assert against yet.

**What's next:** M1 — domain types (frozen Pydantic v2), error taxonomy, pipeline DAG compilation + topological resolution, and their unit tests. After M1 the core is specifiable without any I/O surface area, which is the foundation the rest of the harness rests on.

