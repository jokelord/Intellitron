"""
Agent 调度与会话管理 (Agent Runner)

负责构建 smolagents Agent、管理 Session 生命周期、
以及将 Agent 步骤转换为 SSE 事件流。
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator


class SessionAbortManager:
    """全局会话中止管理器

    用于在会话中止时向正在执行的工具发送中止信号。
    支持:
    - 为每个 session_id 注册独立的 threading.Event
    - 工具执行时检查中止信号并提前返回
    - 清理已结束的会话事件
    """

    def __init__(self):
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def get_event(self, session_id: str) -> threading.Event:
        """获取或创建会话的中止事件"""
        with self._lock:
            if session_id not in self._events:
                self._events[session_id] = threading.Event()
            return self._events[session_id]

    def abort(self, session_id: str) -> bool:
        """触发会话中止

        Returns:
            bool: 是否成功触发（会话存在）
        """
        with self._lock:
            if session_id in self._events:
                self._events[session_id].set()
                return True
            return False

    def is_aborted(self, session_id: str) -> bool:
        """检查会话是否已中止"""
        with self._lock:
            event = self._events.get(session_id)
            if event:
                return event.is_set()
            return False

    def clear(self, session_id: str):
        """清除会话中止状态（可选，用于重用）"""
        with self._lock:
            if session_id in self._events:
                self._events[session_id].clear()

    def cleanup(self, session_id: str):
        """清理会话事件"""
        with self._lock:
            self._events.pop(session_id, None)


abort_manager = SessionAbortManager()


class _CurrentSessionContext:
    """线程局部存储：记录当前正在处理的 session_id

    工具执行时可通过此类查询当前 session 是否已被中止。
    """

    def __init__(self):
        self._storage = threading.local()

    def set(self, session_id: str):
        self._storage.session_id = session_id

    def get(self) -> str | None:
        return getattr(self._storage, "session_id", None)

    def clear(self):
        if hasattr(self._storage, "session_id"):
            del self._storage.session_id


current_session_context = _CurrentSessionContext()

from smolagents import (
    CodeAgent,
    MCPClient,
    PythonInterpreterTool,
    Tool,
    ToolCallingAgent,
    tool,
)
from smolagents.agents import ActionOutput, FinalAnswerStep, MultiStepAgent
from smolagents.memory import ActionStep, TaskStep
from smolagents.models import (
    ChatMessage,
    ChatMessageStreamDelta,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from smolagents.monitoring import LogLevel, TokenUsage

from backend.agent.local_llm_model import LocalLLMModel
from backend.config import AppConfig
from backend.llm_service import LLMService
from backend.tools.file_ops import edit_file, read_file, write_file
from backend.tools.skill_ops import list_skills, read_skill
from backend.tools.sys_ops import exec_command, check_process
from backend.tools.web_tools import web_search, web_fetch
from backend.tools.wikipedia_tools import wikipedia_search, wikipedia_summary

logger = logging.getLogger(__name__)


# ============================================================
# 自定义 PythonInterpreterTool：支持运行时 timeout 参数
# ============================================================


class CustomPythonInterpreterTool(PythonInterpreterTool):
    """支持运行时 timeout 参数的 Python 解释器工具"""

    skip_forward_signature_validation = True

    def __init__(self, *args, timeout_seconds=60, **kwargs):
        super().__init__(*args, timeout_seconds=timeout_seconds, **kwargs)
        self._default_timeout = timeout_seconds
        self.inputs = {
            "code": {
                "type": "string",
                "description": "The python code to run in interpreter",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds for code execution (default: 60)",
            },
        }

    def forward(self, code: str, timeout: int = None) -> str:
        timeout_seconds = timeout if timeout is not None else self._default_timeout
        state = {}
        output = str(
            self.python_evaluator(
                code,
                state=state,
                static_tools=self.base_python_tools,
                custom_tools={},
                authorized_imports=self.authorized_imports,
                timeout_seconds=timeout_seconds,
            )
        )
        return output


# ============================================================
# 工具包装器：为 base tools 添加 session-aware abort 支持
# ============================================================


def _make_session_aware_wrapper(base_func, tool_name: str):
    """创建带 session-aware abort 支持的工具包装器

    生成的 forward() 方法会：
    - 从原始工具输入定义中继承默认值（解决 nullable 参数缺省值丢失的 TypeError）
    - 接受 **_kwargs 以容忍 LLM 传入的多余参数
    - 在执行前检查 session 是否已中止
    - 向原始函数传递 check_abort 回调
    """
    from smolagents import Tool

    if isinstance(base_func, Tool):
        original_forward = base_func.forward
        original_inputs = base_func.inputs

        # 过滤掉内部参数
        input_params = [
            p
            for p in original_inputs.keys()
            if p not in ("check_abort", "use_fallback")
        ]

        type_map = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "array": "list",
            "object": "dict",
        }

        # 只对支持 start_line/end_line/timeout 的工具添加这些参数
        for p in ("timeout", "start_line", "end_line"):
            if p not in input_params:
                # 只有当原始工具有这个参数时才添加
                if p in original_inputs:
                    input_params.append(p)

        # 构建带默认值的参数列表
        param_list = ["self"]
        for p in input_params:
            prop = original_inputs.get(p, {})
            json_type = prop.get("type", "any")
            py_type = type_map.get(json_type, json_type)

            # 从 inputs 的 default 字段提取默认值
            has_default = "default" in prop
            default_val = prop.get("default")

            if prop.get("nullable"):
                # nullable 参数：若有 default 则继承，否则默认 None
                if has_default:
                    param_list.append(f"{p}: {py_type} | None = {repr(default_val)}")
                else:
                    param_list.append(f"{p}: {py_type} | None = None")
            elif json_type in type_map:
                if has_default:
                    param_list.append(f"{p}: {py_type} = {repr(default_val)}")
                else:
                    param_list.append(f"{p}: {py_type}")
            else:
                if has_default:
                    param_list.append(f"{p} = {repr(default_val)}")
                else:
                    param_list.append(f"{p}")

        # 添加 **_kwargs 以容忍 LLM 传入的多余参数（如 timeout 给不支持的工具）
        param_list.append("**_kwargs")

        # 构建参数字典
        args_dict_parts = [f"'{p}': {p}" for p in input_params]
        args_dict_str = "{" + ", ".join(args_dict_parts) + "}"

        # 检查原始函数是否支持 timeout 参数
        _has_timeout = "timeout" in original_inputs

        forward_code = f"""def forward({", ".join(param_list)}) -> str:
    session_id = current_session_context.get()
    if session_id and abort_manager.is_aborted(session_id):
        return f'{tool_name} 已被用户中止'
    def check_abort():
        if session_id and abort_manager.is_aborted(session_id):
            return True
        return False
    args_dict = {args_dict_str}
    args_dict['check_abort'] = check_abort
    # 仅当原始函数支持 timeout 参数时才添加
    if 'timeout' not in args_dict and {_has_timeout}:
        args_dict['timeout'] = 60
    return original_forward(**args_dict)"""
        forward_ns = {
            "current_session_context": current_session_context,
            "abort_manager": abort_manager,
            "original_forward": original_forward,
        }
        exec(forward_code, forward_ns)
        forward_method = forward_ns["forward"]

        class WrappedTool(Tool):
            # 跳过签名校验：forward 含 **_kwargs 以容忍 LLM 传入的多余参数
            skip_forward_signature_validation = True
            name = base_func.name
            description = base_func.description
            # 过滤掉内部参数
            inputs = {
                k: v.copy()
                for k, v in base_func.inputs.items()
                if k not in ("check_abort", "use_fallback")
            }
            # 仅当原始函数支持 timeout 参数时才添加（保留原描述，仅补充 default）
            if "timeout" in original_inputs:
                if "timeout" not in inputs:
                    inputs["timeout"] = original_inputs["timeout"].copy()
                if "default" not in inputs["timeout"]:
                    inputs["timeout"]["default"] = 60
            # 只对支持 start_line/end_line 的工具添加这些参数（如 read_file）
            if "start_line" in original_inputs:
                if "start_line" not in inputs:
                    inputs["start_line"] = original_inputs["start_line"].copy()
            if "end_line" in original_inputs:
                if "end_line" not in inputs:
                    inputs["end_line"] = original_inputs["end_line"].copy()
            output_type = base_func.output_type
            forward = forward_method

        return WrappedTool()


def _create_tool_wrapper(base_func, tool_name: str):
    """创建工具包装器（用于需要传递额外参数的函数）

    生成的 forward() 方法会：
    - 从原始函数签名中继承默认值（解决 nullable 参数缺省值丢失的 TypeError）
    - 接受 **_kwargs 以容忍 LLM 传入的多余参数（如 timeout 给不支持的工具）
    - 以关键字参数形式调用原始函数，避免位置参数错位
    - 若原始函数支持 check_abort，自动注入 session-aware 中止回调
    """
    import functools
    import inspect
    from smolagents._function_type_hints_utils import get_json_schema

    sig = inspect.signature(base_func)

    @functools.wraps(base_func)
    def forward_func(*args, **kwargs):
        return base_func(*args, **kwargs)

    forward_func.__name__ = tool_name

    tool_json_schema = get_json_schema(forward_func)["function"]

    for prop_name, prop_info in tool_json_schema["parameters"]["properties"].items():
        if prop_info.get("type") == "null":
            del prop_info["type"]
            prop_info["nullable"] = True

    type_map = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }

    # 过滤掉内部参数，不暴露给 LLM
    original_params = set(tool_json_schema["parameters"]["properties"].keys())
    input_params = [
        p
        for p in tool_json_schema["parameters"]["properties"].keys()
        if p not in ("check_abort", "use_fallback")
    ]
    # 只对支持 start_line/end_line/timeout 的工具添加这些参数到 forward
    for p in ("timeout", "start_line", "end_line"):
        if p not in input_params:
            # 只有当原始工具有这个参数时才添加
            if p in original_params:
                input_params.append(p)

    # 构建带默认值的参数列表
    param_list = ["self"]
    for p in input_params:
        # 优先使用原始 schema 中的参数信息
        prop = tool_json_schema["parameters"]["properties"].get(p, {"type": "any"})
        json_type = prop.get("type", "any")
        py_type = type_map.get(json_type, json_type)

        # 从原始函数签名中提取默认值
        has_default = (
            p in sig.parameters
            and sig.parameters[p].default is not inspect.Parameter.empty
        )
        default_val = sig.parameters[p].default if has_default else None

        if prop.get("nullable"):
            # nullable 参数：若原始函数有默认值则继承，否则默认 None
            if has_default:
                param_list.append(f"{p}: {py_type} | None = {repr(default_val)}")
            else:
                param_list.append(f"{p}: {py_type} | None = None")
        elif json_type in type_map:
            if has_default:
                param_list.append(f"{p}: {py_type} = {repr(default_val)}")
            else:
                param_list.append(f"{p}: {py_type}")
        else:
            if has_default:
                param_list.append(f"{p} = {repr(default_val)}")
            else:
                param_list.append(f"{p}")

    # 添加 **_kwargs 以容忍 LLM 传入的多余参数（如 timeout 给 read_file）
    param_list.append("**_kwargs")

    # 以关键字参数形式调用原始函数，避免位置参数错位
    kw_parts = [f"{p}={p}" for p in input_params]

    # 检查原始函数是否支持 check_abort 参数
    has_check_abort = "check_abort" in sig.parameters

    if has_check_abort:
        # 注入 session-aware check_abort 回调
        kw_parts_with_abort = kw_parts + ["check_abort=_check_abort"]
        forward_code = (
            f"def forward({', '.join(param_list)}) -> str:\n"
            f"    session_id = current_session_context.get()\n"
            f"    if session_id and abort_manager.is_aborted(session_id):\n"
            f"        return '{tool_name} 已被用户中止'\n"
            f"    def _check_abort():\n"
            f"        return bool(session_id and abort_manager.is_aborted(session_id))\n"
            f"    return forward_func({', '.join(kw_parts_with_abort)})"
        )
        forward_ns = {
            "forward_func": forward_func,
            "current_session_context": current_session_context,
            "abort_manager": abort_manager,
        }
    else:
        forward_code = (
            f"def forward({', '.join(param_list)}) -> str:\n"
            f"    return forward_func({', '.join(kw_parts)})"
        )
        forward_ns = {"forward_func": forward_func}

    exec(forward_code, forward_ns)
    forward_method = forward_ns["forward"]

    class WrappedTool(Tool):
        # 跳过签名校验：forward 含 **_kwargs 以容忍 LLM 传入的多余参数
        skip_forward_signature_validation = True
        name = tool_json_schema["name"]
        description = tool_json_schema["description"]
        # 过滤掉内部参数
        inputs = {
            k: v.copy()
            for k, v in tool_json_schema["parameters"]["properties"].items()
            if k not in ("check_abort", "use_fallback")
        }
        # 仅当原始函数支持 timeout 参数时才添加（保留原描述，仅补充 default）
        if "timeout" in tool_json_schema["parameters"]["properties"]:
            if "timeout" not in inputs:
                inputs["timeout"] = tool_json_schema["parameters"]["properties"][
                    "timeout"
                ].copy()
            if "default" not in inputs["timeout"]:
                inputs["timeout"]["default"] = 60
        # 只对支持 start_line/end_line 的工具添加这些参数
        if "start_line" in tool_json_schema["parameters"]["properties"]:
            if "start_line" not in inputs:
                inputs["start_line"] = tool_json_schema["parameters"]["properties"][
                    "start_line"
                ].copy()
        if "end_line" in tool_json_schema["parameters"]["properties"]:
            if "end_line" not in inputs:
                inputs["end_line"] = tool_json_schema["parameters"]["properties"][
                    "end_line"
                ].copy()
        output_type = tool_json_schema["return"]["type"]
        forward = forward_method

    return WrappedTool()


# ============================================================
# 收集所有可用工具
# ============================================================


def _get_tools(
    authorized_imports: list | None = None,
    mcp_servers: list | None = None,
    web_search_engine: str = "duckduckgo",
    enable_wikipedia: bool = True,
) -> list:
    """构建工具列表

    Args:
        authorized_imports: PythonInterpreterTool 允许的 import 模块
        mcp_servers: MCP 服务器配置列表
        web_search_engine: Web 搜索引擎 (duckduckgo)
        enable_wikipedia: 是否启用 Wikipedia 搜索
    """
    if authorized_imports is None:
        authorized_imports = [
            "collections",
            "math",
            "random",
            "time",
            "re",
            "stat",
            "queue",
            "datetime",
            "itertools",
            "unicodedata",
            "statistics",
        ]

    read_file_tool = _create_tool_wrapper(read_file, "read_file")
    write_file_tool = _create_tool_wrapper(write_file, "write_file")
    edit_file_tool = _create_tool_wrapper(edit_file, "edit_file")
    exec_command_tool = _create_tool_wrapper(exec_command, "exec_command")
    check_process_tool = _create_tool_wrapper(check_process, "check_process")
    list_skills_tool = _create_tool_wrapper(list_skills, "list_skills")
    read_skill_tool = _create_tool_wrapper(read_skill, "read_skill")
    web_search_tool = _make_session_aware_wrapper(web_search, "web_search")
    web_fetch_tool = _make_session_aware_wrapper(web_fetch, "web_fetch")

    tools = [
        CustomPythonInterpreterTool(authorized_imports=authorized_imports),
        read_file_tool,
        write_file_tool,
        edit_file_tool,
        exec_command_tool,
        check_process_tool,
        list_skills_tool,
        read_skill_tool,
        web_search_tool,
        web_fetch_tool,
    ]

    if enable_wikipedia:
        wikipedia_search_tool = _make_session_aware_wrapper(
            wikipedia_search, "wikipedia_search"
        )
        wikipedia_summary_tool = _make_session_aware_wrapper(
            wikipedia_summary, "wikipedia_summary"
        )
        tools.append(wikipedia_search_tool)
        tools.append(wikipedia_summary_tool)

    if mcp_servers:
        from mcp import StdioServerParameters

        for srv in mcp_servers:
            if srv.command and srv.args:
                server_params = StdioServerParameters(
                    command=srv.command,
                    args=srv.args,
                    env=srv.env if srv.env else None,
                )
                mcp_client = MCPClient(server_params)
                mcp_tools = mcp_client.get_tools()
                tools.extend(mcp_tools)

    return tools


def get_tools_from_config(config: AppConfig) -> list:
    """从 AppConfig 构建工具列表"""
    mcp_servers = []
    if config.mcp and config.mcp.servers:
        mcp_servers = config.mcp.servers

    web_engine = "duckduckgo"
    if config.web_tools:
        web_engine = config.web_tools.search_engine or "duckduckgo"
        enable_wikipedia = config.web_tools.enable_wikipedia
    else:
        enable_wikipedia = True

    return _get_tools(
        authorized_imports=config.agent.python_authorized_imports,
        mcp_servers=mcp_servers,
        web_search_engine=web_engine,
        enable_wikipedia=enable_wikipedia,
    )


ALL_TOOLS = _get_tools()


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
        """覆盖父类方法：精确控制图片在多步骤、多轮对话中的传递。

        smolagents 图片有两个来源：
          1. TaskStep.task_images —— 用户在本轮上传的图片，to_messages() 每次都包含
          2. ActionStep.observations_images —— smolagents _run_stream 把本次 run()
             的 images 参数复制到每个 ActionStep（L573），永远是任务级别的图片副本，
             不是工具返回的图片（真正的工具产出图片走 AgentImage/state 路径）。

        正确策略：
          - TaskStep：只有"当前任务"（最后一个 TaskStep）才保留图片；
            早期轮次的 TaskStep 剥离，防止旧轮图片泄漏到新轮。
          - ActionStep：始终剥离 observations_images，因为它们只是任务图片的副本，
            保留会造成重复 visual token 且跨轮泄漏（原 has_following_action 逻辑的 bug：
            最后一个 ActionStep 之后是新一轮 TaskStep 而非 ActionStep，导致
            has_following_action=False，图片没被剥离，旧轮图片污染新轮上下文）。

        任务图片的正确位置：始终在当前轮 TaskStep 的 USER 消息中，
        llm_service._strip_old_images 对 "New task:" 消息有豁免，确保多步骤时也可见。
        """
        from copy import copy

        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        steps = self.memory.steps

        # 找出最后一个 TaskStep 的索引（代表当前任务）
        last_task_idx = max(
            (i for i, s in enumerate(steps) if isinstance(s, TaskStep)),
            default=-1,
        )

        for i, memory_step in enumerate(steps):
            if (
                isinstance(memory_step, TaskStep)
                and i != last_task_idx
                and memory_step.task_images
            ):
                # 非当前任务的 TaskStep：剥离图片，防止旧轮图片泄漏
                stripped = copy(memory_step)
                stripped.task_images = None
                messages.extend(stripped.to_messages(summary_mode=summary_mode))
            elif isinstance(memory_step, ActionStep):
                stripped = copy(memory_step)
                stripped.tool_calls = None
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

        _used_streaming = False
        try:
            if self.stream_outputs and hasattr(self.model, "generate_stream"):
                _used_streaming = True
                output_stream = self.model.generate_stream(
                    input_messages,
                    stop_sequences=["Observation:", "Calling tools:"],
                    tools_to_call_from=self.tools_and_managed_agents,
                )
                from smolagents.models import agglomerate_stream_deltas
                from rich.live import Live
                from rich.markdown import Markdown

                chat_message_stream_deltas = []
                with Live(
                    "", console=self.logger.console, vertical_overflow="visible"
                ) as live:
                    for event in output_stream:
                        chat_message_stream_deltas.append(event)
                        live.update(
                            Markdown(
                                agglomerate_stream_deltas(
                                    chat_message_stream_deltas
                                ).render_as_markdown()
                            )
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
            # 流式模式下 chat_message.token_usage 为 None，需要从 model 获取
            if _used_streaming and hasattr(self.model, "_last_stream_content"):
                output_text = self.model._last_stream_content
                usage = (
                    self.model.llm_service.get_stream_usage(output_text)
                    if hasattr(self.model, "llm_service")
                    else None
                )
                if usage:
                    memory_step.token_usage = TokenUsage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                    )
                    logger.info(
                        f"📊 流式 token 统计: input={memory_step.token_usage.input_tokens}, output={memory_step.token_usage.output_tokens}"
                    )
                else:
                    memory_step.token_usage = chat_message.token_usage
            else:
                memory_step.token_usage = chat_message.token_usage
        except Exception as e:
            from smolagents.agents import AgentGenerationError

            raise AgentGenerationError(
                f"Error while generating output:\n{e}", self.logger
            ) from e

        # ── 流式路径修正：将 content 从 reasoning+content 混合体还原为仅 content ──
        # generate_stream 为了让思考框流式显示，把 reasoning 和 content 都
        # 作为 content delta yield 出来。agglomerate_stream_deltas 会把它们
        # 拼成 chat_message.content = reasoning + content。
        # 但后续工具调用解析和 final_answer 只应基于 content 部分（不含 reasoning），
        # 因此用 _last_stream_content（仅追踪 content 部分）覆盖。
        _full_agglomerated = None
        if _used_streaming and hasattr(self.model, "_last_stream_content"):
            _full_agglomerated = (
                chat_message.content
            )  # 保存完整文本（reasoning+content）备用
            chat_message.content = self.model._last_stream_content
            memory_step.model_output = chat_message.content
            logger.info(
                f"📝 流式输出统计: content={len(self.model._last_stream_content)} 字符, "
                f"reasoning={len(getattr(self.model, '_last_stream_reasoning', ''))} 字符, "
                f"完整={len(_full_agglomerated or '')} 字符"
            )

        # ── 1. 收集所有 tool_calls（结构化 + 文本解析） ──
        has_tool_calls = chat_message.tool_calls and len(chat_message.tool_calls) > 0
        if not has_tool_calls:
            # 先在 content 中搜索工具调用（常规路径）
            parsed_calls = self.model._parse_tool_calls_from_text(
                chat_message.content or ""
            )

            # 如果 content 中没有找到，再在 reasoning 中搜索
            # 模型有时将 "动作:" JSON 放在思考块（reasoning_content）里
            if not parsed_calls and _used_streaming:
                reasoning_text = getattr(self.model, "_last_stream_reasoning", "")
                if reasoning_text:
                    logger.info(
                        f"💡 content 中未找到工具调用，尝试在 reasoning ({len(reasoning_text)} 字符) 中搜索"
                    )
                    parsed_calls = self.model._parse_tool_calls_from_text(
                        reasoning_text
                    )

            # 最后回退：在完整文本（reasoning+content 拼接）中搜索
            if (
                not parsed_calls
                and _full_agglomerated
                and _full_agglomerated != (chat_message.content or "")
            ):
                logger.info(
                    f"💡 继续在完整文本 ({len(_full_agglomerated)} 字符) 中搜索工具调用"
                )
                parsed_calls = self.model._parse_tool_calls_from_text(
                    _full_agglomerated
                )

            if parsed_calls:
                # 过滤 final_answer：模型完成任务后应直接返回文本，而非调用 final_answer 工具
                real_calls = [
                    tc for tc in parsed_calls if tc.function.name != "final_answer"
                ]
                if real_calls:
                    chat_message.tool_calls = real_calls
                    has_tool_calls = True
                    logger.info(
                        f"💡 从文本中解析出 {len(real_calls)} 个工具调用: {[tc.function.name for tc in real_calls]}"
                    )
                else:
                    # 仅解析出 final_answer，视为纯文本响应
                    has_tool_calls = False
                    logger.info("💡 从文本中仅解析出 final_answer，视为纯文本响应")

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
                    logger.info(
                        f"💡 拦截 final_answer 工具调用 ({len(intercepted_final)} 字符)"
                    )
                else:
                    real_tool_calls.append(tc)

        # ── 3. 处理 ──
        final_answer = None
        got_final_answer = False

        if real_tool_calls:
            # ── 工具执行前发出思考预览（仅非流式路径） ──
            # 流式路径已在 generate_stream 期间逐 token 推送了思考内容，无需重复发送。
            if not _used_streaming:
                _thought_preview = chat_message.content or ""
                if _thought_preview:
                    yield ChatMessageStreamDelta(content=_thought_preview)

            # 有真实工具 → 执行工具
            chat_message.tool_calls = real_tool_calls
            for output in self.process_tool_calls(chat_message, memory_step):
                yield output

            if intercepted_final is not None:
                # 模型同时返回了工具调用和 final_answer → 使用 final_answer 结束
                got_final_answer = True
                final_answer = intercepted_final
                logger.info(
                    f"💡 工具执行完毕，使用拦截的 final_answer（{len(intercepted_final)} 字符）"
                )
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
            # 不写入 tool_calls / observations：
            # smolagents to_messages() 会把 tool_calls 序列化为
            # "Calling tools:\n[{'id': 'auto_final', ...}]"，放入对话历史后模型在
            # 后续轮次会直接复现这段文本作为纯文本输出，导致前端显示原始工具调用格式。
            logger.info(f"💡 静默处理 final_answer ({len(intercepted_final)} 字符)")

        else:
            # 纯文本 → 自动视为 final_answer
            plain_text = chat_message.content or ""
            got_final_answer = True
            final_answer = plain_text
            # model_output 已由上方 chat_message.content 赋值，作为 ASSISTANT 消息写入历史即可；
            # 不额外写入 tool_calls / observations，避免 "Calling tools:[auto_final]" 污染历史。
            logger.info(
                f"💡 纯文本响应，自动视为 final_answer ({len(plain_text)} 字符)"
            )

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
    aborted: bool = False
    answer_now: bool = False
    accumulated_content: str = ""
    accumulated_thought: str = ""

    def abort(self):
        """标记会话为中止状态"""
        self.aborted = True

    def request_answer_now(self):
        """请求立即返回当前累积的内容"""
        self.answer_now = True

    def check_aborted(self) -> bool:
        """检查是否已中止"""
        return self.aborted

    def check_answer_now(self) -> bool:
        """检查是否请求立即返回答案"""
        return self.answer_now

    def reset_answer_now(self):
        """重置 answer_now 标志"""
        self.answer_now = False


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
            sid
            for sid, s in self._sessions.items()
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

        python_imports = self.config.agent.python_authorized_imports
        tools = get_tools_from_config(self.config)
        kwargs = {
            "tools": tools,
            "model": self.model,
            "max_steps": self.config.agent.max_steps,
            "verbosity_level": 1,
            "stream_outputs": True,
        }

        if prompt_templates:
            kwargs["prompt_templates"] = prompt_templates

        # VLM 模式：动态追加视觉语言模型身份指令
        if self.config.model.model_type == "vision" and prompts_config.vlm_instructions:
            vlm_note = prompts_config.vlm_instructions
            instructions = f"{instructions}\n\n{vlm_note}" if instructions else vlm_note

        if instructions:
            kwargs["instructions"] = instructions

        if mode == "code":
            kwargs["additional_authorized_imports"] = (
                self.config.agent.python_authorized_imports
                or [
                    "json",
                    "os",
                    "re",
                    "math",
                    "datetime",
                    "pathlib",
                    "subprocess",
                    "shutil",
                    "glob",
                    "collections",
                ]
            )

        agent = agent_cls(**kwargs)
        return agent

    def get_or_create_session(
        self, session_id: str | None, mode: str | None = None
    ) -> AgentSession:
        """获取已有 session 或创建新的"""
        resolved_mode = mode or self.config.agent.agent_mode

        if session_id:
            session = self.sessions.get(session_id)
            if session:
                # 检查 session 是否已被中止或已完成（answer_now 触发后）
                # 如果是，需要创建新 session，避免旧状态泄漏到新一轮请求
                if session.aborted or session.answer_now:
                    logger.info(
                        f"Session {session_id} 状态为 aborted={session.aborted} 或 answer_now={session.answer_now}，"
                        "创建新 session 以避免旧状态泄漏"
                    )
                    self.sessions.delete(session_id)
                    abort_manager.cleanup(session_id)
                else:
                    # 重置中止标志，允许会话继续
                    session.aborted = False
                    session.answer_now = False
                    return session

        # 创建新 session
        sid = session_id or str(uuid.uuid4())
        agent = self._build_agent(resolved_mode)
        session = AgentSession(session_id=sid, agent=agent, mode=resolved_mode)
        self.sessions.put(session)
        logger.info(f"新建 Agent session: {sid} (mode={mode})")
        return session

    async def run_stream(
        self,
        session_id: str,
        message: str,
        mode: str | None = None,
        images: list[str] | None = None,
    ) -> AsyncGenerator[str, None]:
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
                    for f in (
                        self.config.model.supported_formats or "png,jpg,jpeg,gif,webp"
                    ).split(",")
                    if f.strip()
                )
                # jpg 和 jpeg 在 PIL format 中均为 "JPEG"
                fmt_to_pil = {
                    "jpg": "JPEG",
                    "jpeg": "JPEG",
                    "png": "PNG",
                    "gif": "GIF",
                    "webp": "WEBP",
                }

                pil_images = []
                for idx, img_data in enumerate(images):
                    # 支持 data URL 格式: "data:image/jpeg;base64,..."
                    mime_hint = ""
                    if img_data.startswith("data:"):
                        header, b64_part = img_data.split(",", 1)
                        # header 形如 "data:image/jpeg;base64"
                        try:
                            mime_hint = header.split(":")[1].split(";")[
                                0
                            ]  # e.g. image/jpeg
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

                    # 透明通道转换：RGBA/P 等带 alpha 通道的图片必须合成到白色背景。
                    # SGLang/Qwen3-VL 处理 RGBA PNG 时将透明区域渲染为黑色，导致模型只能
                    # 看到前景 Logo 而无法感知背景，产生「蓝白色重复图案」等错误描述。
                    _orig_mode = pil_img.mode
                    if pil_img.mode == "RGBA":
                        pil_img.load()
                        _bg = PILImage.new("RGB", pil_img.size, (255, 255, 255))
                        _bg.paste(pil_img, mask=pil_img.split()[3])
                        pil_img = _bg
                        logger.info(
                            f"Agent 图片[{idx}]: RGBA→RGB (白色背景合成, 修复透明通道)"
                        )
                    elif pil_img.mode in ("P", "PA", "LA"):
                        pil_img.load()
                        _rgba_tmp = pil_img.convert("RGBA")
                        _bg = PILImage.new("RGB", _rgba_tmp.size, (255, 255, 255))
                        _bg.paste(_rgba_tmp, mask=_rgba_tmp.split()[3])
                        pil_img = _bg
                        logger.info(
                            f"Agent 图片[{idx}]: {_orig_mode}→RGB (白色背景合成, 修复透明通道)"
                        )
                    elif pil_img.mode not in ("RGB", "L"):
                        pil_img = pil_img.convert("RGB")
                        logger.info(f"Agent 图片[{idx}]: {_orig_mode}→RGB")

                    # 将 format 挂回 PIL 对象（copy/resize 后会置 None），
                    # 供 _convert_content() 在序列化时选择正确的编码格式
                    pil_img.format = original_pil_fmt  # type: ignore[attr-defined]

                    # ── 预编码优化：保留原始 JPEG 字节，避免二次编码质量损失 ──
                    # 如果图片已经是 JPEG 格式且未经缩放/色彩空间转换，直接使用原始字节。
                    # 否则用处理后的 PIL 像素编码一次 JPEG。
                    _resized = max_dim > 0 and (
                        w > max_dim or h > max_dim
                    )  # 是否经过了缩放
                    _color_changed = _orig_mode != pil_img.mode  # 是否经过了色彩转换
                    if not _resized and not _color_changed and detected_fmt == "jpeg":
                        # 无任何变换，直接使用前端传来的原始 JPEG 字节
                        pil_img._pre_encoded_b64 = b64_part  # type: ignore[attr-defined]
                        pil_img._pre_encoded_mime = "image/jpeg"  # type: ignore[attr-defined]
                        logger.info(
                            f"Agent 图片[{idx}]: ⚡ 使用原始 JPEG 字节（无变换）, "
                            f"base64 {len(b64_part)} chars"
                        )
                    else:
                        # 经过变换（缩放/色彩），需要重新编码
                        _enc_buf = io.BytesIO()
                        _save_img = pil_img
                        if _save_img.mode not in ("RGB", "L"):
                            _save_img = _save_img.convert("RGB")
                        _save_img.save(_enc_buf, format="JPEG", quality=90)
                        pil_img._pre_encoded_b64 = base64.b64encode(
                            _enc_buf.getvalue()
                        ).decode()  # type: ignore[attr-defined]
                        pil_img._pre_encoded_mime = "image/jpeg"  # type: ignore[attr-defined]
                        logger.info(
                            f"Agent 图片[{idx}]: 预编码 JPEG {len(_enc_buf.getvalue())} bytes, "
                            f"base64 {len(pil_img._pre_encoded_b64)} chars (原因: {'缩放' if _resized else '色彩转换'})"  # type: ignore[attr-defined]
                        )

                    pil_images.append(pil_img)

                if not pil_images:
                    pil_images = None
                else:
                    logger.info(
                        f"Agent 收到 {len(pil_images)} 张图片（已应用图片配置参数）"
                    )
            except Exception as e:
                logger.warning(f"图片解码失败，将忽略图片: {e}")
                pil_images = None

        # 在后台线程中运行 Agent（smolagents 是同步的）
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def _run_agent():
            from smolagents.agents import ToolOutput as SmolToolOutput
            from smolagents.memory import (
                ToolCall as MemoryToolCall,
                ActionStep as MemoryActionStep,
            )

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
            # 标记本步骤是否已通过 ChatMessageStreamDelta 提前推送了思考内容
            # 用列表模拟可变引用，供嵌套函数修改
            _thought_already_sent: list[bool] = [False]

            def _flush_step(thought_content: str | None):
                """按正确顺序发送：思考 → 工具调用 → 观察结果
                如果本步骤已通过 ChatMessageStreamDelta 提前发送了思考预览（partial=True），
                则跳过重复发送，只发送工具调用和观察结果。
                """
                if thought_content and not _thought_already_sent[0]:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {
                            "type": "agent_step",
                            "step_type": "thought",
                            "content": thought_content[:2000],
                        },
                    )
                # 每步 flush 后重置标记，避免影响后续步骤
                _thought_already_sent[0] = False
                for tc_event in _buf_tool_calls:
                    loop.call_soon_threadsafe(queue.put_nowait, tc_event)
                for obs_event in _buf_observations:
                    loop.call_soon_threadsafe(queue.put_nowait, obs_event)
                _buf_tool_calls.clear()
                _buf_observations.clear()

            try:
                current_session_context.set(session_id)
                abort_manager.get_event(session_id)

                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "session_start", "session_id": session.session_id},
                )

                _should_reset = pil_images is not None
                for step_output in agent.run(
                    message, stream=True, images=pil_images, reset=_should_reset
                ):
                    if session.check_aborted():
                        logger.info(
                            f"Session {session_id} 在 Agent 循环中检测到中止信号，终止执行"
                        )
                        break

                    if isinstance(step_output, ChatMessageStreamDelta):
                        # _step_stream 在工具执行前 yield 的思考预览 token
                        # 立即推送到前端，让用户在工具运行期间就能看到思考内容
                        if step_output.content:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {
                                    "type": "agent_step",
                                    "step_type": "thought",
                                    "content": step_output.content,
                                    "partial": True,
                                },
                            )
                            _thought_already_sent[0] = True

                    elif isinstance(step_output, MemoryToolCall):
                        # 缓冲工具调用，等思考内容到达后再统一发送
                        _buf_tool_calls.append(
                            {
                                "type": "agent_step",
                                "step_type": "tool_call",
                                "name": step_output.name,
                                "input": self._safe_serialize(step_output.arguments),
                            }
                        )

                    elif isinstance(step_output, SmolToolOutput):
                        # 检查是否已中止 - 如果是，直接跳出循环，不发送观察结果
                        if session.check_aborted():
                            logger.info(
                                f"Session {session_id} 在工具执行后检测到中止信号，终止 Agent"
                            )
                            break

                        obs = step_output.observation or ""
                        _buf_observations.append(
                            {
                                "type": "agent_step",
                                "step_type": "observation",
                                "content": obs[:5000],
                            }
                        )

                    elif isinstance(step_output, ActionOutput):
                        # 步骤结束信号：暂存最终答案内容，等 ActionStep flush 后再发送
                        if step_output.is_final_answer:
                            output_str = (
                                str(step_output.output)
                                if step_output.output is not None
                                else ""
                            )
                            _pending_final_content.append(output_str)
                        # 非 final 的 ActionOutput.output 始终是 None，不处理

                    elif isinstance(step_output, MemoryActionStep):
                        # 检查是否已中止
                        if session.check_aborted():
                            logger.info(
                                f"Session {session_id} 在步骤结束后检测到中止信号，终止 Agent"
                            )
                            break

                        thought = None
                        if step_output.model_output and isinstance(
                            step_output.model_output, str
                        ):
                            thought = step_output.model_output
                        _flush_step(thought)

                        if _pending_final_content:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {
                                    "type": "content",
                                    "content": _pending_final_content[0],
                                },
                            )
                            _pending_final_content.clear()

                    elif isinstance(step_output, FinalAnswerStep):
                        # 与 ActionOutput(is_final_answer=True) 重复，跳过
                        pass

                    # 其他类型（ChatMessageStreamDelta 等），忽略

            except Exception as e:
                logger.error(f"Agent 执行异常: {e}", exc_info=True)
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "content": str(e)}
                )
            finally:
                current_session_context.clear()
                if session.check_aborted():
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"type": "aborted", "session_id": session.session_id},
                    )
                abort_manager.cleanup(session_id)
                loop.call_soon_threadsafe(queue.put_nowait, None)

        # 启动后台线程
        import threading

        thread = threading.Thread(target=_run_agent, daemon=True)
        thread.start()

        # 从队列中消费事件并转为 SSE
        while True:
            # Check abort flag
            if session.check_aborted():
                logger.info(f"Session {session_id} 检测到中止信号")
                yield f"data: {json.dumps({'type': 'aborted', 'session_id': session.session_id})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
                break

            # Check answer_now flag
            if session.check_answer_now():
                logger.info(f"Session {session_id} 检测到 answer_now 信号")
                # Extract accumulated content from agent memory
                accumulated = ""
                if hasattr(agent, "memory") and agent.memory:
                    for step in agent.memory.steps:
                        if hasattr(step, "model_output") and step.model_output:
                            accumulated += str(step.model_output)

                # Send the accumulated content as final answer
                yield f"data: {json.dumps({'type': 'content', 'content': accumulated, 'answer_now': True})}\n\n"
                session.reset_answer_now()
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
                break

            event = await queue.get()
            if event is None:
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
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
                },
            )
            return

        # ChatMessageStreamDelta — 流式文本块
        if hasattr(event, "content") and event.content:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "type": "agent_step",
                    "step_type": "thought",
                    "content": event.content,
                },
            )

    def _emit_memory_steps(self, agent, loop, queue):
        """从 Agent 记忆中提取步骤信息并发送 SSE 事件"""
        for step in agent.memory.steps:
            if isinstance(step, ActionStep):
                # 发送思维过程
                if step.model_output and isinstance(step.model_output, str):
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {
                            "type": "agent_step",
                            "step_type": "thought",
                            "content": step.model_output[:2000],
                        },
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
                            },
                        )

                # 发送观察结果
                if step.observations:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {
                            "type": "agent_step",
                            "step_type": "observation",
                            "content": step.observations[:5000],
                        },
                    )

    async def run_sync(
        self, session_id: str, message: str, mode: str | None = None
    ) -> dict:
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

    def abort_session(self, session_id: str) -> bool:
        """中止指定会话的 Agent 执行

        Returns:
            bool: 是否成功中止
        """
        session = self.sessions.get(session_id)
        if session:
            session.abort()
            abort_manager.abort(session_id)
            logger.info(f"Session {session_id} 已标记为中止")
            return True
        return False

    def answer_now_session(self, session_id: str) -> bool:
        """请求立即返回当前累积的内容

        Returns:
            bool: 是否成功触发
        """
        session = self.sessions.get(session_id)
        if session:
            session.request_answer_now()
            logger.info(f"Session {session_id} 已请求立即返回答案")
            return True
        return False

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
