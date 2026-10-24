"""Request-owned ordinary Chat memory preparation; no Hub-derived model state."""
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import os
import stat

from backend.agent.canonical_messages import message_to_template
from backend.api.chat_session_store import append_chat_memory_suffix, chat_content_text
from backend.compact.tokenizer import TokenCounter, _extract_token_count
from backend.config import PROJECT_ROOT
from backend.llm_service import is_request_aborted, resolve_backend_context_length, resolve_effective_max_tokens
from backend.retrieval.contracts import PreparedUserTurn


class ChatMemoryAborted(Exception):
    pass


def _context_validator(service, messages, tools, max_tokens, enable_thinking):
    """Count the actual provider template, complete schema, image/output reserve."""
    try:
        cfg, backend = service.config, service.active_backend
        counter = TokenCounter.get()
        if backend not in {"sglang", "vllm"} or not counter._use_real_tokenizer or counter._tokenizer is None:
            raise ValueError("uncertified tokenizer/provider")
        tokenizer = counter._tokenizer
        limit = resolve_backend_context_length(cfg, backend)
        reserve = resolve_effective_max_tokens(cfg, backend, max_tokens)
        if not isinstance(limit, int) or not isinstance(reserve, int) or limit <= 0 or reserve <= 0:
            raise ValueError("uncertified context budget")
        snapshots = getattr(service, "_chat_local_memory_templates", None)
        if snapshots is None:
            snapshots = {}
            service._chat_local_memory_templates = snapshots
        if backend not in snapshots:
            snapshots[backend] = (False, None)
            configured = getattr(getattr(cfg, backend), "chat_template", None)
            template = None
            if configured:
                path = Path(configured)
                if not path.is_absolute():
                    path = PROJECT_ROOT / path
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, encoding="utf-8") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("invalid provider template")
                    template = source.read(1024 * 1024 + 1)
                if not template.strip() or len(template) > 1024 * 1024:
                    raise ValueError("invalid provider template")
            snapshots[backend] = (True, template)
        valid, template = snapshots[backend]
        if not valid:
            raise ValueError("uncertified provider template")
        body = [message_to_template(message) for message in messages]
        raw = chat_content_text(body[-1]["content"])
        images = sum(1 for message in body for part in (
            message["content"] if isinstance(message["content"], list) else ())
            if isinstance(part, dict) and part.get("type") in {"image", "image_url"})
        image_bound = cfg.model.max_image_tokens
        if images and (not isinstance(image_bound, int) or image_bound <= 0):
            raise ValueError("uncertified image budget")
        schema = deepcopy(tools)
        kwargs = {} if enable_thinking is None else {"enable_thinking": enable_thinking}
    except Exception:
        return lambda _candidate: False

    def fits(candidate):
        try:
            if not candidate.startswith(raw):
                return False
            projected = deepcopy(body)
            projected[-1]["content"] = append_chat_memory_suffix(projected[-1]["content"], candidate[len(raw):])
            counted = tokenizer.apply_chat_template(projected, tools=schema,
                tokenize=True, add_generation_prompt=True, return_dict=False, return_tensors=None,
                **({"chat_template": template} if template is not None else {}), **kwargs)
            return _extract_token_count(counted) + images * (image_bound or 0) + reserve <= limit
        except Exception:
            return False
    return fits


@dataclass
class ChatMemoryTurn:
    service: object
    enabled: bool
    request_id: str
    raw_content: object
    record: object = None
    turn_index: int = 0
    updates: dict = field(default_factory=dict)
    retiring: frozenset = frozenset()
    prepared: PreparedUserTurn | None = None
    committed: bool = False
    history_start_ref: str | None = None
    can_retrieve: bool = True
    bootstrap_updates: dict = field(default_factory=dict)
    initial_bootstrap_updates: dict = field(default_factory=dict)
    retiring_bootstrap: frozenset = frozenset()

    def admit_bootstrap(self):
        if self.record is not None and self.initial_bootstrap_updates:
            self.record.admit_chat_bootstrap(request_id=self.request_id, turn_index=self.turn_index,
                prefixes=self.initial_bootstrap_updates)

    async def prepare(self, llm_service, messages, tools, max_tokens, enable_thinking):
        if is_request_aborted(self.request_id):
            raise ChatMemoryAborted()
        raw = chat_content_text(self.raw_content)
        if self.prepared is None:
            if self.enabled and not self.can_retrieve:
                self.prepared = PreparedUserTurn(raw, raw, "history_anchor_unavailable")
            elif self.service is None or not messages or messages[-1].role != "user":
                self.prepared = PreparedUserTurn(raw, raw, "unavailable")
            else:
                self.prepared = await self.service.prepare_user_turn(raw, enabled=self.enabled,
                    is_aborted=lambda: is_request_aborted(self.request_id),
                    context_validator=(_context_validator(llm_service, messages, tools, max_tokens, enable_thinking)
                        if self.enabled else None))
        if is_request_aborted(self.request_id):
            raise ChatMemoryAborted()
        if not self.prepared.model_user_content.startswith(raw):
            self.prepared = PreparedUserTurn(raw, raw, "invalid_prepared_prefix")
        final = deepcopy(messages)
        if final and final[-1].role == "user":
            final[-1].content = append_chat_memory_suffix(final[-1].content,
                self.prepared.model_user_content[len(raw):])
        return final

    def event(self):
        if self.prepared is None or self.prepared.status != "matched" or not self.prepared.memory_texts:
            return None
        return {"kind": "local_memory_retrieved", "session_id": self.record.session_id if self.record else None,
            "turn_index": self.turn_index, "chunks": [{"text": text} for text in self.prepared.memory_texts],
            **({"history_start_ref": self.history_start_ref} if self.history_start_ref else {})}

    def commit(self):
        if self.committed or self.record is None or self.prepared is None or is_request_aborted(self.request_id):
            return
        raw = chat_content_text(self.raw_content)
        self.record.commit_local_memory_turn(request_id=self.request_id, turn_index=self.turn_index,
            raw_content=self.raw_content, suffix=self.prepared.model_user_content[len(raw):],
            updates=self.updates, retiring=self.retiring,
            bootstrap_updates={**(self.initial_bootstrap_updates if self.prepared.memory_texts else {}), **self.bootstrap_updates},
            retiring_bootstrap=self.retiring_bootstrap,
            memory_bootstrap_anchor=self.history_start_ref if self.prepared.memory_texts else None)
        self.committed = True

    def compacted(self, summary_id, summary, source_snapshot, bootstrap_snapshot=None):
        if self.record is not None:
            if not self.record.register_local_memory_compaction(summary_id, summary, source_snapshot, bootstrap_snapshot):
                raise ValueError("compact source changed before commit")
            self.retiring = self.retiring | frozenset(source_snapshot)
            self.retiring_bootstrap = self.retiring_bootstrap | frozenset(bootstrap_snapshot or {})
            self.initial_bootstrap_updates.clear()
            self.history_start_ref = f"chat:{self.record.session_id}:{self.turn_index}:user"
            self.can_retrieve = True
