"""Accept Qwen3.5 text weights stored under the multimodal checkpoint prefix. See lession-learnt/踩坑记录8.md"""

from typing import Iterable, Set, Tuple

import torch

from sglang.srt.models.qwen3_5_text import Qwen3_5ForCausalLM as SGLangQwen3_5ForCausalLM


class Qwen3_5ForCausalLM(SGLangQwen3_5ForCausalLM):
    """Normalize only the checkpoint prefix before the installed loader runs."""

    def load_weights(self, weights: Iterable[Tuple[str, torch.Tensor]]) -> Set[str]:
        checkpoint_prefix = "model.language_model."
        return super().load_weights(
            (
                "model." + name[len(checkpoint_prefix):]
                if name.startswith(checkpoint_prefix)
                else name,
                tensor,
            )
            for name, tensor in weights
        )


EntryClass = Qwen3_5ForCausalLM
