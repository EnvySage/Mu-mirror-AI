"""Carry request collection explicitly across LangGraph executor boundaries."""
from functools import wraps

from langgraph.runtime import Runtime

from agent_core.types import AgentState, RuntimeContext
from mirror_core.usage import bind_collector


def usage_node(node):
    @wraps(node)
    def invoke(state: AgentState, runtime: Runtime[RuntimeContext]):
        with bind_collector(runtime.context.usage_collector):
            return node(state, runtime)
    return invoke
