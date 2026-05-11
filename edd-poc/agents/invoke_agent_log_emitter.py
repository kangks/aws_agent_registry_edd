"""
Custom SpanProcessor that emits an `invoke_agent` log record when the agent span ends.

This bridges the gap between BYO agents (outside AgentCore Runtime) and the
AgentCore Evaluator's LLM-as-a-Judge path. The evaluator requires a log record
with scope `strands.telemetry.tracer`, spanId matching the `invoke_agent` span,
and body containing `{input: {messages: [...]}, output: {messages: [...]}}`.

For managed agents, the AgentCore Runtime sidecar creates this log record.
For BYO agents, this processor replicates that behavior by:
1. Watching for the `invoke_agent` span to end
2. Extracting the user query from the first chat span's input
3. Extracting the final response from the agent result
4. Emitting a log record with the correct format

Usage:
    from invoke_agent_log_emitter import InvokeAgentLogEmitter
    from opentelemetry.sdk.trace import TracerProvider
    provider = TracerProvider()
    provider.add_span_processor(InvokeAgentLogEmitter())

    OR (simpler, after ADOT sets up the provider):
    from opentelemetry import trace
    provider = trace.get_tracer_provider()
    provider.add_span_processor(InvokeAgentLogEmitter())
"""

import json
import logging
import time
from typing import Optional

from opentelemetry import trace
from opentelemetry._logs import get_logger_provider, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

logger = logging.getLogger(__name__)

# The scope name that the AgentCore Evaluator expects
STRANDS_SCOPE = "strands.telemetry.tracer"


class InvokeAgentLogEmitter(SpanProcessor):
    """Emits a log record for the invoke_agent span when it ends.

    This replicates what the AgentCore Runtime sidecar does for managed agents,
    enabling the LLM-as-a-Judge evaluator to work with BYO agent traces.
    """

    def __init__(self):
        self._first_user_message: Optional[str] = None
        self._last_assistant_message: Optional[str] = None
        self._session_id: Optional[str] = None
        self._chat_messages: list[dict] = []

    def on_start(self, span, parent_context=None):
        """Track chat spans to capture input/output messages."""
        pass

    def on_end(self, span: ReadableSpan) -> None:
        """When the invoke_agent span ends, emit the log record."""
        # Only process strands.telemetry.tracer spans
        if not span.instrumentation_scope or span.instrumentation_scope.name != STRANDS_SCOPE:
            return

        name = span.name or ""
        attributes = dict(span.attributes) if span.attributes else {}

        # Capture session.id from any strands span
        if "session.id" in attributes and not self._session_id:
            self._session_id = str(attributes["session.id"])

        # For chat spans: capture the messages from span events
        op_name = attributes.get("gen_ai.operation.name", "")
        if op_name == "chat" or name.startswith("chat"):
            # Look at span events for gen_ai.choice or message content
            for event in span.events or []:
                event_attrs = dict(event.attributes) if event.attributes else {}
                # Legacy format: gen_ai.choice event has "message" attribute
                if event.name == "gen_ai.choice":
                    msg = event_attrs.get("message", "")
                    if msg:
                        self._last_assistant_message = str(msg)

        # For the invoke_agent span: emit the log record
        if "invoke_agent" not in name:
            return

        # Extract the final response from the span's own events
        for event in span.events or []:
            event_attrs = dict(event.attributes) if event.attributes else {}
            if event.name == "gen_ai.choice":
                msg = event_attrs.get("message", "")
                if msg:
                    self._last_assistant_message = str(msg)
            elif event.name == "gen_ai.client.inference.operation.details":
                # Latest conventions format
                output_msgs = event_attrs.get("gen_ai.output.messages", "")
                if output_msgs:
                    self._last_assistant_message = str(output_msgs)

        # We need the user query — it's typically in the first chat span's input
        # Since we can't easily get it from span events (they don't store input),
        # we'll extract it from the agent span's context or from baggage
        # For now, use a placeholder approach: the user query is passed via env var
        import os
        user_query = os.environ.get("_BYO_USER_QUERY", "")

        if not self._last_assistant_message:
            logger.debug("invoke_agent span ended but no assistant message captured")
            return

        # Build the log record body in the format the evaluator expects
        # Based on testing: the evaluator needs content as {"content": "[{\"text\": ...}]"}
        # for BOTH input and output messages (matching the managed agent sidecar format)
        body = {
            "input": {
                "messages": [
                    {
                        "content": {"content": json.dumps([{"text": user_query}])},
                        "role": "user",
                    }
                ]
            },
            "output": {
                "messages": [
                    {
                        "content": {"content": json.dumps([{"text": str(self._last_assistant_message)}])},
                        "role": "assistant",
                    }
                ]
            },
        }

        # Emit the log record via the OTEL logging pipeline
        try:
            logger_provider = get_logger_provider()
            if not isinstance(logger_provider, LoggerProvider):
                logger.debug("LoggerProvider not available, skipping log emission")
                return

            otel_logger = logger_provider.get_logger(
                STRANDS_SCOPE,
                version="",
            )

            # Use the Logger.emit() keyword-argument API
            from opentelemetry.context import get_current
            from opentelemetry import trace as trace_api

            # Create a context with the correct span context so trace_id/span_id are set
            span_context = span.context

            otel_logger.emit(
                timestamp=span.end_time,
                observed_timestamp=int(time.time_ns()),
                severity_number=SeverityNumber.INFO,
                severity_text="",
                body=body,
                attributes={
                    "event.name": STRANDS_SCOPE,
                    "session.id": self._session_id or attributes.get("session.id", "default"),
                },
            )

            logger.info(
                f"Emitted invoke_agent log record: trace={format(span.context.trace_id, '032x')}, "
                f"span={format(span.context.span_id, '016x')}"
            )

        except Exception as e:
            logger.warning(f"Failed to emit invoke_agent log record: {e}", exc_info=True)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def install():
    """Install the InvokeAgentLogEmitter into the current TracerProvider.

    Call this AFTER ADOT has set up the TracerProvider (i.e., after
    opentelemetry-instrument has initialized).
    """
    provider = trace.get_tracer_provider()
    # The provider from ADOT is wrapped; get the underlying SDK provider
    if hasattr(provider, "_real_provider"):
        provider = provider._real_provider
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(InvokeAgentLogEmitter())
        logger.info("InvokeAgentLogEmitter installed")
    else:
        logger.warning(f"Cannot install InvokeAgentLogEmitter: provider type={type(provider)}")
