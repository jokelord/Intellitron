"""Ordinary Chat session metadata registered in the shared Session Hub runtime.

The store keeps raw initialization prefixes and provider-accepted local-memory
user tails separately from Chat turn/display metadata. Only these explicitly
owned model snapshots construct input; the display journal remains transport-only.
"""

from __future__ import annotations

import threading
import time
import uuid
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.agent.session_hub import AgentSessionHub
from backend.session_hub_runtime import (
    SessionHubEntry,
    SessionHubRuntime,
)

# Existing retention policy. Configuration migration is outside Task 2.
DEFAULT_TTL_SECONDS = 6 * 3600
DEFAULT_MAX_SESSIONS = 200


def chat_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in content or ()
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))


def append_chat_memory_suffix(content: Any, suffix: str) -> Any:
    result = deepcopy(content)
    if not suffix:
        return result
    if isinstance(result, str):
        return result + suffix
    for part in reversed(result):
        if isinstance(part, dict) and part.get("type") == "text" and _placeholder_count(part) is None:
            part["text"] += suffix
            return result
    result.append({"type": "text", "text": suffix})
    return result


def _placeholder_count(part):
    text = part.get("text", "") if isinstance(part, dict) and part.get("type") == "text" else ""
    match = re.fullmatch(r"\[historical images omitted: count=([1-9]\d*); sources=(?:none|(?:manual|desktop|camera_[1-9]\d*)(?:,(?:manual|desktop|camera_[1-9]\d*))*)?; relative_range=(?:unavailable|\d+\.\d{2}\.\.\d+\.\d{2}s)\]", text)
    return int(match.group(1)) if match is not None else None


def _image_omission_projection(original: Any, incoming: Any, allow_new: bool):
    if not isinstance(original, list) or not isinstance(incoming, list):
        return None
    incoming_placeholders = [part for part in incoming if _placeholder_count(part) is not None]
    if any(isinstance(part, dict) and part.get("type") in {"image", "image_url"} for part in incoming):
        return None
    original_text = [part for part in original if _placeholder_count(part) is None
        and not (isinstance(part, dict) and part.get("type") in {"image", "image_url"})]
    incoming_text = [part for part in incoming if _placeholder_count(part) is None]
    if original_text != incoming_text:
        return None
    projected, index, placeholder_index = [], 0, 0
    while index < len(original):
        part = original[index]
        if isinstance(part, dict) and part.get("type") in {"image", "image_url"}:
            if not allow_new:
                return None
            end = index
            while end < len(original) and isinstance(original[end], dict) and original[end].get("type") in {"image", "image_url"}:
                end += 1
            count = end - index
            replacement = incoming_placeholders[placeholder_index] if placeholder_index < len(incoming_placeholders) else None
            if _placeholder_count(replacement) != count:
                return None
            projected.append(deepcopy(replacement))
            placeholder_index += 1
            index = end
        elif _placeholder_count(part) is not None:
            replacement = incoming_placeholders[placeholder_index] if placeholder_index < len(incoming_placeholders) else None
            if _placeholder_count(replacement) != _placeholder_count(part):
                return None
            # Display replay can move/simplify its omission notice. The model
            # retains the already accepted exact canonical placeholder instead.
            projected.append(deepcopy(part))
            placeholder_index += 1
            index += 1
        else:
            projected.append(deepcopy(part))
            index += 1
    return projected if placeholder_index == len(incoming_placeholders) and placeholder_index > 0 else None


def _completed_producer_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


@dataclass(frozen=True, slots=True)
class StagedChatDisplayAssets:
    asset_ids: tuple[str, ...]
    added: tuple[tuple[str, str], ...]
    evicted: tuple[tuple[str, str], ...]
    prior_assets: tuple[tuple[str, str], ...]
    evict_prior: bool
    generation: object
    base_generation: object | None


@dataclass
class ChatSessionRecord:
    session_id: str
    hub: AgentSessionHub = field(default_factory=AgentSessionHub)
    entry: SessionHubEntry | None = None
    request_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    finished: bool = True
    finish_reason: Optional[str] = None
    produce_task: Any = None
    producer_request_id: str | None = field(default=None, repr=False)
    producer_turn_index: int | None = field(default=None, repr=False)
    deletion_turn_index: int | None = field(default=None, repr=False)
    turn_index: int = 0
    deletion_started: bool = False
    # Canonical accepted model tails only; the Hub/display journal never feeds
    # this map. Session-qualified references disambiguate repeated user text.
    _local_memory_turns: dict[int, tuple[Any, str]] = field(default_factory=dict, repr=False)
    _retired_memory_turns: set[int] = field(default_factory=set, repr=False)
    _local_memory_compactions: dict[str, tuple[str, frozenset[int], frozenset[str]]] = field(default_factory=dict, repr=False)

    _chat_bootstrap_prefixes: dict[str, list[Any]] = field(default_factory=dict, repr=False)
    _retired_chat_bootstrap_refs: set[str] = field(default_factory=set, repr=False)
    _active_memory_bootstrap_refs: set[str] = field(default_factory=set, repr=False)

    def admit_chat_bootstrap(self, *, request_id, turn_index, prefixes):
        # Raw initialization state follows the visible admitted logical request.
        # It is independent from provider-accepted enrichment/image projection.
        with self._asset_lock:
            if self.request_id != request_id or self.turn_index != turn_index:
                return False
            for anchor, prefix in prefixes.items():
                if anchor in self._retired_chat_bootstrap_refs:
                    return False
                if anchor not in self._chat_bootstrap_prefixes:
                    self._chat_bootstrap_prefixes = {key: value for key, value in self._chat_bootstrap_prefixes.items()
                        if key in self._active_memory_bootstrap_refs}
                    self._chat_bootstrap_prefixes[anchor] = deepcopy(prefix)
            return True

    def local_memory_bootstrap_snapshot(self):
        with self._asset_lock:
            return deepcopy(self._chat_bootstrap_prefixes)

    def bootstrap_retirements(self, messages):
        with self._asset_lock:
            boundaries = dict(self._local_memory_compactions)
        retiring = set()
        for message in messages:
            metadata = getattr(message, "metadata", None) or {}
            boundary = boundaries.get(metadata.get("summary_id")) if metadata.get("compact_summary") else None
            if boundary is not None and message.content == boundary[0]:
                retiring.update(boundary[2])
        return frozenset(retiring)

    def resolve_chat_bootstrap(self, messages, anchor, *, allow_image_omission=False, current_user_is_new=True):
        """Restore only a previously accepted, opaque-anchor-owned initial prefix."""
        resolved = deepcopy(list(messages))
        with self._asset_lock:
            prefixes = deepcopy(self._chat_bootstrap_prefixes)
            retired = set(self._retired_chat_bootstrap_refs)
            active = set(self._active_memory_bootstrap_refs)
        retiring = self.bootstrap_retirements(resolved)
        if anchor in retired or anchor in retiring:
            raise ValueError("retired_chat_bootstrap_ref")
        if not prefixes or set(prefixes) <= retiring:
            return resolved, {}
        if anchor not in prefixes and not (active - retiring):
            return resolved, {}
        if anchor not in prefixes:
            # The explicit registered compact summary is the only retirement boundary.
            raise ValueError("chat_bootstrap_anchor_required")
        prefix = prefixes[anchor]
        ordinary = lambda message: message.role in {"user", "assistant"} and not (
            (getattr(message, "metadata", None) or {}).get("compact_summary") or
            (getattr(message, "metadata", None) or {}).get("pre_compact"))
        historical = resolved[:-1] if current_user_is_new else resolved
        first = next((message for message in historical if ordinary(message)), None)
        metadata = (getattr(first, "metadata", None) or {}) if first is not None else {}
        if metadata.get("chat_message_ref") == anchor:
            if len(historical) < len(prefix):
                raise ValueError("chat_bootstrap_prefix_mismatch")
            projected = deepcopy(prefix)
            for index, expected in enumerate(prefix):
                actual = historical[index]
                # Compare model fields and metadata without relying on Pydantic model class identity.
                fields = ("role", "tool_calls", "tool_call_id", "name", "reasoning_content", "metadata")
                if any(getattr(actual, key, None) != getattr(expected, key, None) for key in fields):
                    raise ValueError("chat_bootstrap_prefix_mismatch")
                if actual.content != expected.content:
                    content = _image_omission_projection(expected.content, actual.content, allow_image_omission)
                    if content is None:
                        raise ValueError("chat_bootstrap_prefix_mismatch")
                    projected[index].content = content
            return projected + resolved[len(prefix):], {anchor: projected}
        # Replay may omit the imported display rows, but must identify its first
        # later accepted turn explicitly. Neither its body nor Hub is used to guess.
        ref = metadata.get("chat_turn_ref") if getattr(first, "role", None) == "user" else metadata.get("chat_message_ref")
        match = re.fullmatch(r"chat:(.*):([1-9]\d*):(user|assistant)", ref) if isinstance(ref, str) else None
        if (match is None or match.group(1) != self.session_id or match.group(3) != first.role
                or int(match.group(2)) > self.turn_index):
            raise ValueError("invalid_chat_bootstrap_replay_ref")
        system_count = next((index for index, message in enumerate(resolved) if message.role != "system"), len(resolved))
        if system_count:
            expected_systems = [message for message in prefix if message.role == "system"]
            if len(expected_systems) != system_count or any(
                actual.content != expected.content for actual, expected in zip(resolved[:system_count], expected_systems)):
                raise ValueError("chat_bootstrap_system_mismatch")
        return prefix + resolved[system_count:], {}

    def resolve_local_memory_history(self, messages, *, current_user_is_new=True, allow_image_omission=False):
        resolved = deepcopy(list(messages))
        updates = {}
        seen = set()
        with self._asset_lock:
            snapshots = deepcopy(self._local_memory_turns)
            retired = set(self._retired_memory_turns)
            boundaries = dict(self._local_memory_compactions)
        retiring = set()
        for message in resolved:
            metadata = getattr(message, "metadata", None) or {}
            boundary = boundaries.get(metadata.get("summary_id")) if metadata.get("compact_summary") else None
            if boundary is not None and message.content == boundary[0]:
                retiring.update(boundary[1])
        for index, message in enumerate(resolved):
            if getattr(message, "role", None) != "user" or (current_user_is_new and index == len(resolved) - 1):
                continue
            metadata = getattr(message, "metadata", None) or {}
            if metadata.get("pre_compact") or metadata.get("compact_summary"):
                continue
            ref = metadata.get("chat_turn_ref")
            content = message.content
            if ref is None:
                if any(content == raw for raw, _suffix in snapshots.values()):
                    raise ValueError("chat_canonical_history_ref_required")
                continue
            match = re.fullmatch(r"chat:(.*):([1-9]\d*):user", ref) if isinstance(ref, str) else None
            if match is None or match.group(1) != self.session_id:
                if not snapshots:
                    continue
                raise ValueError("foreign_chat_turn_ref")
            turn = int(match.group(2))
            if turn in seen:
                raise ValueError("duplicate_chat_turn_ref")
            seen.add(turn)
            if turn in retired:
                raise ValueError("retired_chat_turn_ref")
            if turn not in snapshots:
                continue
            raw, suffix = snapshots[turn]
            if content != raw:
                if content == append_chat_memory_suffix(raw, suffix):
                    continue
                projected = _image_omission_projection(raw, content, allow_image_omission)
                if projected is None:
                    raise ValueError("chat_canonical_history_mismatch")
                content = projected
                updates[turn] = (deepcopy(content), suffix)
            message.content = append_chat_memory_suffix(content, suffix)
        if set(snapshots) - seen - retiring:
            raise ValueError("chat_canonical_history_ref_required")
        return resolved, updates, frozenset(retiring)

    def commit_local_memory_turn(self, *, request_id, turn_index, raw_content, suffix, updates, retiring=frozenset(), bootstrap_updates=None, retiring_bootstrap=frozenset(), memory_bootstrap_anchor=None):
        with self._asset_lock:
            if self.request_id != request_id or self.turn_index != turn_index:
                return False
            self._active_memory_bootstrap_refs.difference_update(retiring_bootstrap)
            self._retired_chat_bootstrap_refs.update(retiring_bootstrap)
            for anchor in retiring_bootstrap:
                self._chat_bootstrap_prefixes.pop(anchor, None)
            self._chat_bootstrap_prefixes.update({anchor: deepcopy(prefix)
                for anchor, prefix in (bootstrap_updates or {}).items()
                if anchor not in self._retired_chat_bootstrap_refs})
            if memory_bootstrap_anchor in self._chat_bootstrap_prefixes:
                self._active_memory_bootstrap_refs.add(memory_bootstrap_anchor)
            self._retired_memory_turns.update(retiring)
            for turn in retiring:
                self._local_memory_turns.pop(turn, None)
            self._local_memory_turns.update({turn: deepcopy(value) for turn, value in updates.items() if turn not in retiring})
            if suffix:
                self._local_memory_turns[turn_index] = (deepcopy(raw_content), suffix)
            return True

    def local_memory_source_turns(self):
        with self._asset_lock:
            return frozenset(self._local_memory_turns)

    def local_memory_source_snapshot(self):
        with self._asset_lock:
            return deepcopy(self._local_memory_turns)

    def register_local_memory_compaction(self, summary_id, summary_text, source_snapshot, bootstrap_snapshot=None):
        with self._asset_lock:
            if any(self._local_memory_turns.get(turn) != value for turn, value in source_snapshot.items()):
                return False
            bootstrap_snapshot = bootstrap_snapshot or {}
            if any(self._chat_bootstrap_prefixes.get(anchor) != value for anchor, value in bootstrap_snapshot.items()):
                return False
            self._local_memory_compactions[summary_id] = (summary_text, frozenset(source_snapshot), frozenset(bootstrap_snapshot))
            return True
    _producer_cleanup_event: threading.Event = field(
        default_factory=_completed_producer_event,
        repr=False,
    )
    _asset_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )
    _display_assets: dict[str, str] = field(default_factory=dict, repr=False)
    _display_asset_generation: object | None = field(default=None, repr=False)

    @property
    def chat_session_id(self) -> str:
        """Read-only compatibility name; never serialized as a second ID."""

        return self.session_id

    def begin_turn(self, *, request_id: str) -> int:
        with self._asset_lock:
            if self.deletion_started:
                raise RuntimeError("chat session deletion is in progress")
            self.turn_index += 1
            self.request_id = request_id
            self.finished = False
            self.finish_reason = None
            self._producer_cleanup_event.clear()
            self.last_activity = time.time()
            return self.turn_index

    def claim_turn(self, *, request_id: str) -> tuple[object, int]:
        """Claim a producer and reserve its exact turn generation."""

        producer_token = self.hub.claim_producer()
        if producer_token is None:
            raise RuntimeError("chat session producer is already active")
        try:
            turn_index = self.begin_turn(request_id=request_id)
        except BaseException:
            self.hub.release_producer(producer_token)
            raise
        return producer_token, turn_index

    def attach_producer_task(
        self,
        *,
        request_id: str,
        turn_index: int,
        task: Any,
    ) -> bool:
        """Attach a task only to the still-current turn reservation."""

        with self._asset_lock:
            if (
                self.request_id != request_id
                or self.turn_index != turn_index
                or self.finished
            ):
                return False
            self.produce_task = task
            self.producer_request_id = request_id
            self.producer_turn_index = turn_index
            return True

    def current_producer_task(
        self, *, request_id: str | None, turn_index: int | None
    ) -> Any:
        with self._asset_lock:
            if (
                self.producer_request_id == request_id
                and self.producer_turn_index == turn_index
            ):
                return self.produce_task
            return None

    def rollback_turn(self, *, turn_index: int, request_id: str) -> bool:
        """Undo only the exact unaccepted turn reservation."""

        with self._asset_lock:
            if self.turn_index != turn_index or self.request_id != request_id:
                return False
            self.turn_index -= 1
            self.request_id = None
            self.producer_request_id = None
            self.producer_turn_index = None
            self.produce_task = None
            self.finished = True
            self.finish_reason = None
            self._producer_cleanup_event.set()
            self.last_activity = time.time()
            return True

    def begin_deletion(self) -> tuple[str | None, Any, bool]:
        """Pin deletion state against competing turn admission."""

        with self._asset_lock:
            self.deletion_started = True
            self.deletion_turn_index = self.turn_index
            task = self.produce_task
            if (
                self.producer_request_id != self.request_id
                or self.producer_turn_index != self.turn_index
            ):
                task = None
            return self.request_id, task, self.finished

    def wait_for_producer_cleanup(self) -> None:
        self._producer_cleanup_event.wait()

    def stage_display_assets(
        self,
        *,
        turn_index: int,
        images: list[str],
        evict_prior: bool,
    ) -> StagedChatDisplayAssets:
        """Stage one-copy display assets before the acceptance batch append."""

        with self._asset_lock:
            prior_assets = tuple(self._display_assets.items())
            evicted = prior_assets if evict_prior else ()
            added: list[tuple[str, str]] = []
            asset_ids: list[str] = []
            for index, image in enumerate(images, start=1):
                asset_id = f"chat:{self.session_id}:{turn_index}:image:{index}"
                added.append((asset_id, image))
                asset_ids.append(asset_id)
            return StagedChatDisplayAssets(
                asset_ids=tuple(asset_ids),
                added=tuple(added),
                evicted=evicted,
                prior_assets=prior_assets,
                evict_prior=evict_prior,
                generation=object(),
                base_generation=self._display_asset_generation,
            )

    def commit_display_assets(self, staged: StagedChatDisplayAssets) -> None:
        """Commit staged assets synchronously with the acceptance attempt."""

        with self._asset_lock:
            self._commit_display_assets_locked(staged)

    def _commit_display_assets_locked(
        self,
        staged: StagedChatDisplayAssets,
    ) -> None:
        if self._display_asset_generation is not staged.base_generation:
            raise RuntimeError("stale display asset generation")
        if staged.evict_prior:
            self._display_assets.clear()
        self._display_assets.update(staged.added)
        self._display_asset_generation = staged.generation

    def rollback_display_assets(self, staged: StagedChatDisplayAssets) -> bool:
        with self._asset_lock:
            return self._rollback_display_assets_locked(staged)

    def _rollback_display_assets_locked(
        self,
        staged: StagedChatDisplayAssets,
    ) -> bool:
        if self._display_asset_generation is not staged.generation:
            return False
        self._display_assets.clear()
        self._display_assets.update(staged.prior_assets)
        self._display_asset_generation = staged.base_generation
        return True

    def accept_display_assets(
        self,
        staged: StagedChatDisplayAssets,
        accept: Callable[[], Any],
        acceptance_published: Callable[[], bool],
    ) -> Any:
        """Atomically expose assets with synchronous Hub acceptance."""

        with self._asset_lock:
            self._commit_display_assets_locked(staged)
            try:
                return accept()
            except BaseException:
                if not acceptance_published():
                    self._rollback_display_assets_locked(staged)
                raise

    def resolve_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve display assets into an encoded copy, never the journal."""

        event = payload.get("event")
        if not isinstance(event, dict) or event.get("kind") != "turn_started":
            return payload
        resolved = dict(payload)
        resolved_event = dict(event)
        asset_ids = resolved_event.get("image_asset_ids")
        if not isinstance(asset_ids, (list, tuple)):
            return resolved
        with self._asset_lock:
            images = [
                self._display_assets[asset_id]
                for asset_id in asset_ids
                if isinstance(asset_id, str) and asset_id in self._display_assets
            ]
        missing = len(asset_ids) - len(images)
        resolved_event["images"] = images
        if missing > 0:
            resolved_event["historical_image_omission"] = True
            resolved_event["historical_image_placeholder"] = (
                f"[historical images omitted: count={missing}; "
                "sources=none; relative_range=unavailable]"
            )
        resolved["event"] = resolved_event
        return resolved

    def release_resources(self) -> None:
        with self._asset_lock:
            self._local_memory_turns.clear()
            self._retired_memory_turns.clear()
            self._local_memory_compactions.clear()
            self._chat_bootstrap_prefixes.clear()
            self._retired_chat_bootstrap_refs.clear()
            self._active_memory_bootstrap_refs.clear()
            self._display_assets.clear()
            self._display_asset_generation = object()
            self.produce_task = None
            self.producer_request_id = None
            self.producer_turn_index = None


class ChatSessionStore:
    """Chat context directory layered on one process SessionHubRuntime."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSessionRecord] = {}
        self._runtime: SessionHubRuntime | None = None
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions

    def bind_runtime(self, runtime: SessionHubRuntime) -> None:
        if not isinstance(runtime, SessionHubRuntime):
            raise TypeError("runtime must be a SessionHubRuntime")
        with self._lock:
            if self._runtime is not None and self._runtime is not runtime:
                if self._sessions:
                    raise RuntimeError("chat session store is bound to another runtime")
            self._runtime = runtime

    def get_or_create(
        self,
        session_id: str,
        *,
        runtime: SessionHubRuntime,
    ) -> ChatSessionRecord:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        self.bind_runtime(runtime)
        with self._lock:
            self._sweep_locked()
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.last_activity = time.time()
                return existing

            record = ChatSessionRecord(session_id=session_id)
            entry = runtime.register(
                session_id=session_id,
                kind="chat",
                hub=record.hub,
                resolve_payload=record.resolve_payload,
                release_resources=record.release_resources,
            )
            record.entry = entry
            self._sessions[session_id] = record
            return record

    def create(self, session_id: str, *, request_id: str) -> ChatSessionRecord:
        """Compatibility helper for old callers; canonical code uses get_or_create."""

        runtime = self._runtime
        if runtime is None:
            runtime = SessionHubRuntime()
            self.bind_runtime(runtime)
        record = self.get_or_create(session_id or uuid.uuid4().hex, runtime=runtime)
        if record.hub.has_active_producer or not record.finished:
            raise ValueError(
                f"chat session already exists and is still active: {session_id}"
            )
        record.request_id = request_id
        record.finished = False
        return record

    def get(self, session_id: str) -> Optional[ChatSessionRecord]:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is not None:
                record.last_activity = time.time()
            return record

    def begin_delete_if_entry(
        self,
        entry: SessionHubEntry,
    ) -> tuple[ChatSessionRecord, str | None, Any, bool] | None:
        """Pin one exact runtime/store generation for explicit deletion."""

        with self._lock:
            record = self._sessions.get(entry.session_id)
            if record is None or record.entry is not entry:
                return None
            request_id, produce_task, finished = record.begin_deletion()
            return record, request_id, produce_task, finished

    def release_if_same(
        self,
        record: ChatSessionRecord,
        entry: SessionHubEntry,
    ) -> bool:
        """Remove and release only the pinned Chat record/runtime entry."""

        with self._lock:
            if (
                self._sessions.get(record.session_id) is not record
                or record.entry is not entry
            ):
                return False
            self._sessions.pop(record.session_id, None)
            runtime = self._runtime
        if runtime is not None:
            runtime.release_if_same(entry)
        else:
            record.hub.release()
            record.release_resources()
        return True

    def pop(
        self,
        session_id: str,
        *,
        force: bool = False,
    ) -> Optional[ChatSessionRecord]:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            if not force and not record.finished:
                return None
            self._sessions.pop(session_id, None)
            runtime = self._runtime
        if runtime is not None and record.entry is not None:
            runtime.release_if_same(record.entry)
        else:
            record.hub.release()
            record.release_resources()
        return record

    def mark_finished_if_same(
        self,
        record: ChatSessionRecord,
        reason: str,
        *,
        request_id: str,
        turn_index: int,
    ) -> bool:
        with self._lock:
            if (
                self._sessions.get(record.session_id) is not record
                or record.request_id != request_id
                or record.turn_index != turn_index
            ):
                return False
            record.finished = True
            record.finish_reason = reason
            record._producer_cleanup_event.set()
            record.last_activity = time.time()
            return True

    def mark_finished(self, session_id: str, reason: str) -> None:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return
            record.finished = True
            record.finish_reason = reason
            record._producer_cleanup_event.set()
            record.last_activity = time.time()

    def _release_record_locked(self, record: ChatSessionRecord) -> None:
        if self._runtime is not None and record.entry is not None:
            self._runtime.release_if_same(record.entry)
        else:
            record.hub.release()
            record.release_resources()

    def _sweep_locked(self) -> int:
        now = time.time()
        expired = [
            sid
            for sid, record in self._sessions.items()
            if record.finished
            and not record.hub.has_active_producer
            and now - record.last_activity > self.ttl_seconds
        ]
        for sid in expired:
            record = self._sessions.pop(sid, None)
            if record is not None:
                self._release_record_locked(record)
        overflow = len(self._sessions) - self.max_sessions
        if overflow > 0:
            finished_sorted = sorted(
                (
                    (record.last_activity, sid)
                    for sid, record in self._sessions.items()
                    if record.finished and not record.hub.has_active_producer
                ),
            )
            for _, sid in finished_sorted[:overflow]:
                record = self._sessions.pop(sid, None)
                if record is not None:
                    self._release_record_locked(record)
        return len(expired)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for record in self._sessions.values() if not record.finished)


_store_singleton: Optional[ChatSessionStore] = None
_store_lock = threading.Lock()


def chat_session_registry() -> ChatSessionStore:
    """Process-level Chat metadata store; tests may inject an isolated store."""

    global _store_singleton
    with _store_lock:
        if _store_singleton is None:
            _store_singleton = ChatSessionStore()
        return _store_singleton
