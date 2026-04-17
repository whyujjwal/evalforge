"""evalforge — a multi-agent evaluation harness for LLM pipelines.

Everything importable from this top-level module is public API.
Anything under ``evalforge.*`` not re-exported here is private and may change.
"""

from __future__ import annotations

from evalforge.agents import Agent, Judge, llm_agent, llm_judge, rule_judge
from evalforge.engine import Engine, EngineResult
from evalforge.errors import (
    ConfigurationError,
    EvalforgeError,
    FatalError,
    PermanentError,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
    TransientError,
)
from evalforge.events import EventBus
from evalforge.pipeline import Suite, compile_pipeline
from evalforge.storage import Storage
from evalforge.storage.sqlite import SQLiteStorage
from evalforge.types import (
    AgentContext,
    AgentKind,
    AgentOutput,
    CompletionRequest,
    CompletionResponse,
    Event,
    RetryPolicy,
    Run,
    RunConfig,
    RunStatus,
    Score,
    Task,
    TaskResult,
    TaskStatus,
    TokenUsage,
)

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "AgentContext",
    "AgentKind",
    "AgentOutput",
    "CompletionRequest",
    "CompletionResponse",
    "ConfigurationError",
    "Engine",
    "EngineResult",
    "EvalforgeError",
    "Event",
    "EventBus",
    "FatalError",
    "Judge",
    "PermanentError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "RetryPolicy",
    "Run",
    "RunConfig",
    "RunStatus",
    "SQLiteStorage",
    "Score",
    "Storage",
    "Suite",
    "Task",
    "TaskResult",
    "TaskStatus",
    "TokenUsage",
    "TransientError",
    "__version__",
    "compile_pipeline",
    "llm_agent",
    "llm_judge",
    "rule_judge",
]
