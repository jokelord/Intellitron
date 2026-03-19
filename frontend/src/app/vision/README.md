# 视觉聊天页面

本目录包含视觉聊天功能的 Next.js 页面。

## 文件结构

```
vision/
└── page.tsx    # 视觉聊天主页面
```

## 功能特性

1. **多模态对话**: 支持同时发送文本和图片
2. **图片上传**: 支持从本地选择图片上传
3. **摄像头拍照**: 支持实时摄像头预览和拍照
4. **多图支持**: 最多支持 9 张图片同时上传
5. **流式响应**: 支持模型回复的流式输出
6. **推理过程展示**: 支持显示模型的推理过程
7. **自动模式切换**: 根据后端配置自动判断是否启用视觉模式

## 使用方式

### 1. 手动访问
直接访问 `/vision` 路径即可进入视觉聊天页面。

### 2. 自动跳转
当 `config.ini` 中 `model_type` 设置为 `vision` 时:
- 访问根路径 `/` 会自动重定向到 `/vision`

## 配置说明

视觉模式相关配置在 `config/config.ini` 的 `[model]` 段:

```ini
[model]
# 模型类型: text | vision
model_type = vision

# 最大图片大小 (字节)
max_image_size = 10485760

# 支持的图片格式
supported_formats = png,jpg,jpeg,gif,webp

# 最大图片 token 数
max_image_tokens = 8192
```

## API 消息格式

视觉聊天使用 OpenAI 兼容的多模态消息格式:

```json
{
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
      ]
    }
  ]
}
```

## 依赖

- Next.js 14+
- React 18+
- TypeScript
- Tailwind CSS
- Lucide React (图标)
- react-markdown (Markdown 渲染)

## 错误处理

页面包含以下错误处理机制:

1. **连接失败**: 显示连接状态和重试选项
2. **API 错误**: 在消息中显示错误信息
3. **图片验证失败**: 显示具体的错误原因（大小超限、格式不支持等）
4. **摄像头错误**: 提示权限被拒绝或设备未找到
