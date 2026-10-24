"""Native transcript -> resolver -> production execution/memory regressions.

Only the external child worker is replaced. Schema resolution, dispatch
admission, orchestration, timeline, and canonical result serialization are real.
"""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest
from smolagents.memory import ActionStep
from smolagents.models import ChatMessage
from smolagents.monitoring import Timing
from smolagents.tools import Tool

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.agent.text_tool_protocol import TextToolProtocolError
from backend.config import get_config
from backend.security.capabilities import build_session_capability
from backend.session_hub_runtime import SessionHubRuntime


class LiteralRead(Tool):
    name = 'literal_read'
    description = 'Return the original named file contents.'
    inputs = {'file_path': {'type': 'string', 'description': 'The exact file path.'}}
    output_type = 'string'

    def __init__(self, contents):
        super().__init__()
        self.contents = contents
        self.reads = []
        self.lock = threading.Lock()

    def forward(self, file_path: str) -> str:
        with self.lock:
            self.reads.append(file_path)
        return self.contents[file_path]


@pytest.fixture
def native_manager(tmp_path):
    config = deepcopy(get_config())
    config.model.model_type = 'text'
    config.agent.entry_agent_name = 'manager_agent'
    config.agent.enabled_agent_profiles = ['manager_agent', 'basic_agent']
    config.agent.manager_agent_max_parallel_children = 2
    config.agent.manager_agent_child_event_buffer_limit = 5000
    config.agent.session_temp_base_dir = str(tmp_path / 'sessions')
    config.inference.tool_protocol = 'qwen35-native'
    # No inference is performed: the input is an already captured transcript.
    runner = AgentRunner(SimpleNamespace(), config, session_hub_runtime=SessionHubRuntime())
    session = AgentSession(session_id='native-regression-manager', agent=None, mode='tool_calling',
        capability=build_session_capability(config, session_id='native-regression-manager', mode='tool_calling'),
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
        current_agent_name='manager_agent', tool_protocol='qwen35-native')
    runner.sessions.put(session)
    agent = runner._build_profile_agent('manager_agent', session=session)
    session.agent = agent
    starts = []

    async def child_worker(child_run, child_session):
        starts.append(child_run.child_name)
        return 'report for ' + child_run.child_name

    runner._run_child_agent_to_final_report = child_worker
    return runner, session, agent, starts


def block(name, arguments):
    # Hand-built fixture, independent of the production codec.
    return '<tool_call>\n<function=' + name + '>\n' + ''.join(
        '<parameter=' + key + '>\n' + value + '\n</parameter>\n'
        for key, value in arguments.items()) + '</function>\n</tool_call>'


def dispatch(child_id):
    return block('dispatching_parallel_agent', {'child_id': child_id,
        'task': 'Inspect independent source for ' + child_id + ' and report all findings.'})


def resolve_and_execute(agent, transcript):
    resolution = agent.model._resolve_assistant_control(transcript, None, agent.tools_and_managed_agents)
    assert resolution.source == 'native'
    message = ChatMessage(role='assistant', content=transcript, tool_calls=list(resolution.tool_calls))
    step = ActionStep(step_number=1, timing=Timing(start_time=0., end_time=.1),
        model_output=transcript, model_output_message=message)
    # This is the same production retention operation performed by _step_stream
    # immediately after resolving accepted calls, before process_tool_calls.
    agent._retain_native_output(step, transcript)
    events = list(agent.process_tool_calls(message, step))
    agent.memory.steps.append(step)
    return resolution, step, events


def tool_messages(agent):
    return [message for message in agent.write_memory_to_messages() if message.role == 'tool']


def one_stage(events):
    plans = [event for event in events if isinstance(event, dict) and event.get('type') == 'orchestration_plan']
    assert len(plans) == 1
    assert len(plans[0]['stages']) == 1
    return plans[0]['stages'][0]


def test_native_same_name_tool_results_keep_ids_original_whitespace_and_order(native_manager):
    runner, session, agent, starts = native_manager
    first = ' \n    def f():\n        return True\n\n '
    second = '\t  second file\r\n  '
    reader = LiteralRead({'/tmp/first': first, '/tmp/second': second})
    agent.tools[reader.name] = reader
    transcript = block(reader.name, {'file_path': '/tmp/first'}) + '\n' + block(reader.name, {'file_path': '/tmp/second'})
    resolution, step, events = resolve_and_execute(agent, transcript)
    ids = [call.id for call in resolution.tool_calls]
    assert len(set(ids)) == 2
    stage = one_stage(events)
    assert stage['parallel'] is True
    assert [item['call_id'] for item in stage['tools']] == ids
    assert sorted(reader.reads) == ['/tmp/first', '/tmp/second']
    messages = agent.write_memory_to_messages()
    assistant = next(message for message in messages if message.role == 'assistant')
    assert [call['id'] for call in assistant.tool_calls] == ids
    assert assistant.content == ''
    results = tool_messages(agent)
    assert [(m.tool_call_id, m.name, m.content) for m in results] == [
        (ids[0], 'literal_read', first), (ids[1], 'literal_read', second)]


@pytest.mark.parametrize('bad_tail', [
    '<tool_call><function=unknown></function></tool_call>',
    '<tool_call><function=literal_read><parameter=file_path>x',
    block('literal_read', {'file_path': '/tmp/second', 'extra': 'unsupported'}),
])
def test_native_invalid_later_batch_executes_no_earlier_tool_or_child(native_manager, bad_tail):
    runner, session, agent, starts = native_manager
    reader = LiteralRead({'/tmp/first': 'must never be read'})
    agent.tools[reader.name] = reader
    transcript = block(reader.name, {'file_path': '/tmp/first'}) + '\n' + dispatch('valid-child') + '\n' + bad_tail
    with pytest.raises(TextToolProtocolError):
        resolve_and_execute(agent, transcript)
    assert reader.reads == []
    assert starts == []
    assert runner.child_agent_runs == {}


def test_native_last_dispatch_skipped_still_carries_one_ordered_shared_blackboard(native_manager):
    runner, session, agent, starts = native_manager
    transcript = '\n'.join(dispatch(child) for child in ('child-one', 'child-two', 'child-three'))
    resolution, step, events = resolve_and_execute(agent, transcript)
    ids = [call.id for call in resolution.tool_calls]
    stage = one_stage(events)
    assert [item['call_id'] for item in stage['tools']] == ids[:2]
    assert set(starts) == {'child-one', 'child-two'}
    results = tool_messages(agent)
    assert [message.tool_call_id for message in results] == ids
    assert [result.status for result in step.canonical_tool_results] == ['completed', 'completed', 'skipped']
    for message in results[:2]:
        assert 'Shared results: see tool response ' + ids[2] in message.content
        assert 'Final report:' not in message.content
    combined = '\n'.join(message.content for message in results)
    assert combined.count('Shared child-agent blackboard for this turn.') == 1
    assert combined.count('report for child-one') == 1
    assert combined.count('report for child-two') == 1
    aggregate = results[-1].content
    assert aggregate.index('Child: child-one') < aggregate.index('Child: child-two') < aggregate.index('Child: child-three')
    assert 'Status: skipped' in aggregate
    assert 'Not executed: ' + ids[2] + '/child-three' in aggregate
    running_ids = {event['call_id'] for event in session.timeline_events
        if event.get('kind') == 'manager_child_agent_update' and event.get('status') == 'running'}
    assert running_ids == set(ids[:2])


def test_native_duplicate_child_ids_start_zero_children_before_execution(native_manager):
    runner, session, agent, starts = native_manager
    resolution, step, events = resolve_and_execute(agent, dispatch('same-child') + '\n' + dispatch('same-child'))
    assert starts == []
    assert runner.child_agent_runs == {}
    assert [record.status for record in step.canonical_tool_results] == ['failed', 'failed']
    aggregate = tool_messages(agent)[-1].content
    assert aggregate.count('duplicate child_id') == 2
    assert [call.id for call in step.tool_calls] == [call.id for call in resolution.tool_calls]


def test_native_failed_early_child_receipt_records_actual_failure_without_losing_sibling(native_manager):
    runner, session, agent, starts = native_manager

    async def child_worker(child_run, child_session):
        starts.append(child_run.child_name)
        if child_run.child_name == 'bad-child':
            raise RuntimeError('child worker exploded')
        return 'successful sibling report'

    runner._run_child_agent_to_final_report = child_worker
    resolution, step, events = resolve_and_execute(agent, dispatch('bad-child') + '\n' + dispatch('good-child'))
    ids = [call.id for call in resolution.tool_calls]
    assert set(starts) == {'bad-child', 'good-child'}
    assert [record.status for record in step.canonical_tool_results] == ['failed', 'completed']
    results = tool_messages(agent)
    assert results[0].tool_call_id == ids[0]
    assert 'Status: failed' in results[0].content
    assert 'Shared results: see tool response ' + ids[1] in results[0].content
    combined = '\n'.join(result.content for result in results)
    assert combined.count('Shared child-agent blackboard for this turn.') == 1
    assert combined.count('successful sibling report') == 1
    assert 'child worker exploded' in results[-1].content
    done = next(event for event in events if isinstance(event, dict) and event.get('type') == 'orchestration_done')
    assert done['failed'] == 1 and done['completed'] == 1
