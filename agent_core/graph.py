"""Compile the stateless graph_v2 workflow without a checkpointer."""

from langgraph.graph import END, START, StateGraph

from agent_core import nodes
from agent_core.runner import RECURSION_LIMIT
from agent_core.types import AgentState, RuntimeContext


def build_graph():
    """Build a fresh compiled graph; all per-run values arrive as invocation context."""
    graph = StateGraph(AgentState, context_schema=RuntimeContext)
    graph.add_node("prepare", nodes.prepare)
    graph.add_node("understand", nodes.understand)
    graph.add_node("retrieve", nodes.retrieve_evidence)
    graph.add_node("plan", nodes.plan)
    graph.add_node("act", nodes.act)
    graph.add_node("observe", nodes.observe)
    graph.add_node("set_done", nodes.set_done)
    graph.add_node("set_max_rounds", nodes.set_max_rounds)
    graph.add_node("set_loop_budget", nodes.set_loop_budget)
    graph.add_node("set_total_deadline", nodes.set_total_deadline)
    graph.add_node("backstop", nodes.backstop)
    graph.add_node("synthesize", nodes.synthesize)
    graph.add_node("fallback", nodes.fallback)
    graph.add_node("clarify", nodes.clarify)
    graph.add_node("create_record", nodes.create_record)
    graph.add_node("write_disabled", nodes.write_disabled)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "understand")
    graph.add_conditional_edges(
        "understand",
        nodes.route_action,
        {
            "retrieve": "retrieve",
            "synthesize": "synthesize",
            "clarify": "clarify",
            "write_disabled": "write_disabled",
            "create_record": "create_record",
        },
    )
    graph.add_edge("retrieve", "plan")
    graph.add_conditional_edges("plan", nodes.after_plan, {"act": "act", "backstop": "backstop"})
    graph.add_edge("act", "observe")
    graph.add_conditional_edges(
        "observe",
        nodes.after_observe,
        {
            "plan": "plan",
            "backstop": "backstop",
            "finalize": "finalize",
            "set_done": "set_done",
            "set_max_rounds": "set_max_rounds",
            "set_loop_budget": "set_loop_budget",
            "set_total_deadline": "set_total_deadline",
        },
    )
    for node_name in ("set_done", "set_max_rounds", "set_loop_budget"):
        graph.add_edge(node_name, "backstop")
    graph.add_edge("set_total_deadline", "finalize")
    graph.add_conditional_edges(
        "backstop",
        nodes.after_backstop,
        {"act": "act", "synthesize": "synthesize", "fallback": "fallback", "finalize": "finalize"},
    )
    for node_name in ("synthesize", "fallback", "clarify", "write_disabled", "create_record"):
        graph.add_edge(node_name, "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


__all__ = ["build_graph", "RECURSION_LIMIT"]

