import structlog
from ai.graph.state import AnalysisState
from config import settings
from langgraph.graph import END
from langgraph.types import Send
from shared.enums import AgentRole, AgentStatus

logger = structlog.get_logger(__name__)

MAX_DEBATE_ROUNDS = 3
MAX_REFLECTION_RETRIES = 2


def node_name(role: AgentRole) -> str:
    return f"{role.value}_node"


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

_AVAILABILITY_FAILURE_KINDS = frozenset({"rate_limit", "server_overload"})


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
                or out.failure_kind not in _AVAILABILITY_FAILURE_KINDS):
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
