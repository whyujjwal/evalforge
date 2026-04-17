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
