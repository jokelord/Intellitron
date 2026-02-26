# 参数值传递链条

config.ini [inference] max_tokens = 131072
    ↓
ConfigLoader._parse_inference_config() → InferenceConfig.max_tokens
    ↓
backend/main.py → "max_tokens": config.inference.max_tokens
    ↓
frontend API ConfigResponse.inference.max_tokens
    ↓
frontend page.tsx → serverDefaults.maxTokens
    ↓
前端发送 → maxTokens (JS变量)
    ↓
后端API ChatCompletionRequest.max_tokens
    ↓
chat_async(max_new_tokens=request.max_tokens)
    ↓
LLMService.chat(max_new_tokens=...)
    ↓
SGLangProvider.generate(max_new_tokens=...)
    ↓
SGLang --max-new-tokens (命令行参数)

# 兼容性

config采用OpenAI API标准: 使用 max_tokens
SGLang特定: 内部映射到 --max-new-tokens
前端JS: 保持 maxTokens 驼峰命名