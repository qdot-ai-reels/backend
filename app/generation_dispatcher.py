"""Queue boundary for long-running generation jobs.

The default adapter intentionally uses FastAPI BackgroundTasks for a single
host. A production queue can implement GenerationDispatcher and invoke the
same persisted workers without changing the HTTP or database contracts.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from fastapi import BackgroundTasks


GenerationTask = Callable[..., None]


class GenerationDispatcher(Protocol):
    def enqueue(self, task: GenerationTask, *args: Any) -> None: ...


class InProcessGenerationDispatcher:
    """Single-process adapter; persisted state survives but execution does not."""

    def __init__(self, background_tasks: BackgroundTasks) -> None:
        self.background_tasks = background_tasks

    def enqueue(self, task: GenerationTask, *args: Any) -> None:
        self.background_tasks.add_task(task, *args)
