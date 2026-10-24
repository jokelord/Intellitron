from backend.agent.tool_configuration import AgentToolConfigurationRequest
from backend.cycle.models import CycleStartRequest


def test_cycle_carries_optional_candidate_but_non_agent_mode_remains_valid():
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision="catalog-revision",
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    request = CycleStartRequest(
        steps=[],
        agent_mode=False,
        share_context=False,
        tool_configuration=candidate,
    )
    assert request.tool_configuration == candidate
    assert request.agent_mode is False

