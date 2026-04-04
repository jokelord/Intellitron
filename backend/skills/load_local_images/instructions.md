# 加载本地图片

## 适用场景

当用户要求查看或分析本地磁盘上的图片文件时，使用本技能。

## 操作步骤

本技能配备了现成的可执行脚本，**你不需要编写任何代码**，直接调用即可。

### 唯一步骤：调用 exec_command 执行脚本

```bash
.sglang/bin/python {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "<逗号分隔的文件路径>"
```

### 可选参数

- `--max-dim <数值>`: 图片最大边长像素，默认 1280

### 示例

```bash
.sglang/bin/python {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "/tmp/photo1.jpg, ~/images/diagram.png" --max-dim 1024
```

## 限制

- 单次最多 9 张图片
- 支持格式: png, jpg, jpeg, gif, webp
- 单文件不超过 20MB
- 加载成功后，图片会在下一步推理中自动出现在对话上下文中
- **不要自己写代码实现此功能，直接调用上述脚本**
