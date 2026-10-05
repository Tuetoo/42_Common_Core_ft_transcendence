from typing import Literal
from ai.agents.debate_common import (FREE_CASH_FLOW_RULE, LEGAL_AMOUNT_RULE,
                                     OVERLAP_RULE, PERIOD_LABEL_RULE,
                                     PROFIT_TREND_RULE, SOURCE_WORDING_RULE,
                                     UNUSUAL_ITEMS_RULE,
                                     generate_debate_argument)
from ai.graph.state import AnalysisState, DebateArgument
from ai.llm.base import LLMTier
from ai.llm.router import LLMRouter

DebateSide = Literal["bull", "bear"]

_TAG_RULE = (
    "The bracket tag names only research agent roles (market, technical, "
    "fundamental, news, sentiment, filing, macro, risk, portfolio): a line "
    "computed from SEC XBRL data is [fundamental], a filing-record line is "
    "[filing]; never put 'system', a record id or a data-source name in it.")

_LENGTH_RULE = (
    "Respond with only your argument, 3-6 sentences (the statements the "
    "rules above require for what you cite count towards them), no preamble.")


def _side_prompt(
        name: str, stance: str, inflated: str, modest_case: str,
        example: str) -> str:
    return (
        "You are the " + name + " side of an investment debate in "
        "AlphaMind's multi-agent research system. Build the strongest honest "
        "case " + stance + " buying/holding the ticker, using ONLY facts "
        "already reported by the research agents below.\n\n"
        "Hard rule, not a suggestion: every specific number, date, deal name, "
        "analyst rating, or price target you state must be traceable to an "
        "actual sentence in the evidence below. If you cannot point to where "
        "a claim came from, do not write it -- not even if it sounds "
        "plausible or is something you'd expect to be true for a company "
        "like this. Do not fill gaps with your own general knowledge, round "
        "numbers up to sound more " + inflated + ", or state a "
        "consensus/average that isn't explicitly given. A short argument "
        "built only from real evidence is correct output; a longer one "
        "padded with invented specifics is not, regardless of how it reads. "
        "If the evidence is genuinely " + modest_case + " is what's wanted, "
        "not a stronger-sounding one built by adding detail the evidence "
        "doesn't support.\n\n"
        "Tag every specific factual claim inline with which agent's evidence "
        "it came from, right after the claim, in square brackets -- e.g. " +
        example +
        ". This isn't decoration: it forces you to actually locate a real "
        "source for each claim as you write it, rather than writing freely "
        "and checking later. If you can't identify which agent's evidence a "
        "claim would cite, that's the signal it doesn't belong in the "
        "argument at all. " + _TAG_RULE + "\n\n"
        "When net income is above operating income, or the evidence names an "
        "unusual or non-operating item (a tax benefit, unrealized investment "
        "gains, a gain on a sale), do not present net income as evidence of "
        "operating profitability: argue from operating income, and name the "
        "unusual item if you mention net income at all. " +
        UNUSUAL_ITEMS_RULE + " " + OVERLAP_RULE + "\n\n" +
        FREE_CASH_FLOW_RULE + "\n\n" + LEGAL_AMOUNT_RULE + "\n\n" +
        PERIOD_LABEL_RULE + " " + PROFIT_TREND_RULE + "\n\n" +
        SOURCE_WORDING_RULE + "\n\n"
        "If this round's context includes a list of claims Reflection "
        "already flagged as unsupported in your own prior argument, treating "
        "that list correctly is the single highest priority in this round -- "
        "higher than making a compelling case. " + _LENGTH_RULE)


_BULL_SYSTEM_PROMPT = _side_prompt(
    "Bull", "FOR", "impressive",
    "weak, a modest bull case citing that weakness honestly",
    '"revenue grew 16% YoY [fundamental]"')
_BEAR_SYSTEM_PROMPT = _side_prompt(
    "Bear", "AGAINST", "alarming",
    "strong for the bull case, a modest bear case that acknowledges that "
    "honestly", '"free cash flow fell 40% [risk]"')
_SYSTEM_PROMPTS: dict[DebateSide, str] = {
    "bull": _BULL_SYSTEM_PROMPT,
    "bear": _BEAR_SYSTEM_PROMPT
}


class DebateSideAgent:

    def __init__(
            self, side: DebateSide, llm_router: LLMRouter | None = None,
            llm_tier: LLMTier = LLMTier.HIGHEST_ACCURACY) -> None:
        self.side: DebateSide = side
        self.llm_router = llm_router or LLMRouter()
        self.llm_tier = llm_tier

    async def make_argument(self, state: AnalysisState) -> DebateArgument:
        return await generate_debate_argument(
            side=self.side, state=state, llm_router=self.llm_router,
            llm_tier=self.llm_tier, system_prompt=_SYSTEM_PROMPTS[self.side])
