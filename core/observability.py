from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PipelineRun
from db.session import async_session_factory

logger = logging.getLogger(__name__)

COST_PER_MILLION_TOKENS = {
    "gemini-2.5-pro": 2.50,
    "gemini-2.5-flash": 0.30,
}


@dataclass
class StepTrace:
    name: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int = 0
    input_summary: dict = field(default_factory=dict)
    output_summary: dict = field(default_factory=dict)
    tokens_used: int | None = None
    model_used: str | None = None
    errors: list[str] = field(default_factory=list)
    status: str = "running"


@dataclass
class PipelineTrace:
    project_id: str
    started_at: datetime
    completed_at: datetime | None = None
    steps: list[StepTrace] = field(default_factory=list)
    total_duration_ms: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    final_status: str = "running"
    metadata: dict = field(default_factory=dict)


class PipelineTracer:
    """Records timing, tokens, and errors for every pipeline step."""

    def start_pipeline(self, project_id: str) -> PipelineTrace:
        trace = PipelineTrace(
            project_id=project_id,
            started_at=datetime.now(timezone.utc),
        )
        logger.info("Pipeline trace started for project %s", project_id)
        return trace

    def start_step(
        self,
        trace: PipelineTrace,
        name: str,
        input_summary: dict[str, Any] | None = None,
    ) -> StepTrace:
        step = StepTrace(
            name=name,
            started_at=datetime.now(timezone.utc),
            input_summary=input_summary or {},
        )
        trace.steps.append(step)
        logger.debug("Step '%s' started for project %s", name, trace.project_id)
        return step

    def end_step(
        self,
        trace: PipelineTrace,
        step: StepTrace,
        *,
        output_summary: dict[str, Any] | None = None,
        tokens: int = 0,
        model: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        step.completed_at = now
        step.duration_ms = int((now - step.started_at).total_seconds() * 1000)
        step.output_summary = output_summary or {}
        step.tokens_used = tokens or None
        step.model_used = model
        if errors:
            step.errors.extend(errors)
        step.status = "completed"
        logger.debug(
            "Step '%s' completed in %dms (tokens=%d)",
            step.name,
            step.duration_ms,
            tokens,
        )

    def fail_step(
        self,
        trace: PipelineTrace,
        step: StepTrace,
        error: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        step.completed_at = now
        step.duration_ms = int((now - step.started_at).total_seconds() * 1000)
        step.errors.append(error)
        step.status = "failed"
        trace.final_status = "failed"
        logger.warning(
            "Step '%s' failed for project %s: %s",
            step.name,
            trace.project_id,
            error,
        )

    def end_pipeline(self, trace: PipelineTrace, status: str) -> None:
        now = datetime.now(timezone.utc)
        trace.completed_at = now
        trace.total_duration_ms = int(
            (now - trace.started_at).total_seconds() * 1000
        )
        trace.total_tokens = sum(
            s.tokens_used for s in trace.steps if s.tokens_used
        )
        trace.total_cost_usd = self._estimate_cost(trace)
        trace.final_status = status
        logger.info(
            "Pipeline trace ended for project %s — status=%s duration=%dms tokens=%d cost=$%.4f",
            trace.project_id,
            status,
            trace.total_duration_ms,
            trace.total_tokens,
            trace.total_cost_usd,
        )

    async def save_trace(self, trace: PipelineTrace) -> None:
        try:
            async with async_session_factory() as session:
                run = PipelineRun(
                    project_id=trace.project_id,
                    started_at=trace.started_at,
                    completed_at=trace.completed_at,
                    total_duration_ms=trace.total_duration_ms,
                    total_tokens=trace.total_tokens,
                    total_cost_usd=trace.total_cost_usd,
                    final_status=trace.final_status,
                    steps=[_step_to_dict(s) for s in trace.steps],
                    metadata_=trace.metadata,
                )
                session.add(run)
                await session.commit()
                logger.info(
                    "Pipeline trace persisted for project %s (run %s)",
                    trace.project_id,
                    run.id,
                )
        except Exception:
            logger.exception("Failed to persist pipeline trace for project %s", trace.project_id)

    def format_trace(self, trace: PipelineTrace) -> dict[str, Any]:
        return {
            "project_id": trace.project_id,
            "started_at": trace.started_at.isoformat(),
            "completed_at": trace.completed_at.isoformat() if trace.completed_at else None,
            "total_duration_ms": trace.total_duration_ms,
            "total_tokens": trace.total_tokens,
            "total_cost_usd": round(trace.total_cost_usd, 6),
            "final_status": trace.final_status,
            "steps": [_step_to_dict(s) for s in trace.steps],
            "metadata": trace.metadata,
        }

    @staticmethod
    def _estimate_cost(trace: PipelineTrace) -> float:
        total = 0.0
        for step in trace.steps:
            if step.tokens_used and step.model_used:
                rate = COST_PER_MILLION_TOKENS.get(step.model_used, 0.0)
                total += step.tokens_used / 1_000_000 * rate
        return total


class TracedStep:
    """Async context manager for tracing a single pipeline step."""

    def __init__(
        self,
        tracer: PipelineTracer,
        trace: PipelineTrace,
        name: str,
        input_summary: dict[str, Any] | None = None,
    ) -> None:
        self._tracer = tracer
        self._trace = trace
        self._name = name
        self._input_summary = input_summary
        self.step: StepTrace | None = None
        self.tokens: int = 0
        self.model: str | None = None
        self.output_summary: dict[str, Any] = {}

    async def __aenter__(self) -> TracedStep:
        self.step = self._tracer.start_step(
            self._trace, self._name, self._input_summary
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self.step is None:
            return
        if exc_type is not None:
            self._tracer.fail_step(self._trace, self.step, str(exc_val))
        else:
            self._tracer.end_step(
                self._trace,
                self.step,
                output_summary=self.output_summary,
                tokens=self.tokens,
                model=self.model,
            )


def _step_to_dict(step: StepTrace) -> dict[str, Any]:
    return {
        "name": step.name,
        "started_at": step.started_at.isoformat(),
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
        "duration_ms": step.duration_ms,
        "status": step.status,
        "tokens_used": step.tokens_used,
        "model_used": step.model_used,
        "errors": step.errors,
        "input_summary": step.input_summary,
        "output_summary": step.output_summary,
    }
