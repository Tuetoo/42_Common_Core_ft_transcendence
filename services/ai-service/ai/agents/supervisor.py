from ai.agents.filing import FilingAgent
from ai.agents.fundamental import FundamentalAgent
from ai.agents.loop import BaseAgent
from ai.agents.macro import MacroAgent
from ai.agents.market import MarketAgent
from ai.agents.news import NewsAgent
from ai.agents.risk import RiskAgent
from ai.agents.sentiment import SentimentAgent
from ai.agents.technical import TechnicalAgent
from ai.graph.state import AgentSkip
from shared.enums import AgentRole, AnalysisType

AGENT_REGISTRY: dict[AgentRole, type[BaseAgent]] = {
    AgentRole.MARKET: MarketAgent,
    AgentRole.NEWS: NewsAgent,
    AgentRole.SENTIMENT: SentimentAgent,
    AgentRole.FILING: FilingAgent,
    AgentRole.TECHNICAL: TechnicalAgent,
    AgentRole.FUNDAMENTAL: FundamentalAgent,
    AgentRole.RISK: RiskAgent,
    AgentRole.MACRO: MacroAgent
}

_ALL_DATA_ROLES: list[AgentRole] = list(AGENT_REGISTRY)

DEFAULT_AGENTS_BY_TYPE: dict[AnalysisType, list[AgentRole]] = {
    AnalysisType.FULL: list(_ALL_DATA_ROLES),
    AnalysisType.TECHNICAL: [AgentRole.MARKET, AgentRole.TECHNICAL],
    AnalysisType.FUNDAMENTAL: [
        AgentRole.MARKET, AgentRole.FUNDAMENTAL, AgentRole.FILING
    ],
    AnalysisType.SENTIMENT: [AgentRole.NEWS, AgentRole.SENTIMENT],
    AnalysisType.RISK: [AgentRole.MARKET, AgentRole.RISK],
    AnalysisType.MACRO: [AgentRole.MACRO, AgentRole.MARKET],
    AnalysisType.NEWS: [AgentRole.NEWS, AgentRole.SENTIMENT],
    AnalysisType.FILING: [AgentRole.FILING, AgentRole.FUNDAMENTAL]
}


def select_agents(
        *, requested_agents: list[AgentRole] | None,
        analysis_type: AnalysisType,
        holdings_count: int) -> tuple[list[AgentRole], list[AgentSkip]]:
    if requested_agents is not None:
        base_selected = [
            r for r in dict.fromkeys(requested_agents) if r in _ALL_DATA_ROLES
        ]
        skip_reason = "user's explicit agent selection did not include it"
    else:
        base_selected = list(DEFAULT_AGENTS_BY_TYPE[analysis_type])
        skip_reason = (
            "not part of the default agent set for "
            f"analysis_type={analysis_type.value}")

    selected = list(base_selected)
    skipped = [
        AgentSkip(role=role, reason=skip_reason) for role in _ALL_DATA_ROLES
        if role not in selected
    ]

    if holdings_count >= 1:
        if (requested_agents is not None
                and AgentRole.PORTFOLIO not in requested_agents):
            skipped.append(
                AgentSkip(
                    role=AgentRole.PORTFOLIO, reason=(
                        "user's explicit agent selection did not include it")))
        else:
            selected.append(AgentRole.PORTFOLIO)
    else:
        skipped.append(
            AgentSkip(
                role=AgentRole.PORTFOLIO,
                reason="no holdings in the analysis context"))

    return selected, skipped
