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

**What's next:** M1 — domain types (frozen Pydantic v2), error taxonomy, pipeline DAG compilation + topological resolution, and their unit tests.

---

## M1 — Types, errors, and pipeline DAG

**Built:** The full declarative surface of the library.

- `evalforge/errors.py` — `EvalforgeError` hierarchy with `TransientError` / `PermanentError` / `FatalError` plus `ProviderTransientError` / `ProviderPermanentError` / `ConfigurationError` specializations. Every exception carries a structured `context: dict` that flows into events and logs, copied defensively so callers cannot mutate it post-raise.
- `evalforge/types.py` — All domain data as frozen Pydantic v2 models with `extra="forbid"`: `Task`, `Score`, `TokenUsage` (additive), `CompletionRequest` / `CompletionResponse`, `RetryPolicy`, `AgentContext`, `AgentOutput`, `TaskResult`, `RunConfig`, `Run`, plus `Event`, `AgentKind`, `RunStatus`, `TaskStatus`. `Run` exposes computed aggregates (total tokens, mean score per rubric, completed task ids) as pure properties so subscribers can derive state without touching storage.
- `evalforge/providers/__init__.py` — `Provider` protocol + a tiny in-memory registry. Registered by name so agents reference providers as strings at declaration time; concrete implementations land in M2/M4. Registry lookup errors are `ConfigurationError`, not `KeyError` — callers see the taxonomy, never a raw stdlib exception.
- `evalforge/agents.py` — `Agent` / `Judge` runtime protocols (`@runtime_checkable`) and the three user-facing factories: `llm_agent`, `llm_judge`, `rule_judge`. Factory returns are slotted dataclasses, *not* Pydantic models, because they're runtime objects with a `run()` coroutine — no serialization needed. `rule_judge` offloads sync user callables to a threadpool via `anyio.to_thread.run_sync`; the spec says never await sync code directly, and this is where that promise is kept.
- `evalforge/pipeline.py` — `compile_pipeline(decl) -> ResolvedDAG` turns the nested-list declaration into a validated flat DAG with topological layers. `Suite` compiles eagerly in `__init__` so config errors surface at Suite definition time, not mid-run.

**Key decisions and things I weighed:**

1. *Template rendering.* `str.format` treats `{{` as an escape and swallows silent typos. I wrote a tiny `_render` using a regex that **raises `PermanentError` with the available keys listed** on any missing placeholder. Two-line implementation, dramatically better user experience when a rubric references `{expected}` instead of `{expected_output}`.

2. *Judge verdict parsing.* `_extract_score` first looks for `score: <float>` / `rating: <float>`, then falls back to "first float in text". Not bulletproof — LLMs will hallucinate "score: nine" — but the spec calls for an `llm_judge` factory, and structured-output enforcement belongs one layer up (a stretch goal or a follow-up). For strict parsing, users reach for `rule_judge`.

3. *Fan-in semantics for judges.* The nested-list form makes fan-out trivial (one stage, many nodes) but fan-in ambiguous: does a judge following `[A, B]` score A's output, B's, or both? I chose **explicit disambiguation**: if a judge has multiple parents, it must declare `parent="a"` or compilation fails. This is a hard error in `compile_pipeline`, tested. Alternative considered: auto-spawn one judge instance per parent. Rejected — silent duplication is the worse failure mode, and explicitness is cheaper in the rare case someone hits it.

4. *First stage must include agents.* A suite that starts with a judge is meaningless (nothing to score). Validated in `compile_pipeline`, tested. This is the one structural check beyond uniqueness; the nested-list shape is acyclic by construction so there's no cycle check to write.

5. *Suite as a frozen model that takes non-Pydantic Agents.* I had to extend `BaseModel.__init__` to normalize `list` → `tuple` and to compile-then-cache the DAG. Caching via `object.__setattr__` on a frozen model feels dirty; the clean alternative is `@functools.cached_property`, but Pydantic frozen models don't let you assign to properties. Went with the `object.__setattr__` escape hatch and kept it to a single line in one place. Flagged for revisit if the engine wants to refresh the DAG mid-run (it won't).

6. *Runtime Protocols vs. ABCs.* Agents are `Protocol` + `@runtime_checkable`. The DAG validation uses `hasattr(x, "id") and hasattr(x, "run")` as a structural check rather than `isinstance` because `runtime_checkable` Protocols match too permissively (any object with those attrs). For user-facing errors the duck check is clearer — the error message names the missing attribute.

**Recovery moments:**

- First `mypy --strict` pass caught three issues worth flagging: (1) `tuple(stage)` on a `Agent | Sequence[Agent]` union produced `tuple[Agent | Sequence[Agent]]` because mypy doesn't narrow element types through `isinstance(x, (list, tuple))`. Fixed with an explicit `list[Agent]` annotation in the branch and `cast(Agent, stage)` in the single-node branch. (2) `UP042`: `class X(str, Enum)` is now stylistically disfavored in favor of `StrEnum` (3.11+). Fixed. (3) `A002`: `id` shadows the builtin; the natural API name here, so globally ignored with a comment.
- `import-linter` required `include_external_packages = true` at top level because my `types/errors is a leaf` contract names external modules (`httpx`, etc.) as forbidden. Fixed in one line.

**Test surface:** 28 tests across `test_types.py`, `test_errors.py`, `test_pipeline.py`. Covers frozen-ness, forbid-extra, whitespace-in-id, `TokenUsage` addition, `Run` aggregates, fan-out, fan-in-disambiguation, duplicate ids, empty pipeline, empty parallel stage, judge-only stage 0, invalid id regex, non-Agent rejected, `Suite` eager compilation, duplicate task ids, empty task list, and DAG roots/leaves/judges helpers.

**Gates at end of M1:** `ruff check` ✅, `ruff format --check` ✅, `mypy --strict` ✅ (0 errors, 0 `# type: ignore` in the library), `import-linter` ✅ (Layered architecture KEPT; types/errors leaves KEPT), `pytest` ✅ (28 passed).

**What's next:** M2 — the execution layer. EventBus (in-process async pub/sub with monotonic seq), mock provider, then the engine itself: DAG walker with bounded task concurrency, per-agent retry with seeded jitter, signal-driven graceful shutdown, and the first integration tests that exercise the whole pipeline end to end.

