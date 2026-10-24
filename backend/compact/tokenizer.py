"""Process-wide token counter with real AutoTokenizer when available.

Falls back to a multilingual char-based heuristic when AutoTokenizer cannot
be loaded (no model_path configured or transformers import fails). The
fallback path keeps should_compact functional but is intentionally
conservative.

Two regressions (2026-06-06) to be aware of:
  1. Heuristic used to be `len(content) // 4` (English-centric). CJK text
     tokenizes at ~1 char per token, so the heuristic undercounted by 3-4x
     and auto-compact never fired for Chinese conversations. Fixed by
     counting CJK chars separately (CJK ~= 1 token/char; non-CJK ~=
     1 token / 4 chars).

  2. The "real tokenizer" path did `len(apply_chat_template(tokenize=True))`.
     In transformers >=4.43 that call returns a `BatchEncoding` whose
     `__len__` is the number of keys (2), NOT the token count. Fixed by
     handling BatchEncoding explicitly via `ret["input_ids"]` (or by
     passing `return_tensors=None, return_dict=False` to force a flat
     list).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# Conservative CJK block ranges. Each char in these blocks is ~1 token
# for BPE-based tokenizers (Qwen, LLaMA, etc.) once chat-template
# overhead is included. The ranges are inclusive.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0xFF00, 0xFFEF),   # Halfwidth and Fullwidth Forms (fullwidth ASCII)
    (0x3000, 0x303F),   # CJK Symbols and Punctuation
    (0x2E80, 0x2EFF),   # CJK Radicals Supplement
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


class TokenCounter:
    _instance: Optional["TokenCounter"] = None
    _tokenizer: Any = None  # transformers.PreTrainedTokenizerBase when loaded
    _model_path: Optional[str] = None
    _use_real_tokenizer: bool = False

    def __init__(self) -> None:
        pass

    @classmethod
    def get(cls, model_path: Optional[str] = None, force_reload: bool = False) -> "TokenCounter":
        if cls._instance is None:
            cls._instance = cls()
        if model_path and (force_reload or cls._instance._model_path != model_path):
            cls._instance._try_load_tokenizer(model_path)
        return cls._instance

    def _try_load_tokenizer(self, model_path: str) -> None:
        try:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                os.path.expanduser(model_path), trust_remote_code=True
            )
            self._model_path = model_path
            self._use_real_tokenizer = True
            logger.info(
                f"TokenCounter loaded AutoTokenizer from {model_path} "
                f"(vocab_size={self._tokenizer.vocab_size})"
            )
        except Exception as exc:
            logger.warning(
                f"TokenCounter could not load AutoTokenizer from {model_path}: {exc}. "
                f"Falling back to multilingual char-based heuristic."
            )
            self._tokenizer = None
            self._use_real_tokenizer = False

    def count_messages(self, messages: List[Any], image_count: int = 0) -> int:
        if not messages:
            return 0
        if self._use_real_tokenizer and self._tokenizer is not None:
            return self._count_with_real_tokenizer(messages, image_count)
        return self._count_with_heuristic(messages, image_count)

    def count_text(self, text: str) -> int:
        """Count tokens in a single text string.

        Used for completion-token estimation in streaming flows (when SGLang
        does not return usage in the response). CJK-aware, real-tokenizer-aware;
        matches the per-text accounting of `_count_with_heuristic` /
        `_count_with_real_tokenizer` so chat-mode streaming usage stays in
        sync with the auto-compact threshold check.
        """
        if not text:
            return 0
        if self._use_real_tokenizer and self._tokenizer is not None:
            try:
                ids = self._tokenizer.encode(text, add_special_tokens=False)
                return len(ids)
            except Exception:
                pass
        cjk = sum(1 for c in text if _is_cjk(c))
        non_cjk = len(text) - cjk
        return cjk + non_cjk // 4

    def _normalize(self, messages: List[Any]) -> List[dict]:
        out: List[dict] = []
        for m in messages:
            if isinstance(m, dict):
                out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
            else:
                out.append(
                    {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
                )
        return out

    def _count_with_real_tokenizer(self, messages: List[Any], image_count: int) -> int:
        """Count tokens using the loaded AutoTokenizer.

        `apply_chat_template(tokenize=True)` return type depends on
        transformers version:
          - <4.43 (and with `return_tensors=None, return_dict=False`): flat
            list[int] whose length IS the token count.
          - >=4.43 (default): BatchEncoding dict-like. `len(...)` is the
            number of keys (2), NOT the token count. We must read
            `["input_ids"]` explicitly.

        We pass the explicit kwargs to make the contract uniform across
        versions, and ALSO defensively handle BatchEncoding in case the
        caller (or a different transformers version) returns one.
        """
        normalized = self._normalize(messages)
        try:
            ret = self._tokenizer.apply_chat_template(
                normalized,
                tokenize=True,
                add_generation_prompt=False,
                return_tensors=None,
                return_dict=False,
            )
            text_tokens = _extract_token_count(ret)
        except Exception:
            return self._count_with_heuristic(messages, image_count)
        return text_tokens + image_count * 512

    def _count_with_heuristic(self, messages: List[Any], image_count: int) -> int:
        """Multilingual char-based estimate.

        CJK characters tokenize at roughly 1 token per char under BPE
        (Qwen, LLaMA, GPT-style). Non-CJK (English/punctuation/code) at
        ~4 chars per token. The previous uniform `len // 4` undercounted
        CJK by 3-4x, making auto-compact never fire for Chinese
        conversations.
        """
        text_tokens = 0
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
            if isinstance(content, str):
                cjk = sum(1 for c in content if _is_cjk(c))
                non_cjk = len(content) - cjk
                # 1 CJK char -> 1 token (conservative; many tokenizers go
                # higher, but a lower bound keeps auto-compact from firing
                # too early). 4 non-CJK chars -> 1 token.
                text_tokens += cjk + non_cjk // 4
            elif isinstance(content, list):
                for part in content:
                    text_tokens += len(str(part)) // 4
            else:
                text_tokens += len(str(content)) // 4
        return int(text_tokens) + image_count * 512


def _extract_token_count(ret: Any) -> int:
    """Extract token count from various apply_chat_template return shapes.

    Handles:
      - list[int] / tuple[int] (older transformers, or with the
        `return_tensors=None, return_dict=False` flags): length is the
        token count.
      - BatchEncoding / dict-like with `"input_ids"`: token count is
        the length of `input_ids` (after collapsing the optional batch
        dimension).
      - torch.Tensor: token count is the last dimension.
    """
    # List / tuple of ints: direct len
    if isinstance(ret, (list, tuple)) and (not ret or isinstance(ret[0], int)):
        return len(ret)
    # BatchEncoding / dict-like: read input_ids
    if hasattr(ret, "keys") and "input_ids" in ret:
        ids = ret["input_ids"]
        # torch tensor: shape is (batch, seq_len) typically
        if hasattr(ids, "shape"):
            return int(ids.shape[-1])
        # 2D list (batched): take first row
        if isinstance(ids, list) and ids and isinstance(ids[0], list):
            return len(ids[0])
        # 1D list
        return len(ids)
    # Anything else with __len__: best effort
    if hasattr(ret, "__len__"):
        return len(ret)
    return int(ret)
