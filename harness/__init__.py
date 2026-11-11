"""Generic Pluggable Agent Harness.

A domain-agnostic execution container powered by Google ADK.
"""

from .config import config
from .loader import agent_catalog
from .runner import get_agent_runner, run_prompt_stream
from .schema import AgentDefinition

__all__ = [
    "config",
    "agent_catalog",
    "get_agent_runner",
    "run_prompt_stream",
    "AgentDefinition",
]
