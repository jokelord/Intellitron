# 参数值传递链条

## 推理参数

config [inference] max_tokens = 131072
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

## 视觉模型配置

config.ini [model] model_type = vision
    ↓
ConfigLoader._parse_model_config() → ModelConfig.model_type
    ↓
backend/api/chat.py → get_config_endpoint()
    ↓
frontend API ConfigResponse.model.model_type
    ↓
frontend page.tsx → 检测 model_type
    ↓
如果是 vision → router.push('/vision')
    ↓
vision/page.tsx → 获取视觉配置并渲染界面

# 配置项说明

## [model] 段

| 配置项 | 类型 | 说明 |
|--------|------|------|
| model_type | string | 模型类型: text 或 vision |
| max_image_size | int | 最大图片大小 (字节) |
| supported_formats | string | 支持的图片格式 (逗号分隔) |
| max_image_tokens | int | 最大图片 token 数 |

## 使用示例

```ini
[model]
model_type = vision
max_image_size = 10485760
supported_formats = png,jpg,jpeg,gif,webp
max_image_tokens = 8192
```