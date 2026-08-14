from enum import StrEnum


class DocumentCategory(StrEnum):
    FILING = "filing"
    ANALYST_REPORT = "analyst_report"
    EARNINGS_CALL = "earnings_call"
    NEWS = "news"
    MACRO_RESEARCH = "macro_research"
    RISK_ANALYSIS = "risk_analysis"
