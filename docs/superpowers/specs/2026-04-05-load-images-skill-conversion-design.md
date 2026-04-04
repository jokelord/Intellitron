# load_images 工具转 Skill 设计文档

日期: 2026-04-05

## 背景

`load_images` 当前是注册在 `_get_tools()` 中的 smolagents 工具，用于让 VLM agent 加载本地磁盘图片到对话上下文。将其从工具列表中移除，转为 skill（标准作业程序文档），agent 通过 `python_interpreter` 工具执行等效代码来完成图片加载。

## 目标

- 减少工具列表中的专用工具数量，降低 LLM token 开销
- 让 agent 通过 skill + `python_interpreter` 组合完成相同任务
- 保持 `store_tool_images → get_and_clear_tool_images` 内存缓冲机制不变
- 保持 `write_memory_to_messages()` 中的图片注入逻辑不变

## 设计

### 1. 创建 skill 文件

路径: `backend/skills/load_local_images/`

**metadata.yaml**:
```yaml
name: load_local_images
description: "加载本地磁盘图片文件，使 VLM 能在对话中直接看到并分析图片内容"
tags:
  - image
  - vlm
  - file-loading
```

**instructions.md** — SOP 指导 agent 使用 `python_interpreter` 调用 `load_images()`:
```markdown
# 加载本地图片

## 适用场景
当用户要求查看或分析本地磁盘上的图片文件时，使用本技能。

## 操作步骤
使用 python_interpreter 工具执行以下代码：
from backend.tools.image_ops import load_images
result = load_images(file_paths="<逗号分隔的文件路径>")
print(result)

## 参数说明
- file_paths: 逗号分隔的文件路径，支持 ~、环境变量。例如 "/tmp/a.png, ~/b.jpg"
- max_dimension: 可选，图片最大边长像素，默认 1280
- session_id: 无需传入，函数自动从上下文获取

## 限制
- 单次最多 9 张图片
- 支持格式: png, jpg, jpeg, gif, webp
- 单文件不超过 20MB
- 加载成功后，图片会在下一步推理中自动出现在对话上下文中
```

### 2. 配置变更

**config/config-misc.yaml** — `agent.python_authorized_imports` 列表添加:
```yaml
- backend
```
添加 `backend` 顶层模块即可，这样 `from backend.tools.image_ops import load_images` 能正常工作。

### 3. 代码变更

**backend/agent/agent_runner.py** — `_get_tools()` 函数:
- 删除 line 552: `load_images_tool = _create_tool_wrapper(load_images, "load_images")`
- 删除 line 565: `load_images_tool,`（从 tools 列表中移除）
- 保留 line 140 的 import: `from backend.tools.image_ops import load_images, get_and_clear_tool_images`
  - `get_and_clear_tool_images` 仍被 `write_memory_to_messages()` 使用
  - `load_images` 的 import 不再被本文件直接使用，可以从 import 行中移除

**config/config-agent-prompts.yaml** — `vlm_instructions`:
- 移除第 3 行中关于 `load_images` 工具的引用
- 改为引导 agent 使用 skill 系统

### 4. session_id 获取机制

`python_interpreter` 在与 agent 相同的线程中执行。`agent_runner.py:1360` 在 `agent.run()` 之前调用 `current_session_context.set(session_id)`。因此 `load_images()` 的 ContextVar 回退（`image_ops.py:70-76`）会正确获取到 session_id，无需显式传参。

### 5. 保持不变的部分

- `backend/tools/image_ops.py` — 整个文件保持不变
- `write_memory_to_messages()` 中的图片注入逻辑（`agent_runner.py:681-703`）
- `store_tool_images` / `get_and_clear_tool_images` 缓冲区机制
- 现有的 `test_image_ops.py` 中 18 个测试（测试的是 `load_images` 函数本身，不涉及工具注册）

### 6. 测试计划

- 验证 66 个现有测试全部通过（移除工具注册不影响单元测试）
- 新增测试: 验证 `backend` 模块在 `python_authorized_imports` 中时，`python_interpreter` 可以成功 `from backend.tools.image_ops import load_images`
- 新增测试: 验证 `_get_tools()` 返回的工具列表中不再包含 `load_images`
