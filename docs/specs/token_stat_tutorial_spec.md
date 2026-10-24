备忘：已由 ZCODE GLM 5.3 完成

建议你**彻底删掉 backend 里 `transformers tokenization + image=500 tokens` 这套计数逻辑**，把 SGLang 作为唯一 token truth source。

截至目前我查了 SGLang 主线源码、官方文档和相关社区 issue。结论是：

> **不要解析 SGLang server terminal 里的 `token usage: 0.xx`。**
>
> 正确做法是让 Agent backend 直接透传 SGLang `/v1/chat/completions` 的 SSE stream，并开启 `include_usage + continuous_usage_stats`。SGLang 自己完成 text + image 的真实模型侧 tokenization，然后把累计 token usage 返回给你。

SGLang 日志中的 `token usage` 实际是 **KV cache memory utilization**，官方调优文档也明确这样定义；社区讨论进一步解释它反映当前 running/pending sequence 占用的 cache，而不是某个请求消耗了多少 token。所以拿 terminal stdout 里的这个值做请求计费/上下文统计是错的。([GitHub][1])

## 1. SGLang 现在已经能给你真实的图片 token

主线代码里的 `MultimodalInputs.compute_mm_token_counts()` 已经直接计算多模态 token：

```python
num_tokens = sum(end - start + 1 for start, end in item.offsets)

if item.is_image():
    image_tokens += num_tokens
elif item.is_audio():
    audio_tokens += num_tokens
elif item.is_video():
    video_tokens += num_tokens
```

也就是说，它不是：

```text
1 image = 500 tokens
```

而是在**模型自己的 multimodal processor 完成图片 resize / dynamic patch / placeholder expansion 之后**，根据最终进入模型 prompt 的真实 multimodal token span 来算。([GitHub][2])

SGLang 官方代码里的 VLM 测试还专门验证了：

```python
usage_details = response_json["usage"].get("prompt_tokens_details")
self.assertGreater(usage_details.get("image_tokens", 0), 0)
```

注释直接写明 `image_tokens` 来自 prefill 阶段的 multimodal item offsets。([GitHub][3])

返回协议当前也已经定义了：

```json
{
  "usage": {
    "prompt_tokens": 1234,
    "completion_tokens": 56,
    "total_tokens": 1290,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "image_tokens": 1024
    }
  }
}
```

SGLang 的 `PromptTokensDetails` 当前包含 `cached_tokens / image_tokens / audio_tokens / video_tokens`，并原生支持 `include_usage` 和 `continuous_usage_stats`。([GitHub][4])

这里有个**非常重要的语义**：

```text
prompt_tokens 已经包含 image_tokens
```

不能这样：

```python
total = prompt_tokens + image_tokens + completion_tokens   # ❌ 重复计算
```

正确是：

```python
total = prompt_tokens + completion_tokens                  # ✅

image_tokens = prompt_tokens_details.image_tokens          # prompt 的子集
```

SGLang 官方 benchmark 文档同样说明 input token throughput 包括 text + vision，并另外报告 text/vision breakdown。([SGLang Documentation][5])

---

# 2. 我建议你的最终架构

你现在大概是：

```text
                         ┌─ transformers tokenizer → text_tokens
Agent Backend ─ request ─┤
                         └─ image_count × 500 → image_tokens

                              ↓

                           SGLang
```

直接改成：

```text
Agent Backend
     │
     │ POST /v1/chat/completions
     │ stream=true
     │ include_usage=true
     │ continuous_usage_stats=true
     ▼
┌──────────────────────────────┐
│          SGLang Worker       │
│                              │
│ tokenizer                    │
│ multimodal processor         │
│ vision token expansion       │
│ scheduler / decode           │
│                              │
│ authoritative token counter  │
└──────────────┬───────────────┘
               │
               │ SSE
               ▼
Agent Backend

data: {... "usage": {
    "prompt_tokens": 1857,
    "completion_tokens": 1,
    "total_tokens": 1858
}}

data: {... "usage": {
    "prompt_tokens": 1857,
    "completion_tokens": 2,
    "total_tokens": 1859
}}

...

final authoritative usage
```

这样你的 Agent 根本不需要依赖 transformers 来重新 tokenize。

---

# 3. 请求就这么发

请求 SGLang **worker 本身**：

```bash
curl -N http://127.0.0.1:30000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "your-model",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "image_url",
            "image_url": {
              "url": "https://example.com/test.jpg"
            }
          },
          {
            "type": "text",
            "text": "describe this image"
          }
        ]
      }
    ],
    "stream": true,
    "stream_options": {
      "include_usage": true,
      "continuous_usage_stats": true
    }
  }'
```

`StreamOptions` 这两个字段现在就在 SGLang OpenAI-compatible protocol 中。([GitHub][4])

其中：

```text
include_usage=true
```

保证流结束时拿到 usage。

而：

```text
continuous_usage_stats=true
```

让生成过程中各个 SSE chunk 同时返回**累计 usage**。

SGLang 社区的实际复现也确认了：直接请求 worker 时，每个 chunk 能返回 usage。([GitHub][6])

---

# 4. Backend 代码可以直接这样改

你既然现在已经用 Python/transformers，我建议直接用 `httpx`：

```python
import json
import httpx


async def sglang_stream(payload):
    payload = {
        **payload,
        "stream": True,
        "stream_options": {
            "include_usage": True,
            "continuous_usage_stats": True,
        },
    }

    latest_usage = None

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            "http://127.0.0.1:30000/v1/chat/completions",
            json=payload,
        ) as resp:
            resp.raise_for_status()

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue

                data = line[6:]

                if data == "[DONE]":
                    break

                chunk = json.loads(data)

                usage = chunk.get("usage")
                if usage:
                    latest_usage = usage

                    details = usage.get("prompt_tokens_details") or {}

                    token_event = {
                        "request_id": chunk.get("id"),

                        # authoritative cumulative values
                        "prompt_tokens": usage["prompt_tokens"],
                        "completion_tokens": usage["completion_tokens"],
                        "total_tokens": usage["total_tokens"],

                        # modality breakdown
                        "image_tokens": details.get("image_tokens", 0),
                        "audio_tokens": details.get("audio_tokens", 0),
                        "video_tokens": details.get("video_tokens", 0),
                        "cached_tokens": details.get("cached_tokens", 0),
                    }

                    # 这里直接推给你的 agent frontend / telemetry bus
                    await publish_token_usage(token_event)

                # 正常继续透传模型输出
                yield chunk

    return latest_usage
```

### 特别注意：continuous usage 是“累计值”

比如 SGLang 返回：

```text
chunk 1: completion_tokens = 1
chunk 2: completion_tokens = 2
chunk 3: completion_tokens = 3
chunk 4: completion_tokens = 4
```

千万不要：

```python
1 + 2 + 3 + 4
```

正确是直接覆盖：

```python
current_completion_tokens = usage["completion_tokens"]
```

如果你的 frontend 需要 delta：

```python
delta = current_completion_tokens - previous_completion_tokens
```

对于一个 agent run 中的多次模型调用，应该在**每个 request 完成后**累加最终 usage：

```python
run_prompt_tokens += final_usage["prompt_tokens"]
run_completion_tokens += final_usage["completion_tokens"]
run_total_tokens += final_usage["total_tokens"]
```

这样 tool call → model → tool → model → model 的 agentic loop 也能准确累计。

---

# 5. 图片 token 这块能比你现在准确多少？

非常明显。

比如 Qwen-VL 类模型，图片 token 数可能随：

```text
原始 resolution
→ resize
→ dynamic patch 数
→ processor 配置
→ min/max pixels / dynamic patch
→ model-specific vision merge
```

而变化。

所以同样是“一张图片”：

```text
image A → 256 vision tokens
image B → 1024 vision tokens
image C → 3584 vision tokens
```

都是可能的。

你的：

```python
image_tokens = len(images) * 500
```

从原理上就无法准确。

而 SGLang 是在多模态 processor **实际生成 offset 后**统计 span，因此它代表实际进入 LM context 的多模态 prompt token。([GitHub][2])

这也是我认为应该把 token accounting ownership 完全交给 inference server 的主要原因。

---

# 6. 但是有一个 SGLang 当前的坑：实时 image_tokens 明细

这部分是我翻源码后发现的，比较重要。

当前 SGLang 的 usage processor 已经完整支持：

```python
calculate_token_usage(
    prompt_tokens=...,
    completion_tokens=...,
    cached_tokens=...,
    image_tokens=...,
    audio_tokens=...,
    video_tokens=...,
)
```

而且最终 response 可以携带 `image_tokens`。([GitHub][7])

但是当前 main 的 chat continuous streaming 路径中，每个生成 chunk 调用的是：

```python
UsageProcessor.calculate_token_usage(
    prompt_tokens=prompt_tokens.get(index, 0),
    reasoning_tokens=reasoning_tokens.get(index, 0),
    completion_tokens=completion_tokens.get(index, 0),
    cached_tokens=self._continuous_usage_cached_details(content),
)
```

注意：**这里没有传 `image_tokens/audio_tokens/video_tokens`。** ([GitHub][8])

所以 stock SGLang 当前的行为我建议你理解成：

```text
实时：
prompt_tokens       ✅
completion_tokens   ✅
total_tokens        ✅

最终 multimodal breakdown：
image_tokens        ✅

每一个 streamed chunk 都带 image_tokens：
当前不要依赖它      ⚠️
```

这并不影响你替换现在的计算方案。

因为 image prompt token 在 **prefill 完成之后就是常量**，不会随着 decode 改变。

所以 A 档实现完全可以：

```text
实时 UI：
Input:  1848
Output: 1 → 2 → 3 → 4 → 5
Total:  1849 → 1850 → ...

请求结束：
Input: 1848
├─ image: 1536
└─ other prompt: 312
Output: 127
Total: 1975
```

这已经是**真实实时总消耗 + 最终真实多模态 breakdown**。

---

# 7. 如果你要求“每一个 SSE chunk 都必须有 image_tokens”

那我建议直接给 SGLang 打一个很小的 patch。

改：

```text
python/sglang/srt/entrypoints/openai/serving_chat.py
```

当前 `_generate_stream_content()` 中所有：

```python
usage = UsageProcessor.calculate_token_usage(
    prompt_tokens=prompt_tokens.get(index, 0),
    reasoning_tokens=reasoning_tokens.get(index, 0),
    completion_tokens=completion_tokens.get(index, 0),
    cached_tokens=self._continuous_usage_cached_details(content),
)
```

改成类似：

```python
meta = content["meta_info"]

usage = UsageProcessor.calculate_token_usage(
    prompt_tokens=prompt_tokens.get(index, 0),
    reasoning_tokens=reasoning_tokens.get(index, 0),
    completion_tokens=completion_tokens.get(index, 0),
    cached_tokens=self._continuous_usage_cached_details(content),

    image_tokens=meta.get("image_tokens", 0),
    audio_tokens=meta.get("audio_tokens", 0),
    video_tokens=meta.get("video_tokens", 0),
)
```

SGLang scheduler → detokenizer 的内部数据结构本来就已经携带：

```python
image_tokens=recv_obj.image_tokens,
audio_tokens=recv_obj.audio_tokens,
video_tokens=recv_obj.video_tokens,
```

也就是说**真实数据已经在 inference pipeline 里了**，问题只是当前 continuous OpenAI stream 没有在每个 chunk 的 `calculate_token_usage()` 调用中把它塞进去。([GitHub][9])

如果你当前 checkout 的 branch 在 `content["meta_info"]` 这一层还没有保留这三个字段，那就再在 tokenizer-manager 的 BatchStrOutput → meta_info 组装处透传三个 int；**不要重新计算图片**。

最终 SSE 就可以变成：

```json
{
  "id": "chatcmpl-xxx",
  "choices": [{
    "delta": {
      "content": "a"
    }
  }],
  "usage": {
    "prompt_tokens": 1857,
    "completion_tokens": 43,
    "total_tokens": 1900,
    "prompt_tokens_details": {
      "image_tokens": 1536,
      "cached_tokens": 0
    }
  }
}
```

下一帧：

```json
{
  "usage": {
    "prompt_tokens": 1857,
    "completion_tokens": 44,
    "total_tokens": 1901,
    "prompt_tokens_details": {
      "image_tokens": 1536
    }
  }
}
```

这就是你描述的“**SGLang 直接透传实时、真实 token 消耗**”。

---

# 8. 一个额外坑：尽量不要让 Router 挡在中间

如果你的部署现在是：

```text
Agent
  ↓
SGLang Model Gateway / Router
  ↓
Worker
```

需要特别测试。

2026 年 5 月有 SGLang issue 专门报告：

```json
"stream_options": {
    "include_usage": true,
    "continuous_usage_stats": true
}
```

直接打 worker：

```text
每个 chunk 有 usage ✅
```

打 Model Gateway/Router：

```text
只有最后 chunk 有 usage ❌
```

issue 当时明确复现为 router 层行为。([GitHub][6])

因此你的 token telemetry 数据面，我建议：

```text
                  ┌───────────────┐
Agent Backend ───►│ SGLang Worker │
                  └───────────────┘
                         │
                         └── SSE usage
```

如果生产环境必须经过 gateway，则上线前做 integration test，确认你当前版本已经正确 forward `continuous_usage_stats`；不要假设 gateway 和 worker 行为相同。

---

# 9. `cached_tokens` 我建议也一起接进来

如果你想统计的不只是“API logical token usage”，还想统计**真正重新算了多少 prefill token**，启动 SGLang 时可以考虑：

```bash
--enable-cache-report
```

因为 SGLang 的 protocol 和 serving path 支持把 cache 数写进：

```json
"prompt_tokens_details": {
    "cached_tokens": 1200
}
```

相关 serving 代码只有开启 `enable_cache_report` 时才返回 cached-token detail。([GitHub][8])

于是你可以同时维护：

```python
logical_input_tokens = prompt_tokens

cached_input_tokens = cached_tokens

new_prefill_tokens = (
    prompt_tokens - cached_tokens
)
```

这两个概念不要混。

对于 agentic 工具特别有价值，因为多轮 system prompt / tool schema 很容易命中 prefix cache。

---

# 10. Ubuntu 24 + ARM64

这个 telemetry 方案本身完全在 OpenAI-compatible HTTP/SSE 层，所以 **x86_64/ARM64 没区别**。

SGLang upstream 当前也确实有：

```text
docker/arm64.Dockerfile
FROM ubuntu:24.04
```

ARM64 Dockerfile 会安装 CPU engine；而通用 CUDA Dockerfile里现在也已经有 `aarch64` 判断和 JIT fallback。([GitHub][10])

版本方面，我不建议为了这个功能跑旧 release。目前官方安装文档给的 release branch 是 **v0.5.16**，GitHub release 页也将 v0.5.16 标为最新 release；官方还建议生产环境 pin immutable version tag。([GitHub][11])

所以如果让我替你定方案，我会定成：

```text
Ubuntu 24 / ARM64
        │
        ▼
SGLang >= v0.5.16
        │
        ├── OpenAI /v1/chat/completions
        │
        ├── stream=true
        │
        ├── include_usage=true
        │
        └── continuous_usage_stats=true
                 │
                 ▼
            Backend SSE parser
                 │
        ┌────────┴──────────┐
        │                   │
live request usage     final authoritative
prompt_tokens          prompt_tokens
completion_tokens      completion_tokens
total_tokens           total_tokens
                      image_tokens
                      cached_tokens
```

然后把你 backend 里的：

```python
transformers tokenizer
```

和：

```python
image_count * 500
```

**全部从正式 accounting path 删除**。

如果 transformers 还有别的用途可以保留，但它不应该再决定 token usage。

---

## 最后给你一个我会实际落地的版本

只需要做这 5 件事：

1. **SGLang pin 到 v0.5.16 或更新的已验证版本**。
2. backend 调 SGLang worker 时统一强制注入：

   ```json
   "stream": true,
   "stream_options": {
       "include_usage": true,
       "continuous_usage_stats": true
   }
   ```
3. backend SSE parser 遇到 `usage` 就**覆盖当前累计值而不是相加**。
4. 最终 usage 作为 request authoritative record，`prompt_tokens_details.image_tokens` 替代你的 `500 × image_count`。
5. 如果你的产品要求 UI 在生成第一个字的时候就展示 `image_tokens=xxxx`，给 `serving_chat.py` 补上上面的 3 个 multimodal 参数透传；**不要自己重新 tokenize 图片**。

这套架构以后你从 Qwen2.5-VL 换 Qwen3-VL、Kimi-VL 或其他动态分辨率模型，agent backend 都不需要知道“图片应该折算多少 token”——**模型 serving 层自己告诉你。**

而且从 SGLang 当前源码设计看，这也是最合理的 ownership：scheduler 已经掌握真正的 multimodal token span，usage processor 已经支持 multimodal detail，你的 backend 只负责消费和透传。([GitHub][2])

[1]: https://github.com/sgl-project/sglang/discussions/21117?utm_source=chatgpt.com "Clarification needed: What does `token usage` in Prefill/Decode batch logs actually represent? · sgl-project sglang · Discussion #21117 · GitHub"
[2]: https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/managers/schedule_batch.py?utm_source=chatgpt.com "sglang/python/sglang/srt/managers/schedule_batch.py at main · sgl-project/sglang · GitHub"
[3]: https://github.com/sgl-project/sglang/blob/main/test/registered/dp_attn/test_dp_attention.py?utm_source=chatgpt.com "sglang/test/registered/dp_attn/test_dp_attention.py at main · sgl-project/sglang · GitHub"
[4]: https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/entrypoints/openai/protocol.py?utm_source=chatgpt.com "sglang/python/sglang/srt/entrypoints/openai/protocol.py at main · sgl-project/sglang · GitHub"
[5]: https://docs.sglang.ai/developer_guide/bench_serving?utm_source=chatgpt.com "Bench Serving Guide — SGLang"
[6]: https://github.com/sgl-project/sglang/issues/24608?utm_source=chatgpt.com "[Bug] Model Gateway does not support continuous_usage_stat parameter in streaming generation · Issue #24608 · sgl-project/sglang · GitHub"
[7]: https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/entrypoints/openai/usage_processor.py "sglang/python/sglang/srt/entrypoints/openai/usage_processor.py at main · sgl-project/sglang · GitHub"
[8]: https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/entrypoints/openai/serving_chat.py "sglang/python/sglang/srt/entrypoints/openai/serving_chat.py at main · sgl-project/sglang · GitHub"
[9]: https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/managers/detokenizer_manager.py?utm_source=chatgpt.com "sglang/python/sglang/srt/managers/detokenizer_manager.py at main · sgl-project/sglang · GitHub"
[10]: https://github.com/sgl-project/sglang/blob/main/docker/arm64.Dockerfile?utm_source=chatgpt.com "sglang/docker/arm64.Dockerfile at main · sgl-project/sglang · GitHub"
[11]: https://github.com/sgl-project/sglang/releases?ref=codersera.com&utm_source=chatgpt.com "Releases · sgl-project/sglang · GitHub"
