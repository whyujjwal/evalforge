"""End-to-end example: math word problems against the mock provider.

Run it::

    uv run evalforge run examples/math_suite.py

This deliberately uses the mock provider so anyone can run it without an
API key. Swap ``provider="mock"`` for ``"anthropic"`` or ``"openai"`` and
set the appropriate env var to hit a real model.
"""

from __future__ import annotations

from evalforge import RetryPolicy, RunConfig, Suite, Task, llm_agent, llm_judge, rule_judge
from evalforge.providers.mock import install_mock_provider

# For the mock run we seed a predictable script. Delete this block when
# pointing at a real provider.
install_mock_provider(
    script=[
        # solver answers — cycle through these
        "42",
        "7",
        "12",
        "100",
        # judge verdicts — parsed by _extract_score
        "score: 1.0 correct answer",
        "score: 0.2 incorrect",
        "score: 1.0 correct",
        "score: 0.8 almost",
    ]
)

TASKS = [
    Task(id="t1", input={"question": "What is 6 * 7?"}, expected_output="42"),
    Task(id="t2", input={"question": "What is 3 + 4?"}, expected_output="7"),
    Task(id="t3", input={"question": "What is 3 * 4?"}, expected_output="12"),
    Task(id="t4", input={"question": "What is 10 * 10?"}, expected_output="100"),
]

solver = llm_agent(
    "solver",
    provider="mock",
    model="mock",
    prompt="Solve this math problem and answer with just the number.\n{question}",
    retry=RetryPolicy(max_retries=2),
)

correctness = llm_judge(
    "correctness",
    provider="mock",
    model="mock",
    rubric=(
        "Is the solver's answer correct? "
        "Expected={expected_output}. Output={output}. "
        "Reply 'score: 1.0' if correct, 'score: 0.0' otherwise."
    ),
)

is_digit = rule_judge("is_digit", lambda v: bool(isinstance(v, str) and v.strip().isdigit()))

suite = Suite(
    name="math_word_problems",
    tasks=TASKS,
    pipeline=[solver, [correctness, is_digit]],
    config=RunConfig(concurrency=4, seed=42),
)
