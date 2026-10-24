"""Tests for TokenCounter."""
from types import SimpleNamespace
import json

import pytest

from backend.compact.tokenizer import TokenCounter


@pytest.mark.parametrize("home_name", ["alice", "bob"])
def test_tokenizer_loads_local_model_from_current_home(tmp_path, monkeypatch, home_name):
    from tokenizers import Tokenizer, models

    home = tmp_path / home_name
    model_dir = home / "models" / "tiny-tokenizer"
    model_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "你好": 1}, unk_token="[UNK]"))
    tokenizer.save(str(model_dir / "tokenizer.json"))
    (model_dir / "tokenizer_config.json").write_text(
        json.dumps({"tokenizer_class": "PreTrainedTokenizerFast", "unk_token": "[UNK]"})
    )

    counter = TokenCounter()
    counter._try_load_tokenizer("~/models/tiny-tokenizer")
    assert counter.count_text("你好") == 1
    assert counter._model_path == "~/models/tiny-tokenizer"


def test_tokenizer_keeps_huggingface_model_identifiers(monkeypatch):
    from transformers import AutoTokenizer

    received = []

    def load(model_path, **kwargs):
        received.append(model_path)
        return _FakeEncodeTokenizer(encoded_len=7)

    monkeypatch.setattr(AutoTokenizer, "from_pretrained", load)
    counter = TokenCounter()
    counter._try_load_tokenizer("organization/model")
    assert received == ["organization/model"]
    assert counter.count_text("你好") == 7


def test_token_count_text_messages_returns_positive_int():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    messages = [
        {"role": "user", "content": "Hello world"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    n = counter.count_messages(messages)
    assert n > 0
    assert isinstance(n, int)


def test_token_count_grows_with_content_length():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    short = counter.count_messages([{"role": "user", "content": "hi"}])
    long = counter.count_messages([{"role": "user", "content": "hi " * 200}])
    assert long > short


def test_token_count_empty_messages_returns_zero():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    assert counter.count_messages([]) == 0


def test_token_count_handles_dict_and_object_messages():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    a = counter.count_messages([{"role": "user", "content": "abc"}])
    b = counter.count_messages([SimpleNamespace(role="user", content="abc")])
    assert a == b  # heuristic path is the same for both shapes


def test_token_count_with_image_count_adds_estimate():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    n0 = counter.count_messages([{"role": "user", "content": "x"}])
    n2 = counter.count_messages([{"role": "user", "content": "x"}], image_count=2)
    assert n2 > n0


def test_token_count_with_real_tokenizer_if_loaded():
    """When AutoTokenizer loads, apply_chat_template path is exercised.

    We attempt a tiny model path; if it fails (no network or invalid path),
    the test still passes because the heuristic fallback is correct.
    """
    counter = TokenCounter()
    counter._tokenizer = _FakeTokenizer()
    counter._use_real_tokenizer = True
    n = counter.count_messages([{"role": "user", "content": "Hello"}])
    assert n == 5  # FakeTokenizer returns sum of chars


def test_token_count_heuristic_cjk_aware_does_not_undercount_chinese():
    """Defense in depth: heuristic must not pathologically undercount CJK text.

    Bug (2026-06-06): users running in Chinese saw auto-compact never trigger
    because the heuristic was `len(content) // 4` (English-centric). For CJK
    text, real tokenizers produce ~1 token per character, but the heuristic
    reported ~0.25 tokens per character — a 4x undercount. With 30k+ SGLang
    full tokens of Chinese, the heuristic returned ~7k, well below any
    reasonable threshold.

    We don't pin the exact ratio (it depends on the model), but the heuristic
    MUST be within 2x of the real-tokenizer rate for pure CJK. We use 1000
    CJK chars and assert the heuristic reports at least 500 (i.e. ≥0.5
    tokens/char), which is conservatively below the real ~0.7-1.0.
    """
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False

    cjk_1000 = "你好世界今天天气很好" * 100  # 1000 CJK chars, no ASCII
    n = counter.count_messages([{"role": "user", "content": cjk_1000}])
    # Old heuristic: 1000 // 4 = 250. New CJK-aware heuristic: ≥ 500.
    assert n >= 500, (
        f"heuristic still undercounts CJK text: got {n} for 1000 CJK chars "
        f"(must be >= 500 to be within 2x of real ~700-1000 tokens)"
    )


def test_token_count_real_tokenizer_handles_batch_encoding_return_type():
    """Real `apply_chat_template(tokenize=True)` returns BatchEncoding in
    transformers >=4.43, not a list. `len(BatchEncoding)` returns the number
    of keys (typically 2: input_ids and attention_mask), NOT the token count.

    Bug (2026-06-06): the old code did `len(tokens)` on the result and got 2
    for every chat, regardless of actual length. Combined with the heuristic
    bug, this is why auto-compact never fired.

    Pin the contract: a BatchEncoding-shaped return value must produce the
    real token count, not 2.
    """
    counter = TokenCounter()
    counter._tokenizer = _FakeBatchEncodingTokenizer(token_count=1234)
    counter._use_real_tokenizer = True
    n = counter.count_messages(
        [{"role": "user", "content": "x" * 5000}]  # chars don't matter; tokenizer fixed
    )
    assert n == 1234, (
        f"BatchEncoding-shaped return value not handled correctly: got {n}, "
        f"expected 1234. This is the 'len(BatchEncoding)==2' bug."
    )


def test_token_count_real_tokenizer_handles_legacy_list_return_type():
    """Older transformers (<4.43) returns a flat list of int token ids from
    `apply_chat_template(tokenize=True)`. The counter must continue to
    support that shape.
    """
    counter = TokenCounter()
    counter._tokenizer = _FakeListTokenizer(token_count=42)
    counter._use_real_tokenizer = True
    n = counter.count_messages([{"role": "user", "content": "hi"}])
    assert n == 42


def test_count_text_empty_returns_zero():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    assert counter.count_text("") == 0
    assert counter.count_text(None) == 0  # type: ignore[arg-type]


def test_count_text_heuristic_cjk_aware():
    """`count_text` must use the same CJK-aware heuristic as `count_messages`.

    Pin the contract: a single 1000-char CJK string produces ≥500 tokens under
    the heuristic (i.e. ≥0.5 tokens/char). This is what feeds the streaming
    terminal "[Step N: ... | Output tokens ...]" log via
    `LLMService.get_stream_usage`; if it regresses to `len // 4`, completion
    tokens for Chinese responses will be wildly undercounted.
    """
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    cjk_1000 = "你好世界今天天气很好" * 100
    n = counter.count_text(cjk_1000)
    assert n >= 500, (
        f"count_text heuristic undercounts CJK: got {n} for 1000 CJK chars "
        f"(must be >= 500 to stay within 2x of real ~700-1000 tokens)"
    )


def test_count_text_grows_with_length():
    counter = TokenCounter()
    counter._tokenizer = None
    counter._use_real_tokenizer = False
    short = counter.count_text("hi")
    long = counter.count_text("hi " * 200)
    assert long > short


def test_count_text_real_tokenizer_uses_encode():
    """When a real tokenizer is loaded, `count_text` uses `tokenizer.encode`,
    not `apply_chat_template` (which expects a message list, not a string).
    The encode path gives the per-text token count for completion estimation.
    """
    counter = TokenCounter()
    counter._tokenizer = _FakeEncodeTokenizer(encoded_len=7)
    counter._use_real_tokenizer = True
    n = counter.count_text("anything")
    assert n == 7


class _FakeTokenizer:
    """Minimal stand-in for transformers tokenizer (legacy list return)."""

    vocab_size = 1000

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=False, **kwargs):
        # Return a list whose length equals the sum of content character counts
        total = sum(len(str(m.get("content", ""))) for m in messages)
        return list(range(total))


class _FakeBatchEncodingTokenizer:
    """Stand-in for newer transformers (>=4.43) returning BatchEncoding.

    `len(BatchEncoding)` returns the number of keys (2), NOT the token count.
    The counter MUST use `ret["input_ids"]` to get the real count.
    """

    vocab_size = 1000

    def __init__(self, token_count: int) -> None:
        self._token_count = token_count

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=False, **kwargs):
        # Mimic the BatchEncoding shape: a dict-like with `input_ids` and
        # `attention_mask` keys. `__len__` mirrors transformers' behavior:
        # returns the number of keys (2), not the token count.
        class _BatchEncodingLike(dict):
            def __len__(self) -> int:
                return len(dict.keys(self))  # number of keys (2)

        ids = list(range(self._token_count))
        return _BatchEncodingLike(
            {"input_ids": ids, "attention_mask": [1] * len(ids)}
        )


class _FakeListTokenizer:
    """Stand-in for older transformers (<4.43) returning a flat list."""

    vocab_size = 1000

    def __init__(self, token_count: int) -> None:
        self._token_count = token_count

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=False, **kwargs):
        return list(range(self._token_count))


class _FakeEncodeTokenizer:
    """Minimal stand-in for the real-tokenizer encode path used by
    `TokenCounter.count_text`. Returns a fixed list length.
    """

    vocab_size = 1000

    def __init__(self, encoded_len: int) -> None:
        self._encoded_len = encoded_len

    def encode(self, text, add_special_tokens=False):
        return list(range(self._encoded_len))
