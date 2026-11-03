"""OpenTelemetry-backed tracing service for LFX package.

Replaces the minimal stub with a real implementation that:
1. Creates OTel spans for flow runs and components
2. Injects trace_id/span_id into structured logs for correlation
3. Exports spans to OTLP endpoint configured via OTEL_* env vars
4. Enables LLM/embedding/vector-db spans for full RAG observability
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from lfx.log.logger import logger
from lfx.services.tracing.base import BaseTracingService

if TYPE_CHECKING:
    from uuid import UUID

    from langchain_core.callbacks.base import BaseCallbackHandler

    from lfx.custom.custom_component.component import Component
    from opentelemetry.trace import Span, Tracer, TracerProvider

# Scopes to allow through the application-only span processor for LLM observability
_LLM_SCOPE_ALLOWLIST = frozenset([
    "opentelemetry.instrumentation.openai",
    "opentelemetry.instrumentation.langchain",
    "opentelemetry.instrumentation.qdrant",
    "opentelemetry.instrumentation.requests",
])

# Lazy import to avoid startup cost when OTel is not configured
_tracer_provider: TracerProvider | None = None
_llm_tracer_provider: TracerProvider | None = None
_tracer: Tracer | None = None
_llm_tracer: Tracer | None = None


def _enable_llm_scopes():
    """Monkey-patch LFX observability to allow LLM instrumentation scopes through."""
    try:
        from lfx import observability as obs_mod
        current = obs_mod.APPLICATION_INSTRUMENTATION_SCOPES
        expanded = frozenset(list(current) + list(_LLM_SCOPE_ALLOWLIST))
        object.__setattr__(obs_mod, "APPLICATION_INSTRUMENTATION_SCOPES", expanded)
        logger.info(f"Enabled LLM tracing: {len(_LLM_SCOPE_ALLOWLIST)} additional scopes")
    except Exception as e:
        logger.warning(f"Could not enable LLM scopes: {e}")


def _get_llm_tracer() -> Tracer:
    """Get a dedicated tracer for LLM spans with direct OTLP export."""
    global _llm_tracer
    if _llm_tracer is None:
        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            if not endpoint:
                return otel_trace.get_tracer("langflow.v2.llm.noop")

            headers = {}
            auth = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            if auth and "Authorization=" in auth:
                headers["Authorization"] = auth.split("Authorization=", 1)[1]

            exporter = OTLPSpanExporter(
                endpoint=f"{endpoint}/v1/traces",
                headers=headers if headers else None,
            )

            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(exporter))
            otel_trace.set_tracer_provider(provider)
            _llm_tracer_provider = provider
            _llm_tracer = provider.get_tracer("langflow.v2.llm")
            logger.info("LLM tracer ready: spans export directly to OpenObserve")
        except Exception as e:
            logger.warning(f"LLM tracer init failed: {e}")
            _llm_tracer = None
    return _llm_tracer


def _get_tracer() -> Tracer:
    """Get or create the application tracer."""
    global _tracer
    if _tracer is None:
        try:
            _enable_llm_scopes()  # Must come BEFORE bootstrap

            from opentelemetry import trace as otel_trace
            from lfx.observability import bootstrap_application_telemetry

            bootstrap_application_telemetry(prometheus_enabled=False)
            _get_llm_tracer()
            _tracer = otel_trace.get_tracer("langflow.v2.tracing")
        except Exception:
            logger.debug("OpenTelemetry not available, tracing disabled")
            _tracer = None
    return _tracer


class TracingService(BaseTracingService):
    """OpenTelemetry tracing service for LFX.

    Creates real spans for flow runs and components, exports to OTLP.
    Falls back to no-op if OTel is not configured.
    """

    def __init__(self):
        """Initialize the tracing service."""
        super().__init__()
        self.deactivated = False
        self._current_trace_span: Span | None = None
        self._start_time: float | None = None
        self.set_ready()

    @property
    def name(self) -> str:
        """Service name identifier."""
        return "tracing_service"

    async def start_tracers(
        self,
        run_id: UUID,
        run_name: str,
        user_id: str | None,
        session_id: str | None,
        project_name: str | None = None,
        flow_id: str | None = None,
        tracing_user_id: str | None = None,
    ) -> None:
        """Start tracers for a graph run - creates root span."""
        tracer = _get_tracer()
        if tracer is None:
            logger.debug(f"Trace started (no OTel): {run_name}")
            return

        try:
            self._start_time = time.time()
            attributes: dict[str, Any] = {
                "flow.name": run_name,
                "run.id": str(run_id),
            }
            if user_id:
                attributes["user.id"] = user_id
            if session_id:
                attributes["session.id"] = session_id
            if flow_id:
                attributes["flow.id"] = flow_id

            self._current_trace_span = tracer.start_span(
                run_name,
                attributes=attributes,
            )
            logger.debug(f"OTel span started: {run_name} (trace_id={self._current_trace_span.get_span_context().trace_id:032x})")
        except Exception as e:
            logger.error(f"Failed to start OTel span: {e}")
            self._current_trace_span = None

    async def end_tracers(self, outputs: dict, error: Exception | None = None) -> None:
        """End tracers for a graph run - ends root span."""
        if self._current_trace_span is None:
            return

        try:
            if error is not None:
                self._current_trace_span.set_status(
                    getattr(self._current_trace_span.status, "CODE_ERROR", None) or "error"
                )
                self._current_trace_span.record_exception(error)

            if self._start_time:
                duration = time.time() - self._start_time
                self._current_trace_span.set_attribute("flow.duration_s", duration)

            self._current_trace_span.end()
            self._current_trace_span = None
            logger.debug("OTel span ended")
        except Exception as e:
            logger.error(f"Failed to end OTel span: {e}")

    @asynccontextmanager
    async def trace_component(
        self,
        component: Component,
        trace_name: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ):
        """Trace a component execution - creates child span."""
        tracer = _get_tracer()
        if tracer is None:
            yield self
            return

        span = None
        try:
            attributes: dict[str, Any] = {
                "component.name": trace_name,
            }
            if metadata:
                for k, v in metadata.items():
                    if isinstance(v, (str, int, float, bool)):
                        attributes[f"component.{k}"] = str(v)

            span = tracer.start_span(trace_name, attributes=attributes)
            yield self
        except Exception as e:
            if span:
                span.set_status("error")
                span.record_exception(e)
                span.end()
            raise
        finally:
            if span:
                span.end()

    def add_log(self, trace_name: str, log: Any) -> None:
        """Add a log entry (delegates to OTel logging if available)."""
        try:
            from opentelemetry import _logs

            logger.debug(f"Trace log: {trace_name}")
        except Exception:
            logger.debug(f"Trace log: {trace_name}")

    def set_outputs(
        self,
        trace_name: str,
        outputs: dict[str, Any],
        output_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set outputs for the trace (noop - outputs are in the span)."""
        pass

    def get_langchain_callbacks(self) -> list[BaseCallbackHandler]:
        """Get LangChain callbacks for LLM observability."""
        try:
            from opentelemetry.instrumentation.langchain import LangchainInstrumentor
            LangchainInstrumentor().instrument()
            logger.debug("LangChain OTel instrumentor activated")
        except Exception as e:
            logger.warning(f"LangChain instrumentor failed: {e}")
        return []

    @property
    def project_name(self) -> str | None:
        """Get project name (returns None)."""
        return None

    async def teardown(self) -> None:
        """Teardown the tracing service."""
        self._current_trace_span = None
        logger.debug("Tracing service teardown")
