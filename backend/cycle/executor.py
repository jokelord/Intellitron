"""
Cycle 执行器

解释和执行 Cycle 步骤序列，通过 SSE 事件流报告进度。
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

from backend.cycle.models import CycleStepRequest

logger = logging.getLogger(__name__)


class CycleExecutor:
    """Cycle 序列执行器"""

    def __init__(
        self,
        agent_runner,
        camera_service,
        max_depth: int = 5,
        max_duration: int = 3600,
    ):
        self._agent_runner = agent_runner
        self._camera_service = camera_service
        self._max_depth = max_depth
        self._max_duration = max_duration
        self._cancel_events: dict[str, asyncio.Event] = {}

    def stop(self, session_id: str) -> bool:
        """停止指定 session 的 cycle 执行"""
        event = self._cancel_events.get(session_id)
        if event:
            event.set()
            return True
        return False

    async def start(
        self,
        session_id: str,
        steps: list[CycleStepRequest],
        agent_mode: bool = False,
        share_context: bool = True,
    ) -> AsyncGenerator[str, None]:
        """执行 cycle 序列，生成 SSE 事件流"""
        cancel_event = asyncio.Event()
        self._cancel_events[session_id] = cancel_event
        start_time = time.monotonic()

        try:
            yield self._sse_event(
                "cycle_started",
                {
                    "session_id": session_id,
                    "total_steps": len(steps),
                },
            )

            async for event in self._execute_steps(
                session_id=session_id,
                steps=steps,
                path=[],
                depth=0,
                agent_mode=agent_mode,
                share_context=share_context,
                cancel_event=cancel_event,
                start_time=start_time,
            ):
                yield event

            if not cancel_event.is_set():
                total_duration = time.monotonic() - start_time
                yield self._sse_event(
                    "cycle_completed",
                    {
                        "total_duration": round(total_duration, 2),
                    },
                )

        except _CycleCancelled:
            yield self._sse_event("cycle_stopped", {"reason": "用户取消"})
        except _CycleError as e:
            yield self._sse_event(
                "cycle_error",
                {
                    "error": str(e),
                    "path": e.path,
                },
            )
        except Exception as e:
            logger.error(f"Cycle 执行异常: {e}", exc_info=True)
            yield self._sse_event(
                "cycle_error",
                {
                    "error": str(e),
                    "path": [],
                },
            )
        finally:
            self._cancel_events.pop(session_id, None)
            yield "data: [DONE]\n\n"

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
    ) -> AsyncGenerator[str, None]:
        """递归执行步骤列表"""
        for i, step in enumerate(steps):
            if cancel_event.is_set():
                raise _CycleCancelled()

            elapsed = time.monotonic() - start_time
            if elapsed > self._max_duration:
                raise _CycleCancelled()

            current_path = path + [i]
            step_start = time.monotonic()

            yield self._sse_event(
                "step_begin",
                {
                    "path": current_path,
                    "step_type": step.type,
                    "detail": self._step_detail(step),
                },
            )

            if step.type == "wait":
                await self._execute_wait(step, cancel_event)

            elif step.type == "message":
                async for event in self._execute_message(
                    session_id=session_id,
                    step=step,
                    path=current_path,
                    agent_mode=agent_mode,
                    share_context=share_context,
                ):
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
                    ):
                        yield event

            step_duration = time.monotonic() - step_start
            yield self._sse_event(
                "step_end",
                {
                    "path": current_path,
                    "duration": round(step_duration, 2),
                },
            )

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
    ) -> AsyncGenerator[str, None]:
        """执行消息步骤（拍照 + 发送 LLM）"""
        images = None

        # 拍照
        if step.photo:
            logger.info("Cycle 步骤 %s: 准备从摄像头 %s 拍照", path, step.photo)
            results = self._camera_service.capture(step.photo)
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
        # 当有摄像头拍照成功时，在消息前追加提示，让 VLM 直接分析内联图片
        # 而不是尝试使用 load_images 工具去加载文件（模型容易幻觉出文件路径）
        if num_photos > 0:
            photo_hint = (
                f"[本消息附带 {num_photos} 张通过摄像头实时拍摄的照片，"
                f"已直接包含在此消息中。请直接分析这些照片内容，"
                f"不要使用 load_images 工具。]\n\n"
            )
            message = photo_hint + message
        llm_session_id = session_id if share_context else None
        full_content = ""

        if agent_mode and self._agent_runner:
            async for chunk in self._agent_runner.run_stream(
                llm_session_id, message, None, images=images
            ):
                if chunk.strip().startswith("data: "):
                    data_str = chunk.strip()[6:]
                    if data_str == "[DONE]":
                        continue
                    try:
                        data = json.loads(data_str)
                        if data.get("type") == "content":
                            full_content += data.get("content", "")
                            yield self._sse_event(
                                "llm_chunk",
                                {
                                    "content": data.get("content", ""),
                                    "role": "assistant",
                                },
                            )
                    except json.JSONDecodeError:
                        pass
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


class _CycleError(Exception):
    """Cycle 执行错误"""

    def __init__(self, message: str, path: list[int]):
        super().__init__(message)
        self.path = path
