"""
Agent 调度与会话管理 (Agent Runner)

负责构建 smolagents Agent、管理 Session 生命周期、
以及将 Agent 步骤转换为 SSE 事件流。
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator

from smolagents import CodeAgent, Tool, ToolCallingAgent, tool
from smolagents.agents import ActionOutput, FinalAnswerStep, MultiStepAgent
from smolagents.memory import ActionStep, TaskStep
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole
from smolagents.monitoring import LogLevel

from backend.agent.local_llm_model import LocalLLMModel
from backend.config import AppConfig
from backend.llm_service import LLMService
from backend.tools.file_ops import edit_file, read_file, write_file
from backend.tools.skill_ops import list_skills, read_skill
from backend.tools.sys_ops import exec_command
from backend.tools.sys_ops import check_process

logger = logging.getLogger(__name__)


# ============================================================
# smolagents @tool 注册：将纯函数包装为 smolagents Tool 对象
# ============================================================
# 注意：smolagents 的 @tool 装饰器必须可以通过 inspect.getsource
# 获取源码，因此这些函数必须定义在文件中（不能在 -c 或 exec 中）。


@tool
def agent_read_file(file_path: str, start_line: int = 0, end_line: int = 0) -> str:
    """读取文件内容，每行前附行号，方便后续定位和编辑。
    Args:
        file_path: 文件的绝对或相对路径
        start_line: 起始行号（1-based），0 表示从第一行开始
        end_line: 结束行号（1-based），0 表示到最后一行
    """
    sl = start_line if start_line > 0 else None
    el = end_line if end_line > 0 else None
    return read_file(file_path, sl, el)


@tool
def agent_write_file(file_path: str, content: str) -> str:
    """写入文件（覆盖已有内容），自动创建父目录。
    Args:
        file_path: 目标文件路径
        content: 要写入的完整内容
    """
    return write_file(file_path, content)


@tool
def agent_edit_file(file_path: str, search_text: str, replace_text: str) -> str:
    """精确编辑文件：查找 search_text 并替换为 replace_text。
    支持精确匹配、缩进容错和模糊匹配。无需重写整个文件，只需提供要修改的几行上下文。
    Args:
        file_path: 要编辑的文件路径
        search_text: 要查找的代码片段（需包含足够上下文以唯一匹配）
        replace_text: 替换后的代码片段
    """
    return edit_file(file_path, search_text, replace_text)


@tool
def agent_exec_command(command: str, timeout: int = 60) -> str:
    """执行 shell 命令并返回输出。受安全黑名单保护，高危命令会被自动拒绝。
    启动图形界面程序时请使用 nohup 和后台运行（如 "nohup gnome-calculator &"），否则会因等待进程退出而超时。
    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间（秒），默认 60 秒
    """
    return exec_command(command, timeout)


@tool
def agent_check_process(name: str) -> str:
    """查询正在运行的进程（只读，不会修改任何进程）。可用于验证程序是否已成功启动。
    Args:
        name: 进程名称或关键词
    """
    return check_process(name)


@tool
def agent_list_skills() -> str:
    """列出所有可用的 Skills（标准作业程序/SOP）。遇到复杂任务时优先查找是否有标准流程可参考。
    """
    return list_skills()


@tool
def agent_read_skill(skill_name: str) -> str:
    """读取指定 Skill 的详细指令内容。先用 agent_list_skills 查找可用 Skill，再用本工具加载具体指令。
    Args:
        skill_name: Skill 名称（目录名）
    """
    return read_skill(skill_name)


@tool
def agent_web_search(query: str) -> str:
    """搜索互联网信息（尚未实现）。
    Args:
        query: 搜索查询词
    """
    raise NotImplementedError("此工具尚未实现。请尝试其他方式获取信息。")


@tool
def agent_web_fetch(url: str) -> str:
    """抓取网页内容并转换为文本（尚未实现）。
    Args:
        url: 要抓取的网页 URL
    """
    raise NotImplementedError("此工具尚未实现。请尝试其他方式获取信息。")


# ============================================================
# 收集所有可用工具
# ============================================================

ALL_TOOLS = [
    agent_read_file,
    agent_write_file,
    agent_edit_file,
    agent_exec_command,
    agent_check_process,
    agent_list_skills,
    agent_read_skill,
    agent_web_search,
    agent_web_fetch,
]


# ============================================================
# SmartToolCallingAgent — 智能工具调用 Agent
# ============================================================


class SmartToolCallingAgent(ToolCallingAgent):
    """
    增强版 ToolCallingAgent：

    1. 纯文本响应自动视为 final_answer（模型无需显式调用）
    2. 拦截 final_answer 工具调用，静默处理（不显示 "Calling tool: 'final_answer'"）
    """

    def write_memory_to_messages(self, summary_mode: bool = False) -> list[ChatMessage]:
        """覆盖父类方法：在 Step 2+ 时从 TaskStep 和 ActionStep 中剥离用户原始图片。

        smolagents 存在两条路径会在每个 step 重复传入用户原始图片：
          1. TaskStep.task_images —— to_messages() 每次都把 task_images 包含在消息里
          2. ActionStep.observations_images —— _run_stream 在创建每个 ActionStep 时
             都传入 observations_images=images（task 原始图片），并非工具返回的新图片
             （确认：smolagents 整个代码库中 observations_images 只在 _run_stream
              L573 被赋值，工具返回的图片走 AgentImage 路径存入 state，不写此字段）
        两条路径叠加导致 visual token 随 step 数线性增长。

        修复策略：
          - Step 1（memory 中只有 TaskStep，无 ActionStep）：原样保留，图片正常传递
          - Step N (N>1)（memory 中已有 ActionStep）：
            * TaskStep：task_images 置空
            * ActionStep：observations_images 置空（内容为 task 原始图片，非工具新图）
        """
        from copy import copy

        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        # 只要 memory 中已存在 ActionStep，就表示当前处于 Step 2+
        has_subsequent_steps = any(isinstance(s, ActionStep) for s in self.memory.steps)
        for memory_step in self.memory.steps:
            if has_subsequent_steps and isinstance(memory_step, TaskStep) and memory_step.task_images:
                # 浅拷贝 TaskStep，仅将 task_images 置空，不影响原对象
                stripped = copy(memory_step)
                stripped.task_images = None
                messages.extend(stripped.to_messages(summary_mode=summary_mode))
            elif has_subsequent_steps and isinstance(memory_step, ActionStep) and memory_step.observations_images:
                # _run_stream 将 task 原始图片设为每个 ActionStep 的 observations_images，
                # Step 2+ 中不应再重复传递，浅拷贝置空即可
                stripped = copy(memory_step)
                stripped.observations_images = None
                messages.extend(stripped.to_messages(summary_mode=summary_mode))
            else:
                messages.extend(memory_step.to_messages(summary_mode=summary_mode))
        return messages

    def _step_stream(self, memory_step: ActionStep):
        """
        覆盖原始 _step_stream：
        - 纯文本 → 自动 final_answer
        - final_answer 工具调用 → 静默拦截，不经过 process_tool_calls
        - 真实工具调用 → 正常执行（不自动完成，允许多步任务）
        """
        from smolagents.memory import ToolCall as MemoryToolCall

        memory_messages = self.write_memory_to_messages()
        input_messages = memory_messages.copy()
        memory_step.model_input_messages = input_messages

        try:
            if self.stream_outputs and hasattr(self.model, "generate_stream"):
                output_stream = self.model.generate_stream(
                    input_messages,
                    stop_sequences=["Observation:", "Calling tools:"],
                    tools_to_call_from=self.tools_and_managed_agents,
                )
                from smolagents.models import ChatMessageStreamDelta, agglomerate_stream_deltas
                from rich.live import Live
                from rich.markdown import Markdown
                chat_message_stream_deltas = []
                with Live("", console=self.logger.console, vertical_overflow="visible") as live:
                    for event in output_stream:
                        chat_message_stream_deltas.append(event)
                        live.update(
                            Markdown(agglomerate_stream_deltas(chat_message_stream_deltas).render_as_markdown())
                        )
                        yield event
                chat_message = agglomerate_stream_deltas(chat_message_stream_deltas)
            else:
                chat_message: ChatMessage = self.model.generate(
                    input_messages,
                    stop_sequences=["Observation:", "Calling tools:"],
                    tools_to_call_from=self.tools_and_managed_agents,
                )
                self.logger.log_markdown(
                    content=str(chat_message.content or chat_message.raw or ""),
                    title="Output message of the LLM:",
                    level=LogLevel.DEBUG,
                )

            memory_step.model_output_message = chat_message
            memory_step.model_output = chat_message.content
            memory_step.token_usage = chat_message.token_usage
        except Exception as e:
            from smolagents.agents import AgentGenerationError
            raise AgentGenerationError(f"Error while generating output:\n{e}", self.logger) from e

        # ── 1. 收集所有 tool_calls（结构化 + 文本解析） ──
        has_tool_calls = chat_message.tool_calls and len(chat_message.tool_calls) > 0
        if not has_tool_calls:
            parsed_calls = self.model._parse_tool_calls_from_text(chat_message.content or "")
            if parsed_calls:
                chat_message.tool_calls = parsed_calls
                has_tool_calls = True
                logger.info(f"💡 从文本中解析出 {len(parsed_calls)} 个工具调用")

        # ── 2. 分离 final_answer 和真实工具调用 ──
        real_tool_calls = []
        intercepted_final = None

        if has_tool_calls:
            for tc in chat_message.tool_calls:
                if isinstance(tc.function.arguments, str):
                    try:
                        tc.function.arguments = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        pass

                if tc.function.name == "final_answer":
                    args = tc.function.arguments
                    if isinstance(args, dict):
                        intercepted_final = args.get("answer", str(args))
                    elif isinstance(args, str):
                        intercepted_final = args
                    else:
                        intercepted_final = str(args)
                    logger.info(f"💡 拦截 final_answer 工具调用 ({len(intercepted_final)} 字符)")
                else:
                    real_tool_calls.append(tc)

        # ── 3. 处理 ──
        final_answer = None
        got_final_answer = False

        if real_tool_calls:
            # 有真实工具 → 执行工具
            chat_message.tool_calls = real_tool_calls
            for output in self.process_tool_calls(chat_message, memory_step):
                yield output

            if intercepted_final is not None:
                # 模型同时返回了工具调用和 final_answer → 使用 final_answer 结束
                got_final_answer = True
                final_answer = intercepted_final
                logger.info(f"💡 工具执行完毕，使用拦截的 final_answer（{len(intercepted_final)} 字符）")
            else:
                # 仅工具调用，无 final_answer → 让 agent 循环继续，模型将基于观察结果生成人类可读的回复
                got_final_answer = False
                final_answer = None
                logger.info("💡 工具执行完毕，等待模型基于观察结果生成回复")

        elif intercepted_final is not None:
            # 仅 final_answer → 静默完成，清除原始 <tool_call> 文本避免前端显示
            got_final_answer = True
            final_answer = intercepted_final
            memory_step.model_output = None  # 清除原始 <tool_call> 格式文本
            memory_step.tool_calls = [
                MemoryToolCall(name="final_answer", arguments={"answer": intercepted_final}, id="auto_final")
            ]
            memory_step.observations = intercepted_final
            logger.info(f"💡 静默处理 final_answer ({len(intercepted_final)} 字符)")

        else:
            # 纯文本 → 自动视为 final_answer
            plain_text = chat_message.content or ""
            got_final_answer = True
            final_answer = plain_text
            memory_step.tool_calls = [
                MemoryToolCall(name="final_answer", arguments={"answer": plain_text}, id="auto_final")
            ]
            memory_step.observations = plain_text
            logger.info(f"💡 纯文本响应，自动视为 final_answer ({len(plain_text)} 字符)")

        yield ActionOutput(
            output=final_answer,
            is_final_answer=got_final_answer,
        )


# ============================================================
# Session 管理
# ============================================================


@dataclass
class AgentSession:
    """Agent 会话"""
    session_id: str
    agent: MultiStepAgent
    mode: str
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)


class SessionStore:
    """内存会话存储"""

    def __init__(self):
        self._sessions: dict[str, AgentSession] = {}

    def get(self, session_id: str) -> AgentSession | None:
        session = self._sessions.get(session_id)
        if session:
            session.last_activity = datetime.now()
        return session

    def put(self, session: AgentSession):
        self._sessions[session.session_id] = session

    def delete(self, session_id: str):
        self._sessions.pop(session_id, None)

    def cleanup_expired(self, ttl_minutes: int):
        """清理过期 session"""
        now = datetime.now()
        expired = [
            sid for sid, s in self._sessions.items()
            if (now - s.last_activity).total_seconds() > ttl_minutes * 60
        ]
        for sid in expired:
            del self._sessions[sid]
            logger.info(f"Session {sid} 已过期清理")

    @property
    def active_count(self) -> int:
        return len(self._sessions)


# ============================================================
# Agent Runner
# ============================================================


class AgentRunner:
    """Agent 调度器，负责构建 Agent 和管理执行"""

    def __init__(self, llm_service: LLMService, config: AppConfig):
        self.llm_service = llm_service
        self.config = config
        self.sessions = SessionStore()
        self.model = LocalLLMModel(
            llm_service=llm_service,
            model_id=config.model.model_name,
        )

    def _build_agent(self, mode: str) -> MultiStepAgent:
        """根据模式构建 Agent，从配置文件加载提示词模板"""
        prompts_config = self.config.agent_prompts

        # 构建 prompt_templates 和 instructions
        instructions = prompts_config.agent_instructions
        prompt_templates = None

        if mode == "tool_calling":
            agent_cls = SmartToolCallingAgent
            if prompts_config.toolcalling_agent:
                prompt_templates = dict(prompts_config.toolcalling_agent)
        else:
            agent_cls = CodeAgent
            if prompts_config.code_agent:
                prompt_templates = dict(prompts_config.code_agent)

        kwargs = {
            "tools": ALL_TOOLS,
            "model": self.model,
            "max_steps": self.config.agent.max_steps,
            "verbosity_level": 1,
        }

        if prompt_templates:
            kwargs["prompt_templates"] = prompt_templates
        if instructions:
            kwargs["instructions"] = instructions

        if mode == "code":
            kwargs["additional_authorized_imports"] = [
                "json", "os", "re", "math", "datetime", "pathlib",
                "subprocess", "shutil", "glob", "collections",
            ]

        agent = agent_cls(**kwargs)
        return agent

    def get_or_create_session(self, session_id: str | None, mode: str | None = None) -> AgentSession:
        """获取已有 session 或创建新的"""
        resolved_mode = mode or self.config.agent.agent_mode

        if session_id:
            session = self.sessions.get(session_id)
            if session:
                return session

        # 创建新 session
        sid = session_id or str(uuid.uuid4())
        agent = self._build_agent(resolved_mode)
        session = AgentSession(session_id=sid, agent=agent, mode=resolved_mode)
        self.sessions.put(session)
        logger.info(f"新建 Agent session: {sid} (mode={mode})")
        return session

    async def run_stream(self, session_id: str, message: str, mode: str | None = None, images: list[str] | None = None) -> AsyncGenerator[str, None]:
        """
        执行 Agent 并以 SSE 事件流返回步骤和结果。

        SSE 事件格式：
        - {"type": "agent_step", "step_type": "thought", "content": "..."}
        - {"type": "agent_step", "step_type": "tool_call", "name": "...", "input": {...}}
        - {"type": "agent_step", "step_type": "observation", "content": "..."}
        - {"type": "content", "content": "..."}
        - {"type": "done"}
        - {"type": "error", "content": "..."}
        """
        session = self.get_or_create_session(session_id, mode)
        agent = session.agent

        # 将 base64 图片字符串转换为 PIL.Image 对象，并应用与普通 VLM 链路相同的图片参数：
        # - max_image_size: 字节大小限制
        # - supported_formats: 格式白名单
        # - max_image_dimension: 按配置缩放（保持宽高比），与 process_image_binary 逻辑一致
        pil_images: list | None = None
        if images:
            try:
                import base64
                import io
                from PIL import Image as PILImage

                max_size = self.config.model.max_image_size or 0
                max_dim = self.config.model.max_image_dimension or 0
                supported_fmts = set(
                    f.strip().lower()
                    for f in (self.config.model.supported_formats or "png,jpg,jpeg,gif,webp").split(",")
                    if f.strip()
                )
                # jpg 和 jpeg 在 PIL format 中均为 "JPEG"
                fmt_to_pil = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                               "gif": "GIF", "webp": "WEBP"}

                pil_images = []
                for idx, img_data in enumerate(images):
                    # 支持 data URL 格式: "data:image/jpeg;base64,..."
                    mime_hint = ""
                    if img_data.startswith("data:"):
                        header, b64_part = img_data.split(",", 1)
                        # header 形如 "data:image/jpeg;base64"
                        try:
                            mime_hint = header.split(":")[1].split(";")[0]  # e.g. image/jpeg
                        except Exception:
                            pass
                    else:
                        b64_part = img_data

                    img_bytes = base64.b64decode(b64_part)

                    # 检查文件大小
                    if max_size > 0 and len(img_bytes) > max_size:
                        logger.warning(
                            f"Agent 图片[{idx}] 大小 {len(img_bytes)} 字节超过限制 {max_size}，跳过"
                        )
                        continue

                    # 检查格式白名单（从 mime_hint 或 PIL 检测）
                    pil_img = PILImage.open(io.BytesIO(img_bytes))
                    detected_fmt = (pil_img.format or "").lower()  # e.g. "jpeg", "png"
                    if detected_fmt == "jpeg":
                        detected_fmt = "jpeg"  # 统一为 jpeg 便于集合比对
                    check_fmt = detected_fmt or mime_hint.replace("image/", "")
                    if supported_fmts and check_fmt and check_fmt not in supported_fmts:
                        logger.warning(
                            f"Agent 图片[{idx}] 格式 {check_fmt!r} 不在支持列表 {supported_fmts}，跳过"
                        )
                        continue

                    # 记录原始 PIL format（copy() 后会丢失）
                    original_pil_fmt = pil_img.format  # e.g. "JPEG", "PNG"

                    # 按 max_image_dimension 缩放（与 process_image_binary 逻辑一致）
                    w, h = pil_img.size
                    if max_dim > 0 and (w > max_dim or h > max_dim):
                        pil_img.load()
                        if w > h:
                            nw, nh = max_dim, int(h * max_dim / w)
                        else:
                            nh, nw = max_dim, int(w * max_dim / h)
                        pil_img = pil_img.resize((nw, nh), PILImage.Resampling.LANCZOS)
                        logger.info(
                            f"Agent 图片[{idx}]: 缩放 {w}x{h} → {nw}x{nh} (max_dim={max_dim})"
                        )
                    else:
                        pil_img = pil_img.copy()

                    # 将 format 挂回 PIL 对象（copy/resize 后会置 None），
                    # 供 _convert_content() 在序列化时选择正确的编码格式
                    pil_img.format = original_pil_fmt  # type: ignore[attr-defined]
                    pil_images.append(pil_img)

                if not pil_images:
                    pil_images = None
                else:
                    logger.info(f"Agent 收到 {len(pil_images)} 张图片（已应用图片配置参数）")
            except Exception as e:
                logger.warning(f"图片解码失败，将忽略图片: {e}")
                pil_images = None

        # 在后台线程中运行 Agent（smolagents 是同步的）
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def _run_agent():
            from smolagents.agents import ToolOutput as SmolToolOutput
            from smolagents.memory import ToolCall as MemoryToolCall, ActionStep as MemoryActionStep

            # smolagents stream 事件在每个 step 内的实际到达顺序：
            # 1. ToolCall        → 工具调用声明（name + arguments）
            # 2. ToolOutput      → 工具执行结果（.observation 包含实际输出）
            # 3. ActionOutput    → 步骤结束（非 final 时 output=None；final 时拿最终答案）
            # 4. ActionStep      → 完整步骤对象（含 model_output 思维过程）
            # 5. FinalAnswerStep → 最终答案（与 ActionOutput final 重复，跳过）
            #
            # 由于 ActionStep（含思考）在 ToolCall/ToolOutput 之后才到达，
            # 若立即推送会导致前端显示顺序错误（工具调用 → 观察结果 → 思考）。
            # 正确逻辑顺序应为：思考 → 工具调用 → 观察结果
            #
            # 修复方案：在每个 step 内缓冲 ToolCall/ToolOutput 事件，
            # 等 ActionStep 到达后拿到思考内容，再按正确顺序统一发送。

            # 每步缓冲区
            _buf_tool_calls: list[dict] = []
            _buf_observations: list[dict] = []
            _pending_final_content: list[str] = []  # 用列表模拟可变引用

            def _flush_step(thought_content: str | None):
                """按正确顺序发送：思考 → 工具调用 → 观察结果"""
                if thought_content:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "agent_step", "step_type": "thought",
                         "content": thought_content[:2000]}
                    )
                for tc_event in _buf_tool_calls:
                    loop.call_soon_threadsafe(queue.put_nowait, tc_event)
                for obs_event in _buf_observations:
                    loop.call_soon_threadsafe(queue.put_nowait, obs_event)
                _buf_tool_calls.clear()
                _buf_observations.clear()

            try:
                for step_output in agent.run(message, stream=True, images=pil_images):  # type: ignore[union-attr]

                    if isinstance(step_output, MemoryToolCall):
                        # 缓冲工具调用，等思考内容到达后再统一发送
                        _buf_tool_calls.append({
                            "type": "agent_step",
                            "step_type": "tool_call",
                            "name": step_output.name,
                            "input": self._safe_serialize(step_output.arguments),
                        })

                    elif isinstance(step_output, SmolToolOutput):
                        # 缓冲观察结果，等思考内容到达后再统一发送
                        obs = step_output.observation or ""
                        _buf_observations.append(
                            {"type": "agent_step", "step_type": "observation",
                             "content": obs[:5000]}
                        )

                    elif isinstance(step_output, ActionOutput):
                        # 步骤结束信号：暂存最终答案内容，等 ActionStep flush 后再发送
                        if step_output.is_final_answer:
                            output_str = str(step_output.output) if step_output.output is not None else ""
                            _pending_final_content.append(output_str)
                        # 非 final 的 ActionOutput.output 始终是 None，不处理

                    elif isinstance(step_output, MemoryActionStep):
                        # ActionStep 到达：拿到思考，按正确顺序 flush 本步骤所有事件
                        thought = None
                        if step_output.model_output and isinstance(step_output.model_output, str):
                            thought = step_output.model_output
                        _flush_step(thought)

                        # 步骤 flush 完毕后，再发送最终答案内容（如有）
                        if _pending_final_content:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {"type": "content", "content": _pending_final_content[0]}
                            )
                            _pending_final_content.clear()

                    elif isinstance(step_output, FinalAnswerStep):
                        # 与 ActionOutput(is_final_answer=True) 重复，跳过
                        pass

                    # 其他类型（ChatMessageStreamDelta 等），忽略

            except Exception as e:
                logger.error(f"Agent 执行异常: {e}", exc_info=True)
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "error", "content": str(e)}
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        # 启动后台线程
        import threading
        thread = threading.Thread(target=_run_agent, daemon=True)
        thread.start()

        # 从队列中消费事件并转为 SSE
        while True:
            event = await queue.get()
            if event is None:
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    def _handle_stream_event(self, event, loop, queue):
        """处理 smolagents 流式事件"""
        # ToolCall from smolagents.memory
        from smolagents.memory import ToolCall as MemoryToolCall
        if isinstance(event, MemoryToolCall):
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "type": "agent_step",
                    "step_type": "tool_call",
                    "name": event.name,
                    "input": self._safe_serialize(event.arguments),
                }
            )
            return

        # ChatMessageStreamDelta — 流式文本块
        if hasattr(event, "content") and event.content:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "agent_step", "step_type": "thought", "content": event.content}
            )

    def _emit_memory_steps(self, agent, loop, queue):
        """从 Agent 记忆中提取步骤信息并发送 SSE 事件"""
        for step in agent.memory.steps:
            if isinstance(step, ActionStep):
                # 发送思维过程
                if step.model_output and isinstance(step.model_output, str):
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "agent_step", "step_type": "thought",
                         "content": step.model_output[:2000]}
                    )

                # 发送工具调用
                if step.tool_calls:
                    for tc in step.tool_calls:
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            {
                                "type": "agent_step",
                                "step_type": "tool_call",
                                "name": tc.name,
                                "input": self._safe_serialize(tc.arguments),
                            }
                        )

                # 发送观察结果
                if step.observations:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "agent_step", "step_type": "observation",
                         "content": step.observations[:5000]}
                    )

    async def run_sync(self, session_id: str, message: str, mode: str | None = None) -> dict:
        """非流式运行 Agent，返回完整结果"""
        session = self.get_or_create_session(session_id, mode)
        agent = session.agent

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: agent.run(message))

        # 从 memory 中提取步骤
        steps: list[dict] = []
        for step in agent.memory.steps:
            if isinstance(step, ActionStep):
                step_info: dict = {"step_number": step.step_number}
                if step.model_output:
                    step_info["thought"] = str(step.model_output)[:2000]
                if step.tool_calls:
                    step_info["tool_calls"] = [
                        {"name": tc.name, "input": self._safe_serialize(tc.arguments)}
                        for tc in step.tool_calls
                    ]
                if step.observations:
                    step_info["observation"] = step.observations[:5000]
                steps.append(step_info)

        return {
            "session_id": session.session_id,
            "answer": str(result) if result is not None else "",
            "steps": steps,
        }

    @staticmethod
    def _safe_serialize(obj) -> str | dict | list:
        """安全序列化对象"""
        if isinstance(obj, (dict, list)):
            return obj
        if isinstance(obj, str):
            try:
                return json.loads(obj)
            except (json.JSONDecodeError, TypeError):
                return obj
        return str(obj)
