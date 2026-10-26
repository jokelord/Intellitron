"""
Agent 调度与会话管理 (Agent Runner)

负责构建 smolagents Agent、管理 Session 生命周期、
以及将 Agent 步骤转换为 SSE 事件流。
"""

import asyncio
import contextvars
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Collection
from copy import copy, deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from PIL.Image import Image as PILImage
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Literal

from .session_hub import AgentSessionHub
from .computer_use_history import AUTO_PROJECTION, computer_use_history_stage_for_request, projected_action_observation, restage_computer_use_history_for_same_blocks
from .canonical_messages import (CanonicalToolResult, call_to_dict, canonicalize_model_messages, clone_message, build_switch_agent_schema)
from .qwen_tool_protocol import native_control_prose
from .text_tool_protocol import (
    TextToolCall,
    TextToolProtocolError,
    format_tool_call_sequence,
    has_malformed_control_attempt,
    reconstruct_split_fenced_tool_call,
    starts_textual_tool_call_attempt,
)

from ..session_hub_runtime import (
    SessionHubConflict,
    SessionHubEntry,
    SessionHubRuntime,
    subscribe_session_hub,
)

from ..vision.contracts import (
    AgentRequestAdmissionStage,
    VisionAutomationTransportMetadata,
)
from ..vision.history_omission import (
    cancel_agent_vision_automation_cleanup,
    commit_agent_history_image_omission,
    finalize_agent_history_image_omission,
    rollback_committed_agent_history_image_omission,
    register_agent_vision_automation_cleanup,
    stage_agent_history_image_omission,
)
from ..vision.validation import VisionRequestValidationError, redact_data_urls
from ..security.capabilities import (
    CapabilityError,
    OperationCapability,
    build_session_capability,
    derive_child_capability,
    require_capability,
    validate_child_id,
)
from ..security.path_policy import PathPolicy, PathPolicyError
from .tool_configuration import (
    AgentToolConfigurationError,
    AgentToolConfigurationRequest,
    AgentToolConfigurationSnapshot,
    BUSINESS_TOOL_NAMES,
    BusinessToolCatalog,
    build_business_tool_catalog,
    DEFAULT_PROFILE_TOOLS,
)


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
        self._pending_aborts: set[str] = set()
        self._lock = threading.Lock()

    def get_event(self, session_id: str) -> threading.Event:
        """获取或创建会话的中止事件"""
        with self._lock:
            if session_id not in self._events:
                self._events[session_id] = threading.Event()
            if session_id in self._pending_aborts:
                self._events[session_id].set()
            return self._events[session_id]

    def abort(self, session_id: str) -> bool:
        """触发会话中止

        Returns:
            bool: 是否成功触发（会话存在）
        """
        with self._lock:
            self._pending_aborts.add(session_id)
            if session_id in self._events:
                self._events[session_id].set()
            return True

    def is_aborted(self, session_id: str) -> bool:
        """检查会话是否已中止"""
        with self._lock:
            event = self._events.get(session_id)
            if event:
                return event.is_set()
            return session_id in self._pending_aborts

    def clear(self, session_id: str):
        """清除会话中止状态（可选，用于重用）"""
        with self._lock:
            if session_id in self._events:
                self._events[session_id].clear()
            self._pending_aborts.discard(session_id)

    def cleanup(self, session_id: str):
        """清理会话事件"""
        with self._lock:
            self._events.pop(session_id, None)
            self._pending_aborts.discard(session_id)


abort_manager = SessionAbortManager()
run_abort_manager = SessionAbortManager()


class SessionRequestManager:
    """Tracks the active underlying request_id for each agent session."""

    def __init__(self):
        self._request_ids: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, session_id: str, request_id: str) -> None:
        with self._lock:
            self._request_ids[session_id] = request_id

    def get(self, session_id: str) -> str | None:
        with self._lock:
            return self._request_ids.get(session_id)

    def clear(self, session_id: str, request_id: str | None = None) -> None:
        with self._lock:
            current = self._request_ids.get(session_id)
            if request_id is None or current == request_id:
                self._request_ids.pop(session_id, None)

    def cleanup(self, session_id: str) -> None:
        self.clear(session_id)


class SessionBusyError(RuntimeError):
    """Raised when a new turn is started while the session already has an active run."""

    def __init__(self, session_id: str):
        super().__init__(f"Session {session_id} already has an active run")
        self.session_id = session_id


class SessionAdmissionError(RuntimeError):
    """Raised when a safe legacy session token attempts to create new state."""

    def __init__(self, code: str = "legacy_session_not_found"):
        super().__init__(code)
        self.code = code


class SessionDisposalError(RuntimeError):
    """Bounded diagnostic for a session that could not be safely disposed."""

    def __init__(
        self,
        code: str,
        *,
        pending_scope_count: int = 0,
        pending_child_count: int = 0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.pending_scope_count = pending_scope_count
        self.pending_child_count = pending_child_count

    def as_detail(self) -> dict[str, int | str]:
        return {
            "code": self.code,
            "pending_scope_count": self.pending_scope_count,
            "pending_child_count": self.pending_child_count,
        }


session_request_manager = SessionRequestManager()


@dataclass(frozen=True)
class RawToolCallDebug:
    """Transient UI-only copy of actual model text or provider call data."""

    raw_tool_call: str
    source: Literal["model_text", "provider_payload"] = "model_text"


@dataclass
class SecondaryHandoffRequest:
    target_agent: str
    task: str | None = None
    additional_args: dict[str, Any] | None = None

# ── 线程→会话ID 映射 ──────────────────────────────────────
# 用 plain dict + thread ident 作为主要机制，避免 threading.local() 在 exec'd 代码中的潜在问题
_thread_session_map: dict[int, str] = {}
_thread_abort_scope_map: dict[int, str] = {}
_thread_session_lock = threading.Lock()


class _CurrentSessionContext:
    """线程局部存储：记录当前正在处理的 session_id

    工具执行时可通过此类查询当前 session 是否已被中止。
    同时维护 _thread_session_map 作为备份机制。
    """

    def __init__(self):
        self._storage = threading.local()

    def set(self, session_id: str, abort_scope_id: str | None = None):
        self._storage.session_id = session_id
        self._storage.abort_scope_id = abort_scope_id
        # 同时写入 plain dict
        with _thread_session_lock:
            _thread_session_map[threading.current_thread().ident] = session_id
            if abort_scope_id is None:
                _thread_abort_scope_map.pop(threading.current_thread().ident, None)
            else:
                _thread_abort_scope_map[threading.current_thread().ident] = abort_scope_id

    def get(self) -> str | None:
        # 先尝试 thread-local，失败则查 plain dict
        val = getattr(self._storage, "session_id", None)
        if val is None:
            with _thread_session_lock:
                val = _thread_session_map.get(threading.current_thread().ident)
        return val

    def get_abort_scope(self) -> str | None:
        val = getattr(self._storage, "abort_scope_id", None)
        if val is None:
            with _thread_session_lock:
                val = _thread_abort_scope_map.get(threading.current_thread().ident)
        return val

    def clear(self):
        if hasattr(self._storage, "session_id"):
            del self._storage.session_id
        if hasattr(self._storage, "abort_scope_id"):
            del self._storage.abort_scope_id
        with _thread_session_lock:
            _thread_session_map.pop(threading.current_thread().ident, None)
            _thread_abort_scope_map.pop(threading.current_thread().ident, None)


current_session_context = _CurrentSessionContext()


class _CurrentToolCallContext:
    def __init__(self):
        self._storage = threading.local()

    def set(self, call_id: str, turn_dispatch_id: str | None) -> None:
        self._storage.call_id = call_id
        self._storage.turn_dispatch_id = turn_dispatch_id

    def get_call_id(self) -> str | None:
        return getattr(self._storage, "call_id", None)

    def get_turn_dispatch_id(self) -> str | None:
        return getattr(self._storage, "turn_dispatch_id", None)

    def clear(self) -> None:
        for name in ("call_id", "turn_dispatch_id"):
            if hasattr(self._storage, name):
                delattr(self._storage, name)


current_tool_call_context = _CurrentToolCallContext()


def _validated_session_images_dir(session_id: str, policy: PathPolicy) -> Path | None:
    try:
        directory = Path(policy.validate_session_dir(session_id))
    except PathPolicyError as error:
        logger.warning("Rejected tool-image session directory: %s", error)
        return None
    try:
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            logger.warning("Rejected unsafe tool-image session directory: %s", directory)
            return None
    except OSError as error:
        logger.warning("Could not inspect tool-image session directory: %s", error)
        return None
    return directory


def _read_and_consume_tool_images(
    session_id: str | None,
    *,
    capability: OperationCapability | None,
) -> list:
    """从文件 IPC 读取并消费工具加载的图片。

    读取 /tmp/agent_images/<session_id>/*.json，解码 base64 为 PIL Image
    （带 _pre_encoded_b64/_pre_encoded_mime 属性），删除已消费的 JSON 文件，
    清理空目录。

    Args:
        session_id: 会话 ID，为 None 时返回空列表

    Returns:
        PIL Image 对象列表
    """
    if not session_id:
        return []
    try:
        PathPolicy.validate_session_id(session_id)
        bound = require_capability(capability, "session", session_id)
    except (CapabilityError, PathPolicyError) as error:
        logger.warning("Rejected tool-image consumption: %s", error)
        return []

    import base64 as _b64
    import io as _io
    from PIL import Image as _PILImage

    images_dir = _validated_session_images_dir(session_id, bound.path_policy())
    if images_dir is None:
        return []
    if not images_dir.exists():
        return []

    tool_images = []
    consumed_files = []

    for json_file in sorted(images_dir.glob("*.json")):
        descriptor: int | None = None
        try:
            metadata = os.lstat(json_file)
            if not stat.S_ISREG(metadata.st_mode):
                logger.warning("Skipping non-regular tool-image IPC entry: %s", json_file)
                continue
            descriptor = os.open(
                json_file,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            logger.warning("Skipping unsafe tool-image IPC entry: %s -> %s", json_file, error)
            continue
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as payload:
                descriptor = None
                data = json.loads(payload.read())
            for img_data in data.get("images", []):
                b64_str = img_data["base64"]
                mime = img_data.get("mime", "image/jpeg")
                raw_bytes = _b64.b64decode(b64_str)
                pil_img = _PILImage.open(_io.BytesIO(raw_bytes))
                pil_img.load()
                pil_img._pre_encoded_b64 = b64_str  # type: ignore[attr-defined]
                pil_img._pre_encoded_mime = mime  # type: ignore[attr-defined]
                tool_images.append(pil_img)
            consumed_files.append(json_file)
        except Exception as e:
            logger.warning(f"读取图片 JSON 失败: {json_file} -> {e}")
            consumed_files.append(json_file)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    for f in consumed_files:
        try:
            f.unlink()
        except OSError:
            pass

    try:
        if images_dir.exists() and not any(images_dir.iterdir()):
            images_dir.rmdir()
    except OSError:
        pass

    return tool_images


def _cleanup_session_images(
    session_id: str,
    *,
    capability: OperationCapability | None,
) -> bool:
    """清理会话的图片 IPC 文件目录。"""
    try:
        PathPolicy.validate_session_id(session_id)
        bound = require_capability(capability, "session", session_id)
    except (CapabilityError, PathPolicyError) as error:
        logger.warning("Rejected tool-image cleanup: %s", error)
        return False
    try:
        policy = bound.path_policy()
        removed = policy.cleanup_session_dir(session_id)
        if removed:
            logger.info("已清理会话图片目录: %s", policy.validate_session_dir(session_id))
        return True
    except (OSError, PathPolicyError) as error:
        logger.warning("清理会话图片目录失败: %s -> %s", session_id, error)
        return False


from smolagents import (
    MCPClient,
    Tool,
    ToolCallingAgent,
    tool,
)
from smolagents.agents import (
    ActionOutput,
    EMPTY_PROMPT_TEMPLATES,
    FinalAnswerStep,
    MultiStepAgent,
    populate_template,
)
from smolagents.memory import ActionStep, TaskStep
from smolagents.models import (
    ChatMessage,
    ChatMessageStreamDelta,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from smolagents.monitoring import LogLevel, TokenUsage, Timing

from backend.agent.local_llm_model import LocalLLMModel, AssistantControlResolution
from backend.config import AppConfig, normalize_agent_mode
from backend.agent.tool_feedback_omission import (
    BOUNDARY_REASON as TOOL_FEEDBACK_OMISSION_REASON,
    commit_historical_tool_feedback,
    projected_observations,
    projected_tool_results,
)
from backend.llm_service import LLMService
from backend.tools.file_ops import (
    edit_file,
    read_file,
    use_file_operation_policy,
    write_file,
)
from backend.tools.search_ops import glob_files, grep_search
from backend.tools.skill_ops import list_skills, read_skill
from backend.tools.sys_ops import exec_command, check_process
from backend.tools.web_tools import web_search, web_fetch
from backend.tools.search_and_fetch import search_and_fetch_impl
from backend.tools.wikipedia_search_and_fetch import wikipedia_search_and_fetch_impl
from backend.utils.logger import get_logger


logger = get_logger(__name__)


# ============================================================
# 工具包装器：为 base tools 添加 session-aware abort 支持
# ============================================================


_CAPABILITY_BOUND_TOOLS = frozenset(
    {
        "read_file",
        "glob",
        "grep",
        "check_process",
        "write_file",
        "edit_file",
        "exec_command",
        "load_local_images",
        "load_images_to_ipc",
    }
)
_FILE_OPERATION_TOOLS = frozenset({"write_file", "edit_file"})


def _capability_denial(error: CapabilityError) -> str:
    return f"操作被安全策略阻断 [{error.code}]: {error.message}"


def _invoke_capability_bound_tool(
    tool_obj,
    base_func,
    tool_name: str,
    call_kwargs: dict[str, Any],
    extra_kwargs: dict[str, Any],
    resolved_session_id: str | None,
    *,
    has_session_id: bool,
    has_runtime_config: bool,
    has_capability: bool,
    fallback_runtime_config=None,
):
    """Invoke one wrapper with internal authority hidden from model inputs."""
    internal_overrides = {
        "session_id",
        "capability",
        "runtime_config",
        "check_abort",
        "request_id",
        "crop_id",
        "binding_id",
    }.intersection(extra_kwargs)
    if internal_overrides:
        return _capability_denial(
            CapabilityError(
                "internal_argument_override",
                "model-supplied internal identity or authority is not allowed",
            )
        )

    capability = getattr(tool_obj, "_injected_capability", None)
    if tool_name in _CAPABILITY_BOUND_TOOLS:
        try:
            capability = require_capability(capability, tool_name)
            if resolved_session_id not in {None, capability.session_id}:
                raise CapabilityError(
                    "capability_session_mismatch",
                    "bound session identity does not match its capability",
                )
        except CapabilityError as error:
            return _capability_denial(error)

    if has_session_id:
        call_kwargs["session_id"] = (
            capability.session_id
            if isinstance(capability, OperationCapability)
            else resolved_session_id
        )
    if has_runtime_config:
        call_kwargs["runtime_config"] = (
            capability.command_runtime_config()
            if isinstance(capability, OperationCapability)
            else fallback_runtime_config
        )
    if has_capability:
        call_kwargs["capability"] = capability

    if tool_name in _FILE_OPERATION_TOOLS:
        assert isinstance(capability, OperationCapability)
        with use_file_operation_policy(
            capability.path_policy(), capability=capability
        ):
            return base_func(**call_kwargs)
    return base_func(**call_kwargs)


def _make_session_aware_wrapper(base_func, tool_name: str):
    """创建带 session-aware abort 支持的工具包装器

    生成的 forward() 方法会：
    - 从原始可执行函数签名继承默认值，兼容输入定义中的 schema-only default
    - 接受 **_kwargs 以容忍 LLM 传入的多余参数
    - 在执行前检查 session 是否已中止
    - 向原始函数传递 check_abort 回调
    """
    from smolagents import Tool
    import inspect

    if isinstance(base_func, Tool):
        original_forward = base_func.forward
        original_inputs = deepcopy(base_func.inputs)
        original_signature = inspect.signature(original_forward)
        # Decorated tools may omit default metadata; the executable signature
        # is authoritative. Preserve schema-only defaults for generic wrappers.
        for parameter_name, prop in original_inputs.items():
            parameter = original_signature.parameters.get(parameter_name)
            if parameter is not None and parameter.default is not inspect.Parameter.empty:
                prop["default"] = parameter.default

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
    import threading as _thr
    session_id = (
        _get_attr(self, '_injected_session_id', None)
        or current_session_context.get()
        or _thread_map.get(_thr.current_thread().ident)
    )
    abort_scope_id = (
        _get_attr(self, '_injected_abort_scope_id', None)
        or current_session_context.get_abort_scope()
        or _abort_scope_map.get(_thr.current_thread().ident)
    )
    if abort_scope_id and run_abort_manager.is_aborted(abort_scope_id):
        return f'{tool_name} 已被用户中止'
    def check_abort():
        if abort_scope_id and run_abort_manager.is_aborted(abort_scope_id):
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
            "run_abort_manager": run_abort_manager,
            "original_forward": original_forward,
            "_get_attr": getattr,
            "_thread_map": _thread_session_map,
            "_abort_scope_map": _thread_abort_scope_map,
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
                for k, v in original_inputs.items()
                if k not in ("check_abort", "use_fallback")
            }
            # 仅当原始函数支持 timeout 参数时才添加（保留原描述及已有 default，不编造默认值）
            if "timeout" in original_inputs:
                if "timeout" not in inputs:
                    inputs["timeout"] = original_inputs["timeout"].copy()
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


def _create_tool_wrapper(base_func, tool_name: str, *, runtime_config=None):
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

    # Strip internal-only parameters from both annotations and signature
    # before schema generation.  smolagents' get_json_schema cannot parse
    # types like Callable, and these params are filtered from tool inputs
    # later anyway.  We must also update __signature__ so inspect.signature
    # does not see the original parameters, and delete __wrapped__ so
    # inspect does not follow it back to the original function.
    _internal_params = (
        "check_abort",
        "use_fallback",
        "session_id",
        "runtime_config",
        "capability",
    )
    if hasattr(forward_func, "__annotations__"):
        for _ip in _internal_params:
            forward_func.__annotations__.pop(_ip, None)
    new_params = [
        p for name, p in sig.parameters.items() if name not in _internal_params
    ]
    forward_func.__signature__ = sig.replace(parameters=new_params)
    if hasattr(forward_func, "__wrapped__"):
        del forward_func.__wrapped__

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
        if p
        not in (
            "check_abort",
            "use_fallback",
            "session_id",
            "runtime_config",
            "capability",
        )
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

    args_dict_parts = [f"'{p}': {p}" for p in input_params]
    args_dict_str = "{" + ", ".join(args_dict_parts) + "}"

    # 检查原始函数是否支持内部注入参数
    has_check_abort = "check_abort" in sig.parameters
    has_session_id = "session_id" in sig.parameters
    has_runtime_config = "runtime_config" in sig.parameters
    has_capability = "capability" in sig.parameters

    forward_code = (
        f"def forward({', '.join(param_list)}) -> str:\n"
        f"    import threading as _thr\n"
        f"    _session_id = (\n"
        f"        _get_attr(self, '_injected_session_id', None)\n"
        f"        or current_session_context.get()\n"
        f"        or _thread_map.get(_thr.current_thread().ident)\n"
        f"    )\n"
        f"    _abort_scope_id = (\n"
        f"        _get_attr(self, '_injected_abort_scope_id', None)\n"
        f"        or current_session_context.get_abort_scope()\n"
        f"        or _abort_scope_map.get(_thr.current_thread().ident)\n"
        f"    )\n"
        f"    if _abort_scope_id and run_abort_manager.is_aborted(_abort_scope_id):\n"
        f"        return '{tool_name} 已被用户中止'\n"
        f"    def _check_abort():\n"
        f"        return bool(_abort_scope_id and run_abort_manager.is_aborted(_abort_scope_id))\n"
        f"    _call_kwargs = {args_dict_str}\n"
        f"    if _has_check_abort:\n"
        f"        _call_kwargs['check_abort'] = _check_abort\n"
        f"    return _invoke(self, forward_func, _tool_name, _call_kwargs, _kwargs, _session_id, has_session_id=_has_session_id, has_runtime_config=_has_runtime_config, has_capability=_has_capability, fallback_runtime_config=_runtime_config)"
    )
    forward_ns = {
        "forward_func": forward_func,
        "current_session_context": current_session_context,
        "run_abort_manager": run_abort_manager,
        "_get_attr": getattr,
        "_thread_map": _thread_session_map,
        "_abort_scope_map": _thread_abort_scope_map,
        "_runtime_config": runtime_config,
        "_invoke": _invoke_capability_bound_tool,
        "_tool_name": tool_name,
        "_has_check_abort": has_check_abort,
        "_has_session_id": has_session_id,
        "_has_runtime_config": has_runtime_config,
        "_has_capability": has_capability,
    }

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
            if k
            not in (
                "check_abort",
                "use_fallback",
                "session_id",
                "runtime_config",
                "capability",
            )
        }
        # 仅当原始函数支持 timeout 参数时才添加（保留原描述及已有 default，不编造默认值）
        if "timeout" in tool_json_schema["parameters"]["properties"]:
            if "timeout" not in inputs:
                inputs["timeout"] = tool_json_schema["parameters"]["properties"][
                    "timeout"
                ].copy()
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
    mcp_servers: list | None = None,
    enable_wikipedia: bool = True,
    command_runtime_config=None,
) -> list:
    """构建工具列表

    Args:
        mcp_servers: MCP 服务器配置列表
        enable_wikipedia: 是否启用 Wikipedia 搜索
    """
    read_file_tool = _create_tool_wrapper(read_file, "read_file")
    write_file_tool = _create_tool_wrapper(write_file, "write_file")
    edit_file_tool = _create_tool_wrapper(edit_file, "edit_file")
    glob_tool = _create_tool_wrapper(glob_files, "glob")
    grep_tool = _create_tool_wrapper(grep_search, "grep")
    exec_command_tool = _create_tool_wrapper(
        exec_command,
        "exec_command",
        runtime_config=command_runtime_config,
    )
    check_process_tool = _create_tool_wrapper(check_process, "check_process")
    list_skills_tool = _create_tool_wrapper(list_skills, "list_skills")
    read_skill_tool = _create_tool_wrapper(read_skill, "read_skill")
    web_search_tool = _make_session_aware_wrapper(web_search, "web_search")
    web_fetch_tool = _make_session_aware_wrapper(web_fetch, "web_fetch")
    search_and_fetch_tool = _create_tool_wrapper(
        search_and_fetch_impl, "search_and_fetch"
    )

    tools = [
        read_file_tool,
        write_file_tool,
        edit_file_tool,
        glob_tool,
        grep_tool,
        exec_command_tool,
        check_process_tool,
        list_skills_tool,
        read_skill_tool,
        web_search_tool,
        web_fetch_tool,
        search_and_fetch_tool,
    ]

    if enable_wikipedia:
        wikipedia_search_and_fetch_tool = _create_tool_wrapper(
            wikipedia_search_and_fetch_impl, "wikipedia_search_and_fetch"
        )
        tools.append(wikipedia_search_and_fetch_tool)

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


def get_tools_from_config(
    config: AppConfig,
    *,
    enable_selectable_wikipedia: bool = False,
) -> list:
    """从 AppConfig 构建工具列表"""
    from backend.security.environment_builder import CommandRuntimeConfig

    mcp_servers = []
    if config.mcp and config.mcp.servers:
        mcp_servers = config.mcp.servers

    if enable_selectable_wikipedia:
        enable_wikipedia = True
    elif config.web_tools:
        enable_wikipedia = config.web_tools.enable_wikipedia
    else:
        enable_wikipedia = True

    return _get_tools(
        mcp_servers=mcp_servers,
        enable_wikipedia=enable_wikipedia,
        command_runtime_config=CommandRuntimeConfig.from_agent_config(config.agent),
    )


ALL_TOOLS = _get_tools()

PROFILE_TOOL_ALLOWLIST: dict[str, set[str] | None] = {
    "coordinator_1": set(),
    "coordinator_2": set(),
    "manager_agent": {
        "read_file",
        "search_and_fetch",
        "web_fetch",
        "grep",
        "glob",
        "list_skills",
        "read_skill",
        "dispatching_parallel_agent",
    },
    "basic_agent": None,
    "explorer_agent": set(),
    "auditor_agent": set(),
}

LEGACY_RUNTIME_PROFILE_NAME = "basic_agent"
COORDINATOR_ROUTER_PROFILE_NAMES = {"coordinator_1"}
RUNTIME_COORDINATOR_ROUTER_NAMES = COORDINATOR_ROUTER_PROFILE_NAMES | {"coordinator_agent"}
COORDINATOR_CLI_SUPPRESSED_SOURCES = {"coordinator_1", "coordinator_agent"}


def _build_runtime_time_context(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return current.strftime("Current local time: %A, %Y-%m-%d %H:%M")


def _profile_uses_runtime_time_context(profile_name: str) -> bool:
    return profile_name in {"coordinator_2", "manager_agent", "basic_agent"}


# ============================================================
# SmartToolCallingAgent — 智能工具调用 Agent
# ============================================================


class NativeCompactSourceError(ValueError):
    """Canonical native compact source cannot safely reconcile committed memory."""


def _native_prompt_execution_conflict(text: str) -> bool:
    """Check framework-owned instructions only, never canonical conversation data."""
    return any(marker in text for marker in (
        "<<TOOL_CALL>>", "<<END_TOOL_CALL>>", "<<SWITCH_AGENT>>", "Calling tools:", "调用工具:",
    )) or re.search(r"<tool_call>\s*\{", text) is not None


class SmartToolCallingAgent(ToolCallingAgent):
    """
    增强版 ToolCallingAgent：

    1. 纯文本响应自动视为 final_answer（模型无需显式调用）
    2. 拦截 final_answer 工具调用，静默处理（不显示 "Calling tool: 'final_answer'"）
    3. 系统提示中过滤隐藏工具，避免 LLM 直接调用
    """

    # 不暴露给 LLM 的工具名称集合：系统提示与 API schema 分离控制。
    _PROMPT_HIDDEN_TOOL_NAMES = {
        "final_answer",
        "wikipedia_search",
        "wikipedia_fetch",
        "wikipedia_summary",
    }
    _SCHEMA_HIDDEN_TOOL_NAMES = {
        "final_answer",
        "wikipedia_fetch",
        "wikipedia_search",
        "wikipedia_summary",
    }

    def __init__(self, *args, **kwargs):
        self._tool_protocol = kwargs.pop("tool_protocol", "legacy_text")
        self._native_tool_protocol_prompt = kwargs.pop("native_tool_protocol_prompt", "")
        self._allowed_switch_targets = frozenset(kwargs.pop("allowed_switch_targets", ()))
        self._agent_python_executable = kwargs.pop("agent_python_executable", None)
        self._manager_agent_max_parallel_children = kwargs.pop(
            "manager_agent_max_parallel_children", None
        )
        self._cli_stream_source = kwargs.pop("cli_stream_source", "basic_agent")
        self._should_emit_cli_stream_chunk = kwargs.pop(
            "should_emit_cli_stream_chunk", None
        )
        self._tool_prompt_plan = kwargs.pop("tool_prompt_plan", None)
        super().__init__(*args, **kwargs)
        from backend.agent.orchestrator import ToolOrchestrator

        self.orchestrator = ToolOrchestrator()
        # Per-run Rich Live handle, set by _run_basic_stream before agent.run()
        # and consumed by _step_stream. _step_stream must not create a local
        # fallback Live when this is None, otherwise Rich freezes one rendered
        # snapshot per step and duplicates terminal output.
        self._cli_live = None

    @property
    def native_tool_protocol(self) -> bool:
        return getattr(getattr(self, "_agent_session", None), "tool_protocol",
                       getattr(self, "_tool_protocol", "legacy_text")) == "qwen35-native"

    @property
    def tools_and_managed_agents(self):
        """返回 schema 可见工具 + managed agents，用于 model.__call__。"""
        visible = [
            t
            for t in self.tools.values()
            if getattr(t, "name", None) not in self._SCHEMA_HIDDEN_TOOL_NAMES
        ]
        schemas = visible + list(self.managed_agents.values())
        if self.native_tool_protocol and getattr(self, "_allowed_switch_targets", ()):
            schemas.append(build_switch_agent_schema(self._allowed_switch_targets))
        return schemas

    def initialize_system_prompt(self) -> str:
        """覆写系统提示渲染：使用独立的 prompt 可见工具集合。"""
        prompt_visible_tools = {
            name: t
            for name, t in self.tools.items()
            if name not in self._PROMPT_HIDDEN_TOOL_NAMES
        }
        agent_python_executable = (
            getattr(self, "_agent_python_executable", None) or "~/.agent/bin/python"
        )
        agent_python_bin_dir = str(Path(agent_python_executable).expanduser().parent)
        plan = getattr(self, "_tool_prompt_plan", None)
        selected_names = set(getattr(plan, "tool_names", ()))
        selected_tools = (
            {
                name: tool
                for name, tool in prompt_visible_tools.items()
                if name in selected_names
            }
            if plan is not None
            else prompt_visible_tools
        )
        try:
            system_prompt = populate_template(
                self.prompt_templates["system_prompt"],
                variables={
                    "tools": prompt_visible_tools,
                    "prompt_visible_tools": prompt_visible_tools,
                    "managed_agents": self.managed_agents,
                    "custom_instructions": self.instructions,
                    "agent_python_executable": agent_python_executable,
                    "agent_python_bin_dir": agent_python_bin_dir,
                    "manager_agent_max_parallel_children": getattr(
                        self, "_manager_agent_max_parallel_children", 4
                    ),
                    # These explicit slots are intentionally separate from the
                    # dynamic tool listing so prompt provenance remains auditable.
                    "native_tool_protocol_prompt": self._native_tool_protocol_prompt if self.native_tool_protocol else "",
                    "business_tool_protocol": getattr(plan, "business_tool_protocol", ""),
                    "selected_tool_prompt_modules": getattr(plan, "tool_modules", ()),
                    "selected_combination_prompt_modules": getattr(plan, "combination_modules", ()),
                    "selected_business_tools": selected_tools,
                },
            )
        except Exception as exc:
            raise AgentToolConfigurationError(
                "tool_prompt_render_failed",
                f"failed to render selected business tool prompt: {exc}",
            ) from exc

        if self.native_tool_protocol and _native_prompt_execution_conflict(system_prompt):
            raise AgentToolConfigurationError("tool_prompt_render_failed", "native prompt contains incompatible legacy execution syntax")

        if plan is not None:
            # Prompt/schema symmetry is a hard contract.  Fail closed rather
            # than deleting matching lines and possibly hiding unrelated rules.
            for business_name in BUSINESS_TOOL_NAMES:
                if business_name in selected_names:
                    continue
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(business_name)}(?![A-Za-z0-9_])",
                    system_prompt,
                ):
                    raise AgentToolConfigurationError(
                        "tool_prompt_render_failed",
                        f"unselected business tool {business_name!r} leaked into rendered prompt",
                    )
        return system_prompt

    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        if tool_name not in self.tools and tool_name not in self.managed_agents:
            from smolagents.agents import AgentToolExecutionError

            hidden_names = self._PROMPT_HIDDEN_TOOL_NAMES | self._SCHEMA_HIDDEN_TOOL_NAMES
            visible_names = [
                name
                for name in (*self.tools.keys(), *self.managed_agents.keys())
                if name not in hidden_names
            ]
            raise AgentToolExecutionError(
                f"Unknown tool {tool_name}, should be one of: {', '.join(visible_names)}.",
                self.logger,
            )
        return super().execute_tool_call(tool_name, arguments)

    def _normalize_tool_call(self, tool_call):
        """Normalize JSON arguments without inventing hidden tool aliases."""
        name = getattr(tool_call.function, "name", "")
        args = tool_call.function.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"command": args}
        if not isinstance(args, dict):
            args = {}

        normalized_args = dict(args)

        tool_call.function.arguments = normalized_args
        return tool_call

    def write_memory_to_messages(self, summary_mode: bool = False, *, computer_use_history_stage=AUTO_PROJECTION, consume_tool_images: bool = True, committed_results_only: bool = False) -> list[ChatMessage]:
        """Serialize canonical memory while preserving trusted tool observations.

        smolagents 图片有两个来源：
          1. TaskStep.task_images —— 用户在本轮上传的图片，to_messages() 每次都包含
          2. ActionStep.observations_images —— smolagents _run_stream 把本次 run()
             的 images 参数复制到每个 ActionStep（L573），永远是任务级别的图片副本，
             不是工具返回的图片（真正的工具产出图片走 AgentImage/state 路径）。

        TaskStep messages and every ActionStep tool call/observation are rendered
        canonically in their original order for stable prefixes. Unmarked
        ActionStep images remain treated as duplicated task images. Images
        issued by the verified computer-use runtime are retained at their
        ActionStep, subject only to the staged latest-only projection.

        用户图片的正确位置：始终在对应 TaskStep 的 USER 消息中。序列化过程
        不删除历史任务图片；provider preparation 负责确定性地编码为 image_url。
        """
        from copy import copy

        messages = self.memory.system_prompt.to_messages(summary_mode=summary_mode)
        steps = self.memory.steps

        # 找出最后一个 TaskStep 的索引（代表当前任务）
        last_task_idx = max(
            (i for i, s in enumerate(steps) if isinstance(s, TaskStep)),
            default=-1,
        )

        # ── 诊断日志：记录 memory steps 状态 ──
        task_steps_info = []
        for i, s in enumerate(steps):
            if isinstance(s, TaskStep):
                has_imgs = s.task_images is not None and len(s.task_images) > 0
                task_steps_info.append(f"TaskStep[{i}](images={has_imgs})")
        logger.info(
            f"[CYCLE-DIAG] write_memory_to_messages: "
            f"total_steps={len(steps)}, last_task_idx={last_task_idx}, "
            f"task_steps={task_steps_info}"
        )

        def _normalize_tool_call_arguments(arguments):
            if isinstance(arguments, str):
                try:
                    return json.loads(arguments)
                except json.JSONDecodeError:
                    return arguments
            return arguments

        def _tool_call_protocol_text(tool_calls) -> str | None:
            if not tool_calls:
                return None

            calls: list[TextToolCall] = []
            for tool_call in tool_calls:
                name = getattr(tool_call, "name", None)
                arguments = getattr(tool_call, "arguments", None)
                if name is None and hasattr(tool_call, "function"):
                    function = getattr(tool_call, "function")
                    name = getattr(function, "name", "")
                    arguments = getattr(function, "arguments", None)
                normalized_arguments = _normalize_tool_call_arguments(arguments)
                if not isinstance(normalized_arguments, dict):
                    raise ValueError(
                        f"Tool call {name!r} arguments must be a JSON object"
                    )
                calls.append(
                    TextToolCall(
                        name=name or "",
                        arguments=normalized_arguments,
                    )
                )
            return format_tool_call_sequence(calls)

        def _normalize_model_output_text(model_output) -> str:
            if isinstance(model_output, str):
                return model_output.strip()

            if isinstance(model_output, list):
                text_parts = []
                for item in model_output:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(str(item.get("text", "")))
                joined = "\n".join(part for part in text_parts if part)
                if joined:
                    return joined.strip()

            return str(model_output).strip()

        def _append_live_action_step_messages(
            step: ActionStep,
            observation_projection=None,
        ) -> None:
            if self.native_tool_protocol:
                from backend.llm_service import Message
                calls = [call_to_dict(c) for c in (step.tool_calls or ())]
                content = getattr(step, "canonical_visible_content", step.model_output) or ""
                reasoning = getattr(step, "canonical_reasoning_content", None)
                if calls or content or reasoning:
                    messages.append(Message(role="assistant", content=content,
                        tool_calls=calls or None, reasoning_content=reasoning))
                records = projected_tool_results(step)
                if records is None and calls and committed_results_only and step.observations is None and step.error is None:
                    records = []
                if records is None and calls:
                    if len(calls) != 1:
                        raise ValueError("Native multi-call memory requires typed per-call results")
                    # The ordinary parent single-call execution has one exact observation.
                    observation = projected_observations(step)
                    if observation is None and step.error is not None:
                        observation = str(step.error)
                    records = [CanonicalToolResult(calls[0]["id"], calls[0]["function"]["name"],
                               observation or "", "failed" if step.error else "completed")]
                for result in (records or ()):
                    messages.append(Message(role="tool", content=result.content,
                        name=result.name, tool_call_id=result.call_id))
                if observation_projection is not None:
                    kind, value = observation_projection
                    projected = ([{"type": "image", "image": image} for image in value]
                                 if kind == "images" else [{"type": "text", "text": value}])
                    messages.append(Message(role="user", content=projected))
                if step.error is not None and not calls:
                    messages.append(Message(role="user", content="Internal server feedback: " + str(step.error)))
                return

            if step.model_output is not None and not summary_mode:
                messages.append(
                    ChatMessage(
                        role=MessageRole.ASSISTANT,
                        content=[
                            {
                                "type": "text",
                                "text": _normalize_model_output_text(
                                    step.model_output
                                ),
                            }
                        ],
                    )
                )

            tool_call_text = _tool_call_protocol_text(step.tool_calls)
            if tool_call_text is not None:
                messages.append(
                    ChatMessage(
                        role=MessageRole.TOOL_CALL,
                        content=[
                            {
                                "type": "text",
                                "text": tool_call_text,
                            }
                        ],
                    )
                )

            if observation_projection is not None:
                projection_kind, projection_value = observation_projection
                if projection_kind == "images":
                    content = [
                        {"type": "image", "image": image}
                        for image in projection_value
                    ]
                else:
                    content = [{"type": "text", "text": projection_value}]
                messages.append(ChatMessage(role=MessageRole.USER, content=content))

            if step.observations is not None:
                messages.append(
                    ChatMessage(
                        role=MessageRole.TOOL_RESPONSE,
                        content=[
                            {
                                "type": "text",
                                "text": f"Observation:\n{projected_observations(step)}",
                            }
                        ],
                    )
                )

            if step.error is not None:
                error_message = (
                    "Error:\n"
                    + str(step.error)
                    + "\nNow let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach.\n"
                )
                message_content = (
                    f"Call id: {step.tool_calls[0].id}\n"
                    if step.tool_calls
                    else ""
                )
                message_content += error_message
                messages.append(
                    ChatMessage(
                        role=MessageRole.TOOL_RESPONSE,
                        content=[
                            {
                                "type": "text",
                                "text": message_content,
                            }
                        ],
                    )
                )

        for memory_step in steps:
            if isinstance(memory_step, TaskStep):
                if self.native_tool_protocol and getattr(memory_step, "canonical_routing_tail", False):
                    from backend.llm_service import Message
                    content = [{"type": "image", "image": image} for image in (memory_step.task_images or ())]
                    placeholder = getattr(memory_step, "historical_image_placeholder", "")
                    if placeholder:
                        content.append({"type": "text", "text": placeholder})
                    content.append({"type": "text", "text": memory_step.task})
                    messages.append(Message(role="user", content=content))
                else:
                    messages.extend(memory_step.to_messages(summary_mode=summary_mode))
            elif isinstance(memory_step, ActionStep):
                observation_projection = projected_action_observation(
                    getattr(self, "_agent_session", None),
                    memory_step,
                    stage=computer_use_history_stage,
                )
                stripped = copy(memory_step)
                # 注意：不能 strip tool_calls！
                # 如果 tool_calls 被设为 None，ActionStep.to_messages() 不会生成
                # TOOL_CALL 消息，导致 observations 变成孤立的 TOOL_RESPONSE，
                # 破坏 action→observation 配对，使模型在多轮对话中"发疯"。
                # Unmarked images are task-image copies and remain stripped.
                # Trusted computer-use images are serialized explicitly above.
                stripped.observations_images = None
                _append_live_action_step_messages(
                    stripped,
                    observation_projection,
                )
            else:
                messages.extend(memory_step.to_messages(summary_mode=summary_mode))

        # ── 注入工具加载的图片（从文件 IPC 读取） ──
        session_id = current_session_context.get()
        tool_images = _read_and_consume_tool_images(
            session_id,
            capability=getattr(self, "_operation_capability", None),
        ) if consume_tool_images else []
        if tool_images:
            image_content = []
            for img in tool_images:
                image_content.append({"type": "image", "image": img})
            image_content.append(
                {
                    "type": "text",
                    "text": "[工具加载的图片已注入，请直接分析这些图片内容]",
                }
            )
            messages.append(
                ChatMessage(
                    role=MessageRole.USER,
                    content=image_content,
                )
            )
            logger.info(
                f"write_memory_to_messages: 注入 {len(tool_images)} 张工具加载图片"
            )

        return messages

    @staticmethod
    def _format_manager_turn_blackboard(
        ordered_dispatch_calls: list,
        results_by_call_id: dict[str, Any],
        *,
        configured_maximum: int,
        executed_count: int,
    ) -> str:
        parts = ["Shared child-agent blackboard for this turn."]
        skipped = []
        for call in ordered_dispatch_calls:
            arguments = call.function.arguments
            child_id = arguments.get("child_id", "<invalid>") if isinstance(arguments, dict) else "<invalid>"
            result = results_by_call_id.get(call.id)
            status = result.status if result is not None else "failed"
            output = result.output if result is not None else None
            if isinstance(output, ManagerChildDispatchOutcome):
                status = output.status
                report = output.final_report
                error = output.error
            else:
                report = ""
                error = result.error if result is not None else "No result"
            section = [f"Call: {call.id}", f"Child: {child_id}", f"Status: {status}"]
            if status == "completed":
                section.extend(["Final report:", report])
            else:
                section.extend(["Error:", error or "No final report."])
            parts.append("\n".join(section))
            if status == "skipped":
                skipped.append(f"{call.id}/{child_id}")
        if skipped:
            parts.append(
                "\n".join(
                    [
                        "Dispatch limit applied.",
                        f"Configured maximum: {configured_maximum}",
                        f"Requested child calls: {len(ordered_dispatch_calls)}",
                        f"Executed child calls: {executed_count}",
                        "Not executed: " + ", ".join(skipped),
                    ]
                )
            )
        return "\n\n---\n\n".join(parts)

    def _promote_computer_use_attachment(self, tool_output, memory_step) -> None:
        """Attach a verified computer-use observation to the current action."""
        attachment = getattr(tool_output, "attachment", None)
        if attachment is None:
            return

        from backend.agent.computer_use_history import (
            mark_computer_use_observation,
            stage_computer_use_history_omission,
        )
        from backend.computer_use.executable_protocol import (
            VisionDesktopCommandResult,
            is_trusted_observation_attachment,
        )

        if not isinstance(tool_output, VisionDesktopCommandResult):
            raise ValueError("untrusted executable result type")
        if not is_trusted_observation_attachment(attachment):
            raise ValueError("untrusted observation provenance")
        session = getattr(self, "_agent_session", None)
        context_id = getattr(session, "session_id", None)
        if not isinstance(context_id, str) or not context_id:
            raise ValueError("missing Agent context identity")
        image = mark_computer_use_observation(
            attachment.image,
            context_id=context_id,
            observation_id=attachment.observation_id,
            geometry_version=attachment.geometry_version,
        )
        current_images = list(memory_step.observations_images or [])
        current_images.append(image)
        memory_step.observations_images = current_images
        # smolagents appends the current ActionStep only after tool processing
        # returns. Include it now so the next accepted provider request can
        # commit the exact candidate after the step is canonical memory.
        stage_computer_use_history_omission(session, pending_step=memory_step)

    def _record_calibration_decision(self, request_id, tool_calls) -> None:
        """Record a parsed Agent decision only for an explicitly armed session."""
        session = getattr(self, "_agent_session", None)
        sink = getattr(session, "computer_use_calibration_evidence", None)
        if (sink is None or not request_id or getattr(session, "is_child_context", False)
                or getattr(sink, "session_id", None) != getattr(session, "session_id", None)):
            return
        try:
            calls = [{"name": call.function.name,
                      "arguments": call.function.arguments} for call in tool_calls]
            sink.record_decision(request_id, session.session_id, calls)
        except Exception as exc:
            logger.warning("computer-use calibration decision capture failed: %s", exc)

    def _handoff_limit_reached(self) -> bool:
        session = getattr(self, "_agent_session", None)
        return bool(self.native_tool_protocol and session is not None
                    and getattr(session, "turn_switch_count", 0) >= getattr(session, "max_agent_switches_per_turn", 2))

    def _retain_native_output(self, memory_step, raw_content: str) -> None:
        memory_step.raw_model_output = raw_content
        memory_step.canonical_visible_content = native_control_prose(raw_content)
        memory_step.model_output = memory_step.canonical_visible_content
        message = memory_step.model_output_message
        if message is not None:
            message.content = memory_step.canonical_visible_content

    def process_tool_calls(self, chat_message, memory_step):
        """Execute all accepted calls from one model turn in one parallel stage."""
        from smolagents.memory import ToolCall as MemoryToolCall
        from backend.agent.orchestrator import ToolResult

        tool_calls = chat_message.tool_calls or []
        # A computer-use executable operation is stateful: the next operation
        # must consume the image returned by the previous one.  The
        # orchestrator intentionally runs one model turn as one stage, so two
        # dependent vision_desktop calls in the same batch would otherwise run
        # concurrently and could race the active frame/crop generation.
        vision_desktop_calls = []
        for call in tool_calls:
            function = getattr(call, "function", None)
            if getattr(function, "name", None) != "exec_command":
                continue
            arguments = getattr(function, "arguments", None)
            command = arguments.get("command") if isinstance(arguments, dict) else None
            if isinstance(command, str) and command.lstrip().startswith("vision_desktop"):
                vision_desktop_calls.append(call)
        if len(vision_desktop_calls) > 1:
            raise ValueError(
                "dependent vision_desktop operations must be issued in separate Agent turns"
            )
        basic_agent_calls = [
            tool_call
            for tool_call in tool_calls
            if getattr(getattr(tool_call, "function", None), "name", None) == "basic_agent"
        ]

        if basic_agent_calls and len(tool_calls) > 1:
            raise ValueError(
                "coordinator_2 must emit either a basic_agent handoff or tool calls, not both"
            )

        if len(tool_calls) == 1:
            tool_call = tool_calls[0]
            function = getattr(tool_call, "function", None)
            if getattr(function, "name", None) == "basic_agent":
                args = getattr(function, "arguments", None)
                if not isinstance(args, dict):
                    raise ValueError(
                        "basic_agent arguments must already be a validated JSON object"
                    )
                target_keys = {"agent_name", "target_agent", "target", "name"}
                if target_keys.intersection(args):
                    target_agent = self.model._normalize_native_switch_target(args)
                    if target_agent != "basic_agent":
                        raise ValueError(
                            "basic_agent call has an invalid or conflicting target"
                        )
                task = args.get("task") or args.get("prompt") or args.get("request")
                additional_args = {
                    key: value
                    for key, value in args.items()
                    if key
                    not in {
                        "task",
                        "prompt",
                        "request",
                        "agent_name",
                        "target_agent",
                        "target",
                        "name",
                    }
                }
                if self.native_tool_protocol:
                    additional_args = args.get("additional_args")
                    memory_step.canonical_tool_results = [CanonicalToolResult(tool_call.id, "basic_agent",
                        "Handoff request accepted: target_agent=basic_agent")]
                memory_step.tool_calls = [
                    MemoryToolCall(
                        name="basic_agent",
                        arguments=args,
                        id=tool_call.id,
                    )
                ]
                if self._handoff_limit_reached():
                    from smolagents.agents import ActionOutput
                    memory_step.canonical_tool_results = [CanonicalToolResult(tool_call.id, "basic_agent",
                        "Handoff request rejected: Agent switch limit reached", "failed")]
                    yield ActionOutput(output=None, is_final_answer=False)
                    return
                yield SecondaryHandoffRequest(
                    target_agent="basic_agent",
                    task=(task if self.native_tool_protocol else str(task).strip())
                    if isinstance(task, str) and task.strip()
                    else None,
                    additional_args=additional_args or None,
                )
                return

        dispatch_name = "dispatching_parallel_agent"
        has_dispatch = any(
            getattr(getattr(call, "function", None), "name", None) == dispatch_name
            for call in tool_calls
        )
        has_vision_desktop = bool(vision_desktop_calls)
        if len(tool_calls) <= 1 and not has_dispatch:
            if not has_vision_desktop:
                if not self.native_tool_protocol:
                    yield from super().process_tool_calls(chat_message, memory_step)
                    return
                from smolagents.agents import ToolOutput
                memory_step.tool_calls = [MemoryToolCall(name=c.function.name, arguments=c.function.arguments, id=c.id)
                                          for c in tool_calls]
                for output in super().process_tool_calls(chat_message, memory_step):
                    if isinstance(output, ToolOutput):
                        original = output.output if isinstance(output.output, str) else output.observation
                        memory_step.canonical_tool_results = [CanonicalToolResult(output.id,
                            output.tool_call.name, original)]
                    yield output
                return

            # Keep the ordinary single-tool execution contract while
            # promoting the executable's verified screenshot attachment.
            from smolagents.agents import ToolOutput

            for output in super().process_tool_calls(chat_message, memory_step):
                if isinstance(output, ToolOutput):
                    try:
                        self._promote_computer_use_attachment(
                            output.output, memory_step
                        )
                    except Exception as exc:
                        rejection = (
                            "computer-use observation attachment rejected: "
                            f"{exc}"
                        )
                        output.output = rejection
                        output.observation = rejection
                    if self.native_tool_protocol:
                        original = output.output if isinstance(output.output, str) else output.observation
                        memory_step.canonical_tool_results = [CanonicalToolResult(output.id,
                            output.tool_call.name, original)]
                yield output
            return

        # ── 多工具：使用 ToolOrchestrator 编排执行 ──
        from smolagents.agents import ActionOutput

        tools_dict = {}
        if isinstance(self.tools, dict):
            tools_dict = dict(self.tools)
        else:
            for tool in self.tools:
                name = getattr(tool, "name", None)
                if name:
                    tools_dict[name] = tool

        dispatch_calls = [
            call for call in tool_calls if call.function.name == dispatch_name
        ]
        configured_maximum = self._manager_agent_max_parallel_children
        if has_dispatch and (
            isinstance(configured_maximum, bool)
            or not isinstance(configured_maximum, int)
            or configured_maximum <= 0
        ):
            raise ValueError("manager_agent_max_parallel_children must be a positive integer")

        duplicate_ids: set[str] = set()
        child_id_counts: dict[str, int] = {}
        for call in dispatch_calls:
            args = call.function.arguments
            if isinstance(args, dict) and isinstance(args.get("child_id"), str):
                child_id = args["child_id"]
                child_id_counts[child_id] = child_id_counts.get(child_id, 0) + 1
        duplicate_ids = {child_id for child_id, count in child_id_counts.items() if count > 1}

        accepted_dispatch_ids: set[str] = set()
        preflight_results: list[ToolResult] = []
        accepted_count = 0
        for call in dispatch_calls:
            args = call.function.arguments
            if not isinstance(args, dict):
                preflight_results.append(ToolResult(call.id, dispatch_name, "failed", error="arguments must be an object with exactly child_id and task"))
                continue
            if set(args) != {"child_id", "task"}:
                preflight_results.append(ToolResult(call.id, dispatch_name, "failed", error="arguments must contain exactly child_id and task"))
                continue
            try:
                AgentRunner._validate_manager_child_dispatch(args["child_id"], args["task"])
            except ValueError as exc:
                preflight_results.append(ToolResult(call.id, dispatch_name, "failed", error=str(exc)))
                continue
            if args["child_id"] in duplicate_ids:
                preflight_results.append(ToolResult(call.id, dispatch_name, "failed", error=f"duplicate child_id in one turn: {args['child_id']}"))
                continue
            if accepted_count >= configured_maximum:
                preflight_results.append(ToolResult(call.id, dispatch_name, "skipped", error="user-configured child-call limit"))
                continue
            accepted_dispatch_ids.add(call.id)
            accepted_count += 1

        executable_calls = [
            call
            for call in tool_calls
            if call.function.name != dispatch_name or call.id in accepted_dispatch_ids
        ]
        turn_dispatch_id = f"manager-dispatch-{uuid.uuid4().hex}" if has_dispatch else None
        dispatch_tool = tools_dict.get(dispatch_name)
        manager_session = getattr(dispatch_tool, "_manager_session", None)
        dispatch_runner = getattr(dispatch_tool, "_runner", None)
        if manager_session is not None and turn_dispatch_id is not None and dispatch_runner is not None:
            dispatch_runner._append_manager_dispatch_timeline_event(
                manager_session,
                "manager_dispatch_started",
                dispatch_id=turn_dispatch_id,
                child_count=accepted_count,
                configured_maximum=configured_maximum,
                requested_count=len(dispatch_calls),
            )

        plan = self.orchestrator.analyze(executable_calls)
        yield {
            "_orchestration_event": True,
            "type": "orchestration_plan",
            **plan.to_dict(),
        }

        buffered_events = []

        def _event_callback(event: dict):
            buffered_events.append(
                {
                    "_orchestration_event": True,
                    **event,
                }
            )

        results = self.orchestrator.execute(
            plan,
            tools_dict,
            event_callback=_event_callback,
            turn_dispatch_id=turn_dispatch_id,
        )
        for result in results:
            if isinstance(result.output, ManagerChildDispatchOutcome):
                result.status = result.output.status
                result.error = result.output.error
        terminal_dispatch_results = {
            result.call_id: result
            for result in results
            if isinstance(result.output, ManagerChildDispatchOutcome)
        }
        for event in buffered_events:
            result = terminal_dispatch_results.get(event.get("call_id"))
            if result is None or event.get("status") == "running":
                continue
            event["status"] = result.status
            if result.error:
                event["error"] = result.error
            if result.status != "completed":
                event.pop("result_preview", None)
        results.extend(preflight_results)
        for result in preflight_results:
            buffered_events.append(
                {
                    "_orchestration_event": True,
                    "type": "orchestration_update",
                    "call_id": result.call_id,
                    "status": result.status,
                    "error": result.error,
                }
            )

        # 5. Yield 所有缓冲的中间事件
        for evt in buffered_events:
            yield evt

        results_by_call_id = {result.call_id: result for result in results}
        normal_parts = []
        memory_tool_calls = []
        for tc in tool_calls:
            result = results_by_call_id.get(tc.id)
            args = (
                tc.function.arguments if isinstance(tc.function.arguments, dict) else {}
            )
            memory_tool_calls.append(
                MemoryToolCall(
                    name=tc.function.name,
                    arguments=args,
                    id=tc.id,
                )
            )

            if tc.function.name != dispatch_name and result:
                # Promote only the verified executable attachment.  A string
                # mentioning provenance, or an arbitrary tool return object,
                # never grants image-history eligibility.
                if tc.function.name == "exec_command" and result.status == "completed":
                    attachment = getattr(result.output, "attachment", None)
                    if attachment is not None:
                        try:
                            self._promote_computer_use_attachment(
                                result.output, memory_step
                            )
                        except Exception as exc:
                            result.status = "failed"
                            result.error = f"computer-use observation attachment rejected: {exc}"
                if result.status == "completed":
                    normal_parts.append(
                        f"[Tool {tc.function.name}] (call_id={tc.id}):\n{result.output}"
                    )
                else:
                    normal_parts.append(
                        f"[Tool {tc.function.name}] (call_id={tc.id}): "
                        f"status={result.status}, error={result.error}"
                    )

        combined_parts = list(normal_parts)
        if dispatch_calls:
            combined_parts.append(
                self._format_manager_turn_blackboard(
                    dispatch_calls,
                    results_by_call_id,
                    configured_maximum=configured_maximum,
                    executed_count=accepted_count,
                )
            )
        combined_observation = "\n\n".join(combined_parts)
        # Legacy observations are indivisible. Retain the entire block when it
        # contains failures/skips rather than guessing which text is an error.
        memory_step._tool_feedback_contains_unsuccessful_result = any(
            result.status != "completed"
            or (isinstance(result.output, ManagerChildDispatchOutcome)
                and result.output.status != "completed")
            for result in results_by_call_id.values()
        )
        if self.native_tool_protocol:
            typed_results = []
            last_dispatch_id = dispatch_calls[-1].id if dispatch_calls else None
            # The final dispatch receipt contains every sibling's result. Its
            # own successful status must not make sibling error feedback eligible
            # for historical text omission.
            if last_dispatch_id is not None and any(
                results_by_call_id.get(call.id) is None
                or results_by_call_id[call.id].status != "completed"
                for call in dispatch_calls
            ):
                memory_step._tool_feedback_error_result_ids = frozenset({last_dispatch_id})
            for tc in tool_calls:
                result = results_by_call_id.get(tc.id)
                status = result.status if result is not None else "failed"
                if result is not None and isinstance(result.output, ManagerChildDispatchOutcome):
                    status = result.output.status
                if tc.function.name == dispatch_name:
                    if tc.id == last_dispatch_id:
                        content = self._format_manager_turn_blackboard(dispatch_calls, results_by_call_id,
                            configured_maximum=configured_maximum, executed_count=accepted_count)
                    else:
                        content = (f"Call: {tc.id}\nChild: {tc.function.arguments['child_id']}\nStatus: {status}\n"
                                   f"Shared results: see tool response {last_dispatch_id} in this batch.")
                elif result is not None and status == "completed":
                    content = str(result.output)
                else:
                    content = f"status={status}, error={result.error if result is not None else 'No result'}"
                typed_results.append(CanonicalToolResult(tc.id, tc.function.name, content, status))
            memory_step.canonical_tool_results = typed_results

        memory_step.observations = combined_observation
        memory_step.tool_calls = memory_tool_calls

        if manager_session is not None and turn_dispatch_id is not None and dispatch_runner is not None:
            status_counts: dict[str, int] = {}
            for call in dispatch_calls:
                status = results_by_call_id[call.id].status
                output = results_by_call_id[call.id].output
                if isinstance(output, ManagerChildDispatchOutcome):
                    status = output.status
                status_counts[status] = status_counts.get(status, 0) + 1
            dispatch_runner._append_manager_dispatch_timeline_event(
                manager_session,
                "manager_dispatch_completed",
                dispatch_id=turn_dispatch_id,
                child_count=accepted_count,
                status_counts=status_counts,
            )

        # 7. Yield 编排完成事件
        yield {
            "_orchestration_event": True,
            "type": "orchestration_done",
            "total_tools": len(tool_calls),
            "completed": sum(1 for r in results if r.status == "completed"),
            "failed": sum(1 for r in results if r.status == "failed"),
            "timeout": sum(1 for r in results if r.status == "timeout"),
            "skipped": sum(1 for r in results if r.status == "skipped"),
        }

        # 8. Yield ActionOutput 让上层知道步骤已完成（非 final）
        yield ActionOutput(output=combined_observation, is_final_answer=False)

    def _step_stream(self, memory_step: ActionStep):
        """
        覆盖原始 _step_stream：
        - 纯文本 → 自动 final_answer
        - final_answer 工具调用 → 静默拦截，不经过 process_tool_calls
        - 真实工具调用 → 正常执行（不自动完成，允许多步任务）
        """
        from smolagents.memory import ToolCall as MemoryToolCall

        session = getattr(self, "_agent_session", None)
        enable_computer_use_projection = bool(
            session is not None
            and not getattr(session, "is_child_context", False)
            and not getattr(session, "answering_now", False)
            and not getattr(session, "answer_now", False)
            and not getattr(session, "aborted", False)
            and getattr(session, "computer_use_history_omission_stage", None) is not None
        )
        if enable_computer_use_projection:
            restage_computer_use_history_for_same_blocks(session)
        # Bind this exact candidate to this serialization/provider call. Late
        # responses must never fetch a newer candidate from mutable session state.
        computer_use_history_stage = (
            computer_use_history_stage_for_request(session) if enable_computer_use_projection else None
        )
        memory_messages = self.write_memory_to_messages(
            computer_use_history_stage=computer_use_history_stage)
        input_messages = memory_messages.copy()
        memory_step.model_input_messages = input_messages

        _used_streaming = False
        _stream_delta_count = 0
        stream_content_from_deltas = ""
        stream_diagnostics = {}
        session_id = current_session_context.get()
        request_id = None
        try:
            if session_id:
                from backend.llm_service import create_request_id

                request_id = create_request_id()
                session_request_manager.set(session_id, request_id)
                # The abort marker and request registration can race between
                # agent steps. Mark this exact request pending and target it
                # before the provider call so a child cannot start a
                # post-abort inference.
                if abort_manager.is_aborted(session_id):
                    from backend.config import get_config
                    from backend.llm_service import (
                        abort_request_by_id,
                        register_request,
                        unregister_request,
                    )

                    config = get_config()
                    request_registration = register_request(request_id)
                    try:
                        abort_request_by_id(
                            request_id,
                            f"http://{config.sglang.host}:{config.sglang.port}",
                            fallback_to_abort_all=False,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Pre-provider targeted request abort failed "
                            "session_id=%s request_id=%s: %s",
                            session_id,
                            request_id,
                            exc,
                        )
                    finally:
                        # This request never reaches the provider, so consume
                        # the temporary pending-abort registration here.
                        unregister_request(request_id, request_registration)
                    raise RuntimeError(
                        f"Session {session_id} was aborted before provider request"
                    )
            if self.stream_outputs and hasattr(self.model, "generate_stream"):
                _used_streaming = True
                output_stream = self.model.generate_stream(
                    input_messages,
                    stop_sequences=["Observation:"],
                    tools_to_call_from=self.tools_and_managed_agents,
                    request_id=request_id,
                    computer_use_history_stage=computer_use_history_stage,
                )
                from smolagents.models import agglomerate_stream_deltas
                from rich.live import Live
                from rich.markdown import Markdown

                chat_message_stream_deltas = []
                pending_cli_text = ""
                # Reuse the per-run Live owned by _run_basic_stream. If no
                # parent Live exists, do NOT create a local per-step Live:
                # Rich freezes the rendered markdown when a Live context exits,
                # so any fallback `with Live(...)` here brings back the
                # duplicate-terminal-snapshot bug for paths that reach
                # _step_stream without the parent run-level Live.
                shared_live: Live | None = self._cli_live
                if shared_live is not None:
                    # Parent (e.g. _run_basic_stream) owns the Live
                    # lifecycle. We just push updates into it. No `with`
                    # block — the parent closes it after agent.run() ends.
                    for event in output_stream:
                        chat_message_stream_deltas.append(event)
                        _stream_delta_count += 1
                        delta_text = getattr(event, "content", None)
                        if delta_text:
                            pending_cli_text += str(delta_text)

                        should_emit_cli_chunk = self._should_emit_cli_stream_chunk
                        if should_emit_cli_chunk is None or should_emit_cli_chunk(
                            pending_cli_text,
                            source=self._cli_stream_source,
                        ):
                            shared_live.update(
                                Markdown(
                                    agglomerate_stream_deltas(
                                        chat_message_stream_deltas
                                    ).render_as_markdown()
                                )
                            )
                            pending_cli_text = ""
                        yield event
                    if (
                        pending_cli_text.strip()
                        and self._cli_stream_source
                        not in COORDINATOR_ROUTER_PROFILE_NAMES
                    ):
                        shared_live.update(
                            Markdown(
                                agglomerate_stream_deltas(
                                    chat_message_stream_deltas
                                ).render_as_markdown()
                            )
                        )
                else:
                    for event in output_stream:
                        chat_message_stream_deltas.append(event)
                        _stream_delta_count += 1
                        yield event
                stream_content_from_deltas = "".join(
                    str(delta.content)
                    for delta in chat_message_stream_deltas
                    if getattr(delta, "content", None)
                    and not getattr(delta, "reasoning", False)
                )
                chat_message = agglomerate_stream_deltas(chat_message_stream_deltas)
            else:
                chat_message: ChatMessage = self.model.generate(
                    input_messages,
                    stop_sequences=["Observation:"],
                    tools_to_call_from=self.tools_and_managed_agents,
                    request_id=request_id,
                    computer_use_history_stage=computer_use_history_stage,
                )
                self.logger.log_markdown(
                    content=str(chat_message.content or chat_message.raw or ""),
                    title="Output message of the LLM:",
                    level=LogLevel.DEBUG,
                )

            stream_state = None
            if _used_streaming and hasattr(self.model, "get_stream_state"):
                stream_state = self.model.get_stream_state(request_id)
            if _used_streaming and hasattr(self.model, "get_stream_diagnostics"):
                stream_diagnostics = self.model.get_stream_diagnostics(request_id)
            memory_step.model_output_message = chat_message
            memory_step.model_output = chat_message.content
            # 流式模式下 chat_message.token_usage 为 None，需要从 model 获取
            if _used_streaming and stream_state is not None:
                output_text = stream_state.get("content", "") or str(
                    chat_message.content or ""
                )
                usage = (
                    self.model.llm_service.get_stream_usage(output_text, request_id=request_id)
                    if hasattr(self.model, "llm_service")
                    else None
                )
                if usage:
                    memory_step.token_usage = TokenUsage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                    )
                    # 权威 usage 为普通 dict；仅服务端 total (>0) 有效时回填，
                    # 否则保留 TokenUsage 自动计算的 input+output
                    server_total = usage.get("total_tokens")
                    if isinstance(server_total, int) and server_total > 0:
                        memory_step.token_usage.total_tokens = server_total
                    logger.info(
                        f"📊 流式 token 统计: input={memory_step.token_usage.input_tokens}, "
                        f"output={memory_step.token_usage.output_tokens}, "
                        f"total={memory_step.token_usage.total_tokens}, "
                        f"image_tokens={usage.get('image_tokens', 0)}, "
                        f"cached_tokens={usage.get('cached_tokens', 0)}, "
                        f"source={usage.get('source', 'unknown')}"
                    )
                else:
                    memory_step.token_usage = chat_message.token_usage
            else:
                memory_step.token_usage = chat_message.token_usage
        except TextToolProtocolError as exc:
            from smolagents.utils import AgentParsingError

            raw_debug = exc.__dict__.pop("raw_call_debug", None)
            if self.native_tool_protocol and raw_debug is None and _used_streaming:
                # A stream gate can reject EOF before the resolver runs. Read
                # its exact request-scoped content before finally clears it.
                state_getter = getattr(self.model, "get_stream_state", None)
                state = state_getter(request_id) if callable(state_getter) else None
                raw = state.get("content", "") if state else ""
                if raw and native_control_prose(raw) != raw:
                    raw_debug = {"text": raw, "source": "model_text"}
            if self.native_tool_protocol and raw_debug is not None:
                yield RawToolCallDebug(raw_tool_call=raw_debug["text"], source=raw_debug["source"])
            raise AgentParsingError(str(exc), self.logger) from exc
        except Exception as e:
            from smolagents.agents import AgentGenerationError

            raise AgentGenerationError(
                f"Error while generating output:\n{e}", self.logger
            ) from e
        finally:
            if session_id and request_id:
                session_request_manager.clear(session_id, request_id)
            if request_id and hasattr(self.model, "clear_stream_state"):
                self.model.clear_stream_state(request_id)
            if hasattr(self.model, "llm_service") and hasattr(
                self.model.llm_service, "discard_stream_usage"
            ):
                self.model.llm_service.discard_stream_usage(request_id)

        # ── 流式路径修正：将 content 从 reasoning+content 混合体还原为仅 content ──
        # generate_stream 为了让思考框流式显示，把 reasoning 和 content 都
        # 作为 content delta yield 出来。agglomerate_stream_deltas 会把它们
        # 拼成 chat_message.content = reasoning + content。
        # 工具调用解析只能基于 content 部分（不含 reasoning）。最终回复也优先
        # 使用 content；仅 reasoning-only 响应沿用下方既有的文本兜底行为。
        _full_agglomerated = None
        tool_parse_content = (
            stream_content_from_deltas
            if _used_streaming
            else chat_message.content or ""
        )
        if _used_streaming and stream_state is not None:
            _full_agglomerated = (
                chat_message.content
            )  # 保存完整文本（reasoning+content）备用
            stream_content = stream_state.get("content", "")
            stream_reasoning = stream_state.get("reasoning", "")
            # Only provider-classified final content may carry executable tool
            # syntax. Reasoning remains display/history text even when it is
            # promoted below as a reasoning-only final-answer fallback.
            tool_parse_content = stream_content
            if not stream_content and stream_reasoning:
                stream_content = stream_reasoning
            if stream_content or stream_reasoning:
                chat_message.content = stream_content
                memory_step.model_output = chat_message.content
                logger.info(
                    f"📝 流式输出统计: content={len(stream_content)} 字符, "
                    f"reasoning={len(stream_reasoning)} 字符, "
                    f"完整={len(_full_agglomerated or '')} 字符"
                )

        if self.native_tool_protocol:
            memory_step.canonical_reasoning_content = (
                stream_state.get("reasoning") or None if _used_streaming and stream_state is not None
                else getattr(chat_message, "reasoning_content", None))

        # Parse exactly one complete assistant control source. Streaming gates may
        # suppress every visible control delta, so execution must use the raw,
        # request-scoped provider-classified content retained above.
        raw_tool_call = str(tool_parse_content or "")
        if _used_streaming and stream_state is not None and not self.native_tool_protocol:
            split_fenced_fragment = reconstruct_split_fenced_tool_call(
                stream_state.get("reasoning", ""),
                raw_tool_call,
            )
            if split_fenced_fragment is not None:
                raw_tool_call = split_fenced_fragment
        provider_tool_calls = chat_message.tool_calls
        if (
            _used_streaming
            and not provider_tool_calls
            and stream_state is not None
        ):
            provider_tool_calls = stream_state.get("provider_tool_calls")
        textual_call_attempt = (
            starts_textual_tool_call_attempt(raw_tool_call)
            or has_malformed_control_attempt(raw_tool_call)
            or (self.native_tool_protocol and native_control_prose(raw_tool_call) != raw_tool_call)
        )
        raw_provider_calls = None
        if self.native_tool_protocol:
            raw_provider_calls = (stream_state.get("provider_tool_calls")
                if _used_streaming and stream_state is not None
                else getattr(chat_message, "raw_provider_tool_calls", None))
        if textual_call_attempt or raw_provider_calls:
            # The exact provider text is transient UI/debug data. Clear the
            # ActionStep before parsing so valid and rejected attempts can never
            # become canonical memory or compact input.
            if not self.native_tool_protocol:
                memory_step.model_output = None
                if memory_step.model_output_message is not None:
                    memory_step.model_output_message.content = None
            debug_source = raw_tool_call if textual_call_attempt else json.dumps(
                raw_provider_calls, ensure_ascii=False, allow_nan=False)
            yield RawToolCallDebug(raw_tool_call=debug_source,
                source="model_text" if textual_call_attempt else "provider_payload")

        try:
            cached_resolution = (stream_state.get("control_resolution") if _used_streaming and stream_state is not None
                                 else getattr(chat_message, "control_resolution", None))
            if self.native_tool_protocol and cached_resolution is not None:
                resolution = cached_resolution
            elif not _used_streaming and chat_message.tool_calls:
                # LocalLLMModel.generate() already selected and validated the
                # provider/text adapter. Re-submitting its adapted text calls as
                # provider-native calls would manufacture a false dual encoding.
                resolution = SimpleNamespace(
                    source="native",
                    tool_calls=tuple(chat_message.tool_calls),
                    switch_agent=None,
                )
            else:
                resolution = self.model._resolve_assistant_control(
                    raw_tool_call,
                    provider_tool_calls,
                    self.tools_and_managed_agents,
                )
        except TextToolProtocolError as exc:
            from smolagents.utils import AgentParsingError
            if self.native_tool_protocol:
                self._retain_native_output(memory_step, raw_tool_call)
                memory_step.tool_calls = None
                memory_step.canonical_tool_results = []
            raise AgentParsingError(str(exc), self.logger) from exc

        if resolution.switch_agent is not None:
            if self.native_tool_protocol:
                self._retain_native_output(memory_step, raw_tool_call)
                call = resolution.control_call
                memory_step.tool_calls = [MemoryToolCall(name="switch_agent", arguments=call.function.arguments, id=call.id)]
                memory_step.canonical_tool_results = [CanonicalToolResult(call.id, "switch_agent",
                    f"Handoff request accepted: target_agent={resolution.switch_agent.target_agent}")]
            else:
                memory_step.model_output = None
                if memory_step.model_output_message is not None:
                    memory_step.model_output_message.content = None
            if self._handoff_limit_reached():
                memory_step.canonical_tool_results = [CanonicalToolResult(call.id, "switch_agent",
                    "Handoff request rejected: Agent switch limit reached", "failed")]
                yield ActionOutput(output=None, is_final_answer=False)
                return
            yield SecondaryHandoffRequest(
                target_agent=resolution.switch_agent.target_agent
            )
            return

        if resolution.tool_calls:
            real_tool_calls = [
                self._normalize_tool_call(tool_call)
                for tool_call in resolution.tool_calls
            ]
            self._record_calibration_decision(request_id, real_tool_calls)
            chat_message.tool_calls = real_tool_calls
            if self.native_tool_protocol:
                self._retain_native_output(memory_step, raw_tool_call)
            else:
                memory_step.model_output = None
                if memory_step.model_output_message is not None:
                    memory_step.model_output_message.content = None
            for output in self.process_tool_calls(chat_message, memory_step):
                yield output
            yield ActionOutput(output=None, is_final_answer=False)
            return

        if resolution.source == "text":
            # Hidden defensive controls (for example final_answer) are never
            # executable and their framed bytes must never become model prose.
            memory_step.model_output = None
            if memory_step.model_output_message is not None:
                memory_step.model_output_message.content = None
            yield ActionOutput(output=None, is_final_answer=False)
            return

        plain_text = chat_message.content or ""
        yield ActionOutput(output=plain_text, is_final_answer=True)
        return

# Session 管理
# ============================================================


@dataclass
class ManagerChildDispatchOutcome:
    call_id: str
    child_id: str
    status: str
    final_report: str = ""
    error: str = ""


EXPLICIT_CHILD_ABORT_REASON = "The user has aborted this child agent run."
CHILD_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "aborted", "timeout"}
)


@dataclass
class ChildAgentRun:
    child_run_id: str
    manager_session_id: str
    dispatch_id: str
    child_name: str
    profile_name: str
    assigned_task: str
    call_id: str | None = None
    status: str = "pending"
    final_report: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    abort_reason: str | None = None
    assignment_anchor_visible: bool = field(default=False, repr=False)
    terminal_event: threading.Event = field(default_factory=threading.Event, repr=False)
    event_hub_producer_token: object | None = field(default=None, repr=False)
    event_hub_terminal_prepared: Any | None = field(default=None, repr=False)
    event_hub_terminal_expected_after_sequence: int | None = field(
        default=None, repr=False
    )
    event_hub_terminal_records: tuple[Any, ...] | None = field(
        default=None, repr=False
    )
    event_hub_terminal_reason: str | None = field(default=None, repr=False)
    event_hub_terminal_appended: bool = field(default=False, repr=False)
    event_hub_terminal_closed: bool = field(default=False, repr=False)
    event_hub_producer_released: bool = field(default=False, repr=False)
    event_hub_terminal_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    child_session_identity: "AgentSession | None" = field(default=None, repr=False)


@dataclass
class StagedEventHubImages:
    """Reversible session-owned asset update paired with one Hub append."""

    session: "AgentSession"
    asset_ids: tuple[str, ...]
    previous_assets: dict[str, str]
    previous_turn_assets: dict[int, tuple[str, ...]]
    next_assets: dict[str, str]
    next_turn_assets: dict[int, tuple[str, ...]]
    committed: bool = False

    def commit(self) -> None:
        with self.session.vision_event_asset_lock:
            self.commit_locked()

    def commit_locked(self) -> None:
        try:
            self.session.vision_event_image_assets.clear()
            self.session.vision_event_image_assets.update(self.next_assets)
            self.session.vision_event_turn_assets.clear()
            self.session.vision_event_turn_assets.update(self.next_turn_assets)
        except BaseException:
            self.rollback_locked(force=True)
            raise
        self.committed = True

    def rollback(self) -> None:
        with self.session.vision_event_asset_lock:
            self.rollback_locked()

    def rollback_locked(self, *, force: bool = False) -> None:
        if not self.committed and not force:
            return
        self.session.vision_event_image_assets.clear()
        self.session.vision_event_image_assets.update(self.previous_assets)
        self.session.vision_event_turn_assets.clear()
        self.session.vision_event_turn_assets.update(self.previous_turn_assets)
        self.committed = False


_MISSING_CAPABILITY = object()


@dataclass
class AgentSession:
    """Agent 会话"""

    session_id: str
    agent: MultiStepAgent
    mode: str
    capability: OperationCapability = field(
        default=_MISSING_CAPABILITY,  # type: ignore[arg-type]
        repr=False,
    )
    tool_configuration_snapshot: AgentToolConfigurationSnapshot | None = field(
        default=None, repr=False
    )
    event_hub: AgentSessionHub = field(default_factory=AgentSessionHub, repr=False)
    session_hub_entry: SessionHubEntry | None = field(default=None, repr=False)
    event_hub_turn_index: int = field(default=0, repr=False)
    current_agent_name: str = "coordinator_1"
    tool_protocol: str = "legacy_text"
    max_agent_switches_per_turn: int = 2
    active_agent: MultiStepAgent | None = None
    turn_switch_count: int = 0
    has_shared_history: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    aborted: bool = False
    answer_now: bool = False
    accumulated_content: str = ""
    accumulated_thought: str = ""
    has_run: bool = False  # 标记 agent.run() 是否已被调用过（用于控制 reset 行为）
    answering_now: bool = False
    run_in_progress: bool = False
    active_run_scope_ids: list[str] = field(default_factory=list, repr=False)
    timeline_events: list[dict[str, Any]] = field(default_factory=list)
    timeline_event_sinks: list[
        tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any] | None]]
    ] = field(default_factory=list, repr=False)
    behavior_records: dict[str, Any] = field(default_factory=dict)
    runtime_context: str | None = None
    last_llm_prompt_snapshot: Any | None = None
    last_compactable_llm_messages: list[Any] | None = None
    last_compactable_llm_tools: list[dict] | None = None
    last_compactable_memory_steps: tuple[Any, ...] | None = field(default=None, repr=False)
    last_compactable_agent_name: str | None = None
    compaction_archive: list[dict[str, Any]] = field(default_factory=list)
    is_child_context: bool = False
    vision_automation_binding: Any | None = None
    vision_automation_cleanup_token: Any | None = None
    vision_automation_image_omission_sticky: bool = False
    computer_use_history_omission_stage: Any | None = field(default=None, repr=False)
    computer_use_history_projection_enabled: bool = field(default=False, repr=False)
    # Explicitly armed, in-process calibration side channel. Ordinary sessions
    # retain no screenshot evidence on disk.
    computer_use_calibration_evidence: Any | None = field(default=None, repr=False)
    vision_event_image_assets: dict[str, str] = field(default_factory=dict, repr=False)
    vision_event_turn_assets: dict[int, tuple[str, ...]] = field(default_factory=dict, repr=False)
    vision_event_asset_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    _run_depth: int = field(default=0, init=False, repr=False)
    _run_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _run_condition: threading.Condition = field(init=False, repr=False)
    _answer_now_in_progress: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        PathPolicy.validate_session_id(self.session_id)
        if self.tool_configuration_snapshot is not None and not isinstance(
            self.tool_configuration_snapshot, AgentToolConfigurationSnapshot
        ):
            raise AgentToolConfigurationError(
                "invalid_tool_configuration",
                "AgentSession requires an immutable tool configuration snapshot",
            )
        # Admission-created sessions always carry a pinned capability.  Keep
        # direct in-process test/inspection sessions constructible without one;
        # every side-effecting wrapper still calls require_capability and
        # therefore remains fail-closed.  The sentinel is normalized to None so
        # cleanup paths do not accidentally treat it as authority.
        if self.capability is _MISSING_CAPABILITY:
            self.capability = None  # type: ignore[assignment]
        if self.capability is not None and not isinstance(self.capability, OperationCapability):
            raise CapabilityError(
                "invalid_capability", "AgentSession capability must be an operation capability"
            )
        if self.capability is not None and self.capability.session_id != self.session_id:
            raise CapabilityError(
                "capability_session_mismatch",
                "AgentSession capability does not match its session identity",
            )
        self._run_condition = threading.Condition(self._run_lock)
        if self.active_agent is None:
            self.active_agent = self.agent

    def abort(self):
        """标记会话为中止状态"""
        self.aborted = True

    def stage_event_hub_images(
        self,
        turn_index: int,
        images: list[str] | None,
        *,
        evict_prior: bool,
    ) -> StagedEventHubImages:
        """Prepare a reversible asset update without exposing it to subscribers."""

        with self.vision_event_asset_lock:
            asset_ids = tuple(
                f"{self.session_id}:hub-turn-{turn_index}:image-{index}"
                for index, _image in enumerate(images or (), start=1)
            )
            previous_assets = dict(self.vision_event_image_assets)
            previous_turn_assets = dict(self.vision_event_turn_assets)
            next_assets = {} if evict_prior else dict(previous_assets)
            next_turn_assets = {} if evict_prior else dict(previous_turn_assets)
            for asset_id, image in zip(asset_ids, images or (), strict=True):
                next_assets[asset_id] = image
            next_turn_assets[turn_index] = asset_ids
            return StagedEventHubImages(
                session=self,
                asset_ids=asset_ids,
                previous_assets=previous_assets,
                previous_turn_assets=previous_turn_assets,
                next_assets=next_assets,
                next_turn_assets=next_turn_assets,
            )

    def resolve_event_hub_images(self, asset_ids: tuple[str, ...] | list[str]) -> list[str] | None:
        with self.vision_event_asset_lock:
            images = [self.vision_event_image_assets.get(asset_id) for asset_id in asset_ids]
        if any(image is None for image in images):
            return None
        return [image for image in images if image is not None]

    def resolve_event_hub_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Project journal-only image assets without touching Agent memory."""

        resolved_payload = dict(payload)
        event = resolved_payload.get("event")
        if not isinstance(event, dict) or "image_asset_ids" not in event:
            return resolved_payload
        resolved_event = dict(event)
        asset_ids = tuple(resolved_event.get("image_asset_ids") or ())
        images = self.resolve_event_hub_images(asset_ids)
        resolved_event.pop("image_asset_ids", None)
        resolved_event["images"] = images if images is not None else []
        if images is None and resolved_event.get("historical_image_placeholder"):
            resolved_event["historical_image_omitted"] = True
        else:
            resolved_event.pop("historical_image_placeholder", None)
        resolved_payload["event"] = resolved_event
        return resolved_payload

    def release_event_hub_resources(self) -> None:
        """Release only Hub projection resources; model state has another owner."""

        with self.vision_event_asset_lock:
            self.vision_event_image_assets.clear()
            self.vision_event_turn_assets.clear()

    def clear_abort(self):
        """清除中止标志，允许会话继续复用。"""
        self.aborted = False

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

    def begin_answer_now(self):
        with self._run_condition:
            self.answering_now = True

    def finish_answer_now(self):
        with self._run_condition:
            self.answering_now = False
            self._answer_now_in_progress = False
            self._run_condition.notify_all()

    def try_begin_answer_now(self) -> bool:
        with self._run_condition:
            if self._answer_now_in_progress:
                return False
            self._answer_now_in_progress = True
            return True

    def capture_active_run_scopes(self) -> frozenset[str]:
        with self._run_condition:
            return frozenset(self.active_run_scope_ids)

    def wait_for_run_scopes(
        self,
        scope_ids: Collection[str],
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        pending_scope_ids = frozenset(scope_ids)
        with self._run_condition:
            while pending_scope_ids.intersection(self.active_run_scope_ids):
                if cancelled is not None and cancelled():
                    return False
                self._run_condition.wait(timeout=0.1)
            return cancelled is None or not cancelled()

    def wait_for_disposal_barrier(self, scope_ids: Collection[str]) -> None:
        """Wait for captured runs and externally owned Answer Now cleanup."""

        # Admission is blocked while disposal runs, so full quiescence also
        # catches a worker that registered immediately after this snapshot.
        with self._run_condition:
            while self.active_run_scope_ids or self._answer_now_in_progress:
                self._run_condition.wait(timeout=0.1)

    def begin_run(self) -> str:
        with self._run_lock:
            run_scope_id = uuid.uuid4().hex
            self._run_depth += 1
            self.run_in_progress = True
            self.active_run_scope_ids.append(run_scope_id)
            return run_scope_id

    def next_event_hub_turn_index(self) -> int:
        """Allocate UI-journal turn order without touching model-visible state."""

        with self._run_lock:
            self.event_hub_turn_index += 1
            return self.event_hub_turn_index

    def finish_run(self, run_scope_id: str | None = None):
        with self._run_lock:
            if run_scope_id is None:
                return
            try:
                self.active_run_scope_ids.remove(run_scope_id)
            except ValueError:
                return
            run_abort_manager.cleanup(run_scope_id)
            if self._run_depth > 0:
                self._run_depth -= 1
            self.run_in_progress = self._run_depth > 0
            self._run_condition.notify_all()

    def abort_active_runs(self) -> None:
        with self._run_lock:
            for run_scope_id in list(self.active_run_scope_ids):
                run_abort_manager.abort(run_scope_id)

    def rollback_run_state(
        self,
        baseline_step_count: int,
        baseline_has_run: bool,
        baseline_state,
    ) -> None:
        """回滚本轮 stop 前新增的临时状态，同时保留当前轮已可见上下文。"""
        preserved_current_turn = False
        if hasattr(self.agent, "memory") and hasattr(self.agent.memory, "steps"):
            all_steps = list(self.agent.memory.steps)
            preserved_steps = list(all_steps[:baseline_step_count])
            current_turn_steps = list(all_steps[baseline_step_count:])

            for step in current_turn_steps:
                if isinstance(step, TaskStep):
                    preserved_steps.append(step)
                    preserved_current_turn = True
                    continue

                if not isinstance(step, ActionStep):
                    continue

                # Completed tool observations are canonical history. Stop only
                # changes the unfinished worker's lifecycle/partial output.
                if (step.observations is not None or step.observations_images or step.error is not None or step.is_final_answer
                        or (getattr(self, "tool_protocol", "legacy_text") == "qwen35-native"
                            and getattr(step, "canonical_tool_results", None))):
                    preserved_steps.append(step)
                    preserved_current_turn = True
                    continue
                has_visible_output = bool(step.model_output or step.observations or step.error)
                if not has_visible_output:
                    continue

                interrupted_step = copy(step)
                interrupted_step.observations = None
                interrupted_step.observations_images = None
                interrupted_step.action_output = None
                interrupted_step.token_usage = None
                interrupted_step.error = None
                interrupted_step.is_final_answer = False
                setattr(interrupted_step, "_interrupted_by_stop", True)
                preserved_steps.append(interrupted_step)
                preserved_current_turn = True

            self.agent.memory.steps = preserved_steps
        if baseline_state is not None and hasattr(self.agent, "state"):
            self.agent.state = baseline_state
        self.has_run = baseline_has_run or preserved_current_turn
        self.answer_now = False
        self.answering_now = False
        self.clear_abort()


class SessionStore:
    """内存会话存储"""

    def __init__(
        self,
        session_hub_runtime: SessionHubRuntime | None = None,
        session_base_dir: str | Path | None = None,
    ):
        self._sessions: dict[str, AgentSession] = {}
        self._cycle_pins: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._session_hub_runtime = session_hub_runtime
        self._session_base_dir = (
            None
            if session_base_dir is None
            else Path(session_base_dir).expanduser().resolve(strict=False)
        )

    def cleanup_orphan_session_images(self, session_id: str) -> bool:
        """Best-effort cleanup for a validated ID with no in-memory session."""
        PathPolicy.validate_session_id(session_id)
        if self._session_base_dir is None:
            return False
        try:
            policy = PathPolicy(
                file_operation_roots=(),
                session_base_dir=self._session_base_dir,
            )
            removed = policy.cleanup_session_dir(session_id)
        except (OSError, PathPolicyError) as error:
            logger.warning("Rejected orphan session-image cleanup: %s", error)
            return False
        if removed:
            logger.info("已清理孤儿会话图片目录: %s", policy.validate_session_dir(session_id))
        return removed

    def _register_session_hub_locked(self, session: AgentSession) -> bool:
        runtime = self._session_hub_runtime
        if runtime is None:
            return True
        existing = runtime.resolve(session.session_id)
        if existing is not None:
            if existing.kind != "agent" or existing.hub is not session.event_hub:
                return False
            session.session_hub_entry = existing
            return True
        try:
            session.session_hub_entry = runtime.register(
                session_id=session.session_id,
                kind="agent",
                hub=session.event_hub,
                resolve_payload=session.resolve_event_hub_payload,
                release_resources=session.release_event_hub_resources,
            )
        except SessionHubConflict:
            return False
        return True

    def ensure_session_hub_registered(
        self,
        session: AgentSession,
        runtime: SessionHubRuntime | None = None,
    ) -> SessionHubEntry:
        """Register the exact stored Agent Hub in the process directory."""

        with self._lock:
            if self._sessions.get(session.session_id) is not session:
                raise RuntimeError("Agent admission session changed")
            if runtime is not None:
                if (
                    self._session_hub_runtime is not None
                    and self._session_hub_runtime is not runtime
                ):
                    raise RuntimeError("Agent SessionStore runtime changed")
                self._session_hub_runtime = runtime
            if self._session_hub_runtime is None:
                raise RuntimeError("shared Session Hub runtime is unavailable")
            if not self._register_session_hub_locked(session):
                raise SessionHubConflict(session.session_id)
            entry = session.session_hub_entry
            if entry is None:
                raise RuntimeError("Agent Session Hub registration is missing")
            return entry

    def _release_session_hub(self, session: AgentSession, reason: str) -> None:
        session.event_hub.close(reason)
        entry = session.session_hub_entry
        runtime = self._session_hub_runtime
        if entry is not None and runtime is not None:
            runtime.release_if_same(entry)
        else:
            session.event_hub.release()

    def get(self, session_id: str) -> AgentSession | None:
        PathPolicy.validate_session_id(session_id)
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.last_activity = datetime.now()
        return session

    def put(self, session: AgentSession):
        PathPolicy.validate_session_id(session.session_id)
        with self._lock:
            if not self._register_session_hub_locked(session):
                raise SessionHubConflict(session.session_id)
            self._sessions[session.session_id] = session

    def put_if_absent(self, session: AgentSession) -> bool:
        """Insert one exact session only when its ID is still unoccupied."""

        PathPolicy.validate_session_id(session.session_id)
        with self._lock:
            if session.session_id in self._sessions:
                return False
            if not self._register_session_hub_locked(session):
                return False
            self._sessions[session.session_id] = session
            return True

    def delete(self, session_id: str):
        PathPolicy.validate_session_id(session_id)
        with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed is not None:
            self._release_session_hub(removed, "session_deleted")
        if removed is not None:
            _cleanup_session_images(session_id, capability=removed.capability)
        else:
            self.cleanup_orphan_session_images(session_id)

    def delete_if_same(self, session_id: str, expected: AgentSession) -> bool:
        """Delete only the exact pinned object, never a same-ID replacement."""

        PathPolicy.validate_session_id(session_id)
        with self._lock:
            if self._sessions.get(session_id) is not expected:
                return False
            self._sessions.pop(session_id, None)
        self._release_session_hub(expected, "session_deleted")
        _cleanup_session_images(session_id, capability=expected.capability)
        return True

    def pin_cycle_session(self, owner_id: str, session_id: str) -> None:
        """Keep each Cycle-owned context alive even during day-long waits."""
        with self._lock:
            self._cycle_pins.setdefault(owner_id, set()).add(session_id)

    def unpin_cycle_owner(self, owner_id: str) -> None:
        with self._lock:
            self._cycle_pins.pop(owner_id, None)

    def cycle_pinned_session_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(sid for ids in self._cycle_pins.values() for sid in ids)

    def cleanup_expired(
        self,
        ttl_minutes: int,
        *,
        protected_session_ids: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        """清理过期 session"""
        now = datetime.now()
        with self._lock:
            cycle_pins = {sid for ids in self._cycle_pins.values() for sid in ids}
            expired = [
                (sid, session)
                for sid, session in self._sessions.items()
                if sid not in protected_session_ids
                and sid not in cycle_pins
                and not session.run_in_progress
                and not session.event_hub.has_active_producer
                and (now - session.last_activity).total_seconds() > ttl_minutes * 60
            ]
            for sid, _ in expired:
                del self._sessions[sid]
        for sid, session in expired:
            self._release_session_hub(session, "session_ttl_expired")
            _cleanup_session_images(sid, capability=session.capability)
            logger.info(f"Session {sid} 已过期清理")
        return tuple(sid for sid, _ in expired)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def session_ids(self) -> tuple[str, ...]:
        """Return a stable, thread-safe snapshot of stored session IDs."""
        with self._lock:
            return tuple(self._sessions)

    def exact_items(self) -> tuple[tuple[str, AgentSession], ...]:
        """Return pinned identities without refreshing their TTL timestamps."""

        with self._lock:
            return tuple(self._sessions.items())


# ============================================================
# Agent Runner
# ============================================================


def _chat_messages_to_backend_messages(chat_messages) -> list:
    """Convert smolagents ChatMessage objects into backend.llm_service.Message objects.

    Per spec §5.4:
      - role: ChatMessage.role is a MessageRole enum, .value gives the string.
        TOOL_CALL -> "assistant", TOOL_RESPONSE -> "user".
      - content: ChatMessage.content is list[dict] (multimodal shape); compact path
        does not carry images, so flatten to str by joining text parts.
    """
    from ..llm_service import Message
    if any(isinstance(cm, Message) or getattr(cm, "tool_call_id", None) is not None or getattr(cm, "role", None) == "tool"
           or getattr(cm, "tool_calls", None) or getattr(cm, "reasoning_content", None) for cm in chat_messages):
        return canonicalize_model_messages(chat_messages)
    from ..llm_service import Message

    out = []
    for cm in chat_messages:
        # role
        role_obj = cm.role
        if hasattr(role_obj, "value"):
            role_str = role_obj.value
        else:
            role_str = str(role_obj)
        # smolagents uses 'tool-call' / 'tool-response' for the tool turns;
        # the rest of our pipeline expects 'assistant' / 'user'.
        if role_str in ("tool-call", "tool_call"):
            role_str = "assistant"
        elif role_str in ("tool-response", "tool_response"):
            role_str = "user"

        # content
        content = cm.content
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = "\n".join(p for p in text_parts if p)
        elif content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        out.append(Message(role=role_str, content=content))
    return out


class AgentRunner:
    """Agent 调度器，负责构建 Agent 和管理执行"""

    def __init__(
        self,
        llm_service: LLMService,
        config: AppConfig,
        compact_orchestrator=None,
        session_hub_runtime: SessionHubRuntime | None = None,
    ):
        self.llm_service = llm_service
        self.config = config
        self.tool_catalog = build_business_tool_catalog(config)
        self.default_tool_configuration_snapshot = self.tool_catalog.resolve_candidate(None)
        self.sessions = SessionStore(
            session_hub_runtime=session_hub_runtime,
            session_base_dir=getattr(
                getattr(config, "agent", None), "session_temp_base_dir", None
            ),
        )
        self.session_hub_runtime = session_hub_runtime
        self.model = LocalLLMModel(
            llm_service=llm_service,
            model_id=config.model.model_name,
        )
        self.compact_orchestrator = compact_orchestrator
        # Bound by the application lifespan; child/tool/compact paths never
        # prepare ordinary top-level user turns themselves.
        self.local_memory_service = None
        self._local_memory_template_snapshots: dict[str, tuple[bool, str | None]] = {}
        self.child_agent_runs: dict[str, ChildAgentRun] = {}
        self.child_agent_sessions_by_key: dict[tuple[str, str], str] = {}
        self._retained_child_replay_ids: set[str] = set()
        self._child_agent_lock = threading.Lock()
        self._child_context_creation_lock = threading.Lock()
        self._agent_admission_lock = threading.Lock()
        self._pending_agent_admissions: dict[str, AgentRequestAdmissionStage] = {}
        self._session_disposal_tasks: dict[
            str, tuple[AgentSession, asyncio.Task[dict[str, str]]]
        ] = {}

    def _is_session_disposal_active(self, session_id: str) -> bool:
        return session_id in self._session_disposal_tasks

    def try_acquire_answer_now(
        self,
        session_id: str,
        *,
        expected_session: AgentSession | None = None,
    ) -> tuple[str, AgentSession | None]:
        """Atomically exclude disposal and acquire Answer Now ownership."""

        with self._agent_admission_lock:
            session = self.sessions.get(session_id)
            if expected_session is not None and session is not expected_session:
                return "busy", None
            if session is None:
                return "not_found", None
            if self._is_session_disposal_active(session_id):
                return "busy", None
            if not session.try_begin_answer_now():
                return "busy", None
            return "acquired", session

    def _get_profile_prompt_bundle(self, profile_name: str) -> dict:
        prompt_bundle = getattr(self.config.agent_prompts, profile_name, None)
        if not isinstance(prompt_bundle, dict) or not prompt_bundle:
            raise ValueError(f"Prompt bundle for profile '{profile_name}' is not configured")
        return prompt_bundle

    def _profile_prompt_templates(self, profile_name: str, dialect: str) -> dict:
        """Choose a complete profile template without upgrading retained contexts."""
        bundle = self._get_profile_prompt_bundle(profile_name)
        if dialect == "legacy_text":
            compatibility = self.config.agent_prompts.legacy_prompt_compatibility
            templates = bundle.get("legacy_prompt_templates")
            if templates is None:
                templates = compatibility.get("profiles", {}).get(profile_name)
            if templates is None:
                raise ValueError(f"{profile_name}.legacy_prompt_templates is required for legacy context")
        else:
            templates = bundle.get("prompt_templates")
        if not isinstance(templates, dict) or not isinstance(templates.get("system_prompt"), str) or not templates["system_prompt"].strip():
            raise ValueError(f"{profile_name}.prompt_templates.system_prompt is required")
        if dialect == "qwen35-native":
            def owned_strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict):
                    for nested in value.values():
                        yield from owned_strings(nested)
            native_fields = "\n".join(owned_strings({"instructions": bundle.get("instructions", ""), "templates": templates}))
            if _native_prompt_execution_conflict(native_fields):
                raise ValueError(f"{profile_name} native prompt contains legacy execution syntax")
            if "{{ native_tool_protocol_prompt }}" not in templates["system_prompt"]:
                templates = {**templates, "system_prompt": "{{ native_tool_protocol_prompt }}\n" + templates["system_prompt"]}
        return templates

    @staticmethod
    def _make_child_identity_id(manager_session_id: str, child_name: str) -> str:
        safe_parent = re.sub(r"[^A-Za-z0-9_.-]+", "-", manager_session_id).strip("-._")
        parent_digest = hashlib.sha256(manager_session_id.encode("utf-8")).hexdigest()[:12]
        safe_parent = safe_parent or "manager"
        safe_child = re.sub(r"[^A-Za-z0-9_.:-]+", "-", child_name).strip("-._:")
        safe_child = safe_child or "child"
        identity = f"{safe_parent}-{parent_digest}-child-{safe_child}"
        if len(identity) <= 128:
            return identity

        # Keep the existing readable identity for normal inputs.  At the
        # dispatch contract maximum, reserve enough room for deterministic
        # digests of both raw inputs so truncation cannot alias two children.
        child_digest = hashlib.sha256(child_name.encode("utf-8")).hexdigest()[:16]
        return f"{safe_parent[:31]}-{parent_digest}-child-{safe_child[:60]}-{child_digest}"

    def _ensure_profile_enabled(self, profile_name: str) -> None:
        if profile_name not in self.config.agent.enabled_agent_profiles:
            raise ValueError(f"Agent profile '{profile_name}' is not enabled")

    def _require_manager_tool_snapshot(
        self, manager_session: AgentSession, *, operation: str
    ) -> AgentToolConfigurationSnapshot:
        snapshot = manager_session.tool_configuration_snapshot
        if snapshot is not None:
            return snapshot
        audit_record = {
            "event": "manager_snapshot_missing",
            "operation": operation,
            "manager_session_id": manager_session.session_id,
        }
        manager_session.behavior_records.setdefault(
            "tool_configuration_audit", []
        ).append(audit_record)
        logger.error(
            "Manager tool snapshot missing: manager=%s operation=%s",
            manager_session.session_id,
            operation,
        )
        raise AgentToolConfigurationError(
            "tool_configuration_locked",
            "Manager Agent session has no frozen tool configuration",
        )

    def _assert_child_tool_snapshot_matches_manager(
        self,
        manager_session: AgentSession,
        child_session: AgentSession,
        *,
        operation: str,
    ) -> AgentToolConfigurationSnapshot:
        """Fail closed before a child shell can reuse or rebuild with drift."""

        expected = self._require_manager_tool_snapshot(
            manager_session,
            operation=operation,
        )
        actual = child_session.tool_configuration_snapshot
        if (
            actual is not None
            and actual.fingerprint == expected.fingerprint
            and actual.basic_agent == expected.basic_agent
        ):
            return expected

        audit_record = {
            "event": "child_snapshot_mismatch",
            "operation": operation,
            "child_session_id": child_session.session_id,
            "expected_fingerprint": expected.fingerprint,
            "actual_fingerprint": actual.fingerprint if actual is not None else None,
        }
        manager_session.behavior_records.setdefault(
            "tool_configuration_audit", []
        ).append(audit_record)
        logger.error(
            "Child tool snapshot mismatch: manager=%s child=%s operation=%s "
            "expected=%s actual=%s",
            manager_session.session_id,
            child_session.session_id,
            operation,
            expected.fingerprint,
            actual.fingerprint if actual is not None else "<missing>",
        )
        raise AgentToolConfigurationError(
            "tool_configuration_locked",
            "Child Agent tool configuration does not match its manager session",
        )

    def _filter_tools_for_profile(self, profile_name: str, tools: list) -> list:
        if profile_name in {"manager_agent", "basic_agent"}:
            raise RuntimeError(
                "manager/basic tool filtering requires an explicit frozen tool snapshot"
            )
        allowed_tools = PROFILE_TOOL_ALLOWLIST.get(profile_name)
        if allowed_tools is None:
            return tools
        return [tool for tool in tools if tool.name in allowed_tools]

    @staticmethod
    def _validate_manager_child_dispatch(child_id: str, task: str) -> tuple[str, str]:
        validate_child_id(child_id)
        if not isinstance(task, str) or len(re.sub(r"\s+", "", task)) < 10:
            raise ValueError("task must contain at least 10 non-space characters")
        return child_id, task

    def _build_dispatching_parallel_agent_tool(
        self, session: AgentSession | None
    ) -> Tool:
        runner = self

        class DispatchingParallelAgentTool(Tool):
            name = "dispatching_parallel_agent"
            description = (
                "Dispatch exactly one basic_agent child. The exact same child_id reuses "
                "that child context in a later turn; a different child_id creates a fresh context."
            )
            inputs = {
                "child_id": {
                    "type": "string",
                    "description": "Stable manager-scoped child identity.",
                },
                "task": {
                    "type": "string",
                    "description": "Self-contained English task briefing for exactly one child.",
                },
            }
            output_type = "any"

            def forward(
                self,
                child_id: str,
                task: str,
            ) -> ManagerChildDispatchOutcome:
                manager_session = session
                if manager_session is None:
                    current_session_id = current_session_context.get()
                    if current_session_id:
                        manager_session = runner.sessions.get(current_session_id)
                if manager_session is None:
                    raise ValueError("manager session is required")
                call_id = current_tool_call_context.get_call_id()
                turn_dispatch_id = current_tool_call_context.get_turn_dispatch_id()
                if not call_id or not turn_dispatch_id:
                    raise RuntimeError("dispatch tool requires turn execution context")
                return runner._run_async_blocking(
                    runner._dispatching_parallel_agent_for_session(
                        manager_session,
                        child_id,
                        task,
                        turn_dispatch_id=turn_dispatch_id,
                        call_id=call_id,
                    )
                )

        tool = DispatchingParallelAgentTool()
        tool._manager_session = session
        tool._runner = runner
        return tool

    def _run_async_blocking(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result: dict[str, Any] = {}

        def _run_in_thread() -> None:
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(
            target=_run_in_thread,
            name="manager-dispatch-tool-loop",
            daemon=True,
        )
        thread.start()
        thread.join()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _append_manager_dispatch_timeline_event(
        self, manager_session: AgentSession, kind: str, **event_fields: Any
    ) -> dict[str, Any]:
        event = {"kind": kind, "source": "manager_agent", **event_fields}
        self._append_timeline_event(manager_session, event)
        self._emit_timeline_event_to_active_sinks(manager_session, event)
        return event

    @staticmethod
    def _emit_timeline_event_to_active_sinks(
        session: AgentSession, event: dict[str, Any]
    ) -> None:
        for loop, queue in list(session.timeline_event_sinks):
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "timeline_event", "event": event},
            )

    def _append_manager_child_update(
        self,
        manager_session: AgentSession,
        child_run: ChildAgentRun,
        status: str,
        *,
        final_preview: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "dispatch_id": child_run.dispatch_id,
            "child_run_id": child_run.child_run_id,
            "child_name": child_run.child_name,
            "call_id": child_run.call_id,
            "status": status,
            "assigned_task": child_run.assigned_task,
            "href": f"/agent/child/{child_run.child_run_id}",
        }
        if final_preview:
            fields["final_preview"] = final_preview[:500]
        if error:
            fields["error"] = error
        return self._append_manager_dispatch_timeline_event(
            manager_session, "manager_child_agent_update", **fields
        )

    def _record_child_agent_event(
        self, child_run: ChildAgentRun, payload: dict[str, Any]
    ) -> None:
        with self._child_agent_lock:
            if self.child_agent_runs.get(child_run.child_run_id) is not child_run:
                raise RuntimeError("stale child Agent run callback")
            child_session = child_run.child_session_identity
            if child_session is None:
                raise RuntimeError("child Agent session identity is unavailable")
            if self.sessions.get(child_run.child_run_id) is not child_session:
                raise RuntimeError("stale child Agent session callback")
            producer_token = child_run.event_hub_producer_token
            if producer_token is None:
                raise RuntimeError("child Agent Hub producer is unavailable")
            if child_session.event_hub.capture_active_producer() is not producer_token:
                raise RuntimeError("stale child Agent producer callback")
        if child_session is None or producer_token is None:
            raise RuntimeError("child Agent Hub producer is unavailable")
        event = {**payload, "seq": child_session.event_hub.last_sequence}
        child_session.event_hub.append(event, producer_token=producer_token)

    def _record_child_terminal_event(
        self, child_run: ChildAgentRun, payload: dict[str, Any]
    ) -> None:
        reason = (
            "completed"
            if payload.get("type") == "done"
            else "aborted"
            if payload.get("type") == "aborted"
            else "error"
        )
        with self._child_agent_lock:
            if self.child_agent_runs.get(child_run.child_run_id) is not child_run:
                raise RuntimeError("stale child Agent run callback")
            child_session = child_run.child_session_identity
            if child_session is None:
                raise RuntimeError("child Agent session identity is unavailable")
            if self.sessions.get(child_run.child_run_id) is not child_session:
                raise RuntimeError("stale child Agent session callback")
            producer_token = child_run.event_hub_producer_token
            if not child_run.event_hub_producer_released:
                if producer_token is None:
                    raise RuntimeError("child Agent Hub producer is unavailable")
                if child_session.event_hub.capture_active_producer() is not producer_token:
                    raise RuntimeError("stale child Agent producer callback")
        with child_run.event_hub_terminal_lock:
            if child_run.event_hub_producer_released:
                return
            if child_session is None or producer_token is None:
                if child_run.event_hub_producer_released:
                    return
                raise RuntimeError("child Agent Hub producer is unavailable")
            hub = child_session.event_hub

            if child_run.event_hub_terminal_prepared is None:
                expected_after_sequence = hub.last_sequence
                event = {**payload, "seq": expected_after_sequence}
                prepared = hub.prepare_append_batch((event,))
                child_run.event_hub_terminal_prepared = prepared
                child_run.event_hub_terminal_expected_after_sequence = (
                    expected_after_sequence
                )
                child_run.event_hub_terminal_reason = reason
            elif child_run.event_hub_terminal_reason != reason:
                raise RuntimeError("child Agent terminal status already selected")

            if not child_run.event_hub_terminal_appended:
                if hub.capture_active_producer() is not producer_token:
                    raise RuntimeError("child Agent producer token is not current")
                try:
                    records = hub.append_batch(
                        child_run.event_hub_terminal_prepared,
                        producer_token=producer_token,
                    )
                except BaseException:
                    expected_after_sequence = (
                        child_run.event_hub_terminal_expected_after_sequence
                    )
                    if expected_after_sequence is None:
                        raise RuntimeError(
                            "child Agent terminal append sequence was not prepared"
                        )
                    records = hub.find_exact_appended_batch(
                        child_run.event_hub_terminal_prepared,
                        producer_token=producer_token,
                        after_sequence=expected_after_sequence,
                    )
                    if records is not None:
                        child_run.event_hub_terminal_records = records
                        child_run.event_hub_terminal_appended = True
                    raise
                child_run.event_hub_terminal_records = records
                child_run.event_hub_terminal_appended = True

            if not child_run.event_hub_terminal_closed:
                if hub.capture_active_producer() is not producer_token:
                    raise RuntimeError("child Agent producer token is not current")
                try:
                    closed = hub.close(reason, producer_token=producer_token)
                except BaseException:
                    if (
                        hub.capture_active_producer() is producer_token
                        and hub.close_reason == reason
                    ):
                        child_run.event_hub_terminal_closed = True
                    raise
                if not closed and not (
                    hub.capture_active_producer() is producer_token
                    and hub.close_reason == reason
                ):
                    raise RuntimeError("child Agent producer token is not current")
                child_run.event_hub_terminal_closed = True

            if child_run.event_hub_producer_released:
                return
            if hub.capture_active_producer() is not producer_token:
                if child_run.event_hub_terminal_closed:
                    child_run.event_hub_producer_released = True
                    child_run.event_hub_producer_token = None
                    return
                raise RuntimeError("child Agent producer token is not current")
            try:
                released = hub.release_producer(producer_token)
            except BaseException:
                if hub.capture_active_producer() is not producer_token:
                    child_run.event_hub_producer_released = True
                    child_run.event_hub_producer_token = None
                raise
            if not released:
                raise RuntimeError("child Agent producer token is not current")
            child_run.event_hub_producer_released = True
            if child_run.event_hub_producer_token is producer_token:
                child_run.event_hub_producer_token = None

    @staticmethod
    def _child_assignment_anchor_is_durable(
        child_run: ChildAgentRun, child_session: AgentSession
    ) -> bool:
        if child_run.assignment_anchor_visible:
            return True
        return bool(
            child_session.event_hub.find_payloads_by_fields(
                {
                    "type": "manager_assigned_task",
                    "child_run_id": child_run.child_run_id,
                    "child_name": child_run.child_name,
                    "call_id": child_run.call_id,
                    "task": child_run.assigned_task,
                }
            )
        )

    def _rollback_child_admission(
        self,
        child_run: ChildAgentRun,
        child_session: AgentSession,
        *,
        anchor_visible: bool | None = None,
        error: str | None = None,
    ) -> None:
        """Rollback only one failed pre-worker child admission transaction."""

        with self._child_agent_lock:
            if self.child_agent_runs.get(child_run.child_run_id) is not child_run:
                return
        if self.sessions.get(child_session.session_id) is not child_session:
            return

        producer_token = child_run.event_hub_producer_token
        if (
            producer_token is None
            or child_session.event_hub.capture_active_producer() is not producer_token
        ):
            return

        durable_anchor = self._child_assignment_anchor_is_durable(
            child_run, child_session
        )
        if anchor_visible is not None:
            durable_anchor = durable_anchor or anchor_visible
        child_run.status = "failed"
        child_run.error = error or child_run.error or "Child admission failed"
        child_run.completed_at = datetime.now()

        hub = child_session.event_hub
        if durable_anchor:
            terminal_settled = False
            try:
                self._record_child_terminal_event(
                    child_run,
                    {
                        "type": "error",
                        "status": child_run.status,
                        "child_run_id": child_run.child_run_id,
                        "call_id": child_run.call_id,
                        "error": child_run.error,
                    },
                )
            except BaseException:
                # The terminal recorder already reconciles append/close/release
                # mutations. A second invocation only completes its pending
                # phase and never duplicates an already durable phase.
                if not child_run.event_hub_producer_released:
                    try:
                        self._record_child_terminal_event(
                            child_run,
                            {
                                "type": "error",
                                "status": child_run.status,
                                "child_run_id": child_run.child_run_id,
                                "call_id": child_run.call_id,
                                "error": child_run.error,
                            },
                        )
                    except BaseException as exc:
                        logger.warning(
                            "Child admission terminal rollback did not settle child_run_id=%s: %s",
                            child_run.child_run_id,
                            exc,
                        )
            terminal_settled = (
                child_run.event_hub_terminal_appended
                and child_run.event_hub_terminal_closed
                and child_run.event_hub_producer_released
                and child_run.event_hub_producer_token is None
            )
        else:
            close_settled = False
            producer_released = False
            try:
                close_settled = bool(
                    hub.close("failed", producer_token=producer_token)
                )
                if not close_settled:
                    close_settled = hub.close_reason == "failed"
            except BaseException as exc:
                logger.warning(
                    "Child admission close rollback failed child_run_id=%s: %s",
                    child_run.child_run_id,
                    exc,
                )
                close_settled = hub.close_reason == "failed"
            if close_settled and hub.capture_active_producer() is producer_token:
                try:
                    if hub.release_producer(producer_token):
                        producer_released = True
                        child_run.event_hub_producer_released = True
                        child_run.event_hub_producer_token = None
                except BaseException as exc:
                    logger.warning(
                        "Child admission producer rollback failed child_run_id=%s: %s",
                        child_run.child_run_id,
                        exc,
                    )
                    if hub.capture_active_producer() is not producer_token:
                        producer_released = True
                        child_run.event_hub_producer_released = True
                        child_run.event_hub_producer_token = None
            terminal_settled = close_settled and producer_released
        if terminal_settled:
            child_run.terminal_event.set()

    def _ensure_disposed_child_terminal_replay(
        self, child_run: ChildAgentRun
    ) -> None:
        if child_run.status not in CHILD_TERMINAL_STATUSES:
            child_run.status = "aborted"
            child_run.error = (
                child_run.abort_reason or "The manager session is being disposed."
            )
        if child_run.completed_at is None:
            child_run.completed_at = datetime.now()

        terminal_type = (
            "done"
            if child_run.status == "completed"
            else "aborted"
            if child_run.status == "aborted"
            else "error"
        )
        terminal_payload: dict[str, Any] = {
            "type": terminal_type,
            "status": child_run.status,
            "child_run_id": child_run.child_run_id,
            "call_id": child_run.call_id,
        }
        if child_run.status == "completed":
            terminal_payload["final_report"] = child_run.final_report or ""
        else:
            terminal_payload["error"] = child_run.error or "Child agent failed"
        self._record_child_terminal_event(child_run, terminal_payload)
        child_run.terminal_event.set()

    def get_child_agent_run(self, child_run_id: str) -> ChildAgentRun:
        with self._child_agent_lock:
            child_run = self.child_agent_runs.get(child_run_id)
            if child_run is None:
                raise KeyError(child_run_id)
            return child_run

    async def _wait_for_child_run_quiescence(
        self,
        child_run: ChildAgentRun,
        child_session: AgentSession,
        *,
        request_abort: bool = True,
    ) -> None:
        """Abort one child run and wait for every captured worker scope to exit."""

        captured_scope_ids = child_session.capture_active_run_scopes()
        captured_request_id = self.get_current_request_id(child_session.session_id)

        if request_abort:
            child_session.abort()
            abort_manager.abort(child_session.session_id)
            child_session.abort_active_runs()

        if request_abort and captured_request_id:
            targeted_abort_task = asyncio.create_task(
                self._targeted_abort_disposal_request(
                    child_session.session_id,
                    captured_request_id,
                )
            )
            while not targeted_abort_task.done():
                try:
                    await asyncio.shield(targeted_abort_task)
                except asyncio.CancelledError:
                    continue
            targeted_abort_task.result()

        if not captured_scope_ids:
            return

        cleanup_task = asyncio.create_task(
            self._poll_child_run_scope_barrier(child_session, captured_scope_ids)
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Keep ownership of the cleanup task even when its caller is
            # cancelled.  A later same-child admission must never overlap it.
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            cleanup_task.result()
            return

    @staticmethod
    async def _poll_child_run_scope_barrier(
        child_session: AgentSession, captured_scope_ids: Collection[str]
    ) -> None:
        pending_scope_ids = frozenset(captured_scope_ids)
        while pending_scope_ids.intersection(child_session.capture_active_run_scopes()):
            await asyncio.sleep(0.01)

    async def abort_child_agent_run(self, child_run_id: str) -> dict[str, str]:
        """Abort one child and reset its model context after worker cleanup."""
        with self._child_agent_lock:
            child_run = self.child_agent_runs.get(child_run_id)
        if child_run is None:
            raise KeyError(child_run_id)

        child_session = self.sessions.get(child_run.child_run_id)
        explicit_terminal_abort = (
            child_run.status == "aborted"
            and child_run.abort_reason == EXPLICIT_CHILD_ABORT_REASON
        )
        if child_run.status in CHILD_TERMINAL_STATUSES and not explicit_terminal_abort:
            return {"status": "already_terminal", "child_run_id": child_run_id}
        if child_session is None:
            raise KeyError(child_run_id)

        if not explicit_terminal_abort:
            child_run.abort_reason = EXPLICIT_CHILD_ABORT_REASON
            await self._wait_for_child_run_quiescence(child_run, child_session)
            await self._wait_for_child_terminal_settlement(child_run)

        # The marker remains set until this short critical section completes.
        # Redispatch therefore cannot clear the abort signal and claim the old
        # Agent while the same shell is being reset.
        with self._child_context_creation_lock:
            with self._child_agent_lock:
                if self.child_agent_runs.get(child_run_id) is not child_run:
                    raise SessionBusyError(child_run_id)
                child_key = (child_run.manager_session_id, child_run.child_name)
                if self.child_agent_sessions_by_key.get(child_key) != child_run.child_run_id:
                    raise SessionBusyError(child_run_id)
            if self.sessions.get(child_run.child_run_id) is not child_session:
                raise SessionBusyError(child_run_id)
            self._reset_child_model_context(child_session, child_run)
            child_run.abort_reason = None
        return {"status": "aborted", "child_run_id": child_run_id}

    @staticmethod
    async def _wait_for_child_terminal_settlement(child_run: ChildAgentRun) -> None:
        """Wait for one exact run without occupying the loop's executor."""

        while not child_run.terminal_event.is_set():
            await asyncio.sleep(0.01)

    def _reset_child_model_context(
        self,
        child_session: AgentSession,
        child_run: ChildAgentRun | None = None,
    ) -> None:
        """Replace model-owned state while preserving one child shell and Hub."""

        if child_run is not None and not child_run.terminal_event.is_set():
            raise SessionBusyError(child_session.session_id)
        if (
            child_session.run_in_progress
            or child_session.capture_active_run_scopes()
            or child_session.event_hub.has_active_producer
        ):
            raise SessionBusyError(child_session.session_id)
        if child_run is None:
            raise SessionBusyError(child_session.session_id)
        manager_session = self.sessions.get(child_run.manager_session_id)
        if manager_session is None or manager_session.is_child_context:
            raise SessionBusyError(child_session.session_id)
        self._assert_child_tool_snapshot_matches_manager(
            manager_session,
            child_session,
            operation="reset",
        )

        self._release_session_resources(
            child_session,
            set(),
            release_hub=False,
        )
        runtime_context = self._build_runtime_context_for_profile(
            "basic_agent", session=child_session
        )
        fresh_agent = self._build_profile_agent(
            "basic_agent",
            runtime_context=runtime_context,
            session=child_session,
        )
        child_session.agent = fresh_agent
        child_session.active_agent = fresh_agent
        child_session.current_agent_name = "basic_agent"
        child_session.turn_switch_count = 0
        child_session.has_run = False
        child_session.has_shared_history = False
        child_session.answer_now = False
        child_session.answering_now = False
        session_request_manager.cleanup(child_session.session_id)
        abort_manager.clear(child_session.session_id)
        child_session.clear_abort()

    def _dispose_child_context(
        self, child_run: ChildAgentRun, child_session: AgentSession
    ) -> None:
        if child_session.run_in_progress:
            return
        is_current_context = self.sessions.get(child_session.session_id) is child_session
        self._release_session_resources(
            child_session,
            set(),
            release_hub=False,
        )
        if not is_current_context:
            return
        session_request_manager.cleanup(child_session.session_id)
        abort_manager.cleanup(child_session.session_id)
        child_key = (child_run.manager_session_id, child_run.child_name)
        with self._child_agent_lock:
            if self.child_agent_sessions_by_key.get(child_key) == child_session.session_id:
                self.child_agent_sessions_by_key.pop(child_key, None)

    async def stream_child_agent_events(
        self, child_run_id: str
    ) -> AsyncGenerator[str, None]:
        with self._child_agent_lock:
            child_run = self.child_agent_runs.get(child_run_id)
            if child_run is None:
                raise KeyError(child_run_id)
            child_session_id = child_run.child_run_id
        runtime = self.session_hub_runtime
        entry = (
            runtime.resolve(child_session_id)
            if isinstance(runtime, SessionHubRuntime)
            else None
        )
        if entry is None or entry.kind != "agent" or entry.hub.released:
            raise KeyError(child_run_id)
        async for frame in subscribe_session_hub(entry, 0):
            yield frame

    async def _dispatching_parallel_agent_for_session(
        self,
        manager_session: AgentSession,
        child_id: str,
        task: str,
        *,
        turn_dispatch_id: str,
        call_id: str,
    ) -> ManagerChildDispatchOutcome:
        child_id, task = self._validate_manager_child_dispatch(child_id, task)
        child_key = (manager_session.session_id, child_id)
        child_session: AgentSession
        reused_context: bool
        prior_step_count = 0
        expected_tool_snapshot = self._require_manager_tool_snapshot(
            manager_session,
            operation="create",
        )
        with self._child_context_creation_lock:
            while True:
                live_session_ids = set(self.sessions.session_ids())
                with self._child_agent_lock:
                    child_session_id = self.child_agent_sessions_by_key.get(
                        child_key
                    )
                    allocated_identity = child_session_id is None
                    if allocated_identity:
                        base_child_session_id = self._make_child_identity_id(
                            manager_session.session_id, child_id
                        )
                        child_session_id = base_child_session_id
                        if (
                            child_session_id in self._retained_child_replay_ids
                            or child_session_id in live_session_ids
                        ):
                            raise SessionBusyError(child_session_id)
                        self.child_agent_sessions_by_key[child_key] = (
                            child_session_id
                        )

                child_session = self.sessions.get(child_session_id)
                if child_session is not None:
                    if not allocated_identity:
                        if not child_session.is_child_context:
                            raise SessionBusyError(child_session_id)
                        with self._child_agent_lock:
                            current_child_run = self.child_agent_runs.get(
                                child_session_id
                            )
                        if current_child_run is not None and (
                            current_child_run.child_run_id != child_session_id
                            or current_child_run.manager_session_id
                            != manager_session.session_id
                            or current_child_run.child_name != child_id
                            or current_child_run.child_session_identity is not child_session
                        ):
                            raise SessionBusyError(child_session_id)
                        if (
                            current_child_run is not None
                            and not current_child_run.terminal_event.is_set()
                        ):
                            raise SessionBusyError(child_session_id)
                        if (
                            current_child_run is not None
                            and current_child_run.abort_reason
                            == EXPLICIT_CHILD_ABORT_REASON
                        ):
                            raise SessionBusyError(child_session_id)
                        if (
                            child_session.run_in_progress
                            or child_session.capture_active_run_scopes()
                            or child_session.event_hub.has_active_producer
                        ):
                            raise SessionBusyError(child_session_id)
                        self._assert_child_tool_snapshot_matches_manager(
                            manager_session,
                            child_session,
                            operation="reuse",
                        )
                        reused_context = True
                        if (
                            hasattr(child_session.agent, "memory")
                            and hasattr(child_session.agent.memory, "steps")
                        ):
                            prior_step_count = len(child_session.agent.memory.steps)
                        reused_context = bool(
                            child_session.has_run or prior_step_count
                        )
                        child_session.clear_abort()
                        child_session.reset_answer_now()
                        abort_manager.clear(child_session.session_id)
                        break
                    with self._child_agent_lock:
                        if (
                            self.child_agent_sessions_by_key.get(child_key)
                            == child_session_id
                        ):
                            self.child_agent_sessions_by_key.pop(child_key, None)
                    continue

                proposed_session: AgentSession | None = None
                proposed_inserted = False
                try:
                    # Keep the shell private until its Agent and model are
                    # fully constructed.  LocalLLMModel then captures this
                    # exact shell for both runtime-context configurations.
                    proposed_session = AgentSession(
                        tool_protocol=getattr(manager_session, "tool_protocol", "legacy_text"),
                        max_agent_switches_per_turn=self.config.agent.max_agent_switches_per_turn,
                        session_id=child_session_id,
                        agent=SimpleNamespace(
                            memory=SimpleNamespace(steps=[]), state={}
                        ),
                        mode=manager_session.mode,
                        tool_configuration_snapshot=expected_tool_snapshot,
                        capability=derive_child_capability(
                            manager_session.capability, child_session_id
                        ),
                        current_agent_name="basic_agent",
                        is_child_context=True,
                    )
                    proposed_agent = self._build_profile_agent(
                        "basic_agent", runtime_context=None, session=proposed_session
                    )
                    proposed_session.agent = proposed_agent
                    proposed_session.active_agent = proposed_agent
                    proposed_inserted = self.sessions.put_if_absent(proposed_session)
                    if not proposed_inserted:
                        raise SessionBusyError(child_session_id)
                    child_session = proposed_session
                    reused_context = False
                    break
                except BaseException:
                    if proposed_session is not None and not proposed_inserted:
                        self._release_session_resources(proposed_session, set())
                    with self._child_agent_lock:
                        if (
                            self.child_agent_sessions_by_key.get(child_key)
                            == child_session_id
                        ):
                            self.child_agent_sessions_by_key.pop(child_key, None)
                    raise

            child_run = ChildAgentRun(
                child_run_id=child_session_id,
                manager_session_id=manager_session.session_id,
                dispatch_id=turn_dispatch_id,
                child_name=child_id,
                profile_name="basic_agent",
                assigned_task=task,
                call_id=call_id,
                child_session_identity=child_session,
            )
            child_run.event_hub_producer_token = child_session.event_hub.claim_producer()
            if child_run.event_hub_producer_token is None:
                raise SessionBusyError(child_session.session_id)
            with self._child_agent_lock:
                self.child_agent_runs[child_run.child_run_id] = child_run

        try:
            self._record_child_agent_event(
                child_run,
                {
                    "type": "manager_assigned_task",
                    "child_run_id": child_run.child_run_id,
                    "child_name": child_id,
                    "call_id": call_id,
                    "task": task,
                    "reused_context": reused_context,
                    "prior_step_count": prior_step_count,
                },
            )
            child_run.assignment_anchor_visible = True
            self._append_manager_child_update(manager_session, child_run, "pending")
        except BaseException as exc:
            self._rollback_child_admission(
                child_run,
                child_session,
                anchor_visible=child_run.assignment_anchor_visible,
                error=str(exc),
            )
            raise

        quiescence_complete = False
        if manager_session.check_aborted() or child_session.check_aborted():
            child_run.status = "aborted"
            child_run.error = (
                child_run.abort_reason or "Child agent run was aborted before start"
            )
        else:
            child_run.status = "running"
            child_run.started_at = datetime.now()
            self._append_manager_child_update(manager_session, child_run, "running")
            try:
                report = await asyncio.wait_for(
                    self._run_child_agent_to_final_report(child_run, child_session),
                    timeout=self.config.agent.manager_agent_child_timeout_seconds,
                )
                if child_run.status not in CHILD_TERMINAL_STATUSES:
                    child_run.status = "completed"
                child_run.final_report = str(report or "")
                if child_run.status == "completed":
                    child_session.has_run = True
            except asyncio.TimeoutError:
                child_run.status = "timeout"
                child_run.error = (
                    "Child agent timed out after "
                    f"{self.config.agent.manager_agent_child_timeout_seconds:g} seconds"
                )
                await self._wait_for_child_run_quiescence(child_run, child_session)
                quiescence_complete = True
            except asyncio.CancelledError:
                child_run.status = "aborted"
                child_run.error = child_run.abort_reason or "Child agent run was aborted"
                cleanup_task = asyncio.create_task(
                    self._wait_for_child_run_quiescence(child_run, child_session)
                )
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        continue
                cleanup_task.result()
                quiescence_complete = True
            except Exception as exc:
                child_run.status = "failed"
                child_run.error = str(exc)
        if child_run.status != "completed" and not quiescence_complete:
            request_abort = bool(
                self.get_current_request_id(child_session.session_id)
                or child_session.capture_active_run_scopes()
            )
            await self._wait_for_child_run_quiescence(
                child_run,
                child_session,
                request_abort=request_abort,
            )

        child_run.completed_at = datetime.now()
        terminal_type = (
            "done" if child_run.status == "completed"
            else "aborted" if child_run.status == "aborted"
            else "error"
        )
        terminal_payload = {
            "type": terminal_type,
            "status": child_run.status,
            "child_run_id": child_run.child_run_id,
            "call_id": call_id,
        }
        if child_run.status == "completed":
            terminal_payload["final_report"] = child_run.final_report or ""
        else:
            terminal_payload["error"] = child_run.error or "Child agent failed"
        self._record_child_terminal_event(child_run, terminal_payload)
        try:
            self._append_manager_child_update(
                manager_session,
                child_run,
                child_run.status,
                final_preview=child_run.final_report,
                error=child_run.error,
            )
        finally:
            # Hub terminalization has already released the producer. The exact
            # run must become settled even if manager-side projection fails.
            child_run.terminal_event.set()
        return ManagerChildDispatchOutcome(
            call_id=call_id,
            child_id=child_id,
            status=child_run.status,
            final_report=child_run.final_report or "",
            error=child_run.error or "",
        )

    async def _run_child_agent_to_final_report(
        self, child_run: ChildAgentRun, session: AgentSession
    ) -> str:
        final_report_parts: list[str] = []
        async for event in self._run_basic_stream(
            session,
            child_run.assigned_task,
            pil_images=None,
            enable_thinking=None,
        ):
            if not event.startswith("data: "):
                continue
            try:
                payload = json.loads(event[6:])
            except json.JSONDecodeError:
                continue
            event_type = payload.get("type")
            if event_type == "content":
                self._record_child_agent_event(child_run, payload)
                final_report_parts.append(str(payload.get("content") or ""))
            elif event_type == "error":
                child_run.status = "failed"
                child_run.error = str(payload.get("error") or "Child agent failed")
                break
            elif event_type == "aborted":
                child_run.status = "aborted"
                child_run.error = child_run.abort_reason or "Child agent run was aborted"
                break
            elif event_type == "done":
                break
            else:
                self._record_child_agent_event(child_run, payload)
        return "".join(final_report_parts).strip()

    def _should_emit_cli_stream_chunk(self, text: str, *, source: str) -> bool:
        if source in COORDINATOR_CLI_SUPPRESSED_SOURCES:
            return False
        return len((text or "").strip()) >= 8

    @staticmethod
    def _build_managed_basic_agent_proxy(*, tool_protocol: str = "legacy_text") -> SimpleNamespace:
        inputs = ({
            "task": {"type": "string", "nullable": True, "description": "Self-contained task, if needed."},
            "additional_args": {"type": "object", "nullable": True, "description": "Explicit additional task arguments."},
        } if tool_protocol == "qwen35-native" else {})
        def _to_tool_calling_prompt() -> str:
            return (
                "basic_agent: 切换到 basic_agent 来执行外部搜索、文件检查、代码编辑、命令运行或其他工具能力。\n"
                f"    Takes inputs: {inputs}\n"
                "    Returns an output of type: string"
            )

        return SimpleNamespace(
            name="basic_agent",
            description="切换到 basic_agent 来执行外部搜索、文件检查、代码编辑、命令运行或其他工具能力。",
            inputs=inputs,
            output_type="string",
            to_tool_calling_prompt=_to_tool_calling_prompt,
        )

    def _build_profile_agent(
        self,
        profile_name: str,
        *,
        session: AgentSession | None = None,
        runtime_context: str | None = None,
        include_legacy_vlm_note: bool = False,
        tool_snapshot: AgentToolConfigurationSnapshot | None = None,
        tool_protocol_override: str | None = None,
        _available_tools_override: list | None = None,
    ) -> MultiStepAgent:
        self._ensure_profile_enabled(profile_name)
        prompt_bundle = self._get_profile_prompt_bundle(profile_name)

        instructions = prompt_bundle.get("instructions")
        tool_protocol = (getattr(session, "tool_protocol", "legacy_text") if session is not None
                         else tool_protocol_override or getattr(self.config.inference, "tool_protocol", "legacy_text"))
        prompt_templates = deepcopy(EMPTY_PROMPT_TEMPLATES)
        base_templates = (
            self.config.agent_prompts.legacy_prompt_compatibility.get("profiles", {}).get("basic_agent", {})
            if tool_protocol == "legacy_text" else self.config.agent_prompts.basic_agent.get("prompt_templates", {})
        )
        # Preserve shared non-protocol fallback instructions without allowing
        # another profile's system or routing template to fill a missing one.
        prompt_templates.update({key: value for key, value in base_templates.items()
                                 if key not in {"system_prompt", "routing_turn_prompt"}})
        profile_prompt_templates = self._profile_prompt_templates(profile_name, tool_protocol)
        prompt_templates.update(profile_prompt_templates)
        if tool_snapshot is None:
            if session is not None:
                tool_snapshot = session.tool_configuration_snapshot
                if tool_snapshot is None:
                    # Synthetic in-process inspection sessions have no
                    # admission capability and cannot execute side effects;
                    # let them use the runner's immutable default snapshot so
                    # prompt-stability checks remain possible. Real admitted
                    # sessions always carry both snapshots and capability.
                    if session.capability is None:
                        tool_snapshot = self.default_tool_configuration_snapshot
                    else:
                        logger.error(
                            "Existing Agent session has no frozen tool snapshot: session=%s profile=%s",
                            session.session_id,
                            profile_name,
                        )
                        raise AgentToolConfigurationError(
                            "tool_configuration_locked",
                            "Existing Agent session has no frozen tool configuration",
                        )
            else:
                tool_snapshot = self.default_tool_configuration_snapshot
        elif (
            session is not None
            and session.tool_configuration_snapshot is not None
            and (
                tool_snapshot.fingerprint
                != session.tool_configuration_snapshot.fingerprint
            )
        ):
            raise AgentToolConfigurationError(
                "tool_configuration_locked",
                "Profile build tool configuration does not match its session",
            )
        # The catalog is authoritative for selectable Agent tools.  The old
        # web_tools.enable_wikipedia switch remains honored by legacy callers
        # of _get_tools()/get_tools_from_config(), but cannot block an
        # explicitly selected composite in a frozen Agent snapshot.
        available_tools = (_available_tools_override if _available_tools_override is not None else
            get_tools_from_config(self.config, enable_selectable_wikipedia=True))
        business_tools_by_name = {
            tool.name: tool
            for tool in available_tools
            if getattr(tool, "name", None) in BUSINESS_TOOL_NAMES
        }

        def _selected_business_tools(selected_plan) -> list:
            missing = [
                name
                for name in selected_plan.tool_names
                if name not in business_tools_by_name
            ]
            if missing:
                logger.error(
                    "Selected business tool wrappers are unavailable: profile=%s missing=%s",
                    profile_name,
                    missing,
                )
                raise AgentToolConfigurationError(
                    "tool_prompt_render_failed",
                    "Selected business tool wrappers are unavailable: "
                    + ", ".join(missing),
                )
            return [
                business_tools_by_name[name]
                for name in selected_plan.tool_names
            ]

        if profile_name == "manager_agent":
            selected_plan = tool_snapshot.manager_agent
            tools = _selected_business_tools(selected_plan)
            # Keep the manager's pre-feature non-business profile policy.  Its
            # only runtime control tool is mandatory dispatch, built below.
            # In particular, business selection must not accidentally grant
            # dynamically discovered MCP tools to the manager.
            tools.append(self._build_dispatching_parallel_agent_tool(session))
        elif profile_name == "basic_agent":
            selected_plan = tool_snapshot.basic_agent
            selected_business_tools = _selected_business_tools(selected_plan)
            # basic_agent previously used PROFILE_TOOL_ALLOWLIST=None, so MCP
            # and other externally discovered non-business tools were visible.
            # The new checklist owns BUSINESS_TOOL_NAMES only and must not
            # silently revoke that unrelated legacy capability.
            non_business_tools = [
                tool
                for tool in available_tools
                if getattr(tool, "name", None) not in BUSINESS_TOOL_NAMES
            ]
            tools = selected_business_tools + non_business_tools
        else:
            selected_plan = None
            tools = self._filter_tools_for_profile(profile_name, available_tools)

        if (
            profile_name in COORDINATOR_ROUTER_PROFILE_NAMES
            and self.config.model.model_type == "vision"
            and self.config.agent_prompts.vlm_instructions
        ):
            vlm_note = self.config.agent_prompts.vlm_instructions
            instructions = f"{instructions}\n\n{vlm_note}" if instructions else vlm_note

        if (
            include_legacy_vlm_note
            and profile_name == LEGACY_RUNTIME_PROFILE_NAME
            and self.config.model.model_type == "vision"
            and self.config.agent_prompts.vlm_instructions
        ):
            vlm_note = self.config.agent_prompts.vlm_instructions
            instructions = f"{instructions}\n\n{vlm_note}" if instructions else vlm_note

        if selected_plan is not None:
            protocol = (
                self.config.agent_prompts.legacy_prompt_compatibility.get("business_tool_protocol", "")
                if tool_protocol == "legacy_text" else self.config.agent_prompts.business_tool_protocol
            )
            selected_plan = replace(selected_plan, business_tool_protocol=protocol if selected_plan.tool_names else "")

        kwargs = {
            "tools": tools,
            "model": self.model,
            "max_steps": self.config.agent.max_steps,
            "verbosity_level": 1,
            "stream_outputs": True,
            "agent_python_executable": self.config.agent.python_executable,
            "cli_stream_source": profile_name,
            "should_emit_cli_stream_chunk": self._should_emit_cli_stream_chunk,
            "tool_prompt_plan": selected_plan,
            "tool_protocol": tool_protocol,
            "native_tool_protocol_prompt": self.config.agent_prompts.native_tool_protocol_prompt,
            "allowed_switch_targets": ({"basic_agent"} if profile_name == "coordinator_2"
                                       else set(self.config.agent.enabled_agent_profiles) if profile_name == "coordinator_1" else set()),
        }
        if profile_name == "manager_agent":
            kwargs["manager_agent_max_parallel_children"] = (
                self.config.agent.manager_agent_max_parallel_children
            )

        if runtime_context is None:
            runtime_context = self._build_runtime_context_for_profile(
                profile_name, session=session
            )
        if runtime_context is not None:
            kwargs["model"] = LocalLLMModel(
                llm_service=self.llm_service,
                model_id=self.config.model.model_name,
                runtime_context=runtime_context,
                session=session,
            )
        elif session is not None:
            # A child shell must never fall back to the runner-wide model, even
            # when runtime time-context injection is disabled.
            kwargs["model"] = LocalLLMModel(
                llm_service=self.llm_service,
                model_id=self.config.model.model_name,
                runtime_context=None,
                session=session,
            )

        if profile_name == "coordinator_2":
            proxy = self._build_managed_basic_agent_proxy(tool_protocol=tool_protocol)
            kwargs["managed_agents"] = [proxy]

        kwargs["prompt_templates"] = dict(prompt_templates)

        if instructions:
            kwargs["instructions"] = instructions

        built_agent = SmartToolCallingAgent(**kwargs)
        if session is not None:
            built_agent._agent_session = session
            self._bind_tool_runtime_context(
                built_agent,
                session.session_id,
                None,
                session.capability,
            )
        return built_agent

    def _build_runtime_context_for_profile(
        self, profile_name: str, *, session: AgentSession | None = None
    ) -> str | None:
        if not self.config.runtime_prompts.enable_runtime_time_context:
            return None
        if not _profile_uses_runtime_time_context(profile_name):
            return None
        if session is not None:
            if session.runtime_context is None:
                session.runtime_context = _build_runtime_time_context()
            return session.runtime_context
        return _build_runtime_time_context()

    def _build_agent(self, mode: str) -> MultiStepAgent:
        """兼容旧运行时入口，暂时保持旧单代理运行时语义。"""
        normalize_agent_mode(mode)
        return self._build_profile_agent(
            LEGACY_RUNTIME_PROFILE_NAME,
            include_legacy_vlm_note=True,
            tool_protocol_override="legacy_text",
        )

    def _build_answer_now_prompt(self, session: AgentSession) -> str:
        return (
            "This is an internal Answer Now instruction, not a new user request. "
            "Based only on the preceding conversation, retained partial reasoning, "
            "and completed tool observations, provide the best immediate final answer now. "
            "Do not call or execute any tool in this Answer Now response. "
            "Use no tools for this response only. This restriction does not apply to later user turns. "
            "Do not route, hand off, "
            "continue planning, or start further investigation. Return only the final "
            "answer for the user."
        )

    def _get_runtime_agent(self, session: AgentSession) -> MultiStepAgent:
        return session.active_agent or session.agent

    def _entry_uses_coordinator_routing(self, session: AgentSession) -> bool:
        return session.current_agent_name in RUNTIME_COORDINATOR_ROUTER_NAMES

    def _stream_active_agent(
        self,
        session: AgentSession,
        message: str,
        pil_images: list | None,
        enable_thinking: bool | None = None,
    ) -> AsyncGenerator[str, None]:
        if session.current_agent_name == "coordinator_2":
            return self._coordinator_stream_secondary_handoff(
                session, message, pil_images, enable_thinking
            )
        return self._run_basic_stream(session, message, pil_images, enable_thinking)

    async def _sync_active_agent(
        self,
        session: AgentSession,
        message: str,
        pil_images: list | None,
        enable_thinking: bool | None = None,
    ) -> dict:
        if session.current_agent_name == "coordinator_2":
            return await self._coordinator_sync_secondary_handoff(
                session, message, pil_images, enable_thinking
            )
        return await self._run_basic_sync(session, message, pil_images, enable_thinking)

    def _build_coordinator_turn_message(
        self, message: str, profile_name: str | None = None, *, session=None
    ) -> str:
        raw_task = (message or "").strip()
        if not raw_task:
            raw_task = "[empty user message]"
        profile_name = profile_name or self.config.agent.entry_agent_name
        prompt_bundle = self._get_profile_prompt_bundle(profile_name)
        dialect = getattr(session, "tool_protocol", "legacy_text")
        templates = self._profile_prompt_templates(profile_name, dialect)
        template = templates.get("routing_turn_prompt")
        if not template:
            raise ValueError(
                f"{profile_name}.prompt_templates.routing_turn_prompt is required"
            )
        custom_instructions = (prompt_bundle.get("instructions") or "").strip()
        return populate_template(
            template,
            variables={
                "custom_instructions": custom_instructions,
                "raw_task": raw_task,
                "allowed_switch_targets": sorted(
                    getattr(getattr(session, "agent", None), "_allowed_switch_targets", ())
                    or self.config.agent.enabled_agent_profiles
                ),
            },
        ).strip()

    def _make_dispatch_step(
        self,
        dispatch_status: str,
        dispatch_stage: str,
        dispatch_target_agent: str | None = None,
        dispatch_message: str | None = None,
        *,
        dispatch_kind: str | None = None,
        display_slot: str | None = None,
        dispatch_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_dispatch_kind = dispatch_kind
        if normalized_dispatch_kind is None:
            normalized_dispatch_kind = (
                "secondary_handoff" if dispatch_stage == "handoff" else "routing"
            )
        normalized_display_slot = display_slot
        if normalized_display_slot is None:
            normalized_display_slot = (
                "after_body"
                if normalized_dispatch_kind == "secondary_handoff"
                else "before_body"
            )
        step = {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": dispatch_status,
            "dispatch_stage": dispatch_stage,
        }
        if normalized_dispatch_kind:
            step["dispatch_kind"] = normalized_dispatch_kind
        if normalized_display_slot:
            step["display_slot"] = normalized_display_slot
        if dispatch_id:
            step["dispatch_id"] = dispatch_id
        if dispatch_target_agent:
            step["dispatch_target_agent"] = dispatch_target_agent
        if dispatch_message:
            step["dispatch_message"] = dispatch_message
        return step

    def _make_dispatch_timeline_event(
        self,
        *,
        source: str,
        message_id: str,
        dispatch_id: str,
        dispatch_kind: str,
        display_slot: str,
        text: str,
        partial: bool,
        target_agent: str | None = None,
    ) -> dict[str, Any]:
        event_fields: dict[str, Any] = {
            "source": source,
            "message_id": message_id,
            "dispatch_id": dispatch_id,
            "dispatch_kind": dispatch_kind,
            "display": "dispatch_card",
            "display_slot": display_slot,
            "text": text,
            "partial": partial,
        }
        if target_agent:
            event_fields["target_agent"] = target_agent
        return self._make_timeline_event("dispatch_delta", **event_fields)

    @staticmethod
    def _normalize_dispatch_message(text: str | None) -> str | None:
        if not text:
            return None
        normalized = " ".join(str(text).split())
        if not normalized:
            return None
        return AgentRunner._truncate_dispatch_text(normalized)

    def _truncate_dispatch_text(text: str | None) -> str | None:
        if not text:
            return text
        if len(text) <= 100:
            return text
        truncated = text[:100]
        if truncated[-1:].isalnum() and text[100:101].isalnum():
            last_space = truncated.rfind(" ")
            if last_space > 0:
                return truncated[: last_space + 1]
        return truncated

    @staticmethod
    def _append_dispatch_text(existing: str | None, incoming: str | None) -> str | None:
        if not incoming:
            return existing
        if not existing:
            return incoming
        if existing.endswith((" ", "\n", "\t")) or incoming.startswith((" ", "\n", "\t")):
            return f"{existing}{incoming}"[:100]
        if existing[-1:].isalnum() and incoming[:1].isalnum():
            if len(existing.split()[-1]) < 4 or len(incoming.split()[0]) < 4:
                return f"{existing}{incoming}"[:100]
            return f"{existing} {incoming}"[:100]
        return f"{existing}{incoming}"[:100]

    @staticmethod
    def _make_timeline_event(kind: str, **event_fields: Any) -> dict[str, Any]:
        return {"kind": "timeline", "event": {"kind": kind, **event_fields}}

    def _append_timeline_event(
        self, session: AgentSession, event: dict[str, Any]
    ) -> None:
        session.timeline_events.append(event)

    @staticmethod
    def _default_routing_dispatch_message() -> str:
        return "正在分析是否需要切换执行代理"

    def _build_secondary_handoff_ledger_entry(
        self, source_agent: str, target_agent: str
    ) -> str:
        return f"{source_agent} -> {target_agent}"

    def _activate_secondary_handoff(
        self,
        session: AgentSession,
        handoff_request: SecondaryHandoffRequest,
        *,
        source_agent: str,
        fallback_user_goal: str,
        append_timeline_event: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        if handoff_request.target_agent != "basic_agent":
            raise ValueError(
                "coordinator_2 secondary handoff only supports target_agent=basic_agent"
            )
        handoff_task = (handoff_request.task or "").strip() or fallback_user_goal
        route_receipt = {
            "decider": source_agent,
            "decision": handoff_request.target_agent,
            "reason_summary": "secondary_handoff",
            "timeout_fallback": False,
        }
        session.behavior_records["route_receipt"] = route_receipt
        handoff_ledger = list(session.behavior_records.get("handoff_ledger") or [])
        handoff_ledger.append(
            self._build_secondary_handoff_ledger_entry(
                source_agent, handoff_request.target_agent
            )
        )
        session.behavior_records["handoff_ledger"] = handoff_ledger
        self._activate_profile_agent(
            session,
            handoff_request.target_agent,
            user_goal=handoff_task,
        )
        timeline_event = {
            "kind": "agent_switched",
            "source": source_agent,
            "target_agent": handoff_request.target_agent,
        }
        if append_timeline_event:
            self._append_timeline_event(session, timeline_event)
        return handoff_task, timeline_event

    @staticmethod
    def _extract_secondary_handoff_event(payload: dict[str, Any]) -> SecondaryHandoffRequest | None:
        if payload.get("type") != "secondary_handoff":
            return None
        target_agent = payload.get("target_agent")
        if not isinstance(target_agent, str) or not target_agent:
            return None
        task = payload.get("task")
        additional_args = payload.get("additional_args")
        return SecondaryHandoffRequest(
            target_agent=target_agent,
            task=task if isinstance(task, str) else None,
            additional_args=additional_args if isinstance(additional_args, dict) else None,
        )

    def _log_handoff_debug(self, event: str, **fields: Any) -> None:
        if not self.config.agent.handoff_debug_log_enabled:
            return
        try:
            payload = {
                "event": event,
                **fields,
            }
            log_path = Path(self.config.agent.handoff_debug_log_path).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("写入 handoff debug log 失败")

    def _coordinator_stream_secondary_handoff(
        self,
        session: AgentSession,
        message: str,
        pil_images: list | None,
        enable_thinking: bool | None = None,
    ) -> AsyncGenerator[str, None]:
        async def _stream() -> AsyncGenerator[str, None]:
            active_message = message
            while True:
                secondary_handoff: SecondaryHandoffRequest | None = None
                buffered_events: list[str] = []
                trailing_terminal_event: str | None = None
                buffered_message_completed_events: list[str] = []
                buffered_content_events: list[str] = []
                buffered_agent_switched_events: list[str] = []
                async for event in self._run_basic_stream(
                    session, active_message, pil_images, enable_thinking
                ):
                    if event.startswith("data: "):
                        try:
                            payload = json.loads(event[6:])
                        except json.JSONDecodeError:
                            yield event
                            continue
                        if payload.get("type") == "session_start":
                            continue
                        timeline_event = payload.get("event")
                        if (
                            payload.get("type") == "timeline_event"
                            and isinstance(timeline_event, dict)
                            and timeline_event.get("kind") == "agent_switched"
                            and timeline_event.get("source") == "coordinator_2"
                        ):
                            buffered_agent_switched_events.append(event)
                            continue
                        extracted_handoff = self._extract_secondary_handoff_event(payload)
                        if extracted_handoff is not None:
                            secondary_handoff = extracted_handoff
                            self._log_handoff_debug(
                                "secondary_handoff_received",
                                session_id=session.session_id,
                                source_agent=session.current_agent_name,
                                target_agent=extracted_handoff.target_agent,
                                task=extracted_handoff.task,
                            )
                            continue
                        if (
                            secondary_handoff is not None
                        ):
                            if payload.get("type") in {"aborted", "error"}:
                                trailing_terminal_event = event
                                continue
                            if payload.get("type") == "done":
                                continue
                            if payload.get("type") == "content":
                                buffered_content_events.append(event)
                                continue
                            if (
                                payload.get("type") == "timeline_event"
                                and isinstance(timeline_event, dict)
                                and timeline_event.get("kind") == "message_completed"
                                and timeline_event.get("source") == "coordinator_2"
                            ):
                                buffered_message_completed_events.append(event)
                                continue
                            buffered_events.append(event)
                            continue
                    yield event

                handoff_request = secondary_handoff
                if handoff_request is None:
                    for buffered_agent_switched_event in buffered_agent_switched_events:
                        yield buffered_agent_switched_event
                    for buffered_content_event in buffered_content_events:
                        yield buffered_content_event
                    return

                for buffered_event in buffered_events:
                    yield buffered_event

                if trailing_terminal_event is not None:
                    yield trailing_terminal_event
                    yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"
                    return

                try:
                    active_message, timeline_event = self._activate_secondary_handoff(
                        session,
                        handoff_request,
                        source_agent="coordinator_2",
                        fallback_user_goal=active_message,
                        append_timeline_event=True,
                    )
                except ValueError as exc:
                    yield f"data: {json.dumps({'type': 'error', 'error': str(exc)}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"
                    return
                self._log_handoff_debug(
                    "secondary_handoff_activated",
                    session_id=session.session_id,
                    target_agent=handoff_request.target_agent,
                    handoff_message=active_message,
                )
                yield f"data: {json.dumps(self._make_dispatch_step('completed', 'handoff', dispatch_target_agent=handoff_request.target_agent, dispatch_kind='secondary_handoff', display_slot='after_body', dispatch_id=f'dispatch-{session.session_id}-secondary-handoff'), ensure_ascii=False)}\n\n"
                for buffered_message_completed_event in buffered_message_completed_events:
                    yield buffered_message_completed_event
                yield f"data: {json.dumps({'type': 'timeline_event', 'event': timeline_event}, ensure_ascii=False)}\n\n"
                for buffered_agent_switched_event in buffered_agent_switched_events:
                    yield buffered_agent_switched_event

        return _stream()

    async def _coordinator_sync_secondary_handoff(
        self,
        session: AgentSession,
        message: str,
        pil_images: list | None,
        enable_thinking: bool | None = None,
    ) -> dict:
        try:
            result = await self._run_basic_sync(
                session, message, pil_images, enable_thinking
            )
        except ValueError as exc:
            return {
                "session_id": session.session_id,
                "answer": str(exc),
                "steps": [],
            }
        if not isinstance(result.get("answer"), SecondaryHandoffRequest):
            return result

        handoff_message, _timeline_event = self._activate_secondary_handoff(
            session,
            result["answer"],
            source_agent="coordinator_2",
            fallback_user_goal=message,
        )
        try:
            return await self._run_basic_sync(
                session, handoff_message, pil_images, enable_thinking
            )
        except ValueError as exc:
            return {
                "session_id": session.session_id,
                "answer": str(exc),
                "steps": [],
            }

    def _compile_context_pack(
        self, session: AgentSession, target_agent: str, user_goal: str
    ) -> dict[str, Any]:
        route_receipt = session.behavior_records.get("route_receipt")
        if not route_receipt:
            for event in reversed(session.timeline_events):
                if event.get("kind") == "routing_decision":
                    decision = event.get("target_agent") or event.get("decision")
                    route_receipt = {
                        "decider": event.get("source"),
                        "decision": decision,
                        "reason_summary": event.get("reason_summary")
                        or event.get("reason"),
                        "timeout_fallback": False,
                    }
                    break
                if event.get("kind") == "routing_timeout_fallback":
                    route_receipt = {
                        "decider": event.get("from_agent") or event.get("source"),
                        "decision": event.get("target_agent"),
                        "reason_summary": event.get("reason") or "timeout_fallback",
                        "timeout_fallback": True,
                    }
                    break

        # v2 Prefix Cache Stability：_compile_context_pack 仅作为 Tier 2 审计快照。
        # 严禁返回任何会随轮次变化或被 LLM 消费的字段。
        # session_id 也不进 prompt 主体，因此 session_frame 不带 session_id。
        return {
            "routing_receipt": route_receipt or {},
        }

    def _derive_current_user_goal(self, session: AgentSession) -> str:
        runtime_agent = self._get_runtime_agent(session)
        if hasattr(runtime_agent, "memory") and hasattr(runtime_agent.memory, "steps"):
            for step in reversed(runtime_agent.memory.steps):
                if isinstance(step, TaskStep) and step.task:
                    return str(step.task)
        return ""

    @staticmethod
    def _strip_transient_state_fields(state: dict[str, Any] | None) -> dict[str, Any] | None:
        # v2 Prefix Cache Stability：v1 时代这里曾 pop "context_pack"，
        # 但 v2 已不再向 state 写入该字段，pop 操作变成 no-op 反而误导阅读。
        # 此函数现在仅作为深拷贝 hook：未来若新增 transient state 字段，
        # 在此显式 pop 即可。
        if state is None:
            return None
        return deepcopy(state)

    async def _run_coordinator_routing_stream(
        self, session: AgentSession, message: str, pil_images: list | None
    ) -> AsyncGenerator[dict[str, Any], None]:
        coordinator_agent = session.agent
        routing_message_id = f"routing-card-{session.session_id}"
        routing_dispatch_id = f"dispatch-{session.session_id}-routing"
        routing_source = "coordinator_1"
        if not isinstance(coordinator_agent, SmartToolCallingAgent) or not isinstance(
            getattr(coordinator_agent, "model", None), LocalLLMModel
        ):
            yield {"kind": "result", "route": {"kind": "passthrough"}}
            return

        routing_messages = list(coordinator_agent.write_memory_to_messages())
        coordinator_content: list[dict] = []
        if pil_images:
            coordinator_content.extend({"type": "image", "image": img} for img in pil_images)
        coordinator_content.append(
            {
                "type": "text",
                "text": self._build_coordinator_turn_message(message, routing_source, session=session),
            }
        )
        routing_messages.append(
            ChatMessage(role=MessageRole.USER, content=coordinator_content)
        )

        yield {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": routing_dispatch_id,
        }
        timeline_event = self._make_timeline_event(
            "routing_started",
            source=routing_source,
            message_id=routing_message_id,
        )
        self._append_timeline_event(session, timeline_event["event"])
        yield timeline_event

        request_id = None
        stream_thread: threading.Thread | None = None
        worker_run_scope_id: str | None = None
        worker_cleanup_required = threading.Event()
        worker_exited = threading.Event()
        cleanup_lock = threading.Lock()
        cleanup_complete = False
        run_scope_id = session.active_run_scope_ids[-1] if session.active_run_scope_ids else None
        current_session_context.set(session.session_id, run_scope_id)
        dispatch_accumulated_text: str | None = None
        timeline_accumulated_text: str | None = None
        routing_request_kwargs = {}
        if routing_source == "coordinator_1":
            routing_request_kwargs = {
                "temperature": self.config.agent.coordinator_1_routing_temperature,
                "max_tokens": self.config.agent.coordinator_1_routing_max_tokens,
                "extra_body": deepcopy(
                    self.config.agent.coordinator_1_routing_extra_body
                ),
                "retry_without_extra_body": True,
            }
        routing_timeout_seconds = None
        routing_started_at = None
        if routing_source == "coordinator_1":
            routing_timeout_seconds = max(
                0.0, float(self.config.agent.coordinator_1_timeout_seconds)
            )
            routing_started_at = time.monotonic()
        try:
            from backend.llm_service import (
                abort_request,
                create_request_id,
            )
            from smolagents.models import agglomerate_stream_deltas

            def _timeout_fallback_route() -> dict[str, Any] | None:
                if routing_timeout_seconds is None or routing_started_at is None:
                    return None
                if time.monotonic() - routing_started_at <= routing_timeout_seconds:
                    return None
                return {
                    "kind": "switch",
                    "target_agent": "basic_agent",
                    "reason": "timeout_fallback",
                }

            def _abort_request_if_needed() -> None:
                if request_id:
                    abort_request(request_id)

            def _join_stream_thread_if_needed() -> None:
                if stream_thread is not None and stream_thread.is_alive():
                    stream_thread.join(timeout=0.05)

            def _emit_timeout_fallback() -> dict[str, Any]:
                _abort_request_if_needed()
                return {
                    "kind": "switch",
                    "target_agent": "basic_agent",
                    "reason": "timeout_fallback",
                }

            request_id = create_request_id()
            session_request_manager.set(session.session_id, request_id)
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[object] = asyncio.Queue()
            stream_done = object()

            def _cleanup_routing_request() -> None:
                nonlocal cleanup_complete
                with cleanup_lock:
                    if cleanup_complete or not request_id:
                        return
                    cleanup_complete = True
                    session_request_manager.clear(session.session_id, request_id)
                    if hasattr(coordinator_agent.model, "clear_stream_state"):
                        coordinator_agent.model.clear_stream_state(request_id)
                    if hasattr(coordinator_agent.model, "llm_service") and hasattr(
                        coordinator_agent.model.llm_service, "discard_stream_usage"
                    ):
                        coordinator_agent.model.llm_service.discard_stream_usage(request_id)

            def _enqueue_stream_item(item: object) -> None:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                except RuntimeError:
                    pass

            def _stream_worker():
                try:
                    stream = coordinator_agent.model.generate_stream(
                        routing_messages,
                        stop_sequences=["Observation:"],
                        tools_to_call_from=coordinator_agent.tools_and_managed_agents if coordinator_agent.native_tool_protocol else [],
                        request_id=request_id,
                        **routing_request_kwargs,
                    )
                    for delta in stream:
                        _enqueue_stream_item(delta)
                except Exception as e:
                    _enqueue_stream_item(e)
                finally:
                    _enqueue_stream_item(stream_done)
                    worker_exited.set()
                    if worker_cleanup_required.is_set():
                        _cleanup_routing_request()
                    current_session_context.clear()
                    if worker_run_scope_id is not None:
                        session.finish_run(worker_run_scope_id)

            # Preserve request-scoped context variables in the worker thread.
            worker_run_scope_id = session.begin_run()
            _routing_ctx = contextvars.copy_context()
            stream_thread = threading.Thread(
                target=_routing_ctx.run,
                args=(_stream_worker,),
                name="coordinator-routing-stream-worker",
                daemon=True,
            )
            try:
                stream_thread.start()
            except Exception:
                session.finish_run(worker_run_scope_id)
                worker_run_scope_id = None
                raise

            deltas = []
            while True:
                timeout_route = _timeout_fallback_route()
                if timeout_route is not None:
                    timeout_route = _emit_timeout_fallback()
                    _join_stream_thread_if_needed()
                    timeline_event = self._make_timeline_event(
                        "routing_timeout_fallback",
                        source=routing_source,
                        message_id=routing_message_id,
                        from_agent="coordinator_1",
                        target_agent="basic_agent",
                        reason="timeout_fallback",
                    )
                    self._append_timeline_event(session, timeline_event["event"])
                    yield timeline_event
                    dispatch_timeline_event = self._make_dispatch_timeline_event(
                        source=routing_source,
                        message_id=routing_message_id,
                        dispatch_id=routing_dispatch_id,
                        dispatch_kind="routing",
                        display_slot="before_body",
                        text="coordinator_1 路由超时，回退到 basic_agent",
                        partial=True,
                        target_agent="basic_agent",
                    )
                    self._append_timeline_event(session, dispatch_timeline_event["event"])
                    yield dispatch_timeline_event
                    yield {
                        "kind": "dispatch",
                        "dispatch_status": "running",
                        "dispatch_stage": "routing",
                        "dispatch_kind": "routing",
                        "display_slot": "before_body",
                        "dispatch_id": routing_dispatch_id,
                        "dispatch_message": "coordinator_1 路由超时，回退到 basic_agent",
                    }
                    yield {"kind": "result", "route": timeout_route}
                    return
                if session.check_aborted() or (run_scope_id and run_abort_manager.is_aborted(run_scope_id)):
                    _abort_request_if_needed()
                    _join_stream_thread_if_needed()
                    yield {
                        "kind": "dispatch",
                        "dispatch_status": "aborted",
                        "dispatch_stage": "routing",
                        "dispatch_kind": "routing",
                        "display_slot": "before_body",
                        "dispatch_id": routing_dispatch_id,
                        "dispatch_message": "任务调度已中止",
                    }
                    yield {"kind": "result", "route": {"kind": "aborted"}}
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if item is stream_done:
                    break
                if isinstance(item, Exception):
                    raise item
                delta = item
                deltas.append(delta)
                delta_text = getattr(delta, "content", None)
                if delta_text:
                    raw_delta_text = str(delta_text)
                    dispatch_message = self._normalize_dispatch_message(raw_delta_text)
                    if raw_delta_text:
                        if timeline_accumulated_text:
                            timeline_accumulated_text = f"{timeline_accumulated_text}{raw_delta_text}"
                        else:
                            timeline_accumulated_text = raw_delta_text
                        dispatch_accumulated_text = self._normalize_dispatch_message(
                            timeline_accumulated_text
                        )
                    if dispatch_message and dispatch_accumulated_text:
                        timeline_event = self._make_timeline_event(
                            "routing_delta",
                            source=routing_source,
                            message_id=routing_message_id,
                            text=timeline_accumulated_text,
                            partial=True,
                        )
                        self._append_timeline_event(session, timeline_event["event"])
                        yield timeline_event
                        dispatch_timeline_event = self._make_dispatch_timeline_event(
                            source=routing_source,
                            message_id=routing_message_id,
                            dispatch_id=routing_dispatch_id,
                            dispatch_kind="routing",
                            display_slot="before_body",
                            text=dispatch_accumulated_text,
                            partial=True,
                        )
                        self._append_timeline_event(session, dispatch_timeline_event["event"])
                        yield dispatch_timeline_event
                        yield {
                            "kind": "dispatch",
                            "dispatch_status": "running",
                            "dispatch_stage": "routing",
                            "dispatch_kind": "routing",
                            "display_slot": "before_body",
                            "dispatch_id": routing_dispatch_id,
                            "dispatch_message": dispatch_accumulated_text,
                        }

            if session.check_aborted() or (run_scope_id and run_abort_manager.is_aborted(run_scope_id)):
                yield {
                    "kind": "dispatch",
                    "dispatch_status": "aborted",
                    "dispatch_stage": "routing",
                    "dispatch_kind": "routing",
                    "display_slot": "before_body",
                    "dispatch_id": routing_dispatch_id,
                    "dispatch_message": "任务调度已中止",
                }
                yield {"kind": "result", "route": {"kind": "aborted"}}
                return

            if deltas:
                chat_message = agglomerate_stream_deltas(deltas)
            else:
                chat_message = ChatMessage(role=MessageRole.ASSISTANT, content="")
            if hasattr(coordinator_agent.model, "get_stream_state"):
                stream_state = coordinator_agent.model.get_stream_state(request_id) or {}
                # The visible gate suppresses control deltas. Classification must
                # therefore use only the raw provider-classified visible content,
                # never reasoning_content or an agglomerated fallback.
                chat_message.content = stream_state.get("content", "") or ""
                if coordinator_agent.native_tool_protocol:
                    chat_message.control_resolution = stream_state.get("control_resolution")
                    chat_message.tool_calls = stream_state.get("provider_tool_calls") or chat_message.tool_calls
                    chat_message.reasoning_content = stream_state.get("reasoning") or None

            coordinator_output = (getattr(chat_message, "content", "") or "").strip()
            if coordinator_output:
                logger.info(f"[COORDINATOR] {coordinator_output}")

            route = self._classify_coordinator_output(chat_message, session=session)
            if coordinator_agent.native_tool_protocol and route.get("kind") == "final":
                route["native_turn"] = (routing_messages[-1], chat_message)
            if coordinator_agent.native_tool_protocol and route.get("control_call") is not None:
                self._record_native_routing_call(session, routing_messages[-1], chat_message, route)
            if route.get("kind") == "switch" and route.get("target_agent"):
                timeline_event = self._make_timeline_event(
                    "routing_decision",
                    source=routing_source,
                    message_id=routing_message_id,
                    target_agent=route["target_agent"],
                    **({"reason": route["reason"]} if route.get("reason") else {}),
                )
                self._append_timeline_event(session, timeline_event["event"])
                yield timeline_event
            elif route.get("kind") == "final":
                timeline_event = self._make_timeline_event(
                    "routing_decision",
                    source=routing_source,
                    message_id=routing_message_id,
                    decision="direct_answer",
                )
                self._append_timeline_event(session, timeline_event["event"])
                yield timeline_event
            yield {"kind": "result", "route": route}
        finally:
            current_session_context.clear()
            if request_id:
                if stream_thread is not None and stream_thread.is_alive():
                    worker_cleanup_required.set()
                    if worker_exited.is_set():
                        _cleanup_routing_request()
                else:
                    _cleanup_routing_request()

    def _record_native_routing_call(self, session, user_message, chat_message, route):
        from smolagents.memory import TaskStep, ActionStep, ToolCall as MemoryToolCall
        from smolagents.monitoring import Timing
        call = route["control_call"]
        # Store the exact accepted routing tail; no UI dispatch text enters memory.
        content = user_message.content
        task_text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        task_images = [part["image"] for part in content if part.get("type") == "image"]
        task = TaskStep(task=task_text, task_images=task_images or None)
        task.canonical_routing_tail = True
        session.agent.memory.steps.append(task)
        step = ActionStep(step_number=len(session.agent.memory.steps), timing=Timing(start_time=time.time(), end_time=time.time()),
            model_output=native_control_prose(chat_message.content or ""),
            tool_calls=[MemoryToolCall(name="switch_agent", arguments=call.function.arguments, id=call.id)])
        step.canonical_reasoning_content = getattr(chat_message, "reasoning_content", None)
        status = route.get("status", "completed")
        receipt = (f"Routing request accepted: target_agent={route['target_agent']}" if status == "completed"
                   else f"Routing request rejected: {route['error']}")
        step.canonical_tool_results = [CanonicalToolResult(call.id, "switch_agent", receipt, status)]
        session.agent.memory.steps.append(step)

    def _classify_coordinator_output(
        self,
        chat_message,
        *,
        parse_tool_text: bool = True,
        session=None,
    ) -> dict:
        content = getattr(chat_message, "content", None)
        if content is not None and not isinstance(content, str):
            return {
                "kind": "invalid",
                "error": "Coordinator returned non-text content",
            }

        try:
            model = session.agent.model if session is not None else self.model
            if not isinstance(model, LocalLLMModel):
                # Transport-only agents and old inspection objects do not own
                # a protocol adapter. Resolve using the actual pinned dialect.
                model = (LocalLLMModel(llm_service=self.llm_service, session=session)
                         if getattr(session, "tool_protocol", "legacy_text") == "qwen35-native" else self.model)
            resolution = getattr(chat_message, "control_resolution", None)
            if not isinstance(resolution, AssistantControlResolution):
                resolution = model._resolve_assistant_control(
                content or "",
                getattr(chat_message, "tool_calls", None),
                (),
                allowed_agent_names=set(
                    self.config.agent.enabled_agent_profiles
                ),
            )
        except TextToolProtocolError as exc:
            return {"kind": "invalid", "error": str(exc)}

        if resolution.tool_calls:
            return {
                "kind": "invalid",
                "error": "Coordinator must return a direct answer or switch_agent command",
            }
        if resolution.switch_agent is not None and session is not None and model.native_tool_protocol:
            maximum = getattr(session, "max_agent_switches_per_turn", self.config.agent.max_agent_switches_per_turn)
            if session.turn_switch_count >= maximum:
                return {"kind": "invalid", "error": "Agent switch limit reached", "status": "failed",
                        "target_agent": resolution.switch_agent.target_agent, "control_call": resolution.control_call}
        if resolution.switch_agent is not None:
            return {
                "kind": "switch",
                "target_agent": resolution.switch_agent.target_agent,
                **({"control_call": resolution.control_call} if resolution.control_call is not None else {}),
            }

        text = (content or "").strip()
        if resolution.source == "natural" and text:
            return {"kind": "final", "answer": content if model.native_tool_protocol is True else text}
        return {
            "kind": "invalid",
            "error": "Coordinator returned an empty routing response",
        }

    def _run_coordinator_routing_pass(
        self, session: AgentSession, message: str, pil_images: list | None
    ) -> dict:
        coordinator_agent = session.agent
        routing_message_id = f"routing-card-{session.session_id}"
        routing_source = "coordinator_1"
        if not isinstance(coordinator_agent, SmartToolCallingAgent) or not isinstance(
            getattr(coordinator_agent, "model", None), LocalLLMModel
        ):
            return {"kind": "passthrough"}
        routing_messages = list(coordinator_agent.write_memory_to_messages())
        coordinator_content: list[dict] = []
        if pil_images:
            coordinator_content.extend({"type": "image", "image": img} for img in pil_images)
        coordinator_content.append(
            {
                "type": "text",
                "text": self._build_coordinator_turn_message(message, routing_source, session=session),
            }
        )
        routing_messages.append(
            ChatMessage(role=MessageRole.USER, content=coordinator_content)
        )

        request_id = None
        current_session_context.set(session.session_id)
        try:
            from backend.llm_service import create_request_id

            self._append_timeline_event(
                session,
                self._make_timeline_event(
                    "routing_started",
                    source=routing_source,
                    message_id=routing_message_id,
                )["event"],
            )
            request_id = create_request_id()
            session_request_manager.set(session.session_id, request_id)
            chat_message = coordinator_agent.model.generate(
                routing_messages,
                stop_sequences=["Observation:"],
                tools_to_call_from=coordinator_agent.tools_and_managed_agents if coordinator_agent.native_tool_protocol else [],
                request_id=request_id,
                temperature=self.config.agent.coordinator_1_routing_temperature,
                max_tokens=self.config.agent.coordinator_1_routing_max_tokens,
                extra_body=deepcopy(
                    self.config.agent.coordinator_1_routing_extra_body
                ),
                retry_without_extra_body=True,
            )
            route = self._classify_coordinator_output(chat_message, session=session)
            if coordinator_agent.native_tool_protocol and route.get("kind") == "final":
                route["native_turn"] = (routing_messages[-1], chat_message)
            if coordinator_agent.native_tool_protocol and route.get("control_call") is not None:
                self._record_native_routing_call(session, routing_messages[-1], chat_message, route)
            if route.get("kind") == "switch" and route.get("target_agent"):
                self._append_timeline_event(
                    session,
                    self._make_timeline_event(
                        "routing_decision",
                        source=routing_source,
                        message_id=routing_message_id,
                        target_agent=route["target_agent"],
                        **({"reason": route["reason"]} if route.get("reason") else {}),
                    )["event"],
                )
            elif route.get("kind") == "final":
                self._append_timeline_event(
                    session,
                    self._make_timeline_event(
                        "routing_decision",
                        source=routing_source,
                        message_id=routing_message_id,
                        decision="direct_answer",
                    )["event"],
                )
            return route
        finally:
            current_session_context.clear()
            if request_id:
                session_request_manager.clear(session.session_id, request_id)

    def _next_action_step_number(self, agent: MultiStepAgent) -> int:
        action_steps = [
            step
            for step in getattr(getattr(agent, "memory", None), "steps", [])
            if isinstance(step, ActionStep)
        ]
        if not action_steps:
            return 1
        return max((step.step_number or 0) for step in action_steps) + 1

    def _commit_direct_answer_to_shared_memory(
        self,
        session: AgentSession,
        message: str,
        answer: str,
        pil_images: list | None,
        *,
        native_turn=None,
    ) -> None:
        runtime_agent = self._get_runtime_agent(session)
        if getattr(session, "tool_protocol", "legacy_text") == "qwen35-native" and native_turn is not None:
            user_message, assistant_message = native_turn
            task_content = user_message.content
            task = TaskStep(task="".join(part.get("text", "") for part in task_content if part.get("type") == "text"),
                task_images=[part["image"] for part in task_content if part.get("type") == "image"] or None)
            task.canonical_routing_tail = True
            runtime_agent.memory.steps.append(task)
        else:
            assistant_message = None
            runtime_agent.memory.steps.append(TaskStep(task=message, task_images=pil_images))
        runtime_agent.memory.steps.append(
            ActionStep(
                step_number=self._next_action_step_number(runtime_agent),
                timing=Timing(start_time=0.0, end_time=0.0),
                model_output=answer,
                tool_calls=None,
                observations=None,
                action_output=answer,
                is_final_answer=True,
            )
        )
        if assistant_message is not None:
            runtime_agent.memory.steps[-1].canonical_reasoning_content = getattr(assistant_message, "reasoning_content", None)
        session.has_run = True
        session.has_shared_history = True

    def _activate_profile_agent(
        self, session: AgentSession, profile_name: str, user_goal: str | None = None
    ) -> MultiStepAgent:
        if getattr(session, "tool_protocol", "legacy_text") == "qwen35-native" and session.turn_switch_count >= getattr(
                session, "max_agent_switches_per_turn", self.config.agent.max_agent_switches_per_turn):
            raise ValueError("Agent switch limit reached")
        prior_agent = self._get_runtime_agent(session)
        shared_steps = []
        if hasattr(prior_agent, "memory") and hasattr(prior_agent.memory, "steps"):
            shared_steps = list(prior_agent.memory.steps)

        shared_state = None
        if hasattr(prior_agent, "state"):
            shared_state = self._strip_transient_state_fields(prior_agent.state)

        # v2 Prefix Cache Stability：不再调用 _compile_context_pack，也不再向
        # next_agent.state 写入 context_pack。该字段历史上是死数据（LLM 从未
        # 消费），写入会误导未来维护者并破坏 prefix cache 稳定性。

        next_agent = self._build_profile_agent(
            profile_name,
            session=session,
            runtime_context=session.runtime_context,
        )
        if hasattr(next_agent, "memory") and hasattr(next_agent.memory, "steps"):
            next_agent.memory.steps = shared_steps
        if shared_state is not None and hasattr(next_agent, "state"):
            next_agent.state = shared_state

        session.agent = next_agent
        session.active_agent = next_agent
        session.current_agent_name = profile_name
        session.turn_switch_count += 1
        session.has_shared_history = session.has_shared_history or bool(shared_steps)
        return next_agent

    def _extract_sync_steps(self, agent: MultiStepAgent) -> list[dict]:
        steps: list[dict] = []
        for step in agent.memory.steps:
            if isinstance(step, ActionStep):
                step_info: dict = {"step_number": step.step_number}
                if step.model_output:
                    step_info["thought"] = str(step.model_output)
                if step.tool_calls:
                    step_info["tool_calls"] = [
                        {"name": tc.name, "input": self._safe_serialize(tc.arguments)}
                        for tc in step.tool_calls
                    ]
                if step.observations:
                    step_info["observation"] = step.observations
                steps.append(step_info)
        return steps

    async def _run_basic_sync(
        self,
        session: AgentSession,
        message: str,
        pil_images: list | None,
        enable_thinking: bool | None = None,
    ) -> dict:
        agent = session.agent
        session_id = session.session_id
        run_scope_id = session.begin_run()
        try:
            def _run():
                from .local_llm_model import _normal_turn_enable_thinking

                reasoning_token = _normal_turn_enable_thinking.set(enable_thinking)
                current_session_context.set(session_id, run_scope_id)
                self._bind_tool_runtime_context(
                    agent, session_id, run_scope_id, session.capability
                )
                try:
                    _should_reset = not session.has_run
                    session.has_run = True
                    return agent.run(message, reset=_should_reset, images=pil_images)
                finally:
                    _normal_turn_enable_thinking.reset(reasoning_token)
                    current_session_context.clear()

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _run)
            session.has_shared_history = True
            return {
                "session_id": session.session_id,
                "answer": result if isinstance(result, SecondaryHandoffRequest) else str(result) if result is not None else "",
                "steps": self._extract_sync_steps(agent),
            }
        finally:
            session.finish_run(run_scope_id)

    async def _run_basic_stream(
        self,
        session: AgentSession,
        message: str,
        pil_images: list | None,
        enable_thinking: bool | None = None,
    ) -> AsyncGenerator[str, None]:
        agent = session.agent
        session_id = session.session_id
        run_scope_id = session.begin_run()
        use_chat_bubble_timeline = session.current_agent_name == "coordinator_2"

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        timeline_sink = (loop, queue)
        session.timeline_event_sinks.append(timeline_sink)

        def _run_agent():
            from .local_llm_model import _normal_turn_enable_thinking
            from smolagents.agents import ToolOutput as SmolToolOutput
            from smolagents.memory import (
                ToolCall as MemoryToolCall,
                ActionStep as MemoryActionStep,
            )

            reasoning_token = _normal_turn_enable_thinking.set(enable_thinking)

            _buf_tool_calls: list[dict] = []
            _buf_observations: list[dict] = []
            _pending_final_content: list[str] = []
            _emitted_final_content: list[str | None] = [None]
            _last_streamed_thought_text: list[str | None] = [None]
            _pending_streamed_thought: list[str | None] = [None]
            _thought_already_sent: list[bool] = [False]
            active_chat_message_id: str | None = None
            streamed_chat_content_parts: list[str] = []
            pending_secondary_handoff: SecondaryHandoffRequest | None = None

            def _chat_bubble_already_ends_with(text: str | None) -> bool:
                if not text:
                    return True
                streamed_chat_content = "".join(streamed_chat_content_parts)
                if not streamed_chat_content:
                    return False
                return streamed_chat_content.endswith(text)

            def _ensure_chat_message_started() -> str:
                nonlocal active_chat_message_id
                if active_chat_message_id is None:
                    active_chat_message_id = f"assistant-msg-{run_scope_id}"
                    self._queue_timeline_message_event(
                        loop,
                        queue,
                        session,
                        kind="message_started",
                        source="coordinator_2",
                        message_id=active_chat_message_id,
                        display="chat_bubble",
                    )
                return active_chat_message_id

            def _emit_chat_bubble_delta(
                text: str, *, partial: bool, reasoning: bool = False
            ) -> None:
                if not text:
                    return
                message_id = _ensure_chat_message_started()
                self._queue_timeline_message_event(
                    loop,
                    queue,
                    session,
                    kind="message_delta",
                    source="coordinator_2",
                    message_id=message_id,
                    text=text,
                    partial=partial,
                    reasoning=reasoning,
                    display="chat_bubble",
                )
                streamed_chat_content_parts.append(text)

            def _emit_chat_bubble_delta_raw(
                text: str, *, partial: bool, reasoning: bool = False
            ) -> None:
                if not text:
                    return
                message_id = _ensure_chat_message_started()
                self._queue_timeline_message_event(
                    loop,
                    queue,
                    session,
                    kind="message_delta",
                    source="coordinator_2",
                    message_id=message_id,
                    text=text,
                    partial=partial,
                    reasoning=reasoning,
                    display="chat_bubble",
                )
                streamed_chat_content_parts.append(text)

            def _normalize_final_answer_candidate(value: Any) -> str | None:
                if value is None or isinstance(value, SecondaryHandoffRequest):
                    return None
                text = str(value).strip()
                return text or None

            def _append_pending_final_content(value: Any) -> str | None:
                text = _normalize_final_answer_candidate(value)
                if not text:
                    return None
                if _emitted_final_content[0] == text:
                    return text
                if _pending_final_content and _pending_final_content[-1] == text:
                    return text
                if _last_streamed_thought_text[0] == text:
                    _thought_already_sent[0] = False
                    _last_streamed_thought_text[0] = None
                _pending_final_content.append(text)
                return text

            def _emit_pending_final_content_if_needed() -> None:
                if not _pending_final_content:
                    return
                text = _pending_final_content[0]
                if _emitted_final_content[0] == text:
                    _pending_final_content.clear()
                    return
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "content",
                        "content": text,
                    },
                )
                _emitted_final_content[0] = text
                _pending_final_content.clear()

            def _flush_pending_streamed_thought() -> None:
                thought_text = _pending_streamed_thought[0]
                if not thought_text:
                    return
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "agent_step",
                        "step_type": "thought",
                        "content": thought_text,
                        "partial": True,
                    },
                )
                _last_streamed_thought_text[0] = thought_text.strip()
                _thought_already_sent[0] = True
                _pending_streamed_thought[0] = None

            def _flush_step(thought_content: str | None):
                if use_chat_bubble_timeline and thought_content is not None:
                    if _chat_bubble_already_ends_with(thought_content):
                        thought_content = None
                    if not thought_content:
                        thought_content = None
                if use_chat_bubble_timeline and thought_content:
                    _emit_chat_bubble_delta(thought_content, partial=False)
                    _thought_already_sent[0] = False
                    for tc_event in _buf_tool_calls:
                        loop.call_soon_threadsafe(queue.put_nowait, tc_event)
                    for obs_event in _buf_observations:
                        loop.call_soon_threadsafe(queue.put_nowait, obs_event)
                    _buf_tool_calls.clear()
                    _buf_observations.clear()
                    return
                if thought_content and not _thought_already_sent[0]:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {
                            "type": "agent_step",
                            "step_type": "thought",
                            "content": thought_content,
                        },
                    )
                _thought_already_sent[0] = False
                for tc_event in _buf_tool_calls:
                    loop.call_soon_threadsafe(queue.put_nowait, tc_event)
                for obs_event in _buf_observations:
                    loop.call_soon_threadsafe(queue.put_nowait, obs_event)
                _buf_tool_calls.clear()
                _buf_observations.clear()

            baseline_step_count = len(agent.memory.steps)
            baseline_has_run = session.has_run
            baseline_state = None
            agent_state = getattr(agent, "state", None)
            if isinstance(agent_state, dict):
                baseline_state = deepcopy(agent_state)

            try:
                current_session_context.set(session_id, run_scope_id)
                self._bind_tool_runtime_context(
                    agent, session_id, run_scope_id, session.capability
                )
                abort_manager.get_event(session_id)
                run_abort_manager.get_event(run_scope_id)

                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"type": "session_start", "session_id": session.session_id},
                )

                _should_reset = not session.has_run
                session.has_run = True
                session.has_shared_history = True
                logger.info(
                    f"Agent run: session={session_id}, reset={_should_reset}, "
                    f"has_images={pil_images is not None}"
                )
                # Backend SSE is already the streaming surface. Do not create a
                # Rich Live renderer in the server terminal: logging emitted
                # while Live is active causes Rich to re-render/freeze snapshots,
                # duplicating model text in backend logs. A dedicated CLI runner
                # may still set agent._cli_live itself; _step_stream will reuse
                # that parent Live, but this web/backend path intentionally does
                # not own one.
                agent._cli_live = None
                agent_stream = agent.run(
                    message, stream=True, images=pil_images, reset=_should_reset
                )
                aborting = False
                for step_output in agent_stream:
                        if session.check_aborted():
                            if not aborting:
                                aborting = True
                                logger.info(
                                    f"Session {session_id} 在 Agent 循环中检测到中止信号，开始清理当前执行"
                                )
                                try:
                                    agent.interrupt_switch = True
                                except Exception:
                                    pass
                            continue

                        if isinstance(step_output, ChatMessageStreamDelta):
                            if step_output.content:
                                if use_chat_bubble_timeline:
                                    _emit_chat_bubble_delta(
                                        step_output.content,
                                        partial=True,
                                        reasoning=bool(getattr(step_output, "reasoning", False)),
                                    )
                                    continue
                                _flush_pending_streamed_thought()
                                _pending_streamed_thought[0] = step_output.content

                        elif isinstance(step_output, RawToolCallDebug):
                            _flush_pending_streamed_thought()
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {
                                    "type": "agent_step",
                                    "step_type": "thought",
                                    "raw_tool_call": step_output.raw_tool_call,
                                    "raw_tool_call_source": step_output.source,
                                    "partial": False,
                                },
                            )

                        elif isinstance(step_output, dict) and step_output.get(
                            "_orchestration_event"
                        ):
                            _flush_pending_streamed_thought()
                            sse_event = {
                                k: v
                                for k, v in step_output.items()
                                if k != "_orchestration_event"
                            }
                            loop.call_soon_threadsafe(queue.put_nowait, sse_event)

                        elif isinstance(step_output, MemoryToolCall):
                            _flush_pending_streamed_thought()
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {
                                    "type": "agent_step",
                                    "step_type": "tool_call",
                                    "name": step_output.name,
                                    "input": self._safe_serialize(step_output.arguments),
                                    "display_slot": "after_body",
                                },
                            )

                        elif isinstance(step_output, SmolToolOutput):
                            _flush_pending_streamed_thought()
                            obs = step_output.observation or ""
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                {
                                    "type": "agent_step",
                                    "step_type": "observation",
                                    "content": obs,
                                    "display_slot": "after_body",
                                },
                            )

                        elif isinstance(step_output, SecondaryHandoffRequest):
                            _flush_pending_streamed_thought()
                            pending_secondary_handoff = step_output

                        elif isinstance(step_output, ActionOutput):
                            if step_output.is_final_answer:
                                if isinstance(step_output.output, SecondaryHandoffRequest):
                                    _flush_pending_streamed_thought()
                                    loop.call_soon_threadsafe(
                                        queue.put_nowait,
                                        {
                                            "type": "secondary_handoff",
                                            "target_agent": step_output.output.target_agent,
                                            "task": step_output.output.task,
                                            "additional_args": self._safe_serialize(
                                                step_output.output.additional_args
                                            )
                                            if step_output.output.additional_args
                                            else None,
                                        },
                                    )
                                    continue
                                output_str = (
                                    str(step_output.output)
                                    if step_output.output is not None
                                    else ""
                                )
                                if _pending_streamed_thought[0] and _pending_streamed_thought[0].strip() != output_str.strip():
                                    _flush_pending_streamed_thought()
                                elif _pending_streamed_thought[0] and _pending_streamed_thought[0].strip() == output_str.strip():
                                    _pending_streamed_thought[0] = None
                                _append_pending_final_content(output_str)

                        elif isinstance(step_output, MemoryActionStep):
                            _flush_pending_streamed_thought()
                            thought = None
                            if step_output.model_output and isinstance(
                                step_output.model_output, str
                            ):
                                thought = step_output.model_output
                            if getattr(step_output, "is_final_answer", False) and thought:
                                final_answer_text = _normalize_final_answer_candidate(
                                    step_output.action_output
                                ) or _normalize_final_answer_candidate(
                                    step_output.model_output
                                )
                                if final_answer_text and thought.strip() == final_answer_text:
                                    _append_pending_final_content(final_answer_text)
                                    thought = None
                            if use_chat_bubble_timeline and _chat_bubble_already_ends_with(thought):
                                thought = None
                            _flush_step(thought)

                            if step_output.error:
                                error_text = str(step_output.error).strip()
                                logger.warning(
                                    "Agent action error: session=%s profile=%s error=%s",
                                    session_id,
                                    session.current_agent_name,
                                    error_text,
                                )
                                loop.call_soon_threadsafe(
                                    queue.put_nowait,
                                    {
                                        "type": "agent_step",
                                        "step_type": "observation",
                                        "content": f"Error:\n{error_text}",
                                        "display_slot": "after_body",
                                    },
                                )

                            if _pending_final_content and not use_chat_bubble_timeline:
                                _emit_pending_final_content_if_needed()

                        elif isinstance(step_output, FinalAnswerStep):
                            _flush_pending_streamed_thought()
                            output_str = (
                                str(step_output.output)
                                if step_output.output is not None
                                else ""
                            )
                            _append_pending_final_content(output_str)
            except Exception as e:
                if session.check_aborted():
                    logger.info(
                        f"Session {session_id} 中止后清理退出: {redact_data_urls(e)}"
                    )
                else:
                    safe_error = redact_data_urls(e)
                    logger.error(f"Agent 执行异常: {safe_error}")
                    loop.call_soon_threadsafe(
                        queue.put_nowait, {"type": "error", "error": safe_error}
                    )
            finally:
                try:
                    _flush_pending_streamed_thought()
                    if _buf_tool_calls:
                        for tc_event in _buf_tool_calls:
                            loop.call_soon_threadsafe(queue.put_nowait, tc_event)
                        _buf_tool_calls.clear()
                    if _buf_observations:
                        for obs_event in _buf_observations:
                            loop.call_soon_threadsafe(queue.put_nowait, obs_event)
                        _buf_observations.clear()
                    if use_chat_bubble_timeline and _pending_final_content:
                        final_text = _pending_final_content[0]
                        if not _chat_bubble_already_ends_with(final_text):
                            _emit_chat_bubble_delta_raw(final_text, partial=False)
                        _pending_final_content.clear()
                    if _pending_final_content and not use_chat_bubble_timeline:
                        _emit_pending_final_content_if_needed()
                    if use_chat_bubble_timeline and pending_secondary_handoff is not None:
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            {
                                "type": "secondary_handoff",
                                "target_agent": pending_secondary_handoff.target_agent,
                                "task": pending_secondary_handoff.task,
                                "additional_args": self._safe_serialize(
                                    pending_secondary_handoff.additional_args
                                )
                                if pending_secondary_handoff.additional_args
                                else None,
                            },
                        )
                        pending_secondary_handoff = None
                    if use_chat_bubble_timeline and active_chat_message_id is not None:
                        self._queue_timeline_message_event(
                            loop,
                            queue,
                            session,
                            kind="message_completed",
                            source="coordinator_2",
                            message_id=active_chat_message_id,
                            display="chat_bubble",
                        )
                    current_session_context.clear()
                    if session.check_aborted():
                        session.rollback_run_state(
                            baseline_step_count=baseline_step_count,
                            baseline_has_run=baseline_has_run,
                            baseline_state=baseline_state,
                        )
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            {"type": "aborted", "session_id": session.session_id},
                        )
                    abort_manager.cleanup(session_id)
                    session.finish_run(run_scope_id)
                    _normal_turn_enable_thinking.reset(reasoning_token)
                finally:
                    try:
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                    except RuntimeError:
                        pass

        # Preserve request-scoped context variables in the worker thread.
        _run_agent_ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=_run_agent_ctx.run,
            args=(_run_agent,),
            daemon=True,
        )
        thread.start()
        # The worker thread owns session.finish_run(run_scope_id). The SSE
        # generator can be closed while a tool is still unwinding after stop;
        # finishing here would let a new run overlap stale native fetch work.
        try:
            while True:
                if session.check_aborted():
                    logger.info(f"Session {session_id} 检测到中止信号")
                    yield f"data: {json.dumps({'type': 'aborted', 'session_id': session.session_id})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
                    break

                if session.check_answer_now():
                    logger.info(f"Session {session_id} 检测到 answer_now 信号")
                    accumulated = ""
                    if hasattr(agent, "memory") and agent.memory:
                        for step in agent.memory.steps:
                            if hasattr(step, "model_output") and step.model_output:
                                accumulated += str(step.model_output)

                    if not use_chat_bubble_timeline:
                        yield f"data: {json.dumps({'type': 'content', 'content': accumulated, 'answer_now': True})}\n\n"
                    session.reset_answer_now()
                    yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if not thread.is_alive():
                        # The worker enqueues its final events with
                        # call_soon_threadsafe.  It may exit just before those
                        # callbacks run; give the loop one turn instead of
                        # dropping already-produced content or handoff events.
                        await asyncio.sleep(0)
                    continue
                if event is None:
                    yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if timeline_sink in session.timeline_event_sinks:
                session.timeline_event_sinks.remove(timeline_sink)

    async def generate_answer_now_reply(
        self,
        session_id: str,
        *,
        captured_scope_ids: Collection[str] = (),
        single_flight_acquired: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        session = self.sessions.get(session_id)
        if not session:
            return {"status": "not_found", "session_id": session_id}
        answer_parts: list[str] = []
        async for event in self.run_answer_now_stream(
            session_id,
            captured_scope_ids=captured_scope_ids,
            single_flight_acquired=single_flight_acquired,
            cancelled=cancelled,
        ):
            payload = json.loads(event[6:])
            if payload.get("type") == "content":
                answer_parts.append(payload.get("content", ""))
            elif payload.get("type") == "error":
                return {"status": "error", "session_id": session_id, "error": payload["error"]}
        if cancelled is not None and cancelled() and not answer_parts:
            return {"status": "cancelled", "session_id": session_id}
        return {"status": "ok", "session_id": session_id, "answer": "".join(answer_parts)}

    async def run_answer_now_stream(
        self,
        session_id: str,
        *,
        captured_scope_ids: Collection[str] = (),
        single_flight_acquired: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[str, None]:
        session = self.sessions.get(session_id)
        if not session:
            yield f"data: {json.dumps({'type': 'error', 'error': 'session_not_found'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
            return

        owns_single_flight = not single_flight_acquired
        if owns_single_flight:
            if not session.try_begin_answer_now():
                yield f"data: {json.dumps({'type': 'error', 'error': 'answer_now_in_progress'})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
                return
        session.begin_answer_now()
        owned_stream = self._run_answer_now_stream_owned(
            session,
            captured_scope_ids=captured_scope_ids,
            cancelled=cancelled,
        )
        try:
            async for event in owned_stream:
                yield event
        except Exception as exc:
            safe_error = redact_data_urls(exc)
            logger.error(f"Answer-now 流式设置异常: {safe_error}")
            yield f"data: {json.dumps({'type': 'error', 'error': safe_error}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
        finally:
            await owned_stream.aclose()
            if owns_single_flight:
                session.finish_answer_now()

    async def _run_answer_now_stream_owned(
        self,
        session: AgentSession,
        *,
        captured_scope_ids: Collection[str],
        cancelled: Callable[[], bool] | None,
    ) -> AsyncGenerator[str, None]:
        captured = frozenset(captured_scope_ids)
        while captured.intersection(session.capture_active_run_scopes()):
            if cancelled is not None and cancelled():
                return
            await asyncio.sleep(0.05)
        waited = cancelled is None or not cancelled()
        if not waited:
            return

        runtime_agent = self._get_runtime_agent(session)
        stable_tool_schema = getattr(runtime_agent, "tools_and_managed_agents", None)
        if isinstance(stable_tool_schema, (list, tuple)):
            tools = list(stable_tool_schema)
        else:
            tools = list(getattr(runtime_agent, "tools", {}).values())
            managed_agents = getattr(runtime_agent, "managed_agents", {})
            if isinstance(managed_agents, dict):
                tools.extend(managed_agents.values())
            elif managed_agents:
                tools.extend(managed_agents)
        answer_now_prompt = self._build_answer_now_prompt(session)
        run_scope_id = session.begin_run()
        request_id = None
        stream_thread: threading.Thread | None = None
        worker_owns_cleanup = False
        worker_stop = threading.Event()
        try:
            yield f"data: {json.dumps({'type': 'session_start', 'session_id': session.session_id})}\n\n"
            memory_messages = list(runtime_agent.write_memory_to_messages())
            memory_messages.append(ChatMessage(
                role=MessageRole.USER,
                content=[{"type": "text", "text": answer_now_prompt}],
            ))
            current_session_context.set(session.session_id, run_scope_id)
            self._bind_tool_runtime_context(
                runtime_agent,
                session.session_id,
                run_scope_id,
                session.capability,
            )

            from backend.llm_service import (
                abort_request,
                abort_request_by_id,
                create_request_id,
            )

            request_id = create_request_id()
            session_request_manager.set(session.session_id, request_id)

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[object | None] = asyncio.Queue()

            def _enqueue_answer_now_item(item: object | None) -> None:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                except RuntimeError:
                    pass

            def _finish_answer_now_worker() -> None:
                if request_id:
                    session_request_manager.clear(session.session_id, request_id)
                    if hasattr(runtime_agent.model, "clear_stream_state"):
                        runtime_agent.model.clear_stream_state(request_id)
                    if hasattr(runtime_agent.model, "llm_service") and hasattr(
                        runtime_agent.model.llm_service, "discard_stream_usage"
                    ):
                        runtime_agent.model.llm_service.discard_stream_usage(request_id)
                current_session_context.clear()
                session.finish_run(run_scope_id)

            def _commit_answer_now_output(full_text: str) -> None:
                next_step_number = 1
                existing_steps = getattr(runtime_agent.memory, "steps", [])
                action_steps = [s for s in existing_steps if isinstance(s, ActionStep)]
                if action_steps:
                    next_step_number = max((s.step_number or 0) for s in action_steps) + 1
                runtime_agent.memory.steps.append(
                    ActionStep(
                        step_number=next_step_number,
                        timing=Timing(start_time=0.0, end_time=0.0),
                        model_output=full_text,
                        tool_calls=None,
                        observations=None,
                        action_output=full_text,
                        is_final_answer=True,
                    )
                )

            def _stream_worker():
                nonlocal request_id
                worker_full_text_parts: list[str] = []
                try:
                    attempt_messages = memory_messages
                    for attempt in range(2):
                        attempt_text_parts: list[str] = []
                        tool_parse_text_parts: list[str] = []
                        buffered_events: list[object] = []
                        provider_tool_calls: list[object] = []
                        # Correctness takes priority over first-token latency here. SSE has
                        # no retraction, so an Answer Now attempt is not observable until its
                        # complete raw visible text and structured metadata pass validation.
                        for event in runtime_agent.model.generate_stream(
                            attempt_messages,
                            stop_sequences=None,
                            tools_to_call_from=tools,
                            request_id=request_id,
                        ):
                            event_tool_calls = getattr(event, "tool_calls", None)
                            if event_tool_calls:
                                provider_tool_calls.extend(event_tool_calls)
                            content = getattr(event, "content", None)
                            if not content:
                                continue
                            attempt_text_parts.append(str(content))
                            if not getattr(event, "reasoning", False):
                                tool_parse_text_parts.append(str(content))
                            buffered_events.append(event)

                        was_stream_aborted = getattr(
                            runtime_agent.model, "was_stream_aborted", None
                        )
                        if callable(was_stream_aborted) and was_stream_aborted(request_id) is True:
                            raise RuntimeError("Answer Now model stream was aborted")

                        full_attempt_text = "".join(attempt_text_parts)
                        stream_state = None
                        get_stream_state = getattr(
                            runtime_agent.model, "get_stream_state", None
                        )
                        if callable(get_stream_state):
                            stream_state = get_stream_state(request_id)
                        raw_visible_content = (
                            str(stream_state.get("content", "") or "")
                            if isinstance(stream_state, dict)
                            else "".join(tool_parse_text_parts)
                        )
                        tool_call_violation = bool(provider_tool_calls)
                        try:
                            resolution = runtime_agent.model._resolve_assistant_control(
                                raw_visible_content,
                                provider_tool_calls or None,
                                tools,
                            )
                        except TextToolProtocolError:
                            tool_call_violation = True
                        else:
                            if (
                                resolution.source != "natural"
                                or resolution.tool_calls
                                or resolution.switch_agent is not None
                            ):
                                tool_call_violation = True

                        if not tool_call_violation:
                            if not full_attempt_text.strip():
                                raise RuntimeError("Answer Now model returned empty output")
                            for event in buffered_events:
                                content = getattr(event, "content", None)
                                if content:
                                    worker_full_text_parts.append(str(content))
                                _enqueue_answer_now_item(event)
                            break
                        if attempt == 1:
                            raise RuntimeError("Answer Now model emitted tool calls twice")
                        session_request_manager.clear(session.session_id, request_id)
                        if hasattr(runtime_agent.model, "clear_stream_state"):
                            runtime_agent.model.clear_stream_state(request_id)
                        if hasattr(runtime_agent.model, "llm_service") and hasattr(
                            runtime_agent.model.llm_service, "discard_stream_usage"
                        ):
                            runtime_agent.model.llm_service.discard_stream_usage(request_id)
                        request_id = create_request_id()
                        session_request_manager.set(session.session_id, request_id)
                        # Append rather than rewrite: the frozen memory and first Answer Now
                        # tail remain byte-for-byte stable for prefix-cache reuse.
                        attempt_messages = [*attempt_messages, ChatMessage(
                            role=MessageRole.USER,
                            content=[{"type": "text", "text": (
                                "Correction: your previous response violated the Answer Now contract. "
                                "Do not call or execute any tool in this Answer Now response. "
                                "Use no tools for this response only. This restriction does not apply to later user turns. "
                                "Return only the final answer."
                            )}],
                        )]
                except Exception as e:
                    _enqueue_answer_now_item(e)
                else:
                    full_text = "".join(worker_full_text_parts)
                    if not worker_stop.is_set() and full_text.strip():
                        _commit_answer_now_output(full_text)
                finally:
                    _finish_answer_now_worker()
                    _enqueue_answer_now_item(None)

            # copy_context() so any ContextVar set on the calling async task
            # is visible inside the worker thread (see _run_basic_stream for
            # the canonical explanation).
            _answer_now_ctx = contextvars.copy_context()
            stream_thread = threading.Thread(
                target=_answer_now_ctx.run,
                args=(_stream_worker,),
                name="answer-now-stream-worker",
                daemon=True,
            )
            stream_thread.start()
            worker_owns_cleanup = True

            cancellation_aborted = False
            while True:
                if cancelled is not None and cancelled() and not cancellation_aborted:
                    cancellation_aborted = True
                    worker_stop.set()
                    from backend.config import get_config

                    config = get_config()
                    abort_request_by_id(
                        request_id,
                        f"http://{config.sglang.host}:{config.sglang.port}",
                        fallback_to_abort_all=False,
                    )
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item

                if cancellation_aborted:
                    continue

                content = getattr(item, "content", None)
                if not content:
                    continue
                yield f"data: {json.dumps({'type': 'content', 'content': content}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
        except Exception as e:
            safe_error = redact_data_urls(e)
            logger.error(f"Answer-now 流式执行异常: {safe_error}")
            yield f"data: {json.dumps({'type': 'error', 'error': safe_error}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
        finally:
            current_session_context.clear()
            if worker_owns_cleanup:
                if stream_thread is not None and stream_thread.is_alive() and request_id:
                    worker_stop.set()
                    abort_request(request_id)
            else:
                if request_id:
                    session_request_manager.clear(session.session_id, request_id)
                    if hasattr(runtime_agent.model, "clear_stream_state"):
                        runtime_agent.model.clear_stream_state(request_id)
                    if hasattr(runtime_agent.model, "llm_service") and hasattr(
                        runtime_agent.model.llm_service, "discard_stream_usage"
                    ):
                        runtime_agent.model.llm_service.discard_stream_usage(request_id)
                session.finish_run(run_scope_id)

    def prepare_session_for_new_turn(self, session_id: str) -> AgentSession | None:
        session = self.sessions.get(session_id)
        if not session:
            return None

        runtime_agent = self._get_runtime_agent(session)
        entry_profile_name = self.config.agent.entry_agent_name
        shared_steps = []
        if hasattr(runtime_agent, "memory") and hasattr(runtime_agent.memory, "steps"):
            shared_steps = list(runtime_agent.memory.steps)

        shared_state = None
        if hasattr(runtime_agent, "state"):
            shared_state = self._strip_transient_state_fields(runtime_agent.state)

        entry_agent = self._build_profile_agent(
            entry_profile_name,
            session=session,
            runtime_context=session.runtime_context,
        )
        if hasattr(entry_agent, "memory") and hasattr(entry_agent.memory, "steps"):
            entry_agent.memory.steps = shared_steps
        if shared_state is not None and hasattr(entry_agent, "state"):
            entry_agent.state = shared_state

        session.agent = entry_agent
        session.active_agent = entry_agent
        session.current_agent_name = entry_profile_name
        session.turn_switch_count = 0
        session.has_shared_history = session.has_shared_history or bool(shared_steps)
        session.answer_now = False
        session.answering_now = False
        session.clear_abort()
        abort_manager.clear(session_id)
        session_request_manager.cleanup(session_id)
        return session

    def _should_prepare_new_turn(self, session: AgentSession) -> bool:
        """Only reset reusable session state when no run is still in flight."""
        if session.run_in_progress:
            return False
        if self.get_current_request_id(session.session_id):
            return False
        if session.answering_now:
            return False
        return True

    @staticmethod
    def _bind_tool_runtime_context(
        agent: MultiStepAgent,
        session_id: str,
        abort_scope_id: str | None,
        capability: OperationCapability | None,
    ) -> None:
        if capability is not None:
            require_capability(capability, "session", session_id)
        agent._operation_capability = capability
        for _tool_obj in agent.tools.values():
            _tool_obj._injected_session_id = session_id
            _tool_obj._injected_abort_scope_id = abort_scope_id
            _tool_obj._injected_capability = capability

    @staticmethod
    def _canonical_task_image_count(session: AgentSession) -> int:
        """Count only canonical images in this top-level Agent context."""

        memory = getattr(getattr(session, "agent", None), "memory", None)
        steps = getattr(memory, "steps", None)
        if steps is None:
            raise VisionRequestValidationError(
                "cannot inspect canonical image shape"
            )
        count = 0
        try:
            for step in steps:
                task_images = getattr(step, "task_images", None)
                if task_images is None:
                    continue
                if not isinstance(task_images, (list, tuple)):
                    raise VisionRequestValidationError(
                        "cannot inspect canonical image shape"
                    )
                count += len(task_images)
        except VisionRequestValidationError:
            raise
        except Exception:
            raise VisionRequestValidationError(
                "cannot inspect canonical image shape"
            ) from None
        return count

    def stage_agent_request_admission(
        self,
        session_id: str | None,
        mode: str | None,
        vision_automation_metadata: VisionAutomationTransportMetadata | None,
        inbound_image_count: int | None = None,
        tool_configuration: AgentToolConfigurationRequest | None = None,
    ) -> AgentRequestAdmissionStage:
        """Freeze the post-omission shape without mutating canonical history."""

        normalize_agent_mode(mode or self.config.agent.agent_mode)
        if session_id is not None:
            PathPolicy.validate_session_id(session_id)
        resolved_session_id = session_id or str(uuid.uuid4())
        with self._agent_admission_lock:
            pending_stage = self._pending_agent_admissions.get(resolved_session_id)
            if pending_stage is not None:
                if tool_configuration is not None:
                    try:
                        self.tool_catalog.assert_compatible(
                            pending_stage.tool_configuration_snapshot,
                            tool_configuration,
                        )
                    except AgentToolConfigurationError:
                        raise
                raise SessionBusyError(resolved_session_id)
            if self._is_session_disposal_active(resolved_session_id):
                raise SessionBusyError(resolved_session_id)
            session = self.sessions.get(resolved_session_id)
            if session is not None and session.is_child_context:
                raise SessionBusyError(resolved_session_id)
            if session is None and not PathPolicy.is_canonical_session_id(resolved_session_id):
                raise SessionAdmissionError()
            if session is not None and not self._should_prepare_new_turn(session):
                raise SessionBusyError(resolved_session_id)
            if session is not None:
                snapshot = session.tool_configuration_snapshot
                if snapshot is None:
                    session.behavior_records.setdefault(
                        "tool_configuration_audit", []
                    ).append(
                        {
                            "event": "session_snapshot_missing",
                            "operation": "admission",
                            "session_id": session.session_id,
                        }
                    )
                    logger.error(
                        "Existing Agent session has no frozen tool snapshot during admission: %s",
                        session.session_id,
                    )
                    raise AgentToolConfigurationError(
                        "tool_configuration_locked",
                        "Existing Agent session has no frozen tool configuration",
                    )
                self.tool_catalog.assert_compatible(snapshot, tool_configuration)
            else:
                snapshot = self.tool_catalog.resolve_candidate(tool_configuration)
            historical_image_count = (
                self._canonical_task_image_count(session) if session is not None else 0
            )
            enable_sticky_omission = (
                vision_automation_metadata is not None
                and vision_automation_metadata.source_count > 0
                and vision_automation_metadata.retained_tick_count > 0
                and inbound_image_count == (
                    vision_automation_metadata.manual_image_count
                    + vision_automation_metadata.source_count
                    * vision_automation_metadata.retained_tick_count
                )
            )
            sticky_omission = bool(
                session is not None
                and getattr(session, "vision_automation_image_omission_sticky", False)
            )
            history_omission = None
            if session is not None and (
                vision_automation_metadata is not None or sticky_omission
            ):
                history_omission = stage_agent_history_image_omission(
                    session,
                    vision_automation_metadata,
                    enable_sticky_omission=enable_sticky_omission,
                    omit_history=(sticky_omission or enable_sticky_omission),
                )
                historical_image_count -= history_omission.omitted_image_count
            stage = AgentRequestAdmissionStage(
                session_id=resolved_session_id,
                projected_historical_image_count=historical_image_count,
                vision_automation_metadata=vision_automation_metadata,
                tool_configuration_snapshot=snapshot,
                tool_configuration_candidate=tool_configuration,
                _session=session,
                _history_omission=history_omission,
                _enable_sticky_vision_automation_omission=enable_sticky_omission,
                _requires_prepare=session is not None,
            )
            self._pending_agent_admissions[resolved_session_id] = stage
            return stage

    def register_agent_vision_automation_cleanup(
        self,
        session_id: str,
        binding,
        cleanup_token: str,
    ):
        """Register one pending cleanup token without touching prompt memory."""

        with self._agent_admission_lock:
            if (
                session_id in self._pending_agent_admissions
                or self._is_session_disposal_active(session_id)
            ):
                raise SessionBusyError(session_id)
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            return register_agent_vision_automation_cleanup(
                session,
                binding,
                cleanup_token,
            )

    def cancel_agent_vision_automation_cleanup(
        self,
        session_id: str,
        binding,
        cleanup_token: str,
    ):
        """Cancel one exact pending cleanup token under admission ownership."""

        with self._agent_admission_lock:
            if (
                session_id in self._pending_agent_admissions
                or self._is_session_disposal_active(session_id)
            ):
                raise SessionBusyError(session_id)
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            return cancel_agent_vision_automation_cleanup(
                session,
                binding,
                cleanup_token,
            )

    def rollback_agent_request_stage(
        self, stage: AgentRequestAdmissionStage
    ) -> None:
        """Release exact pre-accept staging and remove a provisional session."""

        session_to_delete = None
        with self._agent_admission_lock:
            if stage._accepted or stage._rolled_back:
                return
            if self._pending_agent_admissions.get(stage.session_id) is stage:
                self._pending_agent_admissions.pop(stage.session_id, None)
            stage._rolled_back = True
            stage._history_omission = None
            if stage._created_session:
                session_to_delete = stage._session
        if session_to_delete is not None:
            self.sessions.delete_if_same(stage.session_id, session_to_delete)

    def capture_agent_request_run_scopes(
        self, stage: AgentRequestAdmissionStage
    ) -> frozenset[str]:
        session = stage._session
        if session is None:
            return frozenset()
        return session.capture_active_run_scopes()

    async def wait_for_agent_request_run_cleanup(
        self,
        stage: AgentRequestAdmissionStage,
        scope_ids: Collection[str],
    ) -> None:
        session = stage._session
        if session is not None and scope_ids:
            await asyncio.to_thread(session.wait_for_run_scopes, scope_ids)

    def _create_session(
        self,
        session_id: str,
        resolved_mode: str,
        tool_snapshot: AgentToolConfigurationSnapshot | None = None,
    ) -> AgentSession:
        tool_snapshot = tool_snapshot or self.default_tool_configuration_snapshot
        capability = build_session_capability(
            self.config,
            session_id=session_id,
            mode=resolved_mode,
        )
        entry_profile_name = self.config.agent.entry_agent_name
        runtime_context = self._build_runtime_context_for_profile(entry_profile_name)
        agent = self._build_profile_agent(
            entry_profile_name,
            runtime_context=runtime_context,
            session=None,
            tool_snapshot=tool_snapshot,
        )
        session = AgentSession(
            session_id=session_id,
            agent=agent,
            tool_protocol=getattr(self.config.inference, "tool_protocol", "legacy_text"),
            max_agent_switches_per_turn=self.config.agent.max_agent_switches_per_turn,
            mode=resolved_mode,
            tool_configuration_snapshot=tool_snapshot,
            capability=capability,
            current_agent_name=entry_profile_name,
            runtime_context=runtime_context,
        )
        agent._agent_session = session
        if getattr(agent, "model", None) is self.model:
            agent.model = LocalLLMModel(
                llm_service=self.llm_service,
                model_id=self.config.model.model_name,
                runtime_context=runtime_context,
                session=session,
            )
        elif isinstance(getattr(agent, "model", None), LocalLLMModel) and agent.model._session is None:
            # Profile construction can create a dedicated model before its
            # AgentSession exists. Bind it now while preserving runtime context.
            agent.model._session = session
        self._bind_tool_runtime_context(
            agent,
            session_id,
            None,
            capability,
        )
        if abort_manager.is_aborted(session_id):
            session.abort()
        if not self.sessions.put_if_absent(session):
            self._release_session_resources(session, set())
            raise SessionBusyError(session_id)
        logger.info(f"新建 Agent session: {session_id} (mode={resolved_mode})")
        return session

    def _begin_staged_agent_request(
        self,
        stage: AgentRequestAdmissionStage,
        mode: str | None,
        on_run_registered: Callable[
            [AgentRequestAdmissionStage, frozenset[str]], None
        ] | None,
    ) -> tuple[AgentSession, str]:
        if on_run_registered is None:
            raise RuntimeError("staged Agent admission requires registration callback")
        resolved_mode = normalize_agent_mode(mode or self.config.agent.agent_mode)
        try:
            with self._agent_admission_lock:
                if (
                    stage._rolled_back
                    or stage._accepted
                    or self._pending_agent_admissions.get(stage.session_id) is not stage
                ):
                    raise RuntimeError("invalid Agent admission stage")
                session = stage._session
                if session is None:
                    session = self._create_session(
                        stage.session_id, resolved_mode, stage.tool_configuration_snapshot
                    )
                    stage._session = session
                    stage._created_session = True
                elif self.sessions.get(stage.session_id) is not session:
                    raise RuntimeError("Agent admission session changed")
                if session.run_in_progress or session.answering_now:
                    raise SessionBusyError(stage.session_id)
                run_scope_id = session.begin_run()
        except Exception:
            # _create_session builds the profile agent (and therefore renders
            # its prompt) while admission ownership is held.  A render error
            # must release the pending stage and provisional session exactly
            # like a later pre-accept failure; otherwise the session is stuck
            # busy after a 500 response.
            self.rollback_agent_request_stage(stage)
            raise

        try:
            history_omission = stage._history_omission
            if (
                history_omission is None
                and (
                    stage.vision_automation_metadata is not None
                    or stage._enable_sticky_vision_automation_omission
                    or session.vision_automation_image_omission_sticky
                )
            ):
                history_omission = stage_agent_history_image_omission(
                    session,
                    stage.vision_automation_metadata,
                    enable_sticky_omission=(
                        stage._enable_sticky_vision_automation_omission
                    ),
                    omit_history=(
                        session.vision_automation_image_omission_sticky
                        or stage._enable_sticky_vision_automation_omission
                    ),
                )
                stage._history_omission = history_omission
            if history_omission is not None:
                commit_agent_history_image_omission(
                    session,
                    history_omission,
                    release_resources=False,
                )
            stage._committed = True
            on_run_registered(stage, session.capture_active_run_scopes())
            stage._accepted = True
            if history_omission is not None:
                finalize_agent_history_image_omission(history_omission)
                stage._history_omission = None
            with self._agent_admission_lock:
                if self._pending_agent_admissions.get(stage.session_id) is stage:
                    self._pending_agent_admissions.pop(stage.session_id, None)
            if stage._requires_prepare:
                prepared = self.prepare_session_for_new_turn(stage.session_id)
                if prepared is None:
                    raise RuntimeError("Agent admission session disappeared")
                session = prepared
                stage._session = session
            return session, run_scope_id
        except Exception:
            if stage._committed and not stage._accepted and stage._history_omission is not None:
                rollback_committed_agent_history_image_omission(
                    session,
                    stage._history_omission,
                )
                stage._committed = False
                stage._history_omission = None
            session.finish_run(run_scope_id)
            self.rollback_agent_request_stage(stage)
            raise

    def _get_or_create_session_locked(
        self, session_id: str | None, resolved_mode: str
    ) -> AgentSession:
        """Resolve and prepare a session while admission ownership is held."""
        if session_id is not None:
            PathPolicy.validate_session_id(session_id)
        if session_id:
            if (
                session_id in self._pending_agent_admissions
                or self._is_session_disposal_active(session_id)
            ):
                raise SessionBusyError(session_id)

        ttl_minutes = getattr(self.config.agent, "session_ttl_minutes", 0) or 0
        if ttl_minutes > 0:
            self.cleanup_expired_sessions(ttl_minutes)

        if session_id:
            session = self.sessions.get(session_id)
            if session:
                if session.is_child_context:
                    raise SessionBusyError(session_id)
                if self._should_prepare_new_turn(session):
                    if session.aborted:
                        logger.info(f"Session {session_id} 存在残留 abort 标志，清理后继续复用")
                    prepared_session = self.prepare_session_for_new_turn(session_id)
                    if prepared_session is not None:
                        return prepared_session
                else:
                    logger.info(f"Session {session_id} 仍有活动 run，跳过新 turn 预处理")
                    raise SessionBusyError(session_id)
                return session

        if session_id:
            if not PathPolicy.is_canonical_session_id(session_id):
                raise SessionAdmissionError()
            return self._create_session(session_id, resolved_mode)
        while True:
            generated_session_id = str(uuid.uuid4())
            try:
                return self._create_session(generated_session_id, resolved_mode)
            except SessionBusyError:
                continue

    def get_or_create_session(
        self,
        session_id: str | None,
        mode: str | None = None,
        *,
        _admission_lock_held: bool = False,
    ) -> AgentSession:
        """获取已有 session 或创建新的"""
        resolved_mode = normalize_agent_mode(mode or self.config.agent.agent_mode)
        if _admission_lock_held:
            return self._get_or_create_session_locked(session_id, resolved_mode)
        with self._agent_admission_lock:
            return self._get_or_create_session_locked(session_id, resolved_mode)

    def _get_or_create_session_for_run(
        self, session_id: str | None, mode: str | None
    ) -> tuple[AgentSession, str]:
        """Deprecated internal fallback: resolve a session and register its run.

        New production callers must use staged admission so candidate validation,
        immutable tool snapshots, acceptance, and rollback share one lifecycle.
        Agent API and Cycle already do so; retain this path only for legacy
        direct-runner compatibility until those internal callers are migrated.
        """

        resolved_mode = normalize_agent_mode(mode or self.config.agent.agent_mode)
        with self._agent_admission_lock:
            session = self.get_or_create_session(
                session_id,
                resolved_mode,
                _admission_lock_held=True,
            )
            return session, session.begin_run()

    def cleanup_expired_sessions(self, ttl_minutes: int) -> tuple[str, ...]:
        """Expire inactive contexts and cascade exact manager-owned children."""

        session_snapshot = dict(self.sessions.exact_items())
        now = datetime.now()
        ttl_seconds = ttl_minutes * 60
        manager_runs: dict[str, list[ChildAgentRun]] = {}
        manager_mapped_child_ids: dict[str, set[str]] = {}
        with self._child_agent_lock:
            for child_run_id, child_run in self.child_agent_runs.items():
                if child_run_id in self._retained_child_replay_ids:
                    continue
                manager_runs.setdefault(child_run.manager_session_id, []).append(
                    child_run
                )
            for (manager_id, _), child_session_id in (
                self.child_agent_sessions_by_key.items()
            ):
                manager_mapped_child_ids.setdefault(manager_id, set()).add(
                    child_session_id
                )

        manager_owned_children: dict[str, dict[int, AgentSession]] = {}
        for manager_id in set(manager_runs) | set(manager_mapped_child_ids):
            owned: dict[int, AgentSession] = {}
            pinned_child_ids: dict[str, set[int]] = {}
            for child_run in manager_runs.get(manager_id, ()):
                child_session = child_run.child_session_identity
                if child_session is None:
                    candidate = session_snapshot.get(child_run.child_run_id)
                    if candidate is not None and candidate.is_child_context:
                        child_session = candidate
                if child_session is not None:
                    owned[id(child_session)] = child_session
                    pinned_child_ids.setdefault(child_run.child_run_id, set()).add(
                        id(child_session)
                    )
            for child_session_id in manager_mapped_child_ids.get(manager_id, ()):
                child_session = session_snapshot.get(child_session_id)
                if child_session is not None and child_session.is_child_context:
                    pinned_identities = pinned_child_ids.get(child_session_id)
                    if (
                        pinned_identities is not None
                        and id(child_session) not in pinned_identities
                    ):
                        continue
                    owned[id(child_session)] = child_session
            manager_owned_children[manager_id] = owned

        cycle_manager_ids = self.sessions.cycle_pinned_session_ids()
        protected_manager_ids: set[str] = set(cycle_manager_ids)
        # Keep manager-created child contexts pinned with their Cycle owner.
        for manager_id in tuple(protected_manager_ids):
            protected_manager_ids.update(child.session_id for child in manager_owned_children.get(manager_id, {}).values())
        for manager_id, owned in manager_owned_children.items():
            manager = session_snapshot.get(manager_id)
            if manager is None or manager.is_child_context:
                continue
            if (now - manager.last_activity).total_seconds() <= ttl_seconds:
                continue
            child_runs = manager_runs.get(manager_id, ())
            if any(
                child.run_in_progress or child.event_hub.has_active_producer
                for child in owned.values()
            ) or any(
                child_run.status not in CHILD_TERMINAL_STATUSES
                or not child_run.terminal_event.is_set()
                for child_run in child_runs
            ):
                protected_manager_ids.add(manager_id)

        expired_session_ids = set(
            self.sessions.cleanup_expired(
                ttl_minutes,
                protected_session_ids=frozenset(protected_manager_ids),
            )
        )
        active_session_ids = set(self.sessions.session_ids())
        expired_managers = {
            session_id
            for session_id in expired_session_ids
            if session_id in manager_owned_children
            and not session_snapshot[session_id].is_child_context
        }

        seen_images: set[int] = set()
        for session_id in expired_session_ids:
            expired_session = session_snapshot.get(session_id)
            if expired_session is not None:
                self._release_session_resources(
                    expired_session, seen_images, release_hub=False
                )

        cascade_children: dict[int, AgentSession] = {}
        cascade_run_ids: set[str] = set()
        for manager_id in expired_managers:
            cascade_children.update(manager_owned_children.get(manager_id, {}))
            cascade_run_ids.update(
                child_run.child_run_id
                for child_run in manager_runs.get(manager_id, ())
            )
        for child_session in cascade_children.values():
            self._release_session_resources(
                child_session, seen_images, release_hub=False
            )
            if child_session.event_hub.released:
                continue
            deleted_exact = self._delete_disposed_session_exact(child_session)
            if deleted_exact:
                session_request_manager.cleanup(child_session.session_id)
                abort_manager.cleanup(child_session.session_id)

        removed_run_ids: list[str] = []
        with self._child_agent_lock:
            for child_run_id, child_run in list(self.child_agent_runs.items()):
                if child_run.manager_session_id in cycle_manager_ids:
                    continue
                backing_expired = child_run.child_run_id in expired_session_ids
                terminal_age_expired = (
                    child_run.completed_at is not None
                    and (now - child_run.completed_at).total_seconds() > ttl_seconds
                )
                retained_orphan_expired = (
                    child_run_id in self._retained_child_replay_ids
                    and terminal_age_expired
                )
                detached_orphan_expired = (
                    child_run_id not in self._retained_child_replay_ids
                    and child_run.child_run_id not in active_session_ids
                    and terminal_age_expired
                )
                manager_cascade = child_run_id in cascade_run_ids
                if not (
                    backing_expired
                    or retained_orphan_expired
                    or detached_orphan_expired
                    or manager_cascade
                ):
                    continue
                removed_run_ids.append(child_run_id)
                self.child_agent_runs.pop(child_run_id, None)
                self._retained_child_replay_ids.discard(child_run_id)
                child_key = (child_run.manager_session_id, child_run.child_name)
                if self.child_agent_sessions_by_key.get(child_key) == child_run.child_run_id:
                    self.child_agent_sessions_by_key.pop(child_key, None)
            for child_key in list(self.child_agent_sessions_by_key):
                if child_key[0] in expired_managers:
                    self.child_agent_sessions_by_key.pop(child_key, None)
        return tuple(sorted(expired_session_ids))

    @staticmethod
    def _close_owned_images(value: Any, seen_images: set[int]) -> None:
        if isinstance(value, PILImage):
            image_id = id(value)
            if image_id in seen_images:
                return
            seen_images.add(image_id)
            for attribute in ("_pre_encoded_b64", "_pre_encoded_mime"):
                if hasattr(value, attribute):
                    try:
                        delattr(value, attribute)
                    except Exception:
                        pass
            try:
                value.close()
            except Exception as exc:
                logger.warning("Failed to close disposed Agent image: %s", exc)
            return
        if isinstance(value, dict):
            for item in value.values():
                AgentRunner._close_owned_images(item, seen_images)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                AgentRunner._close_owned_images(item, seen_images)
            return
        if isinstance(value, ChatMessage):
            AgentRunner._close_owned_images(value.content, seen_images)

    @classmethod
    def _release_session_resources(
        cls,
        session: AgentSession,
        seen_images: set[int],
        *,
        release_hub: bool = True,
    ) -> None:
        if release_hub:
            session.event_hub.close("session_resources_released")
            session.event_hub.release()
        agents = []
        for agent in (session.agent, session.active_agent):
            if agent is not None and all(agent is not item for item in agents):
                agents.append(agent)

        seen_memories: set[int] = set()
        for agent in agents:
            memory = getattr(agent, "memory", None)
            if memory is not None and id(memory) not in seen_memories:
                seen_memories.add(id(memory))
                steps = list(getattr(memory, "steps", ()) or ())
                for step in steps:
                    for field_name in ("task_images", "observations_images"):
                        images = getattr(step, field_name, None)
                        cls._close_owned_images(images, seen_images)
                        if hasattr(step, field_name):
                            setattr(step, field_name, None)
                memory.steps = []
            state = getattr(agent, "state", None)
            cls._close_owned_images(state, seen_images)
            if hasattr(agent, "state"):
                agent.state = {}

        cls._close_owned_images(session.last_compactable_llm_messages, seen_images)
        cls._close_owned_images(session.last_llm_prompt_snapshot, seen_images)
        cls._close_owned_images(session.compaction_archive, seen_images)
        session.last_compactable_llm_messages = None
        session.last_compactable_llm_tools = None
        session.last_compactable_memory_steps = None
        session.last_compactable_agent_name = None
        session.last_llm_prompt_snapshot = None
        session.compaction_archive = []
        session.vision_automation_binding = None
        session.vision_automation_cleanup_token = None
        session.vision_automation_image_omission_sticky = False
        with session.vision_event_asset_lock:
            session.vision_event_image_assets.clear()
            session.vision_event_turn_assets.clear()
        session.behavior_records = {}
        session.runtime_context = None
        session.timeline_events = []
        for loop, queue in list(session.timeline_event_sinks):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:
                pass
        session.timeline_event_sinks = []
        session.accumulated_content = ""
        session.accumulated_thought = ""

    def _delete_disposed_session_exact(self, session: AgentSession) -> bool:
        """Delete/release one pinned context without touching a replacement."""

        if self.sessions.delete_if_same(session.session_id, session):
            return True
        session.event_hub.close("session_deleted")
        session.event_hub.release()
        return False

    def _manager_child_snapshot(
        self, manager_session_id: str
    ) -> tuple[list[ChildAgentRun], set[str]]:
        with self._child_agent_lock:
            child_runs = [
                child_run
                for child_run_id, child_run in self.child_agent_runs.items()
                if child_run.manager_session_id == manager_session_id
                and child_run_id not in self._retained_child_replay_ids
            ]
            child_session_ids = {
                child_session_id
                for (manager_id, _), child_session_id in self.child_agent_sessions_by_key.items()
                if manager_id == manager_session_id
            }
            child_session_ids.update(
                child_run.child_run_id for child_run in child_runs
            )
            return child_runs, child_session_ids

    async def _targeted_abort_disposal_request(
        self, session_id: str, request_id: str | None
    ) -> None:
        if not request_id:
            return
        from ..llm_service import abort_request_by_id

        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def _finish() -> None:
            if not completed.done():
                completed.set_result(None)

        def _abort_in_worker() -> None:
            try:
                abort_request_by_id(
                    request_id,
                    f"http://{self.config.sglang.host}:{self.config.sglang.port}",
                    fallback_to_abort_all=False,
                )
            except Exception as exc:
                logger.warning(
                    "Targeted session disposal abort failed session_id=%s: %s",
                    session_id,
                    exc,
                )
            finally:
                try:
                    loop.call_soon_threadsafe(_finish)
                except RuntimeError:
                    pass

        threading.Thread(
            target=_abort_in_worker,
            name="targeted-session-abort-worker",
            daemon=True,
        ).start()
        await completed

    async def _dispose_session_pinned(
        self, session_id: str, session: AgentSession
    ) -> dict[str, str]:
        child_runs: list[ChildAgentRun] = []
        child_sessions: dict[str, AgentSession] = {}
        captured_scopes: dict[str, frozenset[str]] = {}
        captured_requests: dict[str, str | None] = {}
        timeout_seconds = self.config.agent.session_disposal_timeout_seconds

        try:
            async with asyncio.timeout(timeout_seconds):
                captured_scopes[session_id] = session.capture_active_run_scopes()
                captured_requests[session_id] = self.get_current_request_id(session_id)
                session.abort()
                abort_manager.abort(session_id)
                session.abort_active_runs()

                child_runs, child_session_ids = self._manager_child_snapshot(session_id)
                for child_run in child_runs:
                    child_run.abort_reason = "The manager session is being disposed."
                for child_session_id in child_session_ids:
                    child_session = self.sessions.get(child_session_id)
                    if child_session is None:
                        continue
                    child_sessions[child_session.session_id] = child_session
                    captured_scopes[child_session.session_id] = (
                        child_session.capture_active_run_scopes()
                    )
                    captured_requests[child_session.session_id] = (
                        self.get_current_request_id(child_session.session_id)
                    )
                    child_session.abort()
                    abort_manager.abort(child_session.session_id)
                    child_session.abort_active_runs()

                await asyncio.gather(
                    *(
                        self._targeted_abort_disposal_request(target_id, request_id)
                        for target_id, request_id in captured_requests.items()
                    )
                )

                barriers = [
                    asyncio.to_thread(
                        session.wait_for_disposal_barrier,
                        captured_scopes[session_id],
                    )
                ]
                for child_session_id, child_session in child_sessions.items():
                    barriers.append(
                        asyncio.to_thread(
                            child_session.wait_for_disposal_barrier,
                            captured_scopes[child_session_id],
                        )
                    )
                for child_run in child_runs:
                    if child_run.status not in CHILD_TERMINAL_STATUSES:
                        barriers.append(asyncio.to_thread(child_run.terminal_event.wait))
                await asyncio.gather(*barriers)

                # The manager barrier prevents new dispatches. Include any child
                # metadata created between the initial abort and that barrier.
                _, final_child_session_ids = self._manager_child_snapshot(session_id)
                late_abort_requests = []
                late_barriers = []
                for child_session_id in final_child_session_ids:
                    child_session = self.sessions.get(child_session_id)
                    if child_session is not None:
                        if child_session.session_id in child_sessions:
                            continue
                        child_sessions[child_session.session_id] = child_session
                        late_scopes = child_session.capture_active_run_scopes()
                        captured_scopes[child_session.session_id] = late_scopes
                        late_request_id = self.get_current_request_id(
                            child_session.session_id
                        )
                        captured_requests[child_session.session_id] = late_request_id
                        child_session.abort()
                        abort_manager.abort(child_session.session_id)
                        child_session.abort_active_runs()
                        late_abort_requests.append(
                            self._targeted_abort_disposal_request(
                                child_session.session_id, late_request_id
                            )
                        )
                        late_barriers.append(
                            asyncio.to_thread(
                                child_session.wait_for_disposal_barrier,
                                late_scopes,
                            )
                        )
                if late_abort_requests:
                    await asyncio.gather(*late_abort_requests)
                if late_barriers:
                    await asyncio.gather(*late_barriers)

                disposed_child_runs, _ = self._manager_child_snapshot(session_id)
                for child_run in disposed_child_runs:
                    self._ensure_disposed_child_terminal_replay(child_run)

                from backend.computer_use.executable_protocol import dispose_vision_desktop_session
                for desktop_session_id in (session_id, *child_sessions.keys()):
                    await asyncio.to_thread(dispose_vision_desktop_session, desktop_session_id)

                seen_images: set[int] = set()
                for child_session in child_sessions.values():
                    self._release_session_resources(
                        child_session, seen_images, release_hub=False
                    )
                self._release_session_resources(
                    session, seen_images, release_hub=False
                )

                with self._child_agent_lock:
                    for child_run in disposed_child_runs:
                        self.child_agent_runs.pop(child_run.child_run_id, None)
                        self._retained_child_replay_ids.discard(
                            child_run.child_run_id
                        )
                    for child_key in list(self.child_agent_sessions_by_key):
                        if child_key[0] == session_id:
                            self.child_agent_sessions_by_key.pop(child_key, None)
                for child_session in child_sessions.values():
                    session_request_manager.cleanup(child_session.session_id)
                    abort_manager.cleanup(child_session.session_id)
                    for scope_id in captured_scopes.get(child_session.session_id, ()):
                        run_abort_manager.cleanup(scope_id)
                    self._delete_disposed_session_exact(child_session)

                session_request_manager.cleanup(session_id)
                abort_manager.cleanup(session_id)
                for scope_id in captured_scopes[session_id]:
                    run_abort_manager.cleanup(scope_id)
                self._delete_disposed_session_exact(session)
        except TimeoutError as exc:
            pending_scope_count = len(session.capture_active_run_scopes()) + sum(
                len(child_session.capture_active_run_scopes())
                for child_session in child_sessions.values()
            )
            pending_child_count = sum(
                child_run.status
                not in CHILD_TERMINAL_STATUSES
                and not child_run.terminal_event.is_set()
                for child_run in child_runs
            )
            raise SessionDisposalError(
                "session_disposal_timeout",
                pending_scope_count=pending_scope_count,
                pending_child_count=pending_child_count,
            ) from exc
        except SessionDisposalError:
            raise
        except Exception as exc:
            logger.exception("Agent session disposal failed session_id=%s", session_id)
            raise SessionDisposalError("session_disposal_failed") from exc

        return {"status": "disposed", "session_id": session_id}

    async def _dispose_session_selected(
        self,
        session_id: str,
        expected_session: AgentSession | None,
    ) -> dict[str, str]:
        """Coordinate disposal, optionally pinned to one exact session object."""

        with self._agent_admission_lock:
            existing = self._session_disposal_tasks.get(session_id)
            if existing is not None:
                disposing_session, task = existing
                if (
                    expected_session is not None
                    and disposing_session is not expected_session
                ):
                    return {"status": "already_disposed", "session_id": session_id}
            else:
                session = self.sessions.get(session_id)
                if session is None:
                    self.sessions.cleanup_orphan_session_images(session_id)
                    return {"status": "already_disposed", "session_id": session_id}
                if expected_session is not None and session is not expected_session:
                    return {"status": "already_disposed", "session_id": session_id}
                if session.is_child_context:
                    raise SessionDisposalError("session_disposal_requires_top_level")
                if session_id in self._pending_agent_admissions:
                    raise SessionDisposalError("session_disposal_admission_pending")
                task = asyncio.create_task(
                    self._dispose_session_pinned(session_id, session),
                    name=f"dispose-agent-session-{session_id}",
                )
                self._session_disposal_tasks[session_id] = (session, task)

                def _forget_disposal(completed: asyncio.Task) -> None:
                    with self._agent_admission_lock:
                        current = self._session_disposal_tasks.get(session_id)
                        if current is not None and current[1] is completed:
                            self._session_disposal_tasks.pop(session_id, None)

                task.add_done_callback(_forget_disposal)
        return await asyncio.shield(task)

    async def dispose_session_exact(
        self,
        session_id: str,
        *,
        expected_session: AgentSession,
    ) -> dict[str, str]:
        """Dispose only a previously pinned top-level Agent session identity."""

        return await self._dispose_session_selected(session_id, expected_session)

    async def dispose_session(self, session_id: str) -> dict[str, str]:
        """Dispose the current top-level context and manager-owned children."""

        return await self._dispose_session_selected(session_id, None)

    def get_current_request_id(self, session_id: str) -> str | None:
        """返回 session 当前正在执行的底层 request_id。"""
        return session_request_manager.get(session_id)

    @staticmethod
    def _context_message_field(message: Any, field: str, default: Any = None) -> Any:
        if isinstance(message, dict):
            return message.get(field, default)
        return getattr(message, field, default)

    @staticmethod
    def _context_content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(str(item["text"]))
                elif hasattr(item, "type") and getattr(item, "type", None) == "text":
                    text = getattr(item, "text", None)
                    if text:
                        parts.append(str(text))
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _normalize_context_text(text: str) -> str:
        return " ".join(str(text or "").split())

    def _bootstrap_session_from_context_messages(
        self,
        session: AgentSession,
        context_messages: list[Any] | None,
        *,
        current_message: str,
    ) -> bool:
        """Import visible chat turns when a new agent session starts mid-chat.

        The normal agent prompt remains system/runtime + memory.steps. We only
        seed/extend memory.steps, skip the chat system prompt, and leave the
        current user turn for agent.run() to append.
        """
        if not context_messages:
            return False

        agent = self._get_runtime_agent(session)
        memory = getattr(agent, "memory", None)
        steps = getattr(memory, "steps", None)
        if steps is None:
            return False

        current_text = self._context_content_to_text(current_message).strip()
        current_user_index: int | None = None
        for idx, item in enumerate(context_messages):
            role = str(self._context_message_field(item, "role", "") or "").lower()
            text = self._context_content_to_text(
                self._context_message_field(item, "content", None)
            ).strip()
            if role == "user" and current_text and text == current_text:
                current_user_index = idx

        context_turns: list[tuple[str, str]] = []
        for idx, item in enumerate(context_messages):
            role = str(self._context_message_field(item, "role", "") or "").lower()
            metadata = self._context_message_field(item, "metadata", {}) or {}
            if role == "system":
                if not isinstance(metadata, dict) or metadata.get("compact_summary") is not True:
                    continue
                role = "user"
            if role not in {"user", "assistant"}:
                continue

            text = self._context_content_to_text(
                self._context_message_field(item, "content", None)
            ).strip()
            if not text:
                continue
            if role == "user" and idx == current_user_index:
                continue
            context_turns.append((role, text))

        if not context_turns:
            return False

        start_index = 0
        if len(steps) > 0:
            existing_texts: set[str] = set()
            for step in steps:
                if isinstance(step, TaskStep) and getattr(step, "task", None):
                    existing_texts.add(self._normalize_context_text(step.task))
                elif isinstance(step, ActionStep):
                    for value in (
                        getattr(step, "action_output", None),
                        getattr(step, "model_output", None),
                    ):
                        if value:
                            existing_texts.add(self._normalize_context_text(str(value)))

            boundary_index = -1
            for idx, (_role, text) in enumerate(context_turns):
                if self._normalize_context_text(text) in existing_texts:
                    boundary_index = idx
            if boundary_index == -1:
                return False
            start_index = boundary_index + 1

        imported_steps: list[Any] = []
        next_step_number = self._next_action_step_number(agent)
        for role, text in context_turns[start_index:]:
            if role == "user":
                imported_steps.append(TaskStep(task=text, task_images=None))
                continue

            imported_steps.append(
                ActionStep(
                    step_number=next_step_number,
                    timing=Timing(start_time=0.0, end_time=0.0),
                    model_output=text,
                    tool_calls=None,
                    observations=None,
                    action_output=text,
                    is_final_answer=True,
                )
            )
            next_step_number += 1

        if not imported_steps:
            return False

        steps.extend(imported_steps)
        session.has_run = True
        session.has_shared_history = True
        logger.info(
            "Bootstrapped agent session %s with %d context memory steps from mode switch",
            session.session_id,
            len(imported_steps),
        )
        return True

    def _decode_request_images(self, images: list[str] | None) -> list | None:
        """将请求中的 base64/data URL 图片解码为 PIL.Image 列表。"""
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

                pil_images = []
                for idx, img_data in enumerate(images):
                    mime_hint = ""
                    if img_data.startswith("data:"):
                        header, b64_part = img_data.split(",", 1)
                        try:
                            mime_hint = header.split(":")[1].split(";")[0]
                        except Exception:
                            pass
                    else:
                        b64_part = img_data

                    img_bytes = base64.b64decode(b64_part)

                    if max_size > 0 and len(img_bytes) > max_size:
                        logger.warning(
                            f"Agent 图片[{idx}] 大小 {len(img_bytes)} 字节超过限制 {max_size}，跳过"
                        )
                        continue

                    pil_img = PILImage.open(io.BytesIO(img_bytes))
                    detected_fmt = (pil_img.format or "").lower()
                    if detected_fmt == "jpeg":
                        detected_fmt = "jpeg"
                    check_fmt = detected_fmt or mime_hint.replace("image/", "")
                    if supported_fmts and check_fmt and check_fmt not in supported_fmts:
                        logger.warning(
                            f"Agent 图片[{idx}] 格式 {check_fmt!r} 不在支持列表 {supported_fmts}，跳过"
                        )
                        continue

                    original_pil_fmt = pil_img.format

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

                    pil_img.format = original_pil_fmt  # type: ignore[attr-defined]

                    _resized = max_dim > 0 and (w > max_dim or h > max_dim)
                    _color_changed = _orig_mode != pil_img.mode
                    if not _resized and not _color_changed and detected_fmt == "jpeg":
                        pil_img._pre_encoded_b64 = b64_part  # type: ignore[attr-defined]
                        pil_img._pre_encoded_mime = "image/jpeg"  # type: ignore[attr-defined]
                        logger.info(
                            f"Agent 图片[{idx}]: ⚡ 使用原始 JPEG 字节（无变换）, "
                            f"base64 {len(b64_part)} chars"
                        )
                    else:
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
                logger.warning(
                    f"图片解码失败，将忽略图片: {redact_data_urls(e)}"
                )
                pil_images = None

        return pil_images

    def _session_memory_to_messages(self, session) -> list:
        """Flatten session.agent.memory.steps into a list of LLM Message
        objects suitable for the compact orchestrator's body.

        smolagents uses TaskStep (the user query) and ActionStep
        (the agent's model_output + tool observations). We render them
        into user/assistant turns in temporal order. The same renderer
        is also used by the auto-compact token-count check below.
        """
        if getattr(session, "tool_protocol", "legacy_text") == "qwen35-native":
            return _chat_messages_to_backend_messages(session.agent.write_memory_to_messages())
        from ..llm_service import Message
        from smolagents.memory import ActionStep, TaskStep
        messages: list[Message] = []
        for step in session.agent.memory.steps:
            if isinstance(step, TaskStep):
                messages.append(Message(role="user", content=step.task or ""))
            elif isinstance(step, ActionStep):
                if getattr(step, "model_output", None):
                    messages.append(Message(role="assistant", content=step.model_output))
                obs = projected_observations(step)
                if obs:
                    messages.append(Message(role="user", content=str(obs)))
        return messages

    def _should_auto_compact_session(self, session) -> bool:
        """Check whether the session's memory exceeds the compact threshold."""
        if self.compact_orchestrator is None:
            return False
        cfg = self.config
        if not cfg.compact.enabled or not cfg.compact.auto_trigger:
            return False
        prompts_cfg = cfg.agent_prompts.compaction
        if prompts_cfg is None:
            return False
        # Get current message count from session memory.
        # v2 path: build the same body that the compact LLM call will see
        # (via session.agent.write_memory_to_messages → _chat_messages_to_backend_messages)
        # so the token count is consistent with what the LLM will be sent.
        try:
            from ..compact.threshold import compute_threshold, should_compact
            from ..compact.tokenizer import TokenCounter
            from ..llm_service import resolve_backend_context_length

            chat_messages = list(session.agent.write_memory_to_messages())
            messages = _chat_messages_to_backend_messages(chat_messages)
            backend = self.llm_service.active_backend or "sglang"
            max_ctx = resolve_backend_context_length(cfg, backend) or 0
            if max_ctx <= 0:
                return False
            threshold = compute_threshold(cfg.compact, max_ctx)
            tokens = TokenCounter.get().count_messages(messages)
            return should_compact(tokens, threshold)
        except Exception as exc:  # defensive: never block main run on compact check
            logger.debug(f"auto-compact check skipped: {exc}")
            return False

    def _replace_agent_memory(self, session, summary_text: str, summary_id: str) -> None:
        """Snapshot old memory.steps into session.compaction_archive, then replace."""
        from smolagents.memory import TaskStep

        import time as _time
        cfg = self.config
        prompts_cfg = cfg.agent_prompts.compaction
        envelope = (
            prompts_cfg.summary_envelope_preface
            + summary_text
            + prompts_cfg.summary_envelope_suffix
        )
        session.compaction_archive.append({
            "summary_id": summary_id,
            "timestamp": _time.time(),
            "archived_step_count": len(session.agent.memory.steps),
            "summary_text": summary_text,
        })
        session.agent.memory.steps = [TaskStep(task=envelope, task_images=None)]
        # CRITICAL: keep has_run=True. The envelope IS the result of
        # the just-completed compact, so semantically the agent has
        # "run" (and produced this state). If we set has_run=False
        # here, the next _run_basic_stream call will set
        # reset=True and smolagents will call self.memory.reset(),
        # which wipes the very envelope we just wrote. The user
        # would then see the next agent turn respond with "I don't
        # remember" — exactly the post-/compact regression that
        # motivated this fix.
        #
        # The re-route (so the next user message goes through
        # coordinator_1) is achieved separately below by
        # current_agent_name + active_agent reset, NOT by has_run.
        session.has_run = True
        session.current_agent_name = cfg.agent.entry_agent_name
        session.active_agent = None
        session.last_compactable_llm_messages = None
        session.last_compactable_llm_tools = None
        session.last_compactable_memory_steps = None
        session.last_compactable_agent_name = None

    def _build_compact_body_messages(
        self, session, prompts_cfg
    ) -> list:
        """Build the body_messages list for an agent-mode compact call.

        Why we override body_messages[0] (Bug 2 fix, 2026-06-12):

          `session.agent.write_memory_to_messages()` returns a list whose
          first element is the **active agent's** system prompt (e.g.
          `coordinator_1`'s "你是极简路由代理，只输出切换 marker/control command"
          or `basic_agent`'s heavy tool-calling instructions). When the
          compact LLM call sends that as the system message, the LLM
          receives a **competing** instruction: the system prompt says
          "you are a router / tool caller" while the user-position-1
          compact instruction says "respond with <analysis> + <summary>".
          The system prompt wins (system > user, especially on a 40k+
          context with lost-in-the-middle), and the LLM emits a routing
          JSON or a tool-call format instead of the requested summary.
          The compact card then displays ~167 chars of routing XML
          instead of the actual 9-section summary.

          Fix: replace `body_messages[0]` with a **compact-specific**
          system prompt built from `compaction.no_tools_preamble +
          compaction.main_prompt + compaction.no_tools_trailer`. This
          is a dedicated summarizer system prompt that does NOT
          mention routing or tool calling. The v2 builder then appends
          the compact instruction at position 1, and the rest of the
          agent history (body_messages[1:]) follows unchanged.

        Trade-off (intentional): this reliable fallback shape diverges from
        the active agent prompt at token 0, so SGLang cannot reuse the prior
        normal request's longest prefix for this attempt. It remains the
        correctness fallback because active routing/tool prompts have been
        observed to produce routing XML/tool-call text instead of summaries.
        Cache-first experiments must use _build_cache_first_compact_body_messages()
        plus validation and fallback rather than weakening this reliable path.

        Args:
          session: AgentSession whose agent memory we're summarizing.
          prompts_cfg: CompactionPromptsConfig from
            config.agent_prompts.compaction.

        Returns:
          list[Message]: body_messages with [0] overridden to the
            compact system prompt; [1:] byte-equal to the agent's
            previous chat messages.
        """
        from ..llm_service import Message

        native = getattr(session, "tool_protocol", "legacy_text") == "qwen35-native"
        chat_messages = list(session.agent.write_memory_to_messages(
            computer_use_history_stage=None, consume_tool_images=False, committed_results_only=True)
            if native else session.agent.write_memory_to_messages())
        body_messages = (self._prepare_native_compact_messages(session, chat_messages)
                         if native else _chat_messages_to_backend_messages(chat_messages))

        # Build the compact-specific system prompt. This is a
        # summarizer persona, not a tool-calling / routing persona.
        # Concatenation order matches ref_src/services/compact/prompt.ts
        # BASE_COMPACT_PROMPT (preamble + main_prompt + trailer).
        compact_system = (
            (prompts_cfg.no_tools_preamble or "").rstrip()
            + "\n\n"
            + (prompts_cfg.main_prompt or "").rstrip()
            + "\n\n"
            + (prompts_cfg.no_tools_trailer or "").rstrip()
        )

        if not body_messages:
            # Defensive: if the agent has no history at all (empty
            # memory.steps AND no system prompt), we still need a
            # non-empty body_messages[0] for the v2 builder's
            # `body_messages[0].role == "system"` invariant.
            return [Message(role="system", content=compact_system)]

        # Replace the active-agent system prompt (body_messages[0])
        # with the compact-specific one. The v2 builder will append
        # the user-position-1 compact instruction between this and
        # body_messages[1] (the agent's history).
        body_messages = list(body_messages)
        body_messages[0] = Message(role="system", content=compact_system)
        return body_messages

    def _prepare_native_compact_messages(self, session, messages):
        """Encode only new compact source messages, before copying PIL provenance."""
        from ..llm_service import prepare_multimodal_messages

        model = self._get_runtime_agent(session).model
        prepared, protected_urls = model._prepare_model_messages(messages)
        service_config = getattr(model.llm_service, "config", None)
        max_dimension = getattr(getattr(service_config, "model", None), "max_image_dimension", None)
        if not isinstance(max_dimension, int):
            max_dimension = self.config.model.max_image_dimension
        return prepare_multimodal_messages(prepared, max_image_dimension=max_dimension,
                                           preserve_image_urls=protected_urls)

    def _native_compact_tools(self, session):
        if getattr(session, "tool_protocol", "legacy_text") != "qwen35-native":
            return None
        if session.last_compactable_llm_messages:
            return deepcopy(getattr(session, "last_compactable_llm_tools", None))
        agent = self._get_runtime_agent(session)
        return agent.model._request_tools(agent.tools_and_managed_agents)

    def _append_native_compact_committed_tail(self, session, body):
        """Append committed memory after an accepted native response, never edit it.

        Anchors are step identities from before response acceptance. Profile
        changes share those steps; compact replacement clears the anchor.
        Missing results or divergent anchored memory fail closed.
        """
        anchor = getattr(session, "last_compactable_memory_steps", None)
        expected = body[-1]
        if anchor is None:
            if expected.tool_calls:
                raise NativeCompactSourceError("Native compact source has calls without a committed memory anchor")
            return body  # Older/unanchored plain snapshots retain their behavior.
        agent = self._get_runtime_agent(session)
        steps = agent.memory.steps
        if len(steps) < len(anchor) or any(left is not right for left, right in zip(anchor, steps)):
            raise NativeCompactSourceError("Native compact committed memory anchor changed")
        # A shallow view shares canonical steps/images but never mutates them,
        # consumes IPC attachments, or projects pending screenshot omissions.
        view = copy(agent)
        view.memory = copy(agent.memory)
        view.memory.steps = list(steps[len(anchor):])
        tail = self._prepare_native_compact_messages(session, view.write_memory_to_messages(
            computer_use_history_stage=None, consume_tool_images=False, committed_results_only=True))
        tail = [message for message in tail if message.role != "system"]
        index = None
        for candidate_index, message in enumerate(tail):
            if message.role != "assistant":
                continue
            if expected.tool_calls:
                match = message.tool_calls == expected.tool_calls
            else:
                match = (not message.tool_calls and message.content == expected.content
                         and message.reasoning_content == expected.reasoning_content)
            if match:
                index = candidate_index
                break
        if index is None:
            raise NativeCompactSourceError("Native compact accepted response has no committed memory action")
        suffix = tail[index + 1:]
        pending = {call["id"] for call in (expected.tool_calls or ())}
        for message in suffix:
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise NativeCompactSourceError("Native compact tail has a result without an accepted call")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise NativeCompactSourceError("Native compact accepted call has no committed result")
                for call in message.tool_calls or ():
                    pending.add(call["id"])
        if pending:
            raise NativeCompactSourceError("Native compact accepted call has no committed result")
        return body + suffix

    def _build_cache_first_compact_body_messages(self, session) -> list | None:
        from ..llm_service import Message

        if not self.config.compact.prompt_control_enabled:
            return None
        prompts_cfg = self.config.agent_prompts.compaction
        if prompts_cfg is None:
            return None
        if session.last_compactable_llm_messages:
            body = canonicalize_model_messages(session.last_compactable_llm_messages)
            if getattr(session, "tool_protocol", "legacy_text") == "qwen35-native":
                body = self._append_native_compact_committed_tail(session, body)
            logger.info(
                f"[compact-cache-first-source] origin=last_compactable_llm_messages "
                f"agent_name={session.last_compactable_agent_name} messages={len(body)}"
            )
            return body
        native = getattr(session, "tool_protocol", "legacy_text") == "qwen35-native"
        chat_messages = list(session.agent.write_memory_to_messages(
            computer_use_history_stage=None, consume_tool_images=False, committed_results_only=True)
            if native else session.agent.write_memory_to_messages())
        body_messages = (self._prepare_native_compact_messages(session, chat_messages)
                         if native else _chat_messages_to_backend_messages(chat_messages))
        if not body_messages or body_messages[0].role != "system":
            return None
        if session.runtime_context:
            body_messages = list(body_messages)
            body_messages[0] = Message(
                role="system",
                content=f"{body_messages[0].content}\n\n{session.runtime_context}",
            )
        logger.info(
            f"[compact-cache-first-source] origin=reconstructed_memory "
            f"agent_name={session.current_agent_name} messages={len(body_messages)}"
        )
        return body_messages

    async def _run_compact_then_continue(
        self,
        session,
        trigger: str,
        next_turn_user_message: Any | None = None,
    ):
        """Run auto-compact; yield SSE timeline_event lines for compact_* events.

        v2 path: build body_messages from session.agent.write_memory_to_messages()
        (turn-shape, byte-equal to previous chat calls' messages) and pass to
        CompactRequest.body_messages. This maximizes SGLang radix cache reuse
        (spec §1.3, §4.3).

        On completion, replaces session memory.
        """
        from ..compact.custom_focus import build_next_turn_focus_instruction
        from ..compact.types import CompactRequest, CompactEventKind

        prompts_cfg = self.config.agent_prompts.compaction
        # v2 path: reuse the exact same message list that the prior chat calls
        # sent to the LLM, BUT override body_messages[0] (the active agent's
        # system prompt) with a compact-specific summarizer system prompt.
        # Without this override, the active agent's routing/tool-calling
        # system prompt dominates the compact instruction at user position 1
        # and the LLM emits routing XML / tool calls instead of a 9-section
        # <summary> (Bug 2, 2026-06-12). See _build_compact_body_messages
        # for the full rationale.
        body_messages = self._build_compact_body_messages(session, prompts_cfg)
        try:
            cache_first_body_messages = self._build_cache_first_compact_body_messages(session)
        except NativeCompactSourceError as exc:
            from ..compact.types import CompactEvent
            yield self._compact_event_to_sse(CompactEvent.failed(
                CompactEvent.new_summary_id(), "source_reconciliation_failed", str(exc)))
            return
        req = CompactRequest(
            mode="agent",
            session_id=session.session_id,
            body_messages=body_messages,
            cache_first_body_messages=cache_first_body_messages,
            reference_messages=cache_first_body_messages,
            tools=self._native_compact_tools(session),
            trigger=trigger,
            custom_instructions=build_next_turn_focus_instruction(next_turn_user_message),
        )

        def _apply(req, summary_id, text):
            self._replace_agent_memory(session, text, summary_id)

        # Build a transient orchestrator with the per-call apply hook
        from ..compact.orchestrator import CompactOrchestrator
        transient = CompactOrchestrator(
            llm_service=self.llm_service,
            config=self.config.compact,
            prompts=prompts_cfg,
            apply_compaction=_apply,
        )
        async for ev in transient.run_compact(req):
            yield self._compact_event_to_sse(ev)
            if ev.kind == CompactEventKind.FAILED:
                # Skip on failure; main run continues
                return
            if ev.kind == CompactEventKind.COMPLETED:
                # The apply_compaction callback already ran and replaced memory.steps
                return

    async def run_compact_for_session(
        self,
        session_id: str,
        mode: str | None = None,
        trigger: str = "manual",
        custom_instructions: str | None = None,
    ) -> AsyncGenerator:
        """Run /compact on an existing agent session.

        Public entry point used by /v1/compact/run when the frontend
        is in agent mode (mode='agent' + session_id). Yields
        CompactEvents; on COMPLETED the session's memory.steps has
        been replaced with the envelope step.

        Reuses the same plumbing as the auto-compact path
        (write_memory_to_messages + transient CompactOrchestrator
        with an _apply hook), so the session-memory replacement is
        guaranteed to be the same code path.

        Hard contract (pinned by tests/test_compact_agent_routes_to_runner.py):

        * If the session does not exist, the first event is a
          CompactEvent.failed(reason='no_session'). No silent success.
        * If the compact LLM call fails, the generator yields
          compact_failed and the session memory is NOT modified.
        * If the compact LLM call succeeds, on completion
          session.agent.memory.steps == [TaskStep(envelope)] and
          session.compaction_archive gains one entry.
        """
        from ..compact.types import CompactEvent, CompactEventKind, CompactRequest

        session = None
        compact_scope_id = None
        session_busy = False
        with self._agent_admission_lock:
            session = self.sessions.get(session_id)
            if session is not None:
                session_busy = (
                    session_id in self._pending_agent_admissions
                    or self._is_session_disposal_active(session_id)
                    or session.run_in_progress
                    or session.answering_now
                    or bool(session.capture_active_run_scopes())
                )
                if not session_busy:
                    compact_scope_id = session.begin_run()
        if session is None:
            sid = CompactEvent.new_summary_id()
            yield CompactEvent.failed(
                sid, "no_session",
                f"No agent session found for session_id={session_id!r}",
            )
            return
        if session_busy:
            sid = CompactEvent.new_summary_id()
            yield CompactEvent.failed(
                sid,
                "session_busy",
                f"Agent session {session_id!r} has a pending admission or active run",
            )
            return

        try:
            # Delegate to the same generator used by the auto-compact path.
            # We can't simply call _run_compact_then_continue because it
            # yields SSE-formatted strings (timeline_event wrapper), whereas
            # /v1/compact/run expects raw CompactEvents. So we re-implement
            # the body here, sharing the same primitives.
            prompts_cfg = self.config.agent_prompts.compaction
            if prompts_cfg is None:
                sid = CompactEvent.new_summary_id()
                yield CompactEvent.failed(
                    sid, "no_prompts",
                    "agent_prompts.yaml missing compaction section",
                )
                return

            body_messages = self._build_compact_body_messages(session, prompts_cfg)
            try:
                cache_first_body_messages = self._build_cache_first_compact_body_messages(session)
            except NativeCompactSourceError as exc:
                yield CompactEvent.failed(CompactEvent.new_summary_id(), "source_reconciliation_failed", str(exc))
                return
            req = CompactRequest(
                mode=mode or "agent",
                session_id=session.session_id,
                # Bug 2 fix (2026-06-12): same override as
                # _run_compact_then_continue — replace the active agent's
                # system prompt with a compact-specific summarizer
                # system prompt so the LLM actually produces a
                # <summary> block instead of routing XML / tool calls.
                body_messages=body_messages,
                cache_first_body_messages=cache_first_body_messages,
                reference_messages=cache_first_body_messages,
                tools=self._native_compact_tools(session),
                trigger=trigger,
                custom_instructions=custom_instructions,
            )

            def _apply(req, summary_id, text):
                self._replace_agent_memory(session, text, summary_id)

            from ..compact.orchestrator import CompactOrchestrator
            transient = CompactOrchestrator(
                llm_service=self.llm_service,
                config=self.config.compact,
                prompts=prompts_cfg,
                apply_compaction=_apply,
            )
            async for ev in transient.run_compact(req):
                yield ev
                if ev.kind == CompactEventKind.FAILED:
                    return
                if ev.kind == CompactEventKind.COMPLETED:
                    return
        finally:
            session.finish_run(compact_scope_id)

    @staticmethod
    def _compact_event_to_sse(ev) -> str:
        """Convert a CompactEvent to an SSE timeline_event line for the agent endpoint."""
        import json as _json
        kind = ev.kind.value if hasattr(ev.kind, "value") else str(ev.kind)
        event = {
            "kind": kind,
            "source": "compactor",
            "summary_message_id": ev.summary_message_id,
            "text": ev.text,
            "partial": ev.partial,
            "metadata": ev.metadata,
        }
        return f"data: {_json.dumps({'type': 'timeline_event', 'event': event}, ensure_ascii=False)}\n\n"

    def _omit_historical_tool_feedback(self, session) -> None:
        """Explicit prefix boundary owned only by accepted ordinary user runs."""
        count = commit_historical_tool_feedback(
            session, getattr(self.config.agent, "tool_feedback_omission", None)
        )
        if count:
            logger.info("[prefix-boundary] reason=%s session_id=%s omitted_results=%s",
                        TOOL_FEEDBACK_OMISSION_REASON, session.session_id, count)

    def _local_memory_context_validator(self, session, pil_images, enable_thinking):
        """Freeze an actual prompt/template budget without altering canonical memory.

        The initialized inference tokenizer must count tools, runtime context,
        routing wrappers and generation markers. Missing template/budget data
        fails closed to the untouched user text. Images additionally reserve
        their configured upper bound, rather than the template's image marker.
        """
        from ..compact.tokenizer import TokenCounter, _extract_token_count
        from ..llm_service import (
            _inject_runtime_context, resolve_backend_context_length,
            resolve_effective_max_tokens,
        )
        from .canonical_messages import message_to_template

        try:
            counter = TokenCounter.get()
            if not counter._use_real_tokenizer or counter._tokenizer is None:
                raise ValueError("tokenizer_unavailable")
            tokenizer = counter._tokenizer
            backend = self.llm_service.active_backend
            if backend not in {"sglang", "vllm"}:
                raise ValueError("provider_template_unavailable")
            # Serving may override the model tokenizer's default Jinja. Pin
            # the configured provider file once, without changing tokenizer
            # state or any model-visible prompt. An unverifiable override must
            # never silently fall back to a different template for admission.
            if backend not in self._local_memory_template_snapshots:
                configured_template = getattr(getattr(self.config, backend), "chat_template", None)
                self._local_memory_template_snapshots[backend] = (False, None)
                template_text = None
                if configured_template:
                    from ..config import PROJECT_ROOT
                    template_path = Path(configured_template).expanduser()
                    if not template_path.is_absolute():
                        template_path = PROJECT_ROOT / template_path
                    fd = os.open(template_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                    with os.fdopen(fd, "r", encoding="utf-8") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ValueError("provider_template_not_regular")
                        template_text = source.read(1024 * 1024 + 1)
                    if not template_text.strip() or len(template_text) > 1024 * 1024:
                        raise ValueError("provider_template_invalid")
                self._local_memory_template_snapshots[backend] = (True, template_text)
            template_valid, template_text = self._local_memory_template_snapshots[backend]
            if not template_valid:
                raise ValueError("provider_template_unavailable")
            limit = resolve_backend_context_length(self.config, backend)
            routing = self._entry_uses_coordinator_routing(session)
            reserve = resolve_effective_max_tokens(self.config, backend)
            if routing and isinstance(reserve, int):
                reserve = max(reserve, resolve_effective_max_tokens(self.config, backend,
                    self.config.agent.coordinator_1_routing_max_tokens) or 0)
            if not isinstance(limit, int) or limit <= 0 or not isinstance(reserve, int) or reserve <= 0:
                raise ValueError("context_budget_unavailable")
            agent = self._get_runtime_agent(session)
            model = agent.model
            if not isinstance(model, LocalLLMModel):
                raise ValueError("model_serializer_unavailable")
            messages = list(agent.write_memory_to_messages(
                computer_use_history_stage=None, consume_tool_images=False,
                committed_results_only=True))
            marker = "__local_memory_candidate_" + uuid.uuid4().hex + "__"
            if routing:
                content = [{"type": "image", "image": image} for image in pil_images or ()]
                content.append({"type": "text", "text": self._build_coordinator_turn_message(
                    marker, "coordinator_1", session=session)})
                messages.append(ChatMessage(role=MessageRole.USER, content=content))
            else:
                # Ordinary agent.run() serializes TaskStep, including its
                # "New task:" envelope. Budget that same detached step rather
                # than a bare user message; never append it to live history.
                messages.extend(TaskStep(task=marker, task_images=pil_images).to_messages())
            prepared, _ = model._prepare_model_messages(messages)
            prepared = _inject_runtime_context(prepared, model.runtime_context)
            template_messages = [message_to_template(message) for message in prepared]
            tools = model._request_tools(agent.tools_and_managed_agents
                if not routing or model.native_tool_protocol else [])
            if not routing and not model.native_tool_protocol and agent.stream_outputs:
                tools = None
            extra = (deepcopy(self.config.agent.coordinator_1_routing_extra_body) if routing else
                     ({"chat_template_kwargs": {"enable_thinking": enable_thinking}}
                      if enable_thinking is not None else {}))
            extra = model._raw_tool_capture_extra_body(tools, extra) or {}
            template_kwargs = deepcopy(extra.get("chat_template_kwargs") or {})
            image_count = sum(1 for message in template_messages
                for part in (message["content"] if isinstance(message["content"], list) else ())
                if isinstance(part, dict) and part.get("type") in {"image", "image_url"})
            image_bound = self.config.model.max_image_tokens
            if image_count and (not isinstance(image_bound, int) or image_bound <= 0):
                raise ValueError("image_budget_unavailable")
            image_reserve = image_count * (image_bound or 0)
            prompt_shapes = [(template_messages, tools, template_kwargs, image_reserve, 0)]

            # A short router prefix cannot certify the larger target system and
            # tool schema. Freeze every reachable target shape before retrieval
            # accepts a tail. The detached build reuses the production renderer,
            # but binds only copied local wrappers to an unregistered shell with
            # no execution capability. Never initialize MCP clients to count.
            source = session.current_agent_name
            paths = []
            if routing:
                targets = sorted(getattr(agent, "_allowed_switch_targets", ())
                    or self.config.agent.enabled_agent_profiles)
                paths = [(target,) for target in targets]
                if "coordinator_2" in targets and "basic_agent" in self.config.agent.enabled_agent_profiles:
                    paths.append(("coordinator_2", "basic_agent"))
            elif source == "coordinator_2" and "basic_agent" in self.config.agent.enabled_agent_profiles:
                paths = [("basic_agent",)]
            if paths and getattr(getattr(self.config, "mcp", None), "servers", ()):
                raise ValueError("routed_mcp_schema_unavailable")

            from smolagents.memory import ToolCall as MemoryToolCall
            normal_output = resolve_effective_max_tokens(self.config, backend)
            routing_output = resolve_effective_max_tokens(self.config, backend,
                self.config.agent.coordinator_1_routing_max_tokens) or 0
            for path in paths:
                detached = AgentSession(session_id=session.session_id,
                    agent=SimpleNamespace(memory=SimpleNamespace(steps=[])), mode=session.mode,
                    capability=None, tool_configuration_snapshot=session.tool_configuration_snapshot,
                    tool_protocol=session.tool_protocol, current_agent_name=source,
                    runtime_context=session.runtime_context,
                    max_agent_switches_per_turn=session.max_agent_switches_per_turn)
                projected_steps = [copy(step) for step in agent.memory.steps]
                generated_reserve = 0
                for index, target in enumerate(path):
                    is_initial_route = routing and index == 0
                    native = session.tool_protocol == "qwen35-native"
                    if is_initial_route:
                        if native:
                            task = TaskStep(task=self._build_coordinator_turn_message(
                                marker, "coordinator_1", session=session), task_images=pil_images)
                            task.canonical_routing_tail = True
                            projected_steps.append(task)
                        generated_reserve += routing_output
                    else:
                        # coordinator_2 retains its admitted task before the
                        # target receives the same fallback task. Model-created
                        # handoff arguments may also contain a new task: reserve
                        # the complete output twice for that possible replay.
                        projected_steps.append(TaskStep(task=marker, task_images=pil_images))
                        generated_reserve += 2 * normal_output
                    if native or not is_initial_route:
                        call_name = "switch_agent" if native else "basic_agent"
                        call_args = {"target_agent": target} if native else {}
                        call_id = "local-memory-budget-call"
                        action = ActionStep(step_number=1,
                            timing=Timing(start_time=0.0, end_time=0.0), model_output="",
                            tool_calls=[MemoryToolCall(name=call_name, arguments=call_args, id=call_id)])
                        if native:
                            receipt = ("Routing" if is_initial_route else "Handoff")
                            action.canonical_tool_results = [CanonicalToolResult(call_id,
                                call_name, f"{receipt} request accepted: target_agent={target}")]
                        projected_steps.append(action)
                    # ALL_TOOLS contains only import-time local wrappers. Copies
                    # prevent _bind_tool_runtime_context touching shared tools;
                    # production discovery remains the default builder path.
                    target_agent = self._build_profile_agent(target, session=detached,
                        runtime_context=detached.runtime_context,
                        _available_tools_override=[copy(tool) for tool in ALL_TOOLS])
                    detached.agent = detached.active_agent = target_agent
                    detached.current_agent_name = target
                    target_agent.memory.steps = list(projected_steps)
                target_model = target_agent.model
                target_messages = list(target_agent.write_memory_to_messages(
                    computer_use_history_stage=None, consume_tool_images=False, committed_results_only=True))
                target_messages.extend(TaskStep(task=marker, task_images=pil_images).to_messages())
                target_prepared, _ = target_model._prepare_model_messages(target_messages)
                target_prepared = _inject_runtime_context(target_prepared, target_model.runtime_context)
                target_body = [message_to_template(message) for message in target_prepared]
                target_tools = target_model._request_tools(target_agent.tools_and_managed_agents)
                if not target_model.native_tool_protocol and target_agent.stream_outputs:
                    target_tools = None
                target_extra = ({"chat_template_kwargs": {"enable_thinking": enable_thinking}}
                    if enable_thinking is not None else {})
                target_extra = target_model._raw_tool_capture_extra_body(target_tools, target_extra) or {}
                target_kwargs = deepcopy(target_extra.get("chat_template_kwargs") or {})
                target_image_count = sum(1 for message in target_body for part in (
                    message["content"] if isinstance(message["content"], list) else ())
                    if isinstance(part, dict) and part.get("type") in {"image", "image_url"})
                # A previously uninitialized profile runtime clock can cross a
                # minute before handoff. Its entire UTF-8 length is an additional
                # conservative bound; the actual system itself stays untouched.
                runtime_reserve = (len((target_model.runtime_context or "").encode("utf-8"))
                    if session.runtime_context is None else 0)
                prompt_shapes.append((target_body, target_tools, target_kwargs,
                    target_image_count * (image_bound or 0), generated_reserve + runtime_reserve))
        except Exception as exc:
            logger.info("Local memory skipped: context_budget_unavailable (%s: %s)", type(exc).__name__, str(exc)[:120])
            return lambda _candidate: False

        def fits(candidate):
            try:
                for body, shape_tools, shape_kwargs, shape_images, generated in prompt_shapes:
                    frozen = deepcopy(body)
                    for message in frozen:
                        content = message["content"]
                        if isinstance(content, str):
                            message["content"] = content.replace(marker, candidate)
                        else:
                            for part in content:
                                if part.get("type") == "text":
                                    part["text"] = part["text"].replace(marker, candidate)
                    tokens = tokenizer.apply_chat_template(frozen, tools=shape_tools,
                        tokenize=True, add_generation_prompt=True, return_tensors=None,
                        return_dict=False,
                        **({"chat_template": template_text} if template_text is not None else {}),
                        **shape_kwargs)
                    if _extract_token_count(tokens) + shape_images + generated + reserve > limit:
                        return False
                return True
            except Exception:
                logger.info("Local memory skipped: context_template_unavailable")
                return False
        return fits

    async def _prepare_local_memory_turn(self, session, run_scope_id, message,
                                         pil_images, enable_thinking, enabled):
        from ..retrieval.contracts import PreparedUserTurn
        service = getattr(self, "local_memory_service", None)
        if service is None:
            return PreparedUserTurn(message, message, "unavailable")
        # The closure is only a read barrier. Retrieval owns no session writes,
        # and a late result must pass this barrier again before model dispatch.
        aborted = lambda: session.check_aborted() or run_abort_manager.is_aborted(run_scope_id)
        if aborted():
            return PreparedUserTurn(message, message, "aborted")
        return await service.prepare_user_turn(message, enabled=enabled,
            is_aborted=aborted,
            context_validator=(self._local_memory_context_validator(session, pil_images, enable_thinking)
                               if enabled else None))

    @staticmethod
    def _local_memory_retrieved_event(session, prepared):
        """UI-only frozen chunk text; never reconstruct it from a prompt/history."""
        texts = getattr(prepared, "memory_texts", ())
        if getattr(prepared, "status", None) != "matched" or not texts:
            return None
        return {"kind": "local_memory_retrieved", "session_id": session.session_id,
            "turn_index": session.event_hub_turn_index,
            "chunks": [{"text": text} for text in texts]}

    async def run_stream(
        self,
        session_id: str | None,
        message: str,
        mode: str | None = None,
        images: list[str] | None = None,
        context_messages: list[Any] | None = None,
        enable_thinking: bool | None = None,
        enable_local_memory: bool = False,
        admission_stage: AgentRequestAdmissionStage | None = None,
        on_run_registered: Callable[
            [AgentRequestAdmissionStage, frozenset[str]], None
        ] | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        执行 Agent 并以 SSE 事件流返回步骤和结果。

        SSE 事件格式：
        - {"type": "agent_step", "step_type": "thought", "content": "..."}
        - {"type": "agent_step", "step_type": "tool_call", "name": "...", "input": {...}}
        - {"type": "agent_step", "step_type": "observation", "content": "..."}
        - {"type": "content", "content": "..."}
        - {"type": "done"}
        - {"type": "error", "error": "..."}
        - {"type": "timeline_event", "event": {...}}  (includes compact_*)
        """
        if admission_stage is not None:
            if session_id is not None and session_id != admission_stage.session_id:
                raise RuntimeError("Agent admission session mismatch")
            session, run_scope_id = self._begin_staged_agent_request(
                admission_stage, mode, on_run_registered
            )
        else:
            session, run_scope_id = self._get_or_create_session_for_run(
                session_id, mode
            )
        session_id = session.session_id
        try:
            self._omit_historical_tool_feedback(session)
            self._bootstrap_session_from_context_messages(
                session,
                context_messages,
                current_message=message,
            )

            # Auto-compact runs only after staged admission is accepted and
            # remains under the outer run-scope cleanup owner.
            if self._should_auto_compact_session(session):
                async for ev in self._run_compact_then_continue(
                    session,
                    trigger="auto",
                    next_turn_user_message=message,
                ):
                    yield ev

            logger.info(
                f"[CYCLE-DIAG] run_stream called: input_session_id={session_id}, "
                f"has_images={images is not None and len(images) if images else 0}, "
                f"session.has_run={session.has_run}, "
                f"agent_memory_steps={len(session.agent.memory.steps)}"
            )

            pil_images = self._decode_request_images(images)
            yield f"data: {json.dumps({'type': 'session_start', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"

            prepared_turn = await self._prepare_local_memory_turn(
                session, run_scope_id, message, pil_images, enable_thinking, enable_local_memory)
            model_message = prepared_turn.model_user_content
            if session.check_aborted() or run_abort_manager.is_aborted(run_scope_id):
                yield f"data: {json.dumps({'type': 'aborted', 'session_id': session.session_id})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id})}\n\n"
                return

            memory_event = (self._local_memory_retrieved_event(session, prepared_turn)
                if enable_local_memory else None)
            if memory_event is not None:
                self._append_timeline_event(session, memory_event)
                yield f"data: {json.dumps({'type': 'timeline_event', 'event': memory_event}, ensure_ascii=False)}\n\n"

            if not self._entry_uses_coordinator_routing(session):
                async for event in self._stream_active_agent(
                    session, model_message, pil_images, enable_thinking
                ):
                    if event.startswith("data: "):
                        try:
                            payload = json.loads(event[6:])
                        except json.JSONDecodeError:
                            yield event
                            continue
                        if payload.get("type") == "session_start":
                            continue
                    yield event
                return

            route = None
            coordinator_emitted_aborted_dispatch = False
            coordinator_emitted_routing_delta = False
            coordinator_emitted_routing_dispatch = False
            async for coordinator_event in self._run_coordinator_routing_stream(
                session, model_message, pil_images
            ):
                if coordinator_event.get("kind") == "timeline":
                    if coordinator_event.get("event", {}).get("kind") == "routing_delta":
                        coordinator_emitted_routing_delta = True
                    yield f"data: {json.dumps({'type': 'timeline_event', 'event': coordinator_event['event']}, ensure_ascii=False)}\n\n"
                    continue

                if coordinator_event.get("kind") == "dispatch":
                    if (
                        coordinator_event.get("dispatch_status") == "running"
                        and coordinator_event.get("dispatch_stage") == "routing"
                    ):
                        coordinator_emitted_routing_dispatch = True
                    if coordinator_event.get("dispatch_status") == "aborted":
                        coordinator_emitted_aborted_dispatch = True
                    dispatch_event = self._make_dispatch_step(
                        dispatch_status=coordinator_event["dispatch_status"],
                        dispatch_stage=coordinator_event["dispatch_stage"],
                        dispatch_target_agent=coordinator_event.get("dispatch_target_agent"),
                        dispatch_message=coordinator_event.get("dispatch_message"),
                        dispatch_kind=coordinator_event.get("dispatch_kind"),
                        display_slot=coordinator_event.get("display_slot"),
                        dispatch_id=coordinator_event.get("dispatch_id"),
                    )
                    yield f"data: {json.dumps(dispatch_event, ensure_ascii=False)}\n\n"
                    continue

                route = coordinator_event["route"]
                break

            if route is None:
                route = {"kind": "invalid", "error": "Coordinator produced no route result"}

            if (
                route.get("kind") in {"switch", "final", "invalid"}
                and self._entry_uses_coordinator_routing(session)
                and not coordinator_emitted_routing_delta
                and not coordinator_emitted_routing_dispatch
            ):
                routing_message_id = f"routing-card-{session.session_id}"
                routing_text = self._default_routing_dispatch_message()
                timeline_event = {
                    "kind": "routing_delta",
                    "source": "coordinator_1",
                    "message_id": routing_message_id,
                    "text": routing_text,
                    "partial": True,
                }
                self._append_timeline_event(session, timeline_event)
                yield f"data: {json.dumps({'type': 'timeline_event', 'event': timeline_event}, ensure_ascii=False)}\n\n"
                dispatch_timeline_event = self._make_dispatch_timeline_event(
                    source="coordinator_1",
                    message_id=routing_message_id,
                    dispatch_id=f"dispatch-{session.session_id}-routing",
                    dispatch_kind="routing",
                    display_slot="before_body",
                    text=routing_text,
                    partial=True,
                )
                self._append_timeline_event(session, dispatch_timeline_event["event"])
                yield f"data: {json.dumps({'type': 'timeline_event', 'event': dispatch_timeline_event['event']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps(self._make_dispatch_step('running', 'routing', dispatch_message=routing_text, dispatch_kind='routing', display_slot='before_body', dispatch_id=f'dispatch-{session.session_id}-routing'), ensure_ascii=False)}\n\n"

            if route["kind"] == "final":
                self._commit_direct_answer_to_shared_memory(
                    session, model_message, route["answer"], pil_images,
                    **({"native_turn": route["native_turn"]} if "native_turn" in route else {}),
                )
                yield f"data: {json.dumps(self._make_dispatch_step('completed', 'direct_answer', dispatch_kind='routing', display_slot='before_body', dispatch_id=f'dispatch-{session.session_id}-routing'), ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'content', 'content': route['answer']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"
                return

            if route["kind"] == "aborted":
                if not coordinator_emitted_aborted_dispatch:
                    yield f"data: {json.dumps(self._make_dispatch_step('aborted', 'routing', dispatch_message='任务调度已中止', dispatch_kind='routing', display_slot='before_body', dispatch_id=f'dispatch-{session.session_id}-routing'), ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'aborted', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"
                return

            if route["kind"] == "passthrough":
                stream = self._stream_active_agent(
                    session, model_message, pil_images, enable_thinking
                )
                async for event in stream:
                    if event.startswith("data: "):
                        try:
                            payload = json.loads(event[6:])
                        except json.JSONDecodeError:
                            yield event
                            continue
                        if payload.get("type") == "session_start":
                            continue
                    yield event
                return

            if route["kind"] != "switch":
                yield f"data: {json.dumps(self._make_dispatch_step('error', 'routing', dispatch_message=route.get('error', '任务调度失败'), dispatch_kind='routing', display_slot='before_body', dispatch_id=f'dispatch-{session.session_id}-routing'), ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'error', 'error': route['error']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session.session_id}, ensure_ascii=False)}\n\n"
                return

            prior_agent_name = "coordinator_1"
            self._activate_profile_agent(
                session,
                route["target_agent"],
                user_goal=message,
            )
            timeline_event = {
                "kind": "agent_switched",
                "source": prior_agent_name,
                "target_agent": route["target_agent"],
                **({"reason": route["reason"]} if route.get("reason") else {}),
            }
            self._append_timeline_event(session, timeline_event)
            yield f"data: {json.dumps({'type': 'timeline_event', 'event': timeline_event}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(self._make_dispatch_step('completed', 'handoff', dispatch_target_agent=route['target_agent'], dispatch_kind='routing', display_slot='before_body', dispatch_id=f'dispatch-{session.session_id}-routing'), ensure_ascii=False)}\n\n"

            stream = self._stream_active_agent(
                session, model_message, pil_images, enable_thinking
            )
            async for event in stream:
                if event.startswith("data: "):
                    try:
                        payload = json.loads(event[6:])
                    except json.JSONDecodeError:
                        yield event
                        continue
                    if payload.get("type") == "session_start":
                        continue
                yield event
        finally:
            session.finish_run(run_scope_id)

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
                            "content": step.model_output,
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
                            "content": step.observations,
                        },
                    )

    def _queue_timeline_message_event(
        self,
        loop,
        queue,
        session: AgentSession,
        *,
        kind: str,
        source: str,
        message_id: str,
        text: str | None = None,
        partial: bool | None = None,
        display: str | None = None,
        reasoning: bool | None = None,
    ) -> None:
        event = {
            "kind": kind,
            "source": source,
            "message_id": message_id,
        }
        if text is not None:
            event["text"] = text
        if partial is not None:
            event["partial"] = partial
        if display is not None:
            event["display"] = display
        if reasoning is not None:
            event["reasoning"] = reasoning
        self._append_timeline_event(session, event)
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"type": "timeline_event", "event": event},
        )

    async def run_sync(
        self,
        session_id: str | None,
        message: str,
        mode: str | None = None,
        images: list[str] | None = None,
        context_messages: list[Any] | None = None,
        enable_thinking: bool | None = None,
        enable_local_memory: bool = False,
        admission_stage: AgentRequestAdmissionStage | None = None,
        on_run_registered: Callable[
            [AgentRequestAdmissionStage, frozenset[str]], None
        ] | None = None,
    ) -> dict:
        """非流式运行 Agent，返回完整结果"""
        if admission_stage is not None:
            if session_id is not None and session_id != admission_stage.session_id:
                raise RuntimeError("Agent admission session mismatch")
            session, run_scope_id = self._begin_staged_agent_request(
                admission_stage, mode, on_run_registered
            )
        else:
            session, run_scope_id = self._get_or_create_session_for_run(
                session_id, mode
            )
        try:
            self._omit_historical_tool_feedback(session)
            self._bootstrap_session_from_context_messages(
                session,
                context_messages,
                current_message=message,
            )
            pil_images = self._decode_request_images(images)
            prepared_turn = await self._prepare_local_memory_turn(
                session, run_scope_id, message, pil_images, enable_thinking, enable_local_memory)
            model_message = prepared_turn.model_user_content
            if session.check_aborted() or run_abort_manager.is_aborted(run_scope_id):
                return {"session_id": session.session_id, "answer": "", "steps": [], "aborted": True}
            memory_event = (self._local_memory_retrieved_event(session, prepared_turn)
                if enable_local_memory else None)
            if memory_event is not None:
                self._append_timeline_event(session, memory_event)

            def complete(result):
                if memory_event is None:
                    return result
                return {**result, "local_memory": {
                    key: value for key, value in memory_event.items() if key != "kind"}}

            if not self._entry_uses_coordinator_routing(session):
                return complete(await self._sync_active_agent(
                    session, model_message, pil_images, enable_thinking
                ))

            route = self._run_coordinator_routing_pass(session, model_message, pil_images)

            if route["kind"] == "final":
                self._commit_direct_answer_to_shared_memory(
                    session, model_message, route["answer"], pil_images,
                    **({"native_turn": route["native_turn"]} if "native_turn" in route else {}),
                )
                return complete({
                    "session_id": session.session_id,
                    "answer": route["answer"],
                    "steps": [],
                })

            if route["kind"] == "passthrough":
                return complete(await self._sync_active_agent(
                    session, model_message, pil_images, enable_thinking
                ))

            if route["kind"] != "switch":
                return complete({
                    "session_id": session.session_id,
                    "answer": route["error"],
                    "steps": [],
                })

            prior_agent_name = "coordinator_1"
            self._activate_profile_agent(
                session,
                route["target_agent"],
                user_goal=message,
            )
            self._append_timeline_event(
                session,
                {
                    "kind": "agent_switched",
                    "source": prior_agent_name,
                    "target_agent": route["target_agent"],
                    **({"reason": route["reason"]} if route.get("reason") else {}),
                },
            )

            return complete(await self._sync_active_agent(
                session, model_message, pil_images, enable_thinking
            ))
        finally:
            session.finish_run(run_scope_id)

    def abort_session(self, session_id: str) -> bool:
        """中止指定会话的 Agent 执行

        Returns:
            bool: 是否成功中止
        """
        session = self.sessions.get(session_id)
        if session:
            session.abort()
            abort_manager.abort(session_id)
            session.abort_active_runs()
            active_child_session_ids: list[str] = []
            captured_child_request_ids: list[str] = []
            with self._child_agent_lock:
                seen_child_session_ids: set[str] = set()
                for child_run in self.child_agent_runs.values():
                    if (
                        child_run.manager_session_id == session_id
                        and child_run.status not in CHILD_TERMINAL_STATUSES
                    ):
                        active_child_session_ids.append(child_run.child_run_id)
                        seen_child_session_ids.add(child_run.child_run_id)
                # Admission publishes the manager -> child mapping before it
                # publishes ChildAgentRun.  A stop can land in that narrow
                # window, so include mapped child shells that are already
                # running or have an active producer/request as well.
                mapped_child_session_ids = {
                    child_session_id
                    for (manager_id, _child_name), child_session_id in (
                        self.child_agent_sessions_by_key.items()
                    )
                    if manager_id == session_id
                }
                for child_session_id in mapped_child_session_ids:
                    if child_session_id in seen_child_session_ids:
                        continue
                    child_session = self.sessions.get(child_session_id)
                    if child_session is None:
                        continue
                    if not (
                        child_session.run_in_progress
                        or child_session.capture_active_run_scopes()
                        or child_session.event_hub.has_active_producer
                        or self.get_current_request_id(child_session_id)
                    ):
                        continue
                    active_child_session_ids.append(child_session_id)
                    seen_child_session_ids.add(child_session_id)
            for child_session_id in active_child_session_ids:
                child_session = self.sessions.get(child_session_id)
                if child_session is None:
                    continue
                child_session.abort()
                abort_manager.abort(child_session_id)
                child_session.abort_active_runs()
                child_request_id = self.get_current_request_id(child_session_id)
                if child_request_id:
                    captured_child_request_ids.append(child_request_id)
            if captured_child_request_ids:
                from ..llm_service import abort_request_by_id

                sglang_url = (
                    f"http://{self.config.sglang.host}:{self.config.sglang.port}"
                )
                for child_request_id in captured_child_request_ids:
                    try:
                        abort_request_by_id(
                            child_request_id,
                            sglang_url,
                            fallback_to_abort_all=False,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Child targeted request abort failed session_id=%s request_id=%s: %s",
                            session_id,
                            child_request_id,
                            exc,
                        )
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
