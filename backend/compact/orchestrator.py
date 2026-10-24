"""Compact orchestrator: drives the LLM summary call and yields SSE-friendly events.

The orchestrator does NOT go through the agent runner, smolagents, or tools.
The compact LLM call is a direct schema-less streaming call to llm_service.

Failure handling is intentionally narrow: cache-first validation failure or
cache-first request failure may fall back once to the reliable prompt shape.
Reliable-shape LLM failure still yields compact_failed with a granular reason;
there is no generic retry or circuit breaker.
"""
from __future__ import annotations

import inspect
import time
from typing import Any, AsyncGenerator, Callable, Optional

from ..config import CompactConfig, CompactionPromptsConfig
from ..llm_service import Message
from ..utils.logger import get_logger
from .prompt_builder import build_compact_messages
from .prompt_diagnostics import compare_prompt_snapshots, extract_cached_tokens, make_prompt_snapshot
from .prompt_plan import (
    CompactPromptPlan,
    build_cache_first_tail_plan,
    build_reliable_position1_plan,
    build_reliable_source_with_compact_system,
)
from .source_budget import trim_messages_to_token_budget
from .summary_runner import format_compact_summary
from .types import CompactEvent, CompactRequest
from .validation import validate_compact_summary

# === DIAGNOSTIC INSTRUMENTATION (2026-06-11) ===
# Use the project's get_logger() utility (same as backend/main.py and
# backend/llm_service.py) so this module gets a properly-configured
# console handler at INFO. Without this, the root logger (uvicorn's
# default config is WARNING) filters out our INFO-level [compact-diag]
# line and the diagnostic is invisible in production.
logger = get_logger(__name__, level="INFO")


class CompactOrchestrator:
    def __init__(
        self,
        llm_service: Any,
        config: CompactConfig,
        prompts: CompactionPromptsConfig,
        apply_compaction: Optional[Callable[[CompactRequest, str, str], None]] = None,
    ) -> None:
        self.llm_service = llm_service
        self.config = config
        self.prompts = prompts
        self._apply_compaction = apply_compaction

    def _chat_stream_async(
        self,
        *,
        messages: list[Message],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        request_id: str,
        preserve_prompt_shape: bool,
        tools: list[dict] | None = None,
    ):
        kwargs: dict[str, Any] = {
            "messages": messages,
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "tools": tools,
            "request_id": request_id,
        }
        try:
            params = inspect.signature(self.llm_service.chat_stream_async).parameters
            accepts_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            params = {}
            accepts_var_kwargs = False
        if "preserve_prompt_shape" in params or accepts_var_kwargs:
            kwargs["preserve_prompt_shape"] = preserve_prompt_shape
        extra_body = getattr(self.config, "extra_body", None)
        if extra_body is not None:
            accepts_extra_body = "extra_body" in params or accepts_var_kwargs
            if accepts_extra_body:
                kwargs["extra_body"] = extra_body
        return self.llm_service.chat_stream_async(**kwargs)

    @staticmethod
    def _normalize_prompt_messages(messages: list[Any]) -> list[Message]:
        from backend.agent.canonical_messages import canonicalize_model_messages
        return canonicalize_model_messages(messages)

    def _build_prompt_plans(self, req: CompactRequest) -> list[CompactPromptPlan]:
        if req.body_messages is None and req.messages is not None:
            logger.warning(
                "CompactRequest.body_messages is None; falling back to v1 path with req.messages. "
                "This is deprecated since 2026-06-11 and will be removed in the next release. "
                "Callers should set body_messages (a List[Message] including the system message) "
                "instead of messages (a body-only list)."
            )
            prompt_messages = build_compact_messages(
                body_messages=req.messages,
                prompts=self.prompts,
                custom_instructions=req.custom_instructions,
            )
            return [
                CompactPromptPlan(
                    "reliable_position1",
                    self._normalize_prompt_messages(prompt_messages),
                    stream_visible=True,
                )
            ]

        if req.body_messages is None:
            raise ValueError("CompactRequest must have body_messages (v2) or messages (v1) set")

        body_messages = trim_messages_to_token_budget(
            req.body_messages,
            max_tokens=getattr(self.config, "source_token_budget", None),
            prompts=self.prompts,
        )
        reliable_source = build_reliable_source_with_compact_system(body_messages, self.prompts)
        reliable = build_reliable_position1_plan(
            reliable_source,
            self.prompts,
            custom_instructions=req.custom_instructions,
        )

        if (
            self.config.prompt_shape == "cache_first_tail_with_fallback"
            and self.config.prompt_control_enabled
            and req.cache_first_body_messages
        ):
            cache_first_source = list(req.cache_first_body_messages)
            # Cache-first is the only attempt designed to reuse the prior normal
            # request prefix. Do not apply source_token_budget here: trimming the
            # source deletes bytes/tokens that existed in the prior request and
            # defeats SGLang radix-cache reuse. The reliable fallback remains
            # budget-trimmed above for latency when cache-first fails validation.
            cache_first = build_cache_first_tail_plan(
                cache_first_source,
                self.prompts,
                custom_instructions=req.custom_instructions,
                tools=req.tools,
            )
            if getattr(self.config, "reliable_fallback_enabled", False):
                return [cache_first, reliable]
            return [cache_first]

        return [reliable]

    async def _run_attempt(self, summary_id: str, plan: CompactPromptPlan):
        accumulated = ""
        usage = None
        t_start = time.monotonic()
        first_chunk_at = None
        visible_events: list[CompactEvent] = []

        async for chunk in self._chat_stream_async(
            messages=plan.messages,
            max_new_tokens=self.config.max_summary_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            request_id=summary_id,
            preserve_prompt_shape=(plan.shape == "cache_first_tail"),
            tools=plan.tools,
        ):
            delta = chunk.get("content") or ""
            if delta:
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                accumulated += delta
                if plan.stream_visible:
                    visible_events.append(CompactEvent.delta(summary_id, delta))
            if chunk.get("usage"):
                usage = chunk.get("usage")

        t_end = time.monotonic()
        prefill_ms = int((first_chunk_at - t_start) * 1000) if first_chunk_at else None
        decode_ms = int((t_end - first_chunk_at) * 1000) if first_chunk_at else None
        return accumulated, usage, visible_events, prefill_ms, decode_ms, int((t_end - t_start) * 1000)

    def _log_attempt_diagnostics(
        self,
        *,
        attempt_index: int,
        plan: CompactPromptPlan,
        req: CompactRequest,
        validation,
        snapshot,
        comparison,
        usage,
        prefill_ms: int | None,
        decode_ms: int | None,
        total_ms: int,
        completion_chars: int,
    ) -> None:
        if not self.config.diagnostics_enabled:
            return
        cached_tokens = extract_cached_tokens(usage)
        diag_path = "v2" if req.body_messages is not None else "v1"
        logger.info(
            "[compact-diag] "
            f"attempt={attempt_index} shape={plan.shape} path={diag_path} "
            f"validation={'passed' if validation.ok else 'failed'} "
            f"validation_reason={validation.reason} "
            f"prompt_chars={snapshot.prompt_chars} "
            f"prompt_tokens_sglang={(usage or {}).get('prompt_tokens')} "
            f"cached_tokens_sglang={cached_tokens} "
            f"completion_chars={completion_chars} "
            f"completion_tokens_sglang={(usage or {}).get('completion_tokens')} "
            f"prefill_ms={prefill_ms} decode_ms={decode_ms} total_ms={total_ms} "
            f"first_divergence_char={comparison.first_divergence_char if comparison else None} "
            f"max_summary_tokens={self.config.max_summary_tokens}"
        )

    def _log_route_plan(self, req: CompactRequest, plans: list[CompactPromptPlan]) -> None:
        if not self.config.diagnostics_enabled:
            return
        route = "->".join(plan.shape for plan in plans)
        skip_reason = next(
            (
                (plan.metadata or {}).get("cache_first_skipped_reason")
                for plan in plans
                if (plan.metadata or {}).get("cache_first_skipped_reason")
            ),
            None,
        )
        logger.info(
            "[compact-route] plan "
            f"mode={req.mode} trigger={req.trigger} route={route} "
            f"cache_first_skipped_reason={skip_reason} "
            f"source_token_budget={getattr(self.config, 'source_token_budget', None)}"
        )

    def _log_route_fallback(
        self,
        *,
        from_shape: str,
        to_shape: str | None,
        reason: str | None,
    ) -> None:
        if not self.config.diagnostics_enabled:
            return
        logger.info(
            "[compact-route] fallback "
            f"from_shape={from_shape} to_shape={to_shape} reason={reason or 'unknown'}"
        )

    def _log_route_result(
        self,
        *,
        plan: CompactPromptPlan,
        attempt_index: int,
        usage,
        comparison,
    ) -> None:
        if not self.config.diagnostics_enabled:
            return
        cached_tokens = extract_cached_tokens(usage)
        if cached_tokens is None:
            prefix_cache_hit = "unknown"
        elif cached_tokens > 0:
            prefix_cache_hit = "yes"
        else:
            prefix_cache_hit = "no"
        skip_reason = (plan.metadata or {}).get("cache_first_skipped_reason")
        logger.info(
            "[compact-route] result "
            f"selected_shape={plan.shape} fallback_used={attempt_index > 1} "
            f"cached_tokens_sglang={cached_tokens} prefix_cache_hit={prefix_cache_hit} "
            f"prompt_tokens_sglang={(usage or {}).get('prompt_tokens')} "
            f"common_prefix_chars={comparison.common_prefix_chars if comparison else None} "
            f"cache_first_skipped_reason={skip_reason}"
        )

    async def run_compact(
        self, req: CompactRequest
    ) -> AsyncGenerator[CompactEvent, None]:
        summary_id = CompactEvent.new_summary_id()
        yield CompactEvent.started(
            summary_message_id=summary_id,
            trigger=req.trigger,
            mode=req.mode,
        )

        try:
            plans = self._build_prompt_plans(req)
        except ValueError as exc:
            yield CompactEvent.failed(summary_id, "no_messages", str(exc))
            return
        except Exception as exc:
            logger.exception("compact prompt plan build failed")
            yield CompactEvent.failed(summary_id, "build_error", str(exc))
            return

        self._log_route_plan(req, plans)

        reference_snapshot = (
            make_prompt_snapshot("reference", req.reference_messages, tools=req.tools)
            if req.reference_messages
            else None
        )
        last_failure_reason = None

        for attempt_index, plan in enumerate(plans, start=1):
            logger.info(
                f"[compact] LLM call: shape={plan.shape} messages={len(plan.messages)} "
                f"max_new_tokens={self.config.max_summary_tokens} "
                f"temperature={self.config.temperature} top_p={self.config.top_p}"
            )
            try:
                if plan.stream_visible:
                    raw = ""
                    usage = None
                    t_start = time.monotonic()
                    first_chunk_at = None
                    async for chunk in self._chat_stream_async(
                        messages=plan.messages,
                        max_new_tokens=self.config.max_summary_tokens,
                        temperature=self.config.temperature,
                        top_p=self.config.top_p,
                        request_id=summary_id,
                        preserve_prompt_shape=(plan.shape == "cache_first_tail"),
            tools=plan.tools,
                    ):
                        delta = chunk.get("content") or ""
                        if delta:
                            if first_chunk_at is None:
                                first_chunk_at = time.monotonic()
                            raw += delta
                            yield CompactEvent.delta(summary_id, delta)
                        if chunk.get("usage"):
                            usage = chunk.get("usage")
                    t_end = time.monotonic()
                    prefill_ms = int((first_chunk_at - t_start) * 1000) if first_chunk_at else None
                    decode_ms = int((t_end - first_chunk_at) * 1000) if first_chunk_at else None
                    total_ms = int((t_end - t_start) * 1000)
                else:
                    raw, usage, _visible_events, prefill_ms, decode_ms, total_ms = await self._run_attempt(summary_id, plan)
            except Exception as exc:
                err_repr = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    f"[compact] LLM call failed during compact "
                    f"(shape={plan.shape}, messages={len(plan.messages)}, "
                    f"max_new_tokens={self.config.max_summary_tokens})"
                )
                if plan.shape == "cache_first_tail" and attempt_index < len(plans):
                    last_failure_reason = "cache_first_exception"
                    self._log_route_fallback(
                        from_shape=plan.shape,
                        to_shape=plans[attempt_index].shape if attempt_index < len(plans) else None,
                        reason=last_failure_reason,
                    )
                    continue
                yield CompactEvent.failed(summary_id, "llm_unreachable", err_repr)
                return

            cleaned = format_compact_summary(raw)
            snapshot = make_prompt_snapshot(plan.shape, plan.messages, tools=plan.tools)
            comparison = compare_prompt_snapshots(reference_snapshot, snapshot)
            validation = validate_compact_summary(cleaned, snapshot.prompt_chars, self.config)
            cached_tokens = extract_cached_tokens(usage)

            self._log_attempt_diagnostics(
                attempt_index=attempt_index,
                plan=plan,
                req=req,
                validation=validation,
                snapshot=snapshot,
                comparison=comparison,
                usage=usage,
                prefill_ms=prefill_ms,
                decode_ms=decode_ms,
                total_ms=total_ms,
                completion_chars=len(raw),
            )

            if not validation.ok:
                last_failure_reason = validation.reason
                if plan.shape == "cache_first_tail":
                    self._log_route_fallback(
                        from_shape=plan.shape,
                        to_shape=plans[attempt_index].shape if attempt_index < len(plans) else None,
                        reason=last_failure_reason,
                    )
                    continue
                yield CompactEvent.failed(summary_id, validation.reason or "validation_failed")
                return

            if not plan.stream_visible:
                yield CompactEvent.delta(summary_id, cleaned)

            self._log_route_result(
                plan=plan,
                attempt_index=attempt_index,
                usage=usage,
                comparison=comparison,
            )

            if self._apply_compaction is not None:
                try:
                    self._apply_compaction(req, summary_id, cleaned)
                except Exception as exc:
                    logger.exception("apply_compaction failed")
                    yield CompactEvent.failed(summary_id, "apply_error", f"{type(exc).__name__}: {exc}")
                    return

            yield CompactEvent.completed(
                summary_id,
                cleaned,
                metadata={
                    "trigger": req.trigger,
                    "mode": req.mode,
                    "prompt_shape": plan.shape,
                    "fallback_used": attempt_index > 1,
                    "cached_tokens": cached_tokens,
                    "prompt_tokens": (usage or {}).get("prompt_tokens"),
                    "completion_tokens": (usage or {}).get("completion_tokens"),
                    "same_as_reference": comparison.same_hash if comparison else None,
                    "common_prefix_chars": comparison.common_prefix_chars if comparison else None,
                    "first_divergence_char": comparison.first_divergence_char if comparison else None,
                    **(plan.metadata or {}),
                },
            )
            return

        yield CompactEvent.failed(summary_id, last_failure_reason or "validation_failed")
