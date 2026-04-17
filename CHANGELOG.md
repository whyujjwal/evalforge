# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from
v0.1.0 onward.

## [Unreleased]

### Added

- Project scaffolding: `pyproject.toml` (uv-managed), ruff, `mypy --strict`,
  `import-linter` with a layered architecture contract, `pre-commit`, and a
  GitHub Actions CI workflow that enforces a 90% coverage floor on core
  modules.
- Domain types: `Task`, `Score`, `TokenUsage`, `CompletionRequest`/`Response`,
  `RetryPolicy`, `AgentContext`, `AgentOutput`, `TaskResult`, `RunConfig`,
  `Run`, `Event`. All frozen Pydantic v2 models.
- Error taxonomy: `EvalforgeError` root with `TransientError`,
  `PermanentError`, `FatalError`, `ConfigurationError`, and
  `ProviderTransientError` / `ProviderPermanentError` specializations.
- `Provider` protocol + in-memory registry.
- `Agent` / `Judge` runtime protocols and the `llm_agent`, `llm_judge`,
  `rule_judge` factories. Sync user callables in `rule_judge` are
  offloaded to a threadpool.
- `compile_pipeline` + `Suite`: nested-list pipeline declarations compile
  eagerly into a validated `ResolvedDAG`.
- `EventBus`: in-process multi-subscriber async pub/sub with monotonic seq
  and per-subscriber back-pressure (bounded memory object streams).
- `MockProvider`: deterministic, seedable provider for tests; accepts a
  scripted list of responses, exceptions, or callables.
- `Engine`: task-level bounded-concurrency executor; per-node retry on
  `TransientError` with seeded exponential-backoff + jitter; graceful
  shutdown via an `anyio.Event` stop signal; emits structured events for
  every state transition so storage/CLI/metrics are simple subscribers.
- `Storage` protocol + `SQLiteStorage` (stdlib sqlite3 in WAL mode,
  wrapped by `anyio.to_thread`). Migrations run on `initialize()` from
  `evalforge/storage/migrations/*.sql`. `storage.attach(bus)` spawns a
  subscriber that persists run headers and full `TaskResult`s in a single
  transaction per task.
- Resume: `Engine.run(suite, resume_from=Run)` skips tasks already
  persisted as `ok`; fault-injection test verifies no double-scoring,
  no dropped tasks, and deterministic parity with a clean run under the
  same seed.
- Real provider adapters: `AnthropicProvider` (Messages API) and
  `OpenAIProvider` (Chat Completions) built directly on `httpx` — no
  vendor SDK dependency. Both translate HTTP errors at the boundary
  (429 / 5xx / network → `ProviderTransientError`; 4xx → `ProviderPermanentError`)
  and populate `TokenUsage.cost_usd` from a pluggable pricing table.
- Structured logging via `structlog` with a `redact_secrets` processor
  that scrubs known API keys (including in nested dicts / lists) before
  records are encoded.
- `evalforge` CLI (Typer): `run`, `show`, `list`, `diff` — thin adapters
  over the library.
- FastAPI server: `POST /runs`, `GET /runs`, `GET /runs/{id}`,
  `GET /runs/{id}/events` (SSE stream). `create_app(db_path=...)` factory
  plus a module-level `app` for `uvicorn evalforge.server:app`.
- `examples/math_suite.py`: runnable against the mock provider
  (`uv run evalforge run examples/math_suite.py`).
