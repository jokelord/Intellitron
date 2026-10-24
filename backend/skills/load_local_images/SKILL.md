---
name: load-local-images
description: Loads local image files from disk so the VLM can directly see and analyze them. Use when the user provides local image paths and asks to inspect or analyze images.
---

# 加载本地图片

## 适用场景

当用户提供本地图片路径并要求查看或分析图片时使用。

## 调用

通过 `exec_command` 直接执行，不套用 Bash 包装器：

```bash
python3 {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "<逗号分隔的文件路径>"
```

使用当前会话的 `{{SESSION_ID}}`。`--files` 是一个加引号的参数，内部用逗号分隔路径，例如 `"/tmp/photo1.jpg, ~/images/diagram.png"`。

可选 `--max-dim <像素>` 设置图片最大边长并保持比例；省略时使用已配置的默认尺寸，`0` 保留原始尺寸。

## 限制

- 单次最多 9 张图片
- 支持格式: png, jpg, jpeg, gif, webp
- 单文件不超过 20 MiB，且路径必须在允许读取图片的范围内
- 成功加载的图片会在下一步推理中自动可见，无需读取或复制 base64/JSON
- 检查返回的成功张数和每个路径的结果；部分加载成功不代表所有图片都可见
