"""Committed historical tool-text projections, separate from UI/tool results.

Only ordinary top-level user admission calls commit_historical_tool_feedback.
Serialization reads the projection without mutating memory or eligibility.
"""
from __future__ import annotations

from dataclasses import replace

from smolagents.memory import ActionStep

PLACEHOLDER = '[Tool feedback omitted by configuration; this placeholder is not a summary.]'
BOUNDARY_REASON = 'tool_feedback_historical_text_omission'


def projected_observations(step):
    return getattr(step, '_tool_feedback_observations', step.observations)


def projected_tool_results(step):
    return getattr(step, '_tool_feedback_canonical_results', getattr(step, 'canonical_tool_results', None))


def _omit_content(content, threshold):
    if isinstance(content, str):
        return PLACEHOLDER if len(content) > threshold else None
    if not isinstance(content, list):
        return None
    text_parts = [part for part in content if isinstance(part, dict) and part.get('type') == 'text']
    if len(text_parts) == 1 and text_parts[0].get('text') == PLACEHOLDER:
        return None
    if sum(len(str(part.get('text', ''))) for part in text_parts) <= threshold:
        return None
    result = []
    inserted = False
    for part in content:
        if isinstance(part, dict) and part.get('type') == 'text':
            if not inserted:
                result.append({**part, 'text': PLACEHOLDER})
                inserted = True
        else:
            # Non-text parts retain their objects/provenance and relative order.
            result.append(part)
    return result


def commit_historical_tool_feedback(session, settings) -> int:
    """Commit eligible completed tool results already present at user admission.

    Keep raw feedback for UI/timeline consumers. The projection is irrevocable
    even if the setting is subsequently disabled. Errors/parser feedback and
    child contexts are excluded. No traversal of other sessions occurs.
    """
    if not getattr(settings, 'enabled', False) or getattr(session, 'is_child_context', False):
        return 0
    threshold = settings.threshold_chars
    native = getattr(session, 'tool_protocol', 'legacy_text') == 'qwen35-native'
    replacements = []
    count = 0
    for step in session.agent.memory.steps:
        if not isinstance(step, ActionStep) or not step.tool_calls or step.error is not None:
            continue
        records = projected_tool_results(step) if native else None
        if records is not None:
            projected = []
            changed = False
            for record in records:
                replacement = (_omit_content(record.content, threshold)
                               if record.status == 'completed' and record.content != PLACEHOLDER
                               and record.call_id not in getattr(step, '_tool_feedback_error_result_ids', ())
                               else None)
                if replacement is not None:
                    projected.append(replace(record, content=replacement))
                    changed = True
                    count += 1
                else:
                    projected.append(record)
            if changed:
                replacements.append((step, '_tool_feedback_canonical_results', projected))
        else:
            # Legacy preserves its combined block. Native parent single-tool
            # execution also keeps its exact observation in this field.
            if getattr(step, '_tool_feedback_contains_unsuccessful_result', False):
                continue
            observation = projected_observations(step)
            if isinstance(observation, str) and observation != PLACEHOLDER and len(observation) > threshold:
                replacements.append((step, '_tool_feedback_observations', PLACEHOLDER))
                count += 1
    for step, attribute, value in replacements:
        setattr(step, attribute, value)
    if count:
        # Snapshots contain the old complete feedback. Reconstruct from projected
        # canonical memory rather than resurrecting it during auto/manual compact.
        session.last_compactable_llm_messages = None
        session.last_compactable_llm_tools = None
        session.last_compactable_memory_steps = None
        session.last_compactable_agent_name = None
        boundaries = list(session.behavior_records.get('prefix_boundaries') or ())
        boundaries.append({'reason': BOUNDARY_REASON, 'omitted_result_count': count})
        session.behavior_records['prefix_boundaries'] = boundaries[-32:]
    return count
