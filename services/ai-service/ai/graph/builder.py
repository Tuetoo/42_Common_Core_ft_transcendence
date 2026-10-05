from typing import Any
from ai.agents.supervisor import AGENT_REGISTRY
from ai.graph.edges import (fan_out_to_selected_agents, node_name,
                            route_after_news, route_after_reflection,
                            should_continue_debate_round,
                            should_continue_to_debate)
from ai.graph.nodes import (_make_node, agent_join_node, bear_node, bull_node,
                            debate_join_node, episodic_memory_node, judge_node,
                            portfolio_node_factory, recommendation_node,
                            reflection_node, supervisor_node)
from ai.graph.state import AnalysisState
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from shared.enums import AgentRole


def build_analysis_graph() -> CompiledStateGraph[AnalysisState, Any, Any, Any]:
    graph = StateGraph(AnalysisState)

    graph.add_node("episodic_memory_node", episodic_memory_node)
    graph.add_node("supervisor_node", supervisor_node)

    for role, agent_cls in AGENT_REGISTRY.items():
        graph.add_node(
            node_name(role),
            _make_node(role, agent_cls)  # type: ignore[arg-type]
        )
    graph.add_node(
        node_name(AgentRole.PORTFOLIO),
        portfolio_node_factory()  # type: ignore[arg-type]
    )

    graph.add_node("agent_join_node", agent_join_node)
    graph.add_node("bull_node", bull_node)
    graph.add_node("bear_node", bear_node)
    graph.add_node("debate_join_node", debate_join_node)
    graph.add_node("judge_node", judge_node)
    graph.add_node("reflection_node", reflection_node)
    graph.add_node("recommendation_node", recommendation_node)

    graph.add_edge(START, "episodic_memory_node")
    graph.add_edge("episodic_memory_node", "supervisor_node")

    graph.add_conditional_edges(
        "supervisor_node", fan_out_to_selected_agents,
        [node_name(role)
         for role in AGENT_REGISTRY] + [node_name(AgentRole.PORTFOLIO)])

    for role in [*list(AGENT_REGISTRY), AgentRole.PORTFOLIO]:
        if role == AgentRole.NEWS:
            continue
        graph.add_edge(node_name(role), "agent_join_node")

    graph.add_conditional_edges(
        "news_node", route_after_news, ["sentiment_node", "agent_join_node"])

    graph.add_conditional_edges(
        "agent_join_node", should_continue_to_debate, [
            "bull_node", "bear_node", "reflection_node", "recommendation_node",
            END
        ])

    graph.add_edge("bull_node", "debate_join_node")
    graph.add_edge("bear_node", "debate_join_node")
    graph.add_edge("debate_join_node", "judge_node")

    graph.add_conditional_edges(
        "judge_node", should_continue_debate_round,
        ["bull_node", "bear_node", "reflection_node"])

    graph.add_conditional_edges(
        "reflection_node", route_after_reflection,
        [node_name(role) for role in AGENT_REGISTRY] + [
            node_name(AgentRole.PORTFOLIO), "bull_node", "bear_node",
            "recommendation_node"
        ])

    graph.add_edge("recommendation_node", END)

    return graph.compile()
