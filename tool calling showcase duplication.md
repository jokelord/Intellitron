## Problem Discription

the "Calling tools:[...]" sometimes repeat twice or 3 times in the "思考" box (output of LLM) as is shown below:
"""
#1 思考
我注意到系统中没有找到`google-chrome`命令。让我尝试使用其他可能的Chrome启动命令。

动作:
```json
{
    "name": "agent_exec_command",
    "arguments": {
        "command": "nohup chromium-browser &"
    }
}
```
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'nohup chromium-browser &'}}}]
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'nohup chromium-browser &'}}}]

#2 工具调用
agent_exec_command({
  "command": "nohup chromium-browser &"
})

#3 观察结果
命令执行超时 (60 秒): nohup chromium-browser &

#4 思考
我注意到`chromium-browser`命令执行超时了。让我检查系统中是否有可用的浏览器，以及系统是否支持图形界面。

动作:
```json
{
    "name": "agent_exec_command",
    "arguments": {
        "command": "which chrome || which google-chrome || which chromium-browser"
    }
}
```
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'which chrome || which google-chrome || which chromium-browser'}}}]
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'which chrome || which google-chrome || which chromium-browser'}}}]

#5 工具调用
agent_exec_command({
  "command": "which chrome || which google-chrome || which chromium-browser"
})

#6 观察结果
/usr/bin/chromium-browser

#7 思考
现在我确认系统中存在`chromium-browser`命令。让我尝试启动它。

动作:
```json
{
    "name": "agent_exec_command",
    "arguments": {
        "command": "nohup chromium-browser &"
    }
}
```
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'nohup chromium-browser &'}}}]
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'nohup chromium-browser &'}}}]
Calling tools:
[{'id': 'call_0', 'type': 'function', 'function': {'name': 'agent_exec_command', 'arguments': {'command': 'nohup chromium-browser &'}}}]
"""

## Analysis by otherAI:
the repeated "思考" (thought) followed by "calling tool:" pattern occurs due to a potential feedback loop in the agent's reasoning process. Here's the root cause:
Root Cause Analysis
The issue stems from how the agent handles tool call history in its conversation context:
1. History Format: When smolagents serializes conversation history for the model, it includes past tool calls in the format: "Calling tools:[{'id': 'call_0', 'type': 'function', ...}]"
2. Model Behavior: The model sometimes sees this historical text in its input and, due to pattern completion tendencies, outputs similar "Calling tools:[...]" text as part of its response.
3. Parsing Logic: The LocalLLMModel._parse_tool_calls_from_text() method has multiple parsing strategies, including one specifically for the "Calling tools:" history format (lines 753-787 in local_llm_model.py). When it detects this format in the model's output, it parses it as a new tool call.
4. Feedback Loop: This creates a cycle:
   - Agent executes a tool → gets recorded in history
   - Next model input includes "Calling tools:..." from history
   - Model outputs similar text → parsed as new tool call
   - Agent executes tool again → history grows
   - Repeat
Evidence from Code
- In agent_runner.py lines 360-371: Comments mention "Calling tools:\n[{'id': 'auto_final', ...}]" being放入对话历史后模型在后续轮次会直接复现这段文本
- In local_llm_model.py lines 753-787: Specific parsing logic for "Calling tools:" format that returns ChatMessageToolCall objects
- In agent_runner.py lines 277-299: Tool call parsing logic that checks content, reasoning, and full aggregated text for tool calls
Why It's Intermittent
The behavior depends on:
- Whether the model's output contains detectable "Calling tools:" text
- The specific parsing path taken (content vs reasoning vs full text)
- The agent's internal state and reasoning trajectory
- Randomness in model output generation
The "思考" Events
The repeated thinking events come from:
- Genuine agent reasoning (streamed as thinking tokens)
- Possibly the reasoning portion of any parsed "Calling tools:" text that gets streamed during generation
This explains the observed pattern: thinking → tool call → thinking → tool call → thinking → tool call, representing multiple iterations of the feedback loop.