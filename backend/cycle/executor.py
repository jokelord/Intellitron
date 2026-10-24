"""
Cycle 执行器

解释和执行 Cycle 步骤序列，通过 SSE 事件流报告进度。
"""

import asyncio
import hashlib
import inspect
import json
import logging
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import AsyncGenerator

from backend.cycle.models import CycleStartRequest, CycleStepRequest, ScheduledMessageRequest
from backend.cycle.run_registry import CycleRunRegistry, CycleRunConflict, CycleRunNotFound
from backend.cycle.scheduler import ScheduledTrack, validate_timezone
from backend.agent.tool_configuration import AgentToolConfigurationRequest

logger = logging.getLogger(__name__)


# Cycle-owned Agent contexts must never reuse the browser/page Agent ID.  UUID5
# keeps a shared Cycle context stable while satisfying PathPolicy's canonical
# new-session rule; non-sharing message steps get independent UUID4 contexts.
_CYCLE_AGENT_SESSION_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "llm-deployment:cycle-agent-session:v1",
)


class CycleExecutor:
    """Cycle 序列执行器"""

    def __init__(
        self,
        agent_runner,
        camera_service,
        max_depth: int = 5,
        max_duration: int = 3600,
        *,
        timeout_policy: str = "exempt_cycle_run",
        scheduled_track_max_duration_seconds: int = 2592000,
        max_scheduled_message_days: int = 30,
        event_journal_retention_seconds: int = 86400,
        event_journal_max_records: int = 20000,
        heartbeat_seconds: float = 15,
        now=None,
    ):
        self._agent_runner = agent_runner
        self._camera_service = camera_service
        self._max_depth = max_depth
        self._max_duration = max_duration
        if timeout_policy not in ("exempt_cycle_run", "legacy_bounded"):
            raise ValueError("unsupported Cycle timeout policy")
        self._timeout_policy = timeout_policy
        self._scheduled_horizon = scheduled_track_max_duration_seconds
        self._max_scheduled_days = max_scheduled_message_days
        self._heartbeat_seconds = heartbeat_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self.registry = CycleRunRegistry(retention_seconds=event_journal_retention_seconds, max_records=event_journal_max_records)
        self._cancel_events: dict[str, asyncio.Event] = {}
        # 记录每个 cycle session 当前使用的 agent session_id，用于停止时中止 agent
        self._agent_session_ids: dict[str, str | None] = {}
        # 累计每个 cycle session 已处理过的 message step 数量（跨嵌套 cycle 共享）。
        # 与 cycle_min_steps_between_compacts 配合，避免在长 cycle 中 thrashing。
        self._message_step_counter: dict[str, int] = {}
        # 记录每个 cycle session 上一次 auto-compact 触发时的 _message_step_counter 值。
        # 节流判断：current_count - last_compact_count >= min_steps 时才检查 compact。
        self._last_compact_cycle_step: dict[str, int] = {}

    @staticmethod
    def _derive_agent_session_id(
        cycle_session_id: str,
        *,
        share_context: bool,
    ) -> str:
        if share_context:
            return str(
                uuid.uuid5(
                    _CYCLE_AGENT_SESSION_NAMESPACE,
                    cycle_session_id,
                )
            )
        return str(uuid.uuid4())

    def stop(self, session_id: str) -> bool:
        """停止指定 session 的 cycle 执行

        同时中止正在执行的 Agent 推理和 SGLang GPU 推理，
        避免停止 cycle 后 GPU 仍在空跑未完成的请求。
        """
        event = self._cancel_events.get(session_id)
        if not event:
            return False

        record = self.registry._records.get(session_id)
        if record is not None:
            record.abandoned_scheduled_due()
        event.set()

        # 中止正在执行的 Agent session（如果有的话）
        if record is not None:
            record.wake.set()
            if record.scheduler_task is not None:
                record.scheduler_task.cancel()
        agent_sid = record.active_agent_session_id if record else self._agent_session_ids.get(session_id)
        request_id = None
        if agent_sid and self._agent_runner:
            try:
                request_id = self._agent_runner.get_current_request_id(agent_sid)
            except Exception as exc:
                logger.warning("Cycle stop: request lookup failed: %s", exc)
        if agent_sid and self._agent_runner:
            try:
                self._agent_runner.abort_session(agent_sid)
                logger.info(
                    "Cycle stop: 已中止 agent session %s (cycle=%s)",
                    agent_sid,
                    session_id,
                )
            except Exception as e:
                logger.warning("Cycle stop: 中止 agent session 失败: %s", e)

        # 定向中止当前 Agent 正在执行的底层推理请求，避免 abort_all 误伤其它请求，
        # 也避免在 SGLang streaming cleanup 边界触发上游异常日志。
        if agent_sid and self._agent_runner:
            try:
                if request_id:
                    from backend.config import get_config
                    from backend.llm_service import abort_request_by_id

                    config = get_config()
                    sglang_url = f"http://{config.sglang.host}:{config.sglang.port}"
                    abort_request_by_id(
                        request_id,
                        sglang_url,
                        fallback_to_abort_all=False,
                    )
                    logger.info(
                        "Cycle stop: 已定向中止 SGLang request %s (agent=%s, cycle=%s)",
                        request_id,
                        agent_sid,
                        session_id,
                    )
                else:
                    logger.info(
                        "Cycle stop: agent session %s 无 active request_id，依赖 session abort",
                        agent_sid,
                    )
            except Exception as e:
                logger.warning("Cycle stop: SGLang request abort 失败: %s", e)

        return True

    def ensure_run(self, request: CycleStartRequest):
        """Validate/admit synchronously, before HTTP response headers are sent."""
        request = request.model_copy(deep=True)
        if request.cycle_run_id is not None and request.session_id is not None and request.cycle_run_id != request.session_id:
            raise CycleRunConflict("cycle_run_identity_mismatch")
        run_id = request.cycle_run_id or request.session_id or str(uuid.uuid4())
        if not run_id.strip():
            raise ValueError("cycle_run_id must not be empty")
        if not request.agent_mode:
            request.tool_configuration = None
        body = request.model_dump(mode="json", exclude={"session_id", "cycle_run_id"})
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            existing = self.registry.resolve(run_id)
        except CycleRunNotFound:
            existing = None
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise CycleRunConflict("cycle_run_fingerprint_mismatch")
            return existing
        self._validate_steps(request.steps)
        started_at = self._now()
        zone = validate_timezone(request.timezone or "UTC")
        if any(item.day_count > self._max_scheduled_days for item in request.scheduled_messages):
            raise ValueError("scheduled_day_count_exceeded")
        # A throwaway finite planner validates the entire requested horizon,
        # including DST skipped dates, without creating any background work.
        validation_track = ScheduledTrack(request.scheduled_messages, timezone=zone, started_at=started_at)
        validation_track.collect_due(started_at + timedelta(seconds=self._scheduled_horizon))
        if validation_track.next_deadline is not None:
            raise ValueError("scheduled_horizon_exceeded")
        record, _ = self.registry.admit(run_id, fingerprint)
        record.request = request
        record.schedule = ScheduledTrack(request.scheduled_messages, timezone=zone, started_at=started_at)
        record.scheduled_expires_at = started_at.astimezone(UTC) + timedelta(seconds=self._scheduled_horizon)
        record.scheduled_remaining = sum(item.day_count for item in request.scheduled_messages)
        # A fresh admission, even after backend restart, must never reuse an
        # old Agent context derived solely from the public Cycle ID.
        record.agent_identity_seed = str(uuid.uuid4())
        self._cancel_events[run_id] = record.cancel_event
        record.producer_task = asyncio.create_task(self._produce(record), name=f"cycle:{run_id}")
        return record

    def _validate_steps(self, steps, depth=0):
        for step in steps:
            if step.type == "wait" and (step.wait is None or not math.isfinite(step.wait) or step.wait < 0):
                raise ValueError("invalid_cycle_wait")
            if step.type == "cycle":
                if depth + 1 > self._max_depth:
                    raise ValueError("cycle_depth_exceeded")
                if step.cycle_count is None or step.cycle_count < 1:
                    raise ValueError("invalid_cycle_count")
                self._validate_steps(step.cycle_steps or [], depth + 1)

    async def shutdown(self):
        records = tuple(self.registry._records.values())
        for record in records:
            if record.finished_at is None:
                self.stop(record.cycle_run_id)
        await asyncio.gather(*(r.producer_task for r in records if r.producer_task), return_exceptions=True)

    def _collect_due(self, record):
        if record.scheduled_remaining and self._now().astimezone(UTC) > record.scheduled_expires_at:
            record.schedule_expired = True
            record.wake.set()
            return
        for occurrence in record.schedule.collect_due(self._now()):
            record.publish({"type": "scheduled_message_queued", **self._occurrence_metadata(occurrence),
                "queued_at": self._now().isoformat(),
                "reason": "ordinary_dispatch_busy" if record.active_track == "ordinary" else "boundary_pending"})
            record.wake.set()

    async def _scheduler_clock(self, record):
        while not record.cancel_event.is_set():
            self._collect_due(record)
            if record.schedule_expired:
                return
            deadline = record.schedule.next_deadline
            if deadline is None:
                record.wake.set()
                return
            # Periodic wall-clock recheck handles clock corrections; only one
            # timer exists per run. It never owns capture/admission/LLM work.
            delay = max(0, (deadline.astimezone(UTC) - self._now().astimezone(UTC)).total_seconds())
            try:
                await asyncio.wait_for(record.cancel_event.wait(), min(delay, self._heartbeat_seconds or 15))
            except asyncio.TimeoutError:
                pass

    def _occurrence_metadata(self, occurrence):
        return {"track": "scheduled", "schedule_id": occurrence.schedule_id,
            "occurrence_index": occurrence.occurrence_index, "scheduled_for": occurrence.scheduled_for.isoformat(),
            "lateness_seconds": max(0, (self._now().astimezone(UTC) - occurrence.scheduled_for.astimezone(UTC)).total_seconds())}

    async def _drain_due(self, record):
        while True:
            if record.cancel_event.is_set():
                raise _CycleCancelled()
            self._collect_due(record)
            if record.schedule_expired:
                raise _CycleExpired()
            due = record.schedule.drain_due()
            if not due:
                return
            for occurrence in due:
                if record.cancel_event.is_set():
                    raise _CycleCancelled()
                self._collect_due(record)
                if record.schedule_expired:
                    raise _CycleExpired()
                metadata = self._occurrence_metadata(occurrence)
                step = CycleStepRequest(type="message", message=occurrence.item.message, photo=occurrence.item.photo)
                yield self._sse_event("step_begin", {**metadata, "path": [], "step_type": "message", "detail": self._step_detail(step)})
                started = time.monotonic()
                async for frame in self._dispatch_message(record, step, [], metadata):
                    yield frame
                record.scheduled_remaining -= 1
                if not record.scheduled_remaining:
                    record.schedule_expired = False
                yield self._sse_event("step_end", {**self._occurrence_metadata(occurrence), "path": [], "duration": time.monotonic() - started})

    async def _dispatch_message(self, record, step, path, metadata):
        """Sole message arbiter: called only by the sequential producer."""
        if record.cancel_event.is_set():
            raise _CycleCancelled()
        request = record.request
        record.active_track = metadata.get("track")
        try:
            async for frame in self._execute_message(record.cycle_run_id, step, path,
                    request.agent_mode, request.share_context, request.tool_configuration,
                    show_agent_steps=request.show_agent_steps,
                    enable_thinking=request.enable_thinking,
                    enable_local_memory=request.enable_local_memory):
                data = json.loads(frame.strip()[6:])
                yield self._sse_event(data.pop("type"), {**data, **metadata, "path": path})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if record.cancel_event.is_set():
                raise _CycleCancelled() from exc
            yield self._sse_event("scheduled_message_failed" if metadata["track"] == "scheduled" else "step_failed", {
                **metadata, "path": path, "error": str(exc), "error_category": type(exc).__name__,
                "scope": "dispatch", "continuing": True,
            })
        finally:
            record.active_track = None
            self._message_step_counter[record.cycle_run_id] = self._message_step_counter.get(record.cycle_run_id, 0) + 1

    async def start(
        self,
        session_id: str,
        steps: list[CycleStepRequest],
        agent_mode: bool = False,
        share_context: bool = True,
        tool_configuration: AgentToolConfigurationRequest | None = None,
        scheduled_messages: list[ScheduledMessageRequest] | None = None,
        timezone: str | None = None,
        after_sequence: int = 0,
    ) -> AsyncGenerator[str, None]:
        """Compatibility entry point; subscriber cancellation never owns work."""
        try:
            record = self.ensure_run(CycleStartRequest(cycle_run_id=session_id, steps=steps,
                agent_mode=agent_mode, share_context=share_context, tool_configuration=tool_configuration,
                scheduled_messages=scheduled_messages or [], timezone=timezone))
        except ValueError as exc:
            yield self._sse_event("cycle_error", {"error": str(exc), "path": []})
            yield "data: [DONE]\n\n"
            return
        subscription = record.events(after_sequence, heartbeat_seconds=self._heartbeat_seconds)
        try:
            async for frame in subscription:
                yield frame
        finally:
            await subscription.aclose()

    async def _produce(self, record):
        session_id = record.cycle_run_id
        request = record.request
        cancel_event = record.cancel_event
        start_time = time.monotonic()
        terminal = "completed"
        terminal_data = {}
        try:
            record.publish({"type": "cycle_started", "session_id": session_id,
                "total_steps": len(request.steps), "timezone": request.timezone,
                "request_fingerprint": record.request_fingerprint,
                "show_agent_steps": request.show_agent_steps})
            record.scheduler_task = asyncio.create_task(self._scheduler_clock(record), name=f"cycle-clock:{session_id}")
            async for event in self._execute_steps(
                session_id=session_id,
                steps=request.steps,
                path=[],
                depth=0,
                agent_mode=request.agent_mode,
                share_context=request.share_context,
                cancel_event=cancel_event,
                start_time=start_time,
                tool_configuration=request.tool_configuration,
            ):
                record.publish(json.loads(event.strip()[6:]))
            while True:
                record.wake.clear()
                async for event in self._drain_due(record):
                    record.publish(json.loads(event.strip()[6:]))
                if record.schedule.is_complete:
                    break
                await record.wake.wait()
            if cancel_event.is_set():
                raise _CycleCancelled()
        except _CycleCancelled:
            terminal, terminal_data = "stopped", {"reason": "用户取消"}
        except _CycleExpired:
            terminal, terminal_data = "expired", {"reason": "scheduled_horizon_exceeded", "scope": "scheduled_track"}
        except asyncio.CancelledError:
            terminal, terminal_data = "stopped", {"reason": "backend_shutdown"}
        except Exception as e:
            logger.error(f"Cycle 执行异常: {e}", exc_info=True)
            terminal, terminal_data = "error", {"error": str(e), "path": getattr(e, "path", []), "scope": "cycle_run"}
        finally:
            if record.scheduler_task is not None:
                record.scheduler_task.cancel()
                await asyncio.gather(record.scheduler_task, return_exceptions=True)
            store = getattr(self._agent_runner, "sessions", None)
            if hasattr(store, "unpin_cycle_owner"):
                store.unpin_cycle_owner(session_id)
            self._cancel_events.pop(session_id, None)
            self._agent_session_ids.pop(session_id, None)
            self._message_step_counter.pop(session_id, None)
            self._last_compact_cycle_step.pop(session_id, None)
            record.finish(terminal, {"type": f"cycle_{terminal}", "total_duration": round(time.monotonic() - start_time, 2), **terminal_data})

    async def _execute_steps(
        self,
        session_id: str,
        steps: list[CycleStepRequest],
        path: list[int],
        depth: int,
        agent_mode: bool,
        share_context: bool,
        cancel_event: asyncio.Event,
        start_time: float,
        tool_configuration: AgentToolConfigurationRequest | None = None,
    ) -> AsyncGenerator[str, None]:
        """递归执行步骤列表

        Auto-compact 触发位置（"最晚那一个相邻消息发送时"）：
        在每个 message step 进入 agent.run() 之前判断并按需触发 compact，
        而不是在上一个 message step 退出之后。语义上：compact 是"新消息
        被发送出去"这一动作的前置条件——保证上一轮 tool chain 不被中断，
        同时确保这一轮 LLM 看到的是已 compact 后的 session。
        """
        for i, step in enumerate(steps):
            if cancel_event.is_set():
                raise _CycleCancelled()
            record = self.registry.resolve(session_id)
            async for event in self._drain_due(record):
                yield event

            # 在 message step 进入 agent.run() 之前 auto-compact（节流）。
            # i == 0 跳过：session 刚创建，_should_auto_compact_session 会自然返回 False。
            # 嵌套 cycle 也会复用同一个 session_id / counter，所以节流在整棵 cycle 树
            # 维度上生效，而不是只对当前深度的 step 计数。
            if (
                i > 0
                and step.type == "message"
                and share_context
                and self._agent_runner is not None
                and getattr(self._agent_runner, "compact_orchestrator", None) is not None
            ):
                from backend.config import get_config
                min_steps = get_config().compact.cycle_min_steps_between_compacts
                current_count = self._message_step_counter.get(session_id, 0)
                last_compact_count = self._last_compact_cycle_step.get(
                    session_id, -(min_steps + 1)
                )
                if current_count - last_compact_count >= min_steps:
                    agent_sid = self._agent_session_ids.get(session_id)
                    if agent_sid:
                        session = self._agent_runner.sessions.get(agent_sid)
                        if session and self._agent_runner._should_auto_compact_session(session):
                            async for ev in self._agent_runner._run_compact_then_continue(
                                session, trigger="auto"
                            ):
                                yield ev
                            self._last_compact_cycle_step[session_id] = current_count

            elapsed = time.monotonic() - start_time
            if self._timeout_policy == "legacy_bounded" and elapsed > self._max_duration:
                raise _CycleCancelled()

            # Compact can take time too. Resolve any tie immediately before
            # admitting the next ordinary step.
            async for event in self._drain_due(record):
                yield event

            current_path = path + [i]
            step_start = time.monotonic()

            yield self._sse_event(
                "step_begin",
                {
                    "path": current_path,
                    "step_type": step.type,
                    "detail": self._step_detail(step),
                    "track": "ordinary",
                },
            )

            if step.type == "wait":
                await self._execute_wait(step, cancel_event)

            elif step.type == "message":
                async for event in self._dispatch_message(record, step, current_path, {"track": "ordinary"}):
                    yield event

            elif step.type == "cycle":
                if depth + 1 > self._max_depth:
                    raise _CycleError(
                        f"嵌套深度超过限制 ({self._max_depth})",
                        current_path,
                    )
                for iteration in range(step.cycle_count or 1):
                    if cancel_event.is_set():
                        raise _CycleCancelled()
                    async for event in self._execute_steps(
                        session_id=session_id,
                        steps=step.cycle_steps or [],
                        path=current_path + [iteration],
                        depth=depth + 1,
                        agent_mode=agent_mode,
                        share_context=share_context,
                        cancel_event=cancel_event,
                        start_time=start_time,
                        tool_configuration=tool_configuration,
                    ):
                        yield event

            step_duration = time.monotonic() - step_start
            yield self._sse_event(
                "step_end",
                {
                    "path": current_path,
                    "duration": round(step_duration, 2),
                    "track": "ordinary",
                },
            )
            async for event in self._drain_due(record):
                yield event

    async def _execute_wait(
        self,
        step: CycleStepRequest,
        cancel_event: asyncio.Event,
    ):
        """执行等待步骤（可被取消）"""
        wait_time = step.wait or 0
        try:
            await asyncio.wait_for(
                cancel_event.wait(),
                timeout=wait_time,
            )
            raise _CycleCancelled()
        except asyncio.TimeoutError:
            pass  # Normal completion

    async def _execute_message(
        self,
        session_id: str,
        step: CycleStepRequest,
        path: list[int],
        agent_mode: bool,
        share_context: bool,
        tool_configuration: AgentToolConfigurationRequest | None = None,
        *,
        show_agent_steps: bool = False,
        enable_thinking: bool = False,
        enable_local_memory: bool = False,
    ) -> AsyncGenerator[str, None]:
        """执行消息步骤（拍照 + 发送 LLM）"""
        images = None
        record = self.registry.resolve(session_id)

        # 拍照
        if step.photo:
            logger.info("Cycle 步骤 %s: 准备从摄像头 %s 拍照", path, step.photo)
            # OpenCV warm-up/read is synchronous; do not stall the single
            # scheduler clock or SSE heartbeats while the arbiter captures.
            results = await asyncio.to_thread(self._camera_service.capture, step.photo)
            if record.cancel_event.is_set():
                raise _CycleCancelled()
            for r in results:
                if "image_base64" in r:
                    b64_str = r["image_base64"]
                    # 计算实际图片数据大小（去掉 data URI 前缀）
                    data_part = b64_str.split(",", 1)[-1] if "," in b64_str else b64_str
                    logger.info(
                        "Cycle 步骤 %s: 摄像头 %d 拍照成功, base64 长度=%d 字节",
                        path,
                        r["camera_index"],
                        len(data_part),
                    )
                    yield self._sse_event(
                        "photo_captured",
                        {
                            "camera_index": r["camera_index"],
                            "image_base64": r["image_base64"],
                        },
                    )
                    if images is None:
                        images = []
                    images.append(r["image_base64"])
                elif "error" in r:
                    logger.warning(
                        "Cycle 步骤 %s: 摄像头 %d 拍照失败: %s",
                        path,
                        r["camera_index"],
                        r["error"],
                    )
                    yield self._sse_event(
                        "photo_captured",
                        {
                            "camera_index": r["camera_index"],
                            "error": r["error"],
                        },
                    )

        # 发送 LLM
        num_photos = len(images) if images else 0
        logger.info(
            "Cycle 步骤 %s: 发送 LLM, agent_mode=%s, 图片数=%d",
            path,
            agent_mode,
            num_photos,
        )
        message = step.message or ""
        llm_session_id = self._derive_agent_session_id(
            record.agent_identity_seed,
            share_context=share_context,
        )
        full_content = ""

        if agent_mode and self._agent_runner:
            record.agent_session_ids.add(llm_session_id)
            store = getattr(self._agent_runner, "sessions", None)
            if hasattr(store, "pin_cycle_session"):
                store.pin_cycle_session(session_id, llm_session_id)
            # Admission resolves the exact ID and freezes one snapshot before
            # the Agent is constructed.  share_context=False deliberately gets
            # a fresh canonical ID for each message step.
            admission = self._agent_runner.stage_agent_request_admission(
                llm_session_id,
                None,
                None,
                len(images or ()),
                tool_configuration,
            )
            self._agent_session_ids[session_id] = admission.session_id
            record.active_agent_session_id = admission.session_id
            scopes = set()
            def on_registered(_stage, scope_ids):
                scopes.update(scope_ids)
            stream = self._agent_runner.run_stream(
                admission.session_id, message, None, images=images,
                admission_stage=admission, on_run_registered=on_registered,
                enable_thinking=enable_thinking,
                enable_local_memory=enable_local_memory,
            )
            controlled_stream = self._stream_until_stopped(stream, record.cancel_event)
            try:
                async for chunk in controlled_stream:
                    if chunk.strip().startswith("data: "):
                        data_str = chunk.strip()[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                            if data.get("type") == "session_start":
                                real_agent_sid = data.get("session_id")
                                if real_agent_sid:
                                    self._agent_session_ids[session_id] = real_agent_sid
                                    record.active_agent_session_id = real_agent_sid
                                    record.agent_session_ids.add(real_agent_sid)
                                    if hasattr(store, "pin_cycle_session"):
                                        store.pin_cycle_session(session_id, real_agent_sid)
                                    logger.info(
                                        "Cycle 步骤 %s: 捕获 agent session_id=%s",
                                        path,
                                        real_agent_sid,
                                    )
                            elif data.get("type") == "error":
                                raise RuntimeError(data.get("error", "Agent dispatch failed"))
                            elif data.get("type") == "content":
                                full_content += data.get("content", "")
                                yield self._sse_event(
                                    "llm_chunk",
                                    {
                                        "content": data.get("content", ""),
                                        "role": "assistant",
                                    },
                                )
                            elif (data.get("type") == "timeline_event"
                                and data.get("event", {}).get("kind") == "local_memory_retrieved") or (show_agent_steps and data.get("type") in {
                                "agent_step",
                                "orchestration_plan",
                                "orchestration_update",
                                "orchestration_done",
                                "timeline_event",
                            }):
                                # Keep the Agent event type and payload intact;
                                # _dispatch_message adds Cycle track/path and
                                # scheduled-occurrence metadata before journal
                                # publication.  A nested timeline_event keeps
                                # its existing {"event": {...}} shape so the
                                # frontend can pass it to its normal reducer.
                                yield self._sse_event(data["type"], data)
                        except json.JSONDecodeError:
                            pass
            finally:
                # Idempotent for accepted stages, but essential when generator
                # startup fails or is cancelled before the registration point.
                cleanup_task = asyncio.create_task(self._finish_agent_dispatch(
                    record, admission, scopes, controlled_stream, stream,
                ))
                cancelled = False
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        # Worker scopes own native resources. Even repeated
                        # cancellation must not release Cycle pins early.
                        cancelled = True
                        self.stop(session_id)
                cleanup_task.result()
                if cancelled:
                    raise asyncio.CancelledError()
        else:
            # 非 agent 模式暂未实现
            yield self._sse_event(
                "llm_chunk",
                {
                    "content": f"[非 Agent 模式暂未实现: {message}]",
                    "role": "assistant",
                },
            )
            full_content = f"[非 Agent 模式暂未实现: {message}]"

        yield self._sse_event(
            "llm_done",
            {
                "content": full_content,
                "path": path,
            },
        )

    async def _finish_agent_dispatch(self, record, admission, scopes, controlled_stream, stream):
        capture = getattr(self._agent_runner, "capture_agent_request_run_scopes", None)
        if callable(capture):
            scopes.update(capture(admission))
        try:
            await controlled_stream.aclose()
            await stream.aclose()
        finally:
            if callable(capture):
                scopes.update(capture(admission))
            cleanup = getattr(self._agent_runner, "wait_for_agent_request_run_cleanup", None)
            if callable(cleanup):
                result = cleanup(admission, frozenset(scopes))
                if inspect.isawaitable(result):
                    await result
            record.active_agent_session_id = None
            self._agent_runner.rollback_agent_request_stage(admission)

    async def _stream_until_stopped(self, stream, cancel_event):
        stopped = asyncio.create_task(cancel_event.wait())
        pending = None
        try:
            while not cancel_event.is_set():
                pending = asyncio.create_task(anext(stream))
                done, _ = await asyncio.wait((pending, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    raise _CycleCancelled()
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield chunk
            raise _CycleCancelled()
        finally:
            stopped.cancel()
            if pending is not None:
                pending.cancel()
            await asyncio.gather(stopped, *((pending,) if pending else ()), return_exceptions=True)

    def _step_detail(self, step: CycleStepRequest) -> dict:
        """获取步骤详情"""
        if step.type == "wait":
            return {"seconds": step.wait}
        elif step.type == "message":
            return {
                "message": step.message,
                "photo": step.photo,
            }
        elif step.type == "cycle":
            return {
                "count": step.cycle_count,
                "steps_count": len(step.cycle_steps) if step.cycle_steps else 0,
            }
        return {}

    @staticmethod
    def _sse_event(event_type: str, data: dict) -> str:
        """构造 SSE 事件字符串"""
        payload = {"type": event_type, **data}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class _CycleCancelled(Exception):
    """Cycle 被取消"""

    pass


class _CycleExpired(Exception):
    """The finite scheduled window ended before outstanding work dispatched."""


class _CycleError(Exception):
    """Cycle 执行错误"""

    def __init__(self, message: str, path: list[int]):
        super().__init__(message)
        self.path = path
