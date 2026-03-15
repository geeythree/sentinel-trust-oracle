"""Append-mode agent_log.json writer with strict schema."""
from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from models import ActionType, AgentRole, LogEntry


class AgentLogger:
    """Append-mode JSON Lines logger with per-entry disk flush.

    Uses JSON Lines format (one JSON object per line) for append safety.
    Each entry is flushed to disk immediately -- survives mid-pipeline crashes.
    """

    def __init__(self, log_path: Path, budget: int = 15) -> None:
        self._path = log_path
        self._budget_total = budget
        self._budget_remaining = budget
        self._tool_calls_used = 0

    @property
    def budget_remaining(self) -> int:
        return self._budget_remaining

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    def consume_budget(self, count: int = 1) -> int:
        """Decrement budget. Returns remaining."""
        self._tool_calls_used += count
        self._budget_remaining = max(0, self._budget_total - self._tool_calls_used)
        return self._budget_remaining

    def log(self, entry: LogEntry) -> None:
        """Append a single log entry to disk. Flush immediately."""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.__dict__, default=str) + "\n")
                f.flush()
        except OSError as e:
            print(f"[LOGGER WARNING] Failed to write log entry: {e}", file=sys.stderr)

    def log_action(
        self,
        agent_role: AgentRole,
        action_type: ActionType,
        tool: Optional[str],
        payload: dict,
        result: dict,
        latency_ms: int,
        compute_tokens: int = 0,
    ) -> None:
        """Convenience method: builds LogEntry and writes it."""
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_role=agent_role.value,
            action_type=action_type.value,
            tool=tool,
            payload=payload,
            result=result,
            latency_ms=latency_ms,
            compute_tokens=compute_tokens,
            compute_budget_remaining=self._budget_remaining,
        )
        self.log(entry)

    @contextmanager
    def timed_action(
        self,
        agent_role: AgentRole,
        action_type: ActionType,
        tool: Optional[str],
        payload: dict,
    ) -> Generator[dict, None, None]:
        """Context manager that auto-logs with timing.

        Usage:
            with logger.timed_action(AgentRole.EVALUATOR, ActionType.TOOL_CALL, "slither", {...}) as result:
                result["status"] = "success"
                result["findings"] = {...}
        """
        result_container: dict = {}
        start = time.monotonic()
        try:
            yield result_container
        except Exception as e:
            result_container["status"] = "error"
            result_container["error"] = str(e)
            raise
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self.log_action(
                agent_role=agent_role,
                action_type=action_type,
                tool=tool,
                payload=payload,
                result=result_container,
                latency_ms=elapsed_ms,
                compute_tokens=result_container.get("compute_tokens", 0),
            )
