import logging

from backend.agent import agent_runner, local_llm_model, orchestrator


def test_agent_module_loggers_are_configured_for_info_output():
    for module_logger in (
        agent_runner.logger,
        local_llm_model.logger,
        orchestrator.logger,
    ):
        assert module_logger.handlers
        assert module_logger.getEffectiveLevel() <= logging.INFO
