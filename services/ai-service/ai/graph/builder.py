from typing import Any
import structlog
from ai.graph.nodes import (AGENT_REGISTRY, agent_join_node, bear_node,
                            bull_node, debate_join_node, episodic_memory_node,
                            judge_node, make_agent_node, recommendation_node,
                            reflection_node, supervisor_node)
from ai.graph.state import (AVAILABILITY_FAILURE_KINDS, MAX_DEBATE_ROUNDS,
                            AnalysisState, node_name)
from config import settings
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from shared.enums import AgentRole, AgentStatus

logger = structlog.get_logger(__name__)


def fan_out_to_selected_agents(state: AnalysisState) -> list[Send]:
    waits = _sentiment_waits_for_news(state, state.selected_agents)
    return [
        Send(node_name(role), state) for role in state.selected_agents
        if not (waits and role == AgentRole.SENTIMENT)
    ]


def route_after_news(state: AnalysisState) -> list[Send] | str:
    if AgentRole.SENTIMENT in state.selected_agents:
        return [Send(node_name(AgentRole.SENTIMENT), state)]
    return "agent_join_node"


def _sentiment_waits_for_news(
        state: AnalysisState, roles: list[AgentRole]) -> bool:
    return (
        AgentRole.NEWS in roles
        and AgentRole.SENTIMENT in state.selected_agents)


_DEBATE_SIDES = ("bull_node", "bear_node")


def _retry_wave_brought_nothing(state: AnalysisState) -> bool:
    verdict = state.debate.latest_verdict
    if verdict is None or verdict.judge_failed:
        return False
    retried = [
        role for role in state.pending_retry_agents
        if role not in (AgentRole.BULL, AgentRole.BEAR)
    ]
    if not retried:
        return False
    for role in retried:
        out = state.agent_outputs.get(role)
        if (out is None or out.status != AgentStatus.FAILED
                or out.failure_kind not in AVAILABILITY_FAILURE_KINDS):
            return False
    return True


def _awaiting_sentiment_after_news_retry(state: AnalysisState) -> bool:
    if not _sentiment_waits_for_news(state, state.pending_retry_agents):
        return False
    news = state.agent_outputs.get(AgentRole.NEWS)
    sentiment = state.agent_outputs.get(AgentRole.SENTIMENT)
    if news is None or sentiment is None:
        return False
    if news.completed_at is None or sentiment.completed_at is None:
        return False
    return sentiment.completed_at <= news.completed_at


def should_continue_to_debate(state: AnalysisState) -> list[Send] | str:
    if not state.agent_outputs:
        logger.debug(
            "debate_debug.should_continue_to_debate",
            route="reflection_node (no agent_outputs)")
        return "reflection_node"
    if not set(state.selected_agents) <= set(state.agent_outputs.keys()):
        logger.debug(
            "debate_debug.should_continue_to_debate",
            route="END (incomplete wave, waiting for more selected agents)",
            selected_agents=[r.value for r in state.selected_agents],
            agent_outputs_keys=[r.value for r in state.agent_outputs])
        return END
    if _awaiting_sentiment_after_news_retry(state):
        logger.debug(
            "debate_debug.should_continue_to_debate",
            route="END (retried News arrived, waiting for re-Sent Sentiment)")
        return END
    if all(out.status == AgentStatus.FAILED
           for out in state.agent_outputs.values()):
        logger.debug(
            "debate_debug.should_continue_to_debate",
            route="reflection_node (every agent failed)")
        return "reflection_node"
    if _retry_wave_brought_nothing(state):
        logger.warning(
            "agent_join_node.retry_brought_no_evidence_keeping_verdict",
            retried=[r.value for r in state.pending_retry_agents])
        return "recommendation_node"
    if state.below_debate_evidence_minimum(
            settings.MIN_USABLE_AGENTS_FOR_DEBATE):
        logger.warning(
            "agent_join_node.insufficient_evidence_skipping_debate",
            usable_agents=state.usable_agent_count(),
            agent_count=len(state.agent_outputs),
            minimum=settings.MIN_USABLE_AGENTS_FOR_DEBATE)
        return "reflection_node"
    logger.debug(
        "debate_debug.should_continue_to_debate", route="fan_out_bull_bear",
        debate_round_number=state.debate.round_number)
    return [Send(name, state) for name in _DEBATE_SIDES]


def should_continue_debate_round(state: AnalysisState) -> list[Send] | str:
    verdict = state.debate.latest_verdict
    loop_again = (
        verdict is not None and not verdict.converged
        and (not verdict.judge_failed or verdict.retry_round)
        and state.debate.mutual_rounds_completed < MAX_DEBATE_ROUNDS)
    logger.debug(
        "debate_debug.should_continue_debate_round",
        route="loop_bull_bear" if loop_again else "reflection_node",
        verdict_converged=verdict.converged if verdict is not None else None,
        debate_round_number=state.debate.round_number,
        mutual_rounds_completed=state.debate.mutual_rounds_completed)
    if loop_again:
        return [Send(name, state) for name in _DEBATE_SIDES]
    return "reflection_node"


def route_after_reflection(state: AnalysisState) -> list[Send] | str:
    verdict = state.debate.latest_verdict
    if (state.pending_retry_agents and verdict is not None
            and verdict.judge_failed and not verdict.retry_round):
        logger.info(
            "route_after_reflection.retries_dropped_judge_failed",
            dropped=[r.value for r in state.pending_retry_agents])
        return "recommendation_node"
    if state.pending_retry_agents:
        logger.debug(
            "debate_debug.route_after_reflection", route="retry_agents",
            pending_retry_agents=[r.value for r in state.pending_retry_agents])
        waits = _sentiment_waits_for_news(state, state.pending_retry_agents)
        return [
            Send(node_name(role), state) for role in state.pending_retry_agents
            if not (waits and role == AgentRole.SENTIMENT)
        ]
    logger.debug(
        "debate_debug.route_after_reflection", route="recommendation_node")
    return "recommendation_node"


def build_analysis_graph() -> CompiledStateGraph[AnalysisState, Any, Any, Any]:
    graph = StateGraph(AnalysisState)

    graph.add_node("episodic_memory_node", episodic_memory_node)
    graph.add_node("supervisor_node", supervisor_node)

    for role in [*AGENT_REGISTRY, AgentRole.PORTFOLIO]:
        graph.add_node(node_name(role), make_agent_node(role))

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
