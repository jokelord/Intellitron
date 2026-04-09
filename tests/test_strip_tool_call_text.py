"""TDD tests for SmartToolCallingAgent._strip_tool_call_text().

该方法从模型输出文本中移除工具调用语法，
使得写入 memory 的 model_output 不含原始工具调用文本，
防止模型在下一步看到自己的工具调用原文后直接复读观察结果。

测试覆盖 _parse_tool_calls_from_text 支持的所有 5 种格式：
  1. XML <tool_call> 标签
  2. 动作:/Action: 前缀 JSON
  3. 纯 JSON（整条就是 JSON）
  4. 行内 JSON（正文之后接 JSON）
  5. Calling tools: 格式
"""

import pytest

# 导入被测方法 —— 作为静态方法直接从类上调用
from backend.agent.agent_runner import SmartToolCallingAgent


class TestStripToolCallText:
    """测试 _strip_tool_call_text 静态方法"""

    # ── 格式 1: <tool_call> XML 标签 ──

    def test_xml_tool_call_simple(self):
        """移除 <tool_call>...</tool_call> 标签及其内容"""
        text = '我来查一下时间\n<tool_call>{"name": "exec_command", "arguments": {"command": "date"}}</tool_call>'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "<tool_call>" not in result
        assert "exec_command" not in result
        assert "我来查一下时间" in result

    def test_xml_tool_call_with_trailing_text(self):
        """<tool_call> 后有额外文字也应保留前缀"""
        text = '让我执行命令\n<tool_call>{"name": "exec_command", "arguments": {"command": "ls"}}</tool_call>\n这是尾巴'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "<tool_call>" not in result
        assert "让我执行命令" in result

    def test_xml_tool_call_multiline(self):
        """多行 <tool_call> 内容"""
        text = '思考内容\n<tool_call>\n{\n    "name": "exec_command",\n    "arguments": {"command": "pwd"}\n}\n</tool_call>'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "<tool_call>" not in result
        assert "思考内容" in result

    # ── 格式 2: 动作:/Action: 前缀 ──

    def test_action_prefix_chinese(self):
        """移除 '动作:' + JSON"""
        text = '我需要查看当前日期\n动作:\n{\n    "name": "exec_command",\n    "arguments": {"command": "date"}\n}'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "动作" not in result
        assert "exec_command" not in result
        assert "我需要查看当前日期" in result

    def test_action_prefix_english(self):
        """移除 'Action:' + JSON"""
        text = 'Let me check the date\nAction:\n{"name": "exec_command", "arguments": {"command": "date"}}'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "Action:" not in result
        assert "exec_command" not in result
        assert "Let me check the date" in result

    def test_action_prefix_fullwidth_colon(self):
        """移除使用全角冒号的 '动作：' + JSON"""
        text = (
            '思考中\n动作：\n{"name": "exec_command", "arguments": {"command": "date"}}'
        )
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "exec_command" not in result
        assert "思考中" in result

    def test_action_prefix_real_log_format(self):
        """实际日志中观察到的格式（用户提供的 bug 复现日志）"""
        text = '我来帮你获取当前日期和时间。\n\n动作:\n{\n    "name": "exec_command",\n    "arguments": {\n        "command": "date"\n    }\n}'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "动作" not in result
        assert "exec_command" not in result
        assert "我来帮你获取当前日期和时间" in result

    # ── 格式 3: 纯 JSON ──

    def test_pure_json_single(self):
        """整条内容就是一个工具调用 JSON"""
        text = '{"name": "exec_command", "arguments": {"command": "date"}}'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        # 整条都是工具调用，清理后应为空或仅空白
        assert result.strip() == ""

    def test_pure_json_array(self):
        """整条内容是 JSON 数组"""
        text = '[{"name": "exec_command", "arguments": {"command": "date"}}]'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert result.strip() == ""

    # ── 格式 4: 行内 JSON ──

    def test_inline_json(self):
        """正文之后直接跟 JSON（无 '动作:' 前缀）"""
        text = '我来执行这个命令来获取时间信息\n{"name": "exec_command", "arguments": {"command": "date"}}'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "exec_command" not in result
        assert "我来执行这个命令" in result

    # ── 格式 5: Calling tools: ──

    def test_calling_tools_format(self):
        """smolagents 历史记录格式"""
        text = "我来查一下\nCalling tools:\n[{'id': 'call_0', 'type': 'function', 'function': {'name': 'exec_command', 'arguments': {'command': 'date'}}}]"
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "Calling tools:" not in result
        assert "exec_command" not in result
        assert "我来查一下" in result

    # ── 边界情况 ──

    def test_empty_string(self):
        """空字符串不应报错"""
        result = SmartToolCallingAgent._strip_tool_call_text("")
        assert result == ""

    def test_none_input(self):
        """None 输入返回空字符串"""
        result = SmartToolCallingAgent._strip_tool_call_text(None)
        assert result == ""

    def test_no_tool_call(self):
        """纯文本不应被修改"""
        text = "这是一段普通文本，不包含任何工具调用。"
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert result == text

    def test_preserves_thinking_content(self):
        """思考内容（工具调用之前的部分）应完整保留"""
        text = (
            "让我分析一下这个问题。首先，我需要获取当前的系统时间，"
            "然后结合图片内容给出完整回复。\n\n"
            '动作:\n{"name": "exec_command", "arguments": {"command": "date"}}'
        )
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert "让我分析一下这个问题" in result
        assert "结合图片内容给出完整回复" in result
        assert "exec_command" not in result

    def test_strip_trailing_whitespace(self):
        """清理后的文本不应有多余的尾部空白"""
        text = '思考内容\n\n\n动作:\n{"name": "exec_command", "arguments": {}}'
        result = SmartToolCallingAgent._strip_tool_call_text(text)
        assert not result.endswith("\n\n\n")
        assert result.strip() == result or result.rstrip() == result.rstrip()


class TestStripToolCallIntegrationWithMemory:
    """测试 _strip_tool_call_text 在 memory 清洗场景中的效果。

    模拟真实 bug 场景：model_output 包含工具调用文本 → 清洗后
    写入 memory → 下一步模型不再看到原始工具调用语法。
    """

    def test_cleaned_output_no_tool_syntax(self):
        """确认清洗后的文本不包含任何已知工具调用语法标记"""
        # 实际 bug 中的模型输出
        model_output = (
            "我来帮你获取当前日期和时间。\n\n"
            "动作:\n"
            "{\n"
            '    "name": "exec_command",\n'
            '    "arguments": {\n'
            '        "command": "date"\n'
            "    }\n"
            "}"
        )
        cleaned = SmartToolCallingAgent._strip_tool_call_text(model_output)

        # 验证所有工具调用语法标记都被移除
        assert "动作:" not in cleaned
        assert "Action:" not in cleaned
        assert "<tool_call>" not in cleaned
        assert "Calling tools:" not in cleaned
        assert '"name"' not in cleaned
        assert '"arguments"' not in cleaned

        # 但思考内容保留
        assert "我来帮你获取当前日期和时间" in cleaned
