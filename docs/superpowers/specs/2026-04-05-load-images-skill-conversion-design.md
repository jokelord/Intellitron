# load_images 工具转 Skill 设计文档 (v2)

日期: 2026-04-05
版本: v2 — 完全重写，基于 exec_command + 独立脚本 + 文件 IPC 架构

## 背景

`load_images` 当前是注册在 `_get_tools()` 中的 smolagents 工具，用于让 VLM agent 加载本地磁盘图片到对话上下文。它使用进程内内存缓冲区 (`store_tool_images` / `get_and_clear_tool_images`) 传递 PIL 图片对象。

**目标**: 将 `load_images` 从工具列表中**彻底移除**，转为 skill（标准作业程序），agent 通过 `exec_command` 工具调用独立 Python 脚本完成图片加载。同时移除整个 `image_ops.py` 模块，不再保留进程内内存缓冲区。

## 关键设计决策

1. **不使用 `python_interpreter`**，使用 `exec_command`（bash 子进程）执行独立脚本
2. **彻底删除 `backend/tools/image_ops.py`** — 不保留任何函数
3. **独立脚本** 放在 `backend/skills/load_local_images/load_local_images.py`，与 `collect_texts_to_csv` 技能模式一致
4. **文件 IPC**: 脚本输出 JSON 到 `/tmp/agent_images/<session_id>/`，`write_memory_to_messages()` 从文件读取
5. **内存安全**: 子进程退出后全部内存释放；JSON 文件在 `write_memory_to_messages()` 消费后立即删除

## 架构

### 数据流

```
Agent 调用 exec_command
    ↓
load_local_images.py 子进程启动
    ↓  读取图片文件 → 缩放/色彩转换 → base64 JPEG 编码
    ↓  写 JSON 到 /tmp/agent_images/<session_id>/<uuid>.json
    ↓  stdout 输出人类可读摘要 → exec_command 返回给 agent
    ↓
子进程退出（内存完全释放）
    ↓
Agent 下一步推理前，write_memory_to_messages() 触发
    ↓  扫描 /tmp/agent_images/<session_id>/*.json
    ↓  读取 base64 → 构造 PIL Image（带 _pre_encoded_b64 属性）
    ↓  注入为 USER 消息中的 image content
    ↓  删除已消费的 JSON 文件
    ↓
VLM 在下一步推理中看到图片
```

### 会话 ID 传递: `{{SESSION_ID}}` 模板变量

**问题**: `exec_command` 启动子进程，子进程无法访问父进程的 `current_session_context`。

**方案**: 在 `read_skill()` 函数中增加 `{{SESSION_ID}}` 占位符替换（类似已有的 `{{SKILL_DIR}}`）。`read_skill()` 从 `current_session_context.get()` 获取当前 session_id 并替换到 instructions.md 内容中。agent 读取 SOP 后，在执行 `exec_command` 时将 session_id 作为命令行参数传入脚本。

## 组件详细设计

### 1. 独立脚本: `backend/skills/load_local_images/load_local_images.py`

**CLI 接口**:
```bash
.sglang/bin/python <SKILL_DIR>/load_local_images.py --session-id <id> --files "<逗号分隔路径>" [--max-dim 1280]
```

**功能**:
- 解析逗号分隔的文件路径，展开 `~` 和环境变量
- 校验: 文件存在性、扩展名（png/jpg/jpeg/gif/webp）、文件大小（≤20MB）、数量（≤9）
- 图片处理: PIL 打开 → 按 max_dimension 等比缩放 → RGBA/P/PA/LA→RGB 白色背景合成 → JPEG 编码
- 预编码优化: 未变换的 JPEG 直接读取原始字节跳过重编码
- 输出 JSON 到 `/tmp/agent_images/<session_id>/<uuid>.json`:
  ```json
  {
    "images": [
      {"mime": "image/jpeg", "base64": "<base64_data>"}
    ],
    "summary": "图片加载完成: 2/2 张成功\n[1] /tmp/a.png: 已加载 (800x600, RGB)\n..."
  }
  ```
- stdout 打印人类可读摘要（`exec_command` 捕获并返回给 agent）
- 退出码: 0=成功（至少 1 张加载成功），1=全部失败或参数错误

**关键**: 脚本中包含完整图片处理逻辑——从 `image_ops.py` 移植而来的缩放、色彩转换、base64 编码代码。脚本是独立的，只依赖标准库和 Pillow。

### 2. Skill 文件

**`backend/skills/load_local_images/metadata.yaml`**:
```yaml
name: load_local_images
description: "加载本地磁盘图片文件，使 VLM 能在对话中直接看到并分析图片内容"
tags:
  - image
  - vlm
  - file-loading
```

**`backend/skills/load_local_images/instructions.md`**:
```markdown
# 加载本地图片

## 适用场景
当用户要求查看或分析本地磁盘上的图片文件时，使用本技能。

## 操作步骤
本技能配备了现成的可执行脚本，**你不需要编写任何代码**，直接调用即可。

### 唯一步骤：调用 exec_command 执行脚本

\`\`\`bash
.sglang/bin/python {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "<逗号分隔的文件路径>"
\`\`\`

### 可选参数
- `--max-dim <数值>`: 图片最大边长像素，默认 1280

### 示例
\`\`\`bash
.sglang/bin/python {{SKILL_DIR}}/load_local_images.py --session-id {{SESSION_ID}} --files "/tmp/photo1.jpg, ~/images/diagram.png" --max-dim 1024
\`\`\`

## 限制
- 单次最多 9 张图片
- 支持格式: png, jpg, jpeg, gif, webp
- 单文件不超过 20MB
- 加载成功后，图片会在下一步推理中自动出现在对话上下文中
- **不要自己写代码实现此功能，直接调用上述脚本**
```

### 3. `read_skill()` 修改 (`backend/tools/skill_ops.py`)

在现有 `{{SKILL_DIR}}` 替换之后，增加 `{{SESSION_ID}}` 替换:
```python
# 将 {{SESSION_ID}} 占位符替换为当前会话 ID
try:
    from backend.agent.agent_runner import current_session_context
    session_id = current_session_context.get() or ""
    content = content.replace("{{SESSION_ID}}", session_id)
except Exception:
    pass
```

### 4. `write_memory_to_messages()` 修改 (`backend/agent/agent_runner.py:681-703`)

替换原有的 `get_and_clear_tool_images()` 调用为文件 IPC 读取:
```python
# ── 注入图片（从文件 IPC 读取，替代内存缓冲区） ──
session_id = current_session_context.get()
if session_id:
    images_dir = Path(f"/tmp/agent_images/{session_id}")
    if images_dir.exists():
        tool_images = []
        consumed_files = []
        for json_file in sorted(images_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                for img_data in data.get("images", []):
                    b64_str = img_data["base64"]
                    mime = img_data.get("mime", "image/jpeg")
                    raw_bytes = base64.b64decode(b64_str)
                    pil_img = PILImage.open(io.BytesIO(raw_bytes))
                    pil_img.load()
                    pil_img._pre_encoded_b64 = b64_str
                    pil_img._pre_encoded_mime = mime
                    tool_images.append(pil_img)
                consumed_files.append(json_file)
            except Exception as e:
                logger.warning(f"读取图片 JSON 失败: {json_file} -> {e}")
                consumed_files.append(json_file)  # 即使失败也删除，避免重复处理
        # 删除已消费的文件
        for f in consumed_files:
            try:
                f.unlink()
            except OSError:
                pass
        # 清理空目录
        try:
            if images_dir.exists() and not any(images_dir.iterdir()):
                images_dir.rmdir()
        except OSError:
            pass
        # 注入图片消息
        if tool_images:
            image_content = [{"type": "image", "image": img} for img in tool_images]
            image_content.append({
                "type": "text",
                "text": "[工具加载的图片已注入，请直接分析这些图片内容]",
            })
            messages.append(ChatMessage(role=MessageRole.USER, content=image_content))
            logger.info(f"write_memory_to_messages: 注入 {len(tool_images)} 张工具加载图片")
```

### 5. `_get_tools()` 修改

删除 `load_images_tool` 的创建和注册:
- 删除 line 552: `load_images_tool = _create_tool_wrapper(load_images, "load_images")`
- 删除 line 565: `load_images_tool,`（从 tools 列表中移除）

### 6. Import 清理

删除 `agent_runner.py` line 140:
```python
from backend.tools.image_ops import load_images, get_and_clear_tool_images
```

新增需要的 import（如果尚不存在）:
```python
import base64
import io
from PIL import Image as PILImage
```

### 7. 删除 `backend/tools/image_ops.py`

整个文件删除。`store_tool_images` / `get_and_clear_tool_images` 仅被 `load_images` 函数和 `write_memory_to_messages()` 使用，两者都被本次修改替换。

### 8. `vlm_instructions` 更新 (`config/config-agent-prompts.yaml`)

原 line 71:
> 当用户要求你查看或分析本地磁盘上的图片文件时，使用 load_images 工具加载这些图片，加载成功后你将在下一步直接看到图片内容并进行分析。

改为:
> 当用户要求你查看或分析本地磁盘上的图片文件时，先调用 list_skills 查找图片加载相关技能，然后调用 read_skill 阅读操作步骤并按指示执行。加载成功后你将在下一步直接看到图片内容并进行分析。

### 9. 会话清理

在 `SessionStore.delete()` 和 `SessionStore.cleanup_expired()` 中添加 `/tmp/agent_images/<session_id>/` 目录清理:
```python
def _cleanup_session_images(self, session_id: str):
    """清理会话的图片 IPC 文件"""
    images_dir = Path(f"/tmp/agent_images/{session_id}")
    if images_dir.exists():
        import shutil
        try:
            shutil.rmtree(images_dir)
            logger.info(f"已清理会话图片目录: {images_dir}")
        except OSError as e:
            logger.warning(f"清理会话图片目录失败: {images_dir} -> {e}")
```

在 `delete()` 和 `cleanup_expired()` 调用此方法。

### 10. 测试变更

**删除**: `tests/test_image_ops.py` 全部 18 个测试（测试已删除模块的函数）

**新增测试**:
1. **`tests/test_load_images_skill.py`** — 独立脚本测试:
   - 正常加载 JPEG/PNG 图片 → JSON 输出正确
   - 缩放逻辑（大图 → max_dim 缩放）
   - RGBA→RGB 白色背景合成
   - 文件不存在/格式不支持/文件过大 → 正确报错
   - 超过 9 张图片限制 → 报错
   - 无参数 → 非零退出码
   - JSON 文件写入到正确路径
   - stdout 输出人类可读摘要

2. **`tests/test_skill_session_id.py`** — `read_skill` `{{SESSION_ID}}` 测试:
   - 有 session_id 时正确替换
   - 无 session_id 时替换为空字符串

3. **`tests/test_write_memory_images.py`** — 文件 IPC 读取测试:
   - 存在 JSON 文件时正确注入图片消息
   - JSON 文件消费后被删除
   - 空目录被清理
   - 损坏的 JSON 文件不阻塞（warning + 删除）
   - 无 JSON 文件时无操作

4. **`tests/test_get_tools.py`** — 工具列表排除测试:
   - `_get_tools()` 返回的工具名称列表不包含 `load_images`

## 不受影响的部分

- **Cycle executor 的图片流**: 循环执行器通过 SSE 事件传递 PIL 图片到 `agent.run(images=...)`，不使用 `store_tool_images`/`get_and_clear_tool_images`，不受影响
- **前端**: 无变更
- **`config-misc.yaml`**: 无变更（`python_authorized_imports` 与本方案无关，因为使用的是 `exec_command`，不是 `python_interpreter`）
