import importlib
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import ai.ingestion.sec_edgar_filings as sec_edgar_filings
import ai.knowledge.filing_summary as filing_summary
import config
import edgar
import httpx
import pytest
from ai.agents import debate_common
from ai.agents import filing as filing_module
from ai.agents import risk as risk_module
from ai.agents.filing import (prior_year_unusual_item_lines,
                              year_to_date_unusual_item_lines)
from ai.graph import nodes as nodes_module
from ai.ingestion.sec_edgar_filings import NewFiling
from ai.knowledge import filing_records as frec
from ai.llm.base import LLMError, LLMResponse, LLMTier, LLMUsage
from ai.providers import sec_edgar
from ai.providers.sec_edgar import FilingPeriod, QuarterResults
from ai.rag.retriever import RetrievedChunk
from ai.tools.filing_tool import NEWEST_FILING_GUIDANCE, SearchFilingsTool
from helpers import _Row
from structlog.testing import capture_logs
from domains.filings.repository import FilingRecordRepository
from shared.enums import DocumentCategory

_SCRIPTS = Path(__file__).resolve().parents[2] / "services" / "ai-service"

_NO_RECORDS = {
    "legal_matters": [],
    "transactions": [],
    "guidance": [],
    "one_off_items": []
}


@pytest.fixture
def fresh_sec_caches(monkeypatch):
    sec_edgar._reset_caches_for_tests()
    monkeypatch.setattr(
        sec_edgar.settings, "SEC_EDGAR_USER_AGENT", "Test test@example.com")
    monkeypatch.setattr(
        sec_edgar.settings, "SEC_EDGAR_MAX_REQUESTS_PER_SECOND", 0.0)
    yield
    sec_edgar._reset_caches_for_tests()


def _validated(data: Any, raw: str, **options: Any) -> dict[str, Any]:
    kept = frec.validate_records(data, raw, **options)
    assert kept is not None
    return kept


class _RecordingRouter:

    def __init__(self, outputs: list[tuple[str | None, str | None]]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, tier, messages, **kwargs):
        self.calls.append({"tier": tier, "messages": messages, **kwargs})
        if len(self.outputs) > 1:
            content, finish = self.outputs.pop(0)
        else:
            content, finish = self.outputs[0]
        if content is None:
            raise LLMError("down")
        return LLMResponse(
            content=content, model="m", provider="p",
            usage=LLMUsage(prompt_tokens=1,
                           completion_tokens=1), finish_reason=finish)


def _records_reply(records: dict[str, Any], prose: str = "Summary.") -> str:
    return f"{prose}\n{frec.RECORDS_MARKER}\n{json.dumps(records)}"


class _FakeFiling:

    def __init__(
            self, filing_date: date, text_content: str, *,
            raises_on_text: bool = False,
            period_of_report: str = "2026-01-01"):
        self.filing_date = filing_date
        self._text_content = text_content
        self._raises_on_text = raises_on_text
        self.text_call_count = 0
        self.form = ""
        self.period_of_report = period_of_report

    def text(self) -> str:
        self.text_call_count += 1
        if self._raises_on_text:
            raise RuntimeError("simulated real filing-text fetch failure")
        return self._text_content


class _FakeCompany:

    def __init__(
            self, ticker: str, filings_by_form: dict[str, list[_FakeFiling]]):
        self.ticker = ticker
        self._filings_by_form = filings_by_form

    def get_filings(self, form: str,
                    trigger_full_load: bool = True) -> list[_FakeFiling]:
        filings = self._filings_by_form.get(form, [])
        for filing in filings:
            filing.form = form
        return filings


def _patch_company(monkeypatch, filings_by_form: dict[str, list]) -> None:
    monkeypatch.setattr(
        edgar, "Company", lambda ticker: _FakeCompany(ticker, filings_by_form))


async def _fake_summarize_filing(
        raw_text, *, form, ticker, filing_date=None, amendment_text=None):
    amendment_marker = (
        f" [+amendment:{len(amendment_text.split())}w]"
        if amendment_text else "")
    return filing_summary.FilingSummary(
        f"[summary of {len(raw_text.split())} words for {ticker} {form}]"
        f"{amendment_marker}")


@pytest.fixture
def edgar_listing(monkeypatch):

    async def _no_records(raw_text, **kwargs):
        return filing_summary.ExtractedRecords(dict(_NO_RECORDS), "p/m")

    async def _no_period(ticker, form, filing_date):
        return None

    monkeypatch.setattr(
        config.settings, "SEC_EDGAR_USER_AGENT", "Test test@x.com")
    monkeypatch.setattr(edgar, "set_identity", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        sec_edgar_filings, "_older_page_filings", lambda *_a, **_kw: ([], 0))
    monkeypatch.setattr(
        sec_edgar_filings, "summarize_filing", _fake_summarize_filing)
    monkeypatch.setattr(
        sec_edgar_filings, "extract_filing_records", _no_records)
    monkeypatch.setattr(sec_edgar_filings, "filing_period", _no_period)


def _raise_not_found(ticker):
    raise ValueError(f"no such company: {ticker}")


_REAL_PROSE = " ".join(f"word{i}" for i in range(150))


@pytest.mark.usefixtures("edgar_listing")
class TestListNewFilings:

    async def test_nothing_is_listed_and_nothing_raised(self, monkeypatch):
        cases = {
            "missing-user-agent": ("", None, "AMD"),
            "unknown-ticker": (
                "Test test@x.com", _raise_not_found, "NOTAREALTICKER")
        }
        for case_id, (user_agent, company, ticker) in cases.items():
            monkeypatch.setattr(
                config.settings, "SEC_EDGAR_USER_AGENT", user_agent)
            if company is not None:
                monkeypatch.setattr(edgar, "Company", company)
            result = await sec_edgar_filings.list_new_filings(
                ticker, already_ingested_filenames=set())
            assert result == [], case_id

    async def test_new_filing_is_fetched_summarized_and_returned(
            self, monkeypatch):
        filing = _FakeFiling(date(2026, 2, 4), f"<p>{_REAL_PROSE}</p>")
        _patch_company(monkeypatch, {"10-K": [filing]})

        result = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames=set())

        assert len(result) == 1
        assert result[0].filename == "AMD_10-K_2026-02-04.txt"
        assert result[0].form == "10-K"
        assert result[0].filing_date == "2026-02-04"
        assert result[0].text == (
            f"[summary of {len(_REAL_PROSE.split())} words for AMD 10-K]")
        assert result[0].report_date == "2026-01-01"
        assert result[0].records == {
            "legal_matters": [],
            "transactions": [],
            "guidance": [],
            "one_off_items": []
        }

    async def test_skipped_filings_are_never_fetched_or_summarized(
            self, monkeypatch):
        filing = _FakeFiling(
            date(2026, 2, 4), f"<p>{_REAL_PROSE}</p>", raises_on_text=True)
        _patch_company(monkeypatch, {"10-K": [filing]})

        result = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames={"AMD_10-K_2026-02-04.txt"})

        assert result == []
        assert filing.text_call_count == 0, (
            "an already-ingested filing must be skipped before the real, "
            "potentially-expensive .text() fetch, not after")

        filing = _FakeFiling(date(2026, 5, 6), "<p>too short</p>")
        _patch_company(monkeypatch, {"10-Q": [filing]})
        summarize_calls: list[str] = []
        monkeypatch.setattr(
            sec_edgar_filings, "summarize_filing",
            lambda raw_text, **kw: summarize_calls.append(raw_text))

        result = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames=set())

        assert result == []
        assert (
            summarize_calls == []
        ), "too-short excerpt must never reach summarization"

    async def test_only_an_amendment_for_the_same_period_is_merged_in(
            self, monkeypatch):
        original = _FakeFiling(
            date(2026, 2, 4), f"<p>{_REAL_PROSE}</p>",
            period_of_report="2025-12-27")
        real_amendment_prose = " ".join(f"correction{i}" for i in range(30))
        amendment = _FakeFiling(
            date(2026, 3, 1), f"<p>{real_amendment_prose}</p>",
            period_of_report="2025-12-27")
        _patch_company(
            monkeypatch, {
                "10-K": [original],
                "10-K/A": [amendment]
            })

        result = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames=set())

        assert len(result) == 1
        assert (
            f"+amendment:{len(real_amendment_prose.split())}w"
            in result[0].text), (
                "the real amendment's own text must reach summarize_filing "
                "as `amendment_text` when `period_of_report` genuinely matches"
            )

        original = _FakeFiling(
            date(2026, 2, 4), f"<p>{_REAL_PROSE}</p>",
            period_of_report="2025-12-27")
        unrelated_amendment = _FakeFiling(
            date(2025, 6, 1), "<p>unrelated prior-year correction</p>",
            period_of_report="2024-12-28")
        _patch_company(
            monkeypatch, {
                "10-K": [original],
                "10-K/A": [unrelated_amendment]
            })

        result = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames=set())

        assert len(result) == 1
        assert "+amendment" not in result[0].text, (
            "a real 10-K/A for a genuinely different real "
            "period_of_report must never be merged in")

    async def test_per_form_cap_allows_several_10q_within_its_window(
            self, monkeypatch):
        filings = [
            _FakeFiling(date(2026, 5, 6), f"<p>{_REAL_PROSE} q1</p>"),
            _FakeFiling(date(2026, 2, 4), f"<p>{_REAL_PROSE} q4</p>"),
            _FakeFiling(date(2025, 11, 5), f"<p>{_REAL_PROSE} q3</p>")
        ]
        _patch_company(monkeypatch, {"10-Q": filings})

        result = await sec_edgar_filings.list_new_filings(
            "AMD", already_ingested_filenames=set())

        assert {r.filename
                for r in result} == {
                    "AMD_10-Q_2026-05-06.txt", "AMD_10-Q_2026-02-04.txt",
                    "AMD_10-Q_2025-11-05.txt"
                }


class _TextFiling:

    def __init__(
            self, form: str, filing_date: str, text: str,
            period: str = "2024-12-31") -> None:
        self.form, self.filing_date, self._text = form, filing_date, text
        self.period_of_report = period

    def text(self) -> str:
        return self._text


class _RecordingCompany:

    def __init__(self, recent: dict[str, list[_TextFiling]]) -> None:
        self.recent = recent
        self.calls: list[tuple[str, bool]] = []

    def get_filings(self, form: str,
                    trigger_full_load: bool = True) -> list[_TextFiling]:
        self.calls.append((form, trigger_full_load))
        return self.recent.get(form, [])


class TestFilingIngestion:

    async def test_a_records_call_error_keeps_the_summary(
            self, monkeypatch, capsys):

        def _fetch(ticker, existing):
            return [(
                "KO_10-Q_2026-07-29.txt", "10-Q", "2026-07-29", "raw", None,
                "2026-07-03")]

        async def _summarize(raw, **kwargs):
            return filing_summary.FilingSummary("summary")

        async def _extract(raw, **kwargs):
            assert kwargs["tier"] == LLMTier.HIGHEST_ACCURACY
            raise ValueError("bad reply")

        async def _upload(*args, **kwargs):
            return None

        monkeypatch.setattr(
            sec_edgar_filings.settings, "SEC_EDGAR_USER_AGENT", "t t@x.com")
        monkeypatch.setattr(
            sec_edgar_filings, "_fetch_real_filings_sync", _fetch)
        monkeypatch.setattr(sec_edgar_filings, "summarize_filing", _summarize)
        monkeypatch.setattr(
            sec_edgar_filings, "extract_filing_records", _extract)
        monkeypatch.setattr(sec_edgar_filings, "upload_bytes", _upload)
        out = await sec_edgar_filings.list_new_filings(
            "KO", already_ingested_filenames=set())
        assert out[0].text == "summary" and out[0].records is None
        assert "sec_edgar_filings.records_failed" in capsys.readouterr().out

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_ingestion_stores_records_after_the_document(
            self, monkeypatch):
        stored: list[dict[str, Any]] = []

        async def _existing(ticker, category):
            return []

        async def _list(ticker, *, already_ingested_filenames):
            return [
                NewFiling(
                    "KO_10-Q_2026-07-29.txt", "summary", "10-Q", "2026-07-29",
                    records={"guidance": []}, report_date=None),
                NewFiling(
                    "KO_10-K_2026-02-20.txt", "summary", "10-K", "2026-02-20")
            ]

        async def _create(**kwargs):
            return "id"

        async def _period(ticker, form, filing_date):
            return FilingPeriod(form, filing_date, "2026-07-03", "a")

        async def _store(**kwargs):
            stored.append(kwargs)
            return True

        monkeypatch.setattr(
            sec_edgar_filings, "get_existing_document_filenames", _existing)
        monkeypatch.setattr(sec_edgar_filings, "list_new_filings", _list)
        monkeypatch.setattr(
            sec_edgar_filings, "create_system_document", _create)
        monkeypatch.setattr(sec_edgar_filings, "filing_period", _period)
        monkeypatch.setattr(sec_edgar_filings, "store_filing_records", _store)
        result = await sec_edgar_filings.ingest_new_filings("KO")
        assert result.documents_created == 2
        assert stored == [{
            "ticker": "KO",
            "filename": "KO_10-Q_2026-07-29.txt",
            "form": "10-Q",
            "filing_date": "2026-07-29",
            "report_date": "2026-07-03",
            "records": {
                "guidance": []
            }
        }]

    async def test_concurrent_ingests_of_one_ticker_share_one_run(
            self, monkeypatch):
        import asyncio
        from ai.ingestion.digests import track_source_failures

        listed: list[str] = []
        created: list[str] = []
        release = asyncio.Event()

        async def _existing(ticker, category):
            return []

        async def _list(ticker, *, already_ingested_filenames):
            listed.append(ticker)
            await release.wait()
            return [
                NewFiling("KO_10-Q_new.txt", "s", "10-Q", "2026-07-29"),
                NewFiling("KO_10-K_fb.txt", "s", "10-K", "2026-02-20", True)
            ]

        async def _create(**kwargs):
            created.append(kwargs["filename"])
            return "id"

        monkeypatch.setattr(
            sec_edgar_filings, "get_existing_document_filenames", _existing)
        monkeypatch.setattr(sec_edgar_filings, "list_new_filings", _list)
        monkeypatch.setattr(
            sec_edgar_filings, "create_system_document", _create)

        async def _tracked():
            with track_source_failures() as failures:
                result = await sec_edgar_filings.ingest_new_filings("KO")
            return result, failures

        first = asyncio.create_task(_tracked())
        await asyncio.sleep(0)
        second = asyncio.create_task(_tracked())
        await asyncio.sleep(0)
        release.set()
        (one,
         one_failures), (two,
                         two_failures) = await asyncio.gather(first, second)
        assert listed == ["KO"] and created == ["KO_10-Q_new.txt"]
        assert one is two and one.documents_created == 1
        assert one_failures == two_failures == ["filing_summary"]
        assert sec_edgar_filings._ingests_in_flight == {}
        await sec_edgar_filings.ingest_new_filings("KO")
        assert listed == ["KO", "KO"]

    async def test_fetch_filing_text_reads_older_pages_and_the_amendment(
            self, monkeypatch):
        monkeypatch.setattr(
            config.settings, "SEC_EDGAR_USER_AGENT", "t t@x.com")
        monkeypatch.setattr(edgar, "set_identity", lambda *_a, **_kw: None)
        company = _RecordingCompany({
            "10-K": [
                _TextFiling("10-K", "2026-02-20", "newer 10-K", "2025-12-31")
            ],
            "10-K/A": [_TextFiling("10-K/A", "2025-04-01", "the amendment")]
        })
        monkeypatch.setattr(edgar, "Company", lambda ticker: company)
        older_calls: list[dict[str, Any]] = []

        def _older(company_, form, needed, *, until_date=None):
            older_calls.append({"form": form, "until_date": until_date})
            return [_TextFiling("10-K", "2025-02-14", "old 10-K")], 2

        monkeypatch.setattr(sec_edgar_filings, "_older_page_filings", _older)
        got = sec_edgar_filings.fetch_filing_text_sync(
            "KO", "10-K", "2025-02-14")
        assert got == ("old 10-K", "the amendment", "2024-12-31")
        assert older_calls == [{"form": "10-K", "until_date": "2025-02-14"}]
        assert company.calls == [("10-K", False), ("10-K/A", True)]
        assert (
            sec_edgar_filings.fetch_filing_text_sync(
                "KO", "10-K", "2026-03-01") is None)
        assert len(older_calls) == 1

    def test_older_pages_stop_at_the_date(self, monkeypatch):
        import edgar.httprequests

        pages = {
            "main": {
                "filings": {
                    "files": [{
                        "name": "p1.json"
                    }, {
                        "name": "p2.json"
                    }, {
                        "name": "p3.json"
                    }]
                }
            },
            "p1.json": {
                "form": ["10-K", "8-K"],
                "filingDate": ["2024-02-10", "2023-11-01"],
                "accessionNumber": ["a1", "a2"]
            },
            "p2.json": {
                "form": ["10-K", "10-Q"],
                "filingDate": ["2023-02-10", "2022-11-01"],
                "accessionNumber": ["b1", "b2"]
            },
            "p3.json": {
                "form": ["10-K"],
                "filingDate": ["2022-02-10"],
                "accessionNumber": ["c1"]
            }
        }

        def _download(url: str) -> dict[str, Any]:
            return pages.get(url.rsplit("/", 1)[-1], pages["main"])

        monkeypatch.setattr(edgar.httprequests, "download_json", _download)
        monkeypatch.setattr(
            edgar, "Filing", lambda **kw: SimpleNamespace(**kw))
        company = SimpleNamespace(cik=1, name="X")
        found, read = sec_edgar_filings._older_page_filings(
            company, "10-K", 10**9, until_date="2023-02-10")
        assert read == 2
        assert [f.filing_date for f in found] == ["2024-02-10", "2023-02-10"]

    async def test_only_the_newest_fresh_filing_is_pasted(self, monkeypatch):

        @dataclass
        class _Result:
            new_filings: list[NewFiling]

        async def _ingest(ticker: str) -> _Result:
            return _Result([
                NewFiling(
                    "X_10-K_2026-02-01.txt", "annual text", "10-K",
                    "2026-02-01"),
                NewFiling(
                    "X_10-Q_2026-08-05.txt", "quarter text", "10-Q",
                    "2026-08-05"),
                NewFiling(
                    "X_10-Q_2026-05-05.txt", "q1 text", "10-Q", "2026-05-05")
            ])

        monkeypatch.setattr(nodes_module, "ingest_new_filings", _ingest)
        context = await nodes_module.load_fresh_filing_context("X")
        assert context is not None
        assert "quarter text" in context
        assert "annual text" not in context and "q1 text" not in context
        assert "10-Q filed on 2026-08-05" in context
        assert "may not be in the searchable filing corpus yet" in context
        assert "2 older new filing(s)" in context

        async def _one(ticker: str) -> _Result:
            return _Result(
                [NewFiling("X_10-Q_a.txt", "only", "10-Q", "2026-08-05")])

        monkeypatch.setattr(nodes_module, "ingest_new_filings", _one)
        context = await nodes_module.load_fresh_filing_context("X")
        assert context is not None and "older new filing" not in context


def _fact(
        start: str, end: str, val: float, *, form: str = "10-Q",
        filed: str = "2026-07-31", fp: str | None = None) -> dict[str, Any]:
    fact = {
        "start": start,
        "end": end,
        "val": val,
        "form": form,
        "filed": filed
    }
    if fp is not None:
        fact["fp"] = fp
    return fact


def _facts(**tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "facts": {
            "us-gaap": {
                t: {
                    "units": {
                        "USD": f
                    }
                }
                for t, f in tags.items()
            }
        }
    }


def _q(period: tuple[str, str], val: float, fp: str = "Q2") -> dict:
    return _fact(*period, val, fp=fp)


def _july30(period: tuple[str, str], val: float) -> dict[str, Any]:
    return _fact(*period, val, filed="2026-07-30")


_Q2, _Q2_PRIOR = ("2026-04-01", "2026-06-30"), ("2025-04-01", "2025-06-30")
_Q1 = ("2026-01-01", "2026-03-31")
_YTD, _YTD_PRIOR = ("2026-01-01", "2026-06-30"), ("2025-01-01", "2025-06-30")


def _four(q: float, q_prior: float, ytd: float,
          ytd_prior: float) -> list[dict[str, Any]]:
    return [
        _q(_Q2, q),
        _q(_Q2_PRIOR, q_prior),
        _q(_YTD, ytd),
        _q(_YTD_PRIOR, ytd_prior)
    ]


_UPWARD = (
    "EquitySecuritiesWithoutReadilyDeterminableFairValueUpwardPriceAdjustment"
    "AnnualAmount")
_AMZN = _facts(
    Revenues=_four(200_606e6, 167_702e6, 382_120e6, 323_370e6),
    OperatingIncomeLoss=_four(27_458e6, 19_171e6, 51_310e6, 37_578e6),
    NetIncomeLoss=_four(62_646e6, 18_164e6, 92_900e6, 35_290e6),
    NonoperatingIncomeExpense=_four(53_396e6, 1_686e6, 69_378e6, 4_960e6), **{
        _UPWARD: [
            _q(_Q1, 12_328e6, "Q1"), *_four(50_486e6, 49e6, 62_814e6, 86e6)
        ]
    }, EquitySecuritiesFvNiGainLoss=[_q(_Q2, 0.0)],
    EquitySecuritiesFvNiUnrealizedGainLoss=[
        _q(_Q2, -60e6), _q(_Q2_PRIOR, -58e6)
    ], GoodwillImpairmentLoss=[_q(_Q1, 3_900e6, "Q1"),
                               _q(_YTD, 3_900e6)
                               ], AssetImpairmentCharges=[_q(_Q2, 2e6)],
    IncomeTaxExpenseBenefit=_four(18_204e6, 2_678e6, 27_760e6, 7_230e6),
    NetCashProvidedByUsedInOperatingActivities=[
        _q(_YTD, 71_420e6), _q(_YTD_PRIOR, 49_530e6)
    ], PaymentsToAcquireProductiveAssets=[
        _q(_YTD, 98_410e6), _q(_YTD_PRIOR, 57_200e6)
    ])

_PRETAX = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems"
    "NoncontrollingInterest")


def _xbrl_item(
        items: tuple[sec_edgar.XbrlItem, ...], key: str) -> sec_edgar.XbrlItem:
    return next(i for i in items if i.key == key)


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _ko_cik(ticker: str) -> int:
    return 21344


def _submissions(recent: dict[str, list[str]], files: list) -> dict:
    return {"filings": {"recent": recent, "files": files}}


class TestQuarterResults:

    @pytest.mark.usefixtures("fresh_sec_caches")
    def test_sign_changes_and_losses(self):
        q = QuarterResults(
            ticker="INTC", form="10-Q", filed="2026-07-24",
            period_start="2026-03-29", period_end="2026-06-27",
            prior_period_end="2025-06-28", derived=False, revenue=16.13e9,
            revenue_prior=12.86e9, operating_income=1.80e9,
            operating_income_prior=-3.18e9, net_income=-11.03e9,
            net_income_prior=-2.92e9, nonoperating=-12.58e9, income_tax=29e6)
        payload = sec_edgar.quarter_results_payload(q)
        assert payload["operating_income_yoy_pct"] is None
        assert payload["net_income_yoy_pct"] == pytest.approx(-277.7)
        assert (
            "operating income $1.80B vs -$3.18B (swung to a profit)"
            in payload["text"])
        assert (
            "net income -$11.03B vs -$2.92B (loss widened 278%)"
            in payload["text"])
        swung = sec_edgar.quarter_results_payload(
            QuarterResults(
                **{
                    **q.__dict__, "net_income": -1e9,
                    "net_income_prior": 2e9
                }))
        assert swung["net_income_yoy_pct"] is None
        assert "(swung to a loss)" in swung["text"]

    def test_cost_lines_and_keys(self):
        facts = _facts(
            Revenues=[_july30(_Q2, 10e9),
                      _july30(_Q2_PRIOR, 8e9)],
            NetIncomeLoss=[_july30(_Q2, 2e9),
                           _july30(_Q2_PRIOR, 1.5e9)],
            CostOfRevenue=[_july30(_Q2, 4.69e9),
                           _july30(_Q2_PRIOR, 3.52e9)],
            ResearchAndDevelopmentExpense=[
                _july30(_Q2, 2.47e9),
                _july30(_Q2_PRIOR, 2.11e9)
            ], SellingGeneralAndAdministrativeExpense=[_july30(_Q2, 0.79e9)])
        q = sec_edgar._quarter_results_from_facts(facts, "X")
        assert q is not None
        payload = sec_edgar.quarter_results_payload(q)
        assert payload["gross_margin_pct"] == 53.1
        assert payload["gross_margin_pct_prior"] == 56.0
        assert payload["rd_expense_yoy_pct"] == 17.1
        assert payload["sga_expense"] == 0.79e9
        assert "sga_expense_prior" not in payload
        assert "sga_expense_yoy_pct" not in payload
        first_line = payload["text"].split("\n")[0]
        assert first_line.endswith(
            "(+33%). Costs: gross margin 53.1% vs 56.0%; R&D $2.47B vs $2.11B "
            "(+17%); SG&A $790M (year-ago quarter not reported).")

        bank = _facts(
            RevenuesNetOfInterestExpense=[
                _july30(_Q2, 10e9),
                _july30(_Q2_PRIOR, 8e9)
            ])
        q = sec_edgar._quarter_results_from_facts(bank, "JPM")
        assert q is not None
        payload = sec_edgar.quarter_results_payload(q)
        assert "Costs:" not in payload["text"]
        assert not any(
            k.startswith(("gross_margin", "rd_", "sga_")) for k in payload)

    def test_gross_margin(self):
        cases = {
            "gross-profit-tag-wins": (
                _facts(
                    Revenues=[_july30(_Q2, 10e9),
                              _july30(_Q2_PRIOR,
                                      8e9)], GrossProfit=[_july30(_Q2, 6e9)],
                    CostOfRevenue=[_july30(_Q2, 1e9),
                                   _july30(_Q2_PRIOR,
                                           4e9)]), "X", (60.0, 50.0)),
            "no-derived-margin-with-policy-benefits": (
                _facts(
                    Revenues=[_july30(_Q2, 111e9),
                              _july30(_Q2_PRIOR, 100e9)],
                    CostOfGoodsAndServicesSold=[
                        _july30(_Q2, 13e9),
                        _july30(_Q2_PRIOR, 12e9)
                    ], PolicyholderBenefitsAndClaimsIncurredNet=[
                        _july30(_Q2, 90e9)
                    ]), "UNH", (None, None))
        }
        for case_id, (facts, ticker, margins) in cases.items():
            q = sec_edgar._quarter_results_from_facts(facts, ticker)
            assert q is not None, case_id
            got = (q.gross_margin_pct, q.gross_margin_pct_prior)
            assert got == margins, case_id

    def test_payload_text_and_keys(self):
        q = sec_edgar._quarter_results_from_facts(_AMZN, "AMZN")
        assert q is not None
        payload = sec_edgar.quarter_results_payload(q)
        lines = payload["text"].split("\n")
        assert lines[1] == "Net income minus operating income: +$35.19B"
        assert lines[2] == (
            "Below operating income (SEC XBRL, quarter ended 2026-06-30, "
            "compared with the quarter ended 2025-06-30): non-operating "
            "income (expense), total +$53.40B vs +$1.69B; unrealized gains "
            "(losses) on equity securities -$60M vs -$58M; upward "
            "adjustments of equity securities without a readily "
            "determinable fair value (gain) $50.49B vs $49M; asset "
            "impairment charges $2M (comparison period not tagged); income "
            "tax expense (benefit) $18.20B vs $2.68B.")
        assert lines[4].startswith(
            "Below operating income, year to date (6 months ended "
            "2026-06-30, compared with the period ended 2025-06-30): "
            "non-operating income (expense), total +$69.38B vs +$4.96B;")
        assert (
            "goodwill impairment (charge) $3.90B (comparison period not "
            "tagged)") in lines[4]
        assert lines[5] == (
            "Cash flow (SEC XBRL, year to date, 6 months 2026-01-01 to "
            "2026-06-30, compared with 2025-01-01 to 2025-06-30): operating "
            "cash flow $71.42B vs $49.53B; capital expenditures $98.41B vs "
            "$57.20B; free cash flow (computed: operating cash flow minus "
            "capital expenditures) -$26.99B vs -$7.67B.")
        assert lines[6] == sec_edgar._ITEMS_NOTE
        assert payload["below_operating"][0] == {
            "key": "nonoperating_total",
            "label": "non-operating income (expense), total",
            "current": 53_396e6,
            "prior": 1_686e6,
            "derived": False,
            "prior_derived": False,
            "computed": False
        }
        assert payload["cash_flow"]["period"] == q.cash_flow_period
        assert len(payload["cash_flow"]["items"]) == 3
        assert payload["year_to_date"]["below_operating"][0]["current"] == (
            69_378e6)

    def test_computed_and_derived_values_are_marked(self):
        facts = _facts(
            Revenues=[_q(_Q2, 100e6), _q(_Q2_PRIOR, 90e6)],
            OperatingIncomeLoss=[_q(_Q2, 10e6),
                                 _q(_Q2_PRIOR, 8e6)],
            NetIncomeLoss=[_q(_Q2, 9e6), _q(_Q2_PRIOR, 7e6)],
            **{_PRETAX: [_q(_Q2, 12e6), _q(_Q2_PRIOR, 7e6)]})
        q = sec_edgar._quarter_results_from_facts(facts, "X")
        assert q is not None
        nonop = _xbrl_item(q.below_operating, "nonoperating_total")
        assert nonop.computed and (nonop.current, nonop.prior) == (2e6, -1e6)
        assert nonop.label.startswith(
            "non-operating total (computed: pretax income minus operating "
            "income)")
        assert [i.key for i in q.below_operating
                ][:2] == ["pretax_income", "nonoperating_total"]
        bank = _facts(
            Revenues=[_q(_Q2, 100e6), _q(_Q2_PRIOR, 90e6)],
            NetIncomeLoss=[_q(_Q2, 9e6), _q(_Q2_PRIOR, 7e6)],
            **{_PRETAX: [_q(_Q2, 12e6), _q(_Q2_PRIOR, 7e6)]})
        q = sec_edgar._quarter_results_from_facts(bank, "BANK")
        assert q is not None
        assert [i.key for i in q.below_operating] == ["pretax_income"]

        facts = _facts(
            Revenues=[_q(_Q2, 100e6), _q(_Q2_PRIOR, 90e6)],
            NetIncomeLoss=[_q(_Q2, 9e6),
                           _q(_Q2_PRIOR, 7e6)], IncomeTaxExpenseBenefit=[
                               _q(_Q1, 1e6, "Q1"),
                               _q(_YTD, 3e6),
                               _q(("2025-01-01", "2025-03-31"), 1e6, "Q1"),
                               _q(_YTD_PRIOR, 2e6)
                           ])
        q = sec_edgar._quarter_results_from_facts(facts, "X")
        assert q is not None
        tax = _xbrl_item(q.below_operating, "income_tax")
        assert (tax.current, tax.prior, tax.derived,
                tax.prior_derived) == (2e6, 1e6, True, True)
        text = sec_edgar.quarter_results_payload(q)["text"]
        assert "income tax expense (benefit) $2M* vs $1M*" in text
        assert text.endswith(sec_edgar._DERIVED_NOTE)

    def test_cash_flow_of_a_10k_is_the_fiscal_year(self):
        year = ("2025-07-01", "2026-06-30")
        prior = ("2024-07-01", "2025-06-30")
        us_gaap = {
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {
                    "USD": [
                        _q(year, 182_940e6, "FY"),
                        _q(prior, 136_160e6, "FY"),
                        _q(("2026-04-01", "2026-06-30"), 40e9, "FY")
                    ]
                }
            }
        }
        items, period = sec_edgar._cash_flow_for(
            us_gaap, form="10-K", start=date(2026, 4, 1),
            end=date(2026, 6, 30), prior_end=date(2025, 6, 30), ytd=None)
        assert [(i.current, i.prior)
                for i in items] == [(182_940e6, 136_160e6)]
        assert period == (
            "fiscal year ended 2026-06-30, compared with the "
            "same period ended 2025-06-30")
        items, period = sec_edgar._cash_flow_for({
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {
                    "USD": [_q(_Q1, 5e9, "Q1")]
                }
            }
        }, form="10-Q", start=date(2026, 1, 1), end=date(2026, 3, 31),
                                                 prior_end=date(2025, 3,
                                                                31), ytd=None)
        assert [(i.current, i.prior) for i in items] == [(5e9, None)]
        assert period is not None
        assert period.startswith("quarter 2026-01-01 to 2026-03-31")

    def test_free_cash_flow_after_returns(self):
        cases = {
            "period-missing-one-return-is-not-computed": (
                {
                    "PaymentsForRepurchaseOfCommonStock": [7.25e9, 2.5e9],
                    "PaymentsOfDividendsCommonStock": [1.34e9, None]
                }, (5.74e9 - 8.59e9,
                    None), "free cash flow after buybacks and "
                "dividends (computed: free cash flow minus share "
                "repurchases minus dividends paid)"),
            "dividends-only": ({
                "PaymentsOfDividends": [1e9, 2e9]
            }, (4.74e9, 5.52e9), "free cash flow after dividends (computed: "
                               "free cash flow minus dividends paid, no share "
                               "repurchases line in the data)"),
            "buybacks-and-ordinary-dividends": (
                {
                    "PaymentsForRepurchaseOfCommonStock": [1e9, 1e9],
                    "PaymentsOfOrdinaryDividends": [2e9, 1e9]
                }, (2.74e9, 5.52e9), "free cash flow after buybacks and "
                "dividends (computed: free cash flow minus share "
                "repurchases minus dividends paid)"),
            "buybacks-only": ({
                "PaymentsForRepurchaseOfCommonStock": [6e9, 1e9]
            }, (-0.26e9, 6.52e9), "free cash flow after buybacks (computed: "
                              "free cash flow minus share repurchases, no "
                              "dividends paid line in the data)"),
            "no-returns": ({}, None, "")
        }
        for case_id, (extra, expected, minus) in cases.items():
            self._check_after_returns(case_id, extra, expected, minus)

    def _check_after_returns(self, case_id, extra, expected, label):

        def facts(current, prior):
            rows = [_q(_YTD, current)]
            if prior is not None:
                rows.append(_q(_YTD_PRIOR, prior))
            return {"units": {"USD": rows}}

        us_gaap = {
            "NetCashProvidedByUsedInOperatingActivities": facts(
                12.52e9, 13.63e9),
            "PaymentsToAcquirePropertyPlantAndEquipment": facts(
                6.78e9, 6.11e9),
            **{
                tag: facts(*values)
                for tag, values in extra.items()
            }
        }
        lookup = sec_edgar._exact_lookup(
            tuple(date.fromisoformat(d) for d in _YTD),
            tuple(date.fromisoformat(d) for d in _YTD_PRIOR))
        items = sec_edgar._cash_flow_items(us_gaap, lookup)
        keys = [i.key for i in items]
        if expected is None:
            assert "free_cash_flow_after_returns" not in keys, case_id
            return
        after = _xbrl_item(items, "free_cash_flow_after_returns")
        assert after.computed and after.signed, case_id
        assert after.current == pytest.approx(expected[0]), case_id
        assert after.prior == (
            None
            if expected[1] is None else pytest.approx(expected[1])), case_id
        assert keys[-1] == "free_cash_flow_after_returns", case_id
        prior = (
            "(comparison period not tagged)" if after.prior is None else
            "vs " + sec_edgar._usd(after.prior, signed=True))
        assert sec_edgar._items_text(items).endswith(
            label + " " + sec_edgar._usd(after.current, signed=True) + " " +
            prior), case_id

    def test_debt_and_cash_change_lines(self):
        cases = {
            "general-debt-tags-and-cash-change": (
                {
                    "RepaymentsOfDebt": [8.376e9, 6.77e8],
                    "ProceedsFromIssuanceOfDebt": [3.5e7, 9.8e7],
                    "CashCashEquivalentsRestrictedCashAndRestricted"
                    "CashEquivalentsPeriodIncreaseDecreaseIncluding"
                    "ExchangeRateEffect": [-3.697e9, -6.026e9]
                }, "debt issued $35M vs $98M; debt repaid $8.38B vs $677M; "
                "net change in cash, cash equivalents and restricted cash "
                "-$3.70B vs -$6.03B"),
            "long-term-tag-first-older-cash-tag": (
                {
                    "RepaymentsOfLongTermDebt": [2e9, 1e9],
                    "RepaymentsOfDebt": [3e9, 2e9],
                    "CashAndCashEquivalentsPeriodIncreaseDecrease": [
                        5e8, -2e8
                    ]
                },
                "long-term debt repaid $2.00B vs $1.00B; net change in cash "
                "and cash equivalents +$500M vs -$200M"),
            "debt-net-of-issuance-costs": (
                {
                    "ProceedsFromDebtNetOfIssuanceCosts": [2.49e10, 1e9],
                    "CashAndCashEquivalentsPeriodIncreaseDecrease": [
                        1e9, -2e8
                    ]
                }, "debt issued (proceeds after issuance costs, before "
                "repayments) $24.90B vs $1.00B; net change in cash and cash "
                "equivalents +$1.00B vs -$200M"),
            "untagged-adds-nothing": ({}, "")
        }
        for case_id, (extra, expected) in cases.items():
            self._check_debt_and_cash(case_id, extra, expected)

    def _check_debt_and_cash(self, case_id, extra, expected):

        def facts(current, prior):
            return {
                "units": {
                    "USD": [_q(_YTD, current),
                            _q(_YTD_PRIOR, prior)]
                }
            }

        us_gaap = {
            "NetCashProvidedByUsedInOperatingActivities": facts(
                1.19e9, -1.39e9),
            "PaymentsToAcquirePropertyPlantAndEquipment": facts(
                2.01e9, 1.10e9),
            "PaymentsOfDividends": facts(1.72e8, 1.58e8),
            **{
                tag: facts(*values)
                for tag, values in extra.items()
            }
        }
        lookup = sec_edgar._exact_lookup(
            tuple(date.fromisoformat(d) for d in _YTD),
            tuple(date.fromisoformat(d) for d in _YTD_PRIOR))
        items = sec_edgar._cash_flow_items(us_gaap, lookup)
        text = sec_edgar._items_text(items)
        after = (
            "free cash flow after dividends (computed: free cash flow minus "
            "dividends paid, no share repurchases line in the data) -$992M "
            "vs -$2.65B")
        if not expected:
            assert text.endswith(after), case_id
            return
        assert text.endswith(after + "; " + expected), case_id
        cash = items[-1]
        assert cash.key == "net_change_in_cash" and cash.signed, case_id
        assert not cash.computed, case_id

    def test_no_after_returns_line_without_free_cash_flow(self):
        us_gaap = {
            "NetCashProvidedByUsedInOperatingActivities": {
                "units": {
                    "USD": [_q(_YTD, 5e9)]
                }
            },
            "PaymentsForRepurchaseOfCommonStock": {
                "units": {
                    "USD": [_q(_YTD, 1e9)]
                }
            }
        }
        lookup = sec_edgar._exact_lookup(
            tuple(date.fromisoformat(d) for d in _YTD), None)
        keys = [i.key for i in sec_edgar._cash_flow_items(us_gaap, lookup)]
        assert keys == ["operating_cash_flow", "share_repurchases"]

    def test_concept_table_is_unique_and_bad_facts_fail_open(self):
        keys = [g[0] for g in sec_edgar._BELOW_OPERATING_GROUPS]
        assert keys[:2] == ["pretax_income", "nonoperating_total"]
        assert "disposal_gain" not in keys
        assert "debt_extinguishment_gain" not in keys
        groups = (
            *sec_edgar._BELOW_OPERATING_GROUPS, *sec_edgar._CASH_FLOW_GROUPS)
        tags = [t for g in groups for t, _ in g[2]]
        assert len(tags) == len(set(tags))

        assert (
            sec_edgar._below_operating_items({
                "NonoperatingIncomeExpense": {
                    "units": {
                        "USD": [{
                            "start": "2026-04-01",
                            "end": "2026-06-30",
                            "val": "x",
                            "form": "10-Q"
                        }]
                    }
                }
            }, sec_edgar._quarter_lookup(date(2026, 6, 30), None)) == ())


class TestSecEdgarLookups:

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_latest_quarter_notes_a_newer_filing_missing_from_xbrl(
            self, monkeypatch):
        calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if "companyfacts" in request.url.path:
                return httpx.Response(
                    200, json=_facts(
                        NetIncomeLoss=[
                            _fact(
                                "2025-01-01", "2025-03-28", 3.33e9,
                                filed="2025-04-29"),
                            _fact(
                                "2026-01-01", "2026-04-03", 3.92e9,
                                filed="2026-04-30")
                        ]))
            return httpx.Response(
                200, json=_submissions({
                    "form": ["10-Q", "10-Q"],
                    "filingDate": ["2026-07-29", "2026-04-30"],
                    "reportDate": ["2026-07-03", "2026-04-03"],
                    "accessionNumber": ["a2", "a1"]
                }, []))

        monkeypatch.setattr(sec_edgar, "_cik_for", _ko_cik)
        monkeypatch.setattr(
            sec_edgar, "get_http_client", lambda: _mock_client(_handler))
        payload = await sec_edgar.latest_quarter_payload("KO")
        assert payload is not None and payload["filed"] == "2026-04-30"
        assert payload["text"].startswith(
            "Latest quarter in SEC XBRL -- the newer 10-Q filed 2026-07-29 "
            "is not in XBRL yet (SEC XBRL, 10-Q filed 2026-04-30")
        assert payload["newer_filing"] == "10-Q filed 2026-07-29"
        assert await sec_edgar.latest_quarter_payload("KO") == payload
        assert len(calls) == 2

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_filing_period_matches_form_and_date_and_fails_open(
            self, monkeypatch):
        calls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("-submissions-001.json"):
                return httpx.Response(
                    200, json={
                        "form": ["10-K"],
                        "filingDate": ["2019-02-27"],
                        "reportDate": ["2018-12-31"],
                        "accessionNumber": ["old"]
                    })
            return httpx.Response(
                200, json=_submissions({
                    "form": ["10-Q", "10-K/A", "10-K", "8-K"],
                    "filingDate": [
                        "2026-08-03", "2026-03-01", "2026-02-18", "2026-02-18"
                    ],
                    "reportDate": [
                        "2026-06-30", "2025-12-31", "2025-12-31", ""
                    ],
                    "accessionNumber": ["q", "ka", "k", "8k"]
                }, [{
                    "name": "CIK0000034088-submissions-001.json",
                    "filingFrom": "2005-10-04",
                    "filingTo": "2020-01-01"
                }]))

        monkeypatch.setattr(sec_edgar, "_cik_for", _ko_cik)
        monkeypatch.setattr(
            sec_edgar, "get_http_client", lambda: _mock_client(_handler))
        assert await sec_edgar.filing_period("XOM", "10-K",
                                             "2026-02-18") == FilingPeriod(
                                                 "10-K", "2026-02-18",
                                                 "2025-12-31", "k")
        amended = await sec_edgar.filing_period("XOM", "10-K/A", "2026-03-01")
        assert amended is not None and amended.accession == "ka"
        assert (
            await sec_edgar.filing_period("XOM", "10-Q", "2026-02-18") is None)
        assert len(calls) == 1
        old = await sec_edgar.filing_period("XOM", "10-K", "2019-02-27")
        assert old is not None and old.report_date == "2018-12-31"
        assert len(calls) == 2

        sec_edgar._reset_caches_for_tests()

        def _not_found(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        monkeypatch.setattr(
            sec_edgar, "get_http_client", lambda: _mock_client(_not_found))
        assert (
            await sec_edgar.filing_period("KO", "10-Q", "2026-07-29") is None)

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_concurrent_lookups_merge_an_older_page_once(
            self, monkeypatch):
        import asyncio

        page_name = "CIK1-submissions-001.json"
        both_waiting = asyncio.Event()
        page_calls: list[str] = []

        async def _get_json(url: str, **kwargs: Any) -> Any:
            if url.endswith(page_name):
                page_calls.append(url)
                if len(page_calls) == 2:
                    both_waiting.set()
                await both_waiting.wait()
                return {
                    "form": ["10-K"],
                    "filingDate": ["2019-02-27"],
                    "reportDate": ["2018-12-31"],
                    "accessionNumber": ["old"]
                }
            return {
                "filings": {
                    "recent": {},
                    "files": [{
                        "name": page_name,
                        "filingFrom": "2005-01-01",
                        "filingTo": "2020-01-01"
                    }]
                }
            }

        monkeypatch.setattr(sec_edgar, "_cik_for", _ko_cik)
        monkeypatch.setattr(sec_edgar, "_sec_get_json", _get_json)
        await sec_edgar._submissions("JPM")
        first, second = await asyncio.gather(
            sec_edgar.filing_period("JPM", "10-K", "2019-02-27"),
            sec_edgar.filing_period("JPM", "10-K", "2019-02-27"))
        assert first == second == FilingPeriod(
            "10-K", "2019-02-27", "2018-12-31", "old")
        subs = await sec_edgar._submissions("JPM")
        assert len(subs.rows) == 1 and subs.pages == []

    async def test_ticker_map_one_entry_per_ticker_and_daily_refresh(
            self, monkeypatch):
        downloads: list[str] = []
        payload = {
            "0": {
                "cik_str": 34088,
                "ticker": "XOM",
                "title": "EXXON MOBIL CORP"
            },
            "1": {
                "cik_str": 2115436,
                "ticker": "XOM",
                "title": "Exxon Holdings Inc."
            },
            "2": {
                "cik_str": 21344,
                "ticker": "KO",
                "title": "COCA COLA CO"
            }
        }

        async def _get(url, *, op_name, what):
            downloads.append(url)
            return payload

        today = [date(2026, 10, 4)]
        monkeypatch.setattr(sec_edgar, "_sec_get_json", _get)
        monkeypatch.setattr(sec_edgar, "_today", lambda: today[0])
        monkeypatch.setattr(sec_edgar, "_ticker_to_cik_cache", None)
        monkeypatch.setattr(sec_edgar, "_ticker_to_title_cache", None)
        monkeypatch.setattr(sec_edgar, "_ticker_map_day", None)
        ciks = await sec_edgar._load_ticker_to_cik()
        titles = await sec_edgar.load_ticker_to_title()
        assert (ciks["XOM"], titles["XOM"]) == (34088, "EXXON MOBIL CORP")
        assert len(downloads) == 1
        today[0] = date(2026, 10, 5)
        await sec_edgar._load_ticker_to_cik()
        assert len(downloads) == 2


_INTC_Q2_RAW = (
    "Net revenue $ 16,130 million. In the second quarter we recorded an "
    "impairment of $ 1,423 related to ... (in millions). Gross margin was "
    "35.2%. On July 23, 2026 the court dismissed the remaining claim. "
    "Item 2. Properties. Fiscal 2026 capital expenditures of $18 billion to "
    "$20 billion are expected.")
_DISMISSED_QUOTE = "On July 23, 2026 the court dismissed the remaining claim."
_IMPAIRMENT_QUOTE = (
    "In the second quarter we recorded an impairment of $ 1,423")
_CAPEX_QUOTE = "Fiscal 2026 capital expenditures of $18 billion to $20 billion"


def _intc_one_off(description: str, amount: str, oid: str) -> dict[str, Any]:
    return {
        "id": oid,
        "description": description,
        "amount_text": amount,
        "period_end": "2026-06-27",
        "period_months": 3,
        "part_of": None,
        "quote": _IMPAIRMENT_QUOTE
    }


_KO_ROWS = [
    SimpleNamespace(
        filename="KO_10-K_2026-02-20.txt", form="10-K",
        filing_date=date(2026, 2,
                         20), report_date=date(2025, 12, 31), records={
                             "legal_matters": [{
                                 "name": "Baltimore suit",
                                 "status": "pending",
                                 "status_date": None,
                                 "detail": "Public nuisance claim remains."
                             }]
                         }),
    SimpleNamespace(
        filename="KO_10-Q_2026-07-29.txt", form="10-Q",
        filing_date=date(2026, 7, 29), report_date=date(2026, 7, 3), records={
            "legal_matters": [{
                "name": "Baltimore suit",
                "status": "dismissed",
                "status_date": "2026-07-23",
                "detail": None
            }],
            "transactions": [],
            "guidance": [],
            "one_off_items": [{
                "description": "impairment",
                "amount_text": "$1.4 billion",
                "period": "Q2 2026"
            }]
        }),
    SimpleNamespace(
        filename="KO_10-Q_2026-04-30.txt", form="10-Q",
        filing_date=date(2026, 4, 30), report_date=None, records={})
]

_CHARGES_RAW = (
    "Item 1. Legal Proceedings\n"
    "In March 2026, the European Commission's decision became final, and in\n"
    "July 2026 we paid the fine of $ 5.2 billion. A hearing on the remaining\n"
    "claims is scheduled for November 12, 2026.\n"
    "The plaintiffs appealed the dismissal to the Ninth Circuit on May 4, "
    "2026.\n"
    "Restructuring and other charges          310          95\n"
    "We recognized a loss of $ 12.5 billion related to the change in fair "
    "value\n"
    "of the Escrowed Shares in the three months ended June 27, 2026, of "
    "which\n"
    "$ 2.6 billion related to the forward contract.\n"
    "For the six months ended June 27, 2026 we recognized $ 14.1 billion of "
    "such losses.\n"
    "We expect capital expenditures in excess of $ 25 billion in 2026.\n")
_EC_FINE_QUOTE = "the European Commission's decision became final"


def _charges_validated(data: dict[str, Any], **kwargs: Any) -> dict:
    options = {
        "form": "10-Q",
        "filing_date": "2026-07-24",
        "report_date": "2026-06-27",
        **kwargs
    }
    return _validated(data, _CHARGES_RAW, **options)


def _matter(name: str, status: str, quote: str, **extra: Any) -> dict:
    return {
        "name": name,
        "status": status,
        "status_date": extra.pop("date", None),
        "quote": quote,
        "detail": None,
        **extra
    }


def _charge(description: str, amount: str, quote: str, **extra: Any) -> dict:
    return {
        "description": description,
        "amount_text": amount,
        "period_end": extra.pop("period_end", "2026-06-27"),
        "period_months": extra.pop("period_months", 3),
        "quote": quote,
        **extra
    }


_META_RAW = (
    "In millions, except per share amounts.\n"
    "Revenue $ 47,516 $ 39,071 $ 89,826 $ 75,377\n"
    "Net income for the three months ended June 30, 2026 was $ 18,337 "
    "million compared with $ 13,465 million for the three months ended "
    "June 30, 2025.\n"
    "Net income for the six months ended June 30, 2026 was $42.62 billion "
    "compared with $34.98 billion for the six months ended June 30, 2025.\n"
    "As of June 30, 2026, we had additional lease agreements that have not "
    "yet commenced. These lease obligations were approximately $278.99 "
    "billion, compared with approximately $103.77 billion as of December "
    "31, 2025.\n"
    "We provided residual value guarantees with a maximum aggregate exposure "
    "of approximately $ 13 billion as of June 30, 2026.\n"
    "The jury returned a verdict of $375 million in civil penalties, and the "
    "state seeks an additional $3.7 billion in the remedies phase of the "
    "trial.\n"
    "On May 4, 2026, the court granted in part our motion to dismiss the "
    "consolidated complaint in the securities litigation.\n")


def _meta_validated(
        data: dict[str, Any],
        report_date: str | None = "2026-06-30") -> dict[str, Any]:
    return _validated(
        data, _META_RAW, ticker="META", filename="META_10-Q_2026-07-30",
        form="10-Q", filing_date="2026-07-30", report_date=report_date)


def _youth_case(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": "New Mexico youth safety case",
        "status": "pending",
        "status_date": None,
        "detail": None,
        "exposure_text": "$3.7 billion",
        "quote": "The jury returned a verdict of $375 million in civil "
        "penalties, and the state seeks an additional $3.7 billion"
    }
    item.update(overrides)
    return item


_PATENT_RAW = "\n".join([
    "Condensed Consolidated Statements of Income (in millions)",
    "Three Months Ended March 31,", "Revenue $ 56,311 $ 42,314",
    "Operating income 18,442 17,555",
    "On March 31, 2026, the court entered judgment against the Company in "
    "the Example Patent Litigation and the Company has appealed the "
    "decision.",
    "The Example Antitrust Matter remains pending before the district "
    "court "
    "and no trial date has been set by the court at this time.",
    "We expect the 737 production rate to increase to 47 per month in "
    "2026 "
    "subject to regulatory approval.",
    "In July 2026, we entered into lease agreements for data centers with "
    "future lease payments of $68 billion that have not yet commenced."
])

_RESTRUCTURING_RAW = "\n".join([
    "Condensed Consolidated Statements of Operations (in millions)",
    "Three Months Ended June 30,", "Revenue $ 56,311 $ 42,314",
    "Net loss $ (1,180) $ (445)",
    "In the quarter we recorded a restructuring charge of $(1,180) "
    "million "
    "related to the closure of two plants.",
    "On June 30, 2026, the court entered judgment against the Company in "
    "the Example Patent Litigation and the Company has appealed it.",
    "As of June 30, 2026, the Example Antitrust Matter remains pending "
    "before the district court.",
    "Plaintiffs in the Example Appraisal Action seek $46.50 per share for "
    "their shares, and the matter remains pending in the Court of "
    "Chancery.",
    "Plaintiffs in the Example Class Action seek damages of billions of "
    "dollars and the matter remains pending in federal district court.",
    "We expect capital expenditures of $280 in fiscal 2027 for new plants "
    "and equipment."
])


def _restructuring_validated(data: dict[str, Any]) -> dict[str, Any]:
    full: dict[str, Any] = {key: [] for key in frec.CATEGORIES}
    full.update(data)
    return _validated(
        full, _RESTRUCTURING_RAW, form="10-Q", filing_date="2026-07-30",
        report_date="2026-06-30")


def _restructuring(amount: str) -> dict[str, Any]:
    return {
        "description": "restructuring charge",
        "amount_text": amount,
        "period_end": "2026-06-30",
        "period_months": 3,
        "quote": "In the quarter we recorded a restructuring charge of "
        "$(1,180) million related to the closure of two plants."
    }


class TestFilingRecords:

    def test_split_summary_and_records(self):
        output = (
            f"Summary paragraph.\n\n{frec.RECORDS_MARKER}\n"
            f"{json.dumps({'guidance': []})}")
        summary, block = frec.split_summary_and_records(output)
        assert summary == "Summary paragraph." and block == {"guidance": []}
        assert frec.split_summary_and_records("Only prose.") == (
            "Only prose.", None)
        fenced = (
            f"Prose\n{frec.RECORDS_MARKER}\n```json\n"
            '{"guidance": []}\n```')
        assert frec.split_summary_and_records(fenced)[1] == {"guidance": []}
        cut = f'Prose\n{frec.RECORDS_MARKER}\n{{"guidance": [{{"metric": '
        assert frec.split_summary_and_records(cut) == ("Prose", None)

    @pytest.mark.usefixtures("fresh_sec_caches")
    def test_validate_records_keeps_only_structurally_valid_records(self):
        data = {
            "legal_matters": [{
                "name": "Baltimore plastic packaging suit",
                "status": "Dismissed",
                "status_date": "2026-07-23",
                "quote": _DISMISSED_QUOTE,
                "detail": "Remaining claim dismissed."
            }, {
                "name": "Some case",
                "status": "won",
                "status_date": None,
                "quote": _DISMISSED_QUOTE,
                "detail": ""
            }, {
                "name": "Other case",
                "status": "pending",
                "status_date": "July 2026",
                "quote": _DISMISSED_QUOTE
            }],
            "transactions": [{
                "name": "Unit sale",
                "type": "divestiture",
                "status": "closing",
                "date": None,
                "quote": _DISMISSED_QUOTE,
                "detail": "x"
            }],
            "guidance": [{
                "metric": "capital expenditures",
                "value_text": "$18 billion to $20 billion",
                "period_covered": "fiscal 2026",
                "quote": _CAPEX_QUOTE
            }, {
                "metric": "revenue",
                "value_text": "$25 billion",
                "period_covered": "2026",
                "quote": _CAPEX_QUOTE
            }],
            "one_off_items": [
                _intc_one_off("goodwill impairment", "$1,423 million", "O1"),
                _intc_one_off("rounded impairment", "$1.4 billion", "O2"),
                _intc_one_off("tax benefit", "$797 million", "O3")
            ]
        }
        kept = frec.validate_records(
            data, _INTC_Q2_RAW, form="10-Q", filing_date="2026-07-24",
            report_date="2026-06-27")
        assert kept is not None
        assert [r["name"] for r in kept["legal_matters"]
                ] == ["Baltimore plastic packaging suit"]
        assert kept["legal_matters"][0]["status"] == "dismissed"
        assert kept["transactions"] == []
        assert [r["metric"]
                for r in kept["guidance"]] == ["capital expenditures"]
        assert [r["description"]
                for r in kept["one_off_items"]] == ["goodwill impairment"]
        assert kept["one_off_items"][0]["period_months"] == "3"
        assert frec.validate_records(None, _INTC_Q2_RAW) is None

    @pytest.mark.usefixtures("fresh_sec_caches")
    def test_render_and_lookup_filing_records(self):
        text = frec.render_filing_records(_KO_ROWS)  # type: ignore[arg-type]
        assert text.split("\n")[1:] == [
            "F1 = 10-Q filed 2026-07-29, period of report 2026-07-03 "
            "[NEWEST]",
            "  F1.O1 unusual item: impairment: $1.4 billion (Q2 2026)",
            "  F1.L1 legal matter: Baltimore suit -- status: dismissed "
            "(2026-07-23).",
            "F2 = 10-Q filed 2026-04-30, period of report not available "
            "[OLDER]", "  (no records)",
            "F3 = 10-K filed 2026-02-20, period of report 2025-12-31 [OLDER]",
            "  F3.L1 legal matter: Baltimore suit -- status: pending. Public "
            "nuisance claim remains."
        ]
        record = frec.record_by_id(_KO_ROWS, "f3.l1")  # type: ignore[arg-type]
        assert record is not None
        assert (
            record["id"], record["status"], record["filing_date"],
            record["report_date"]) == (
                "F3.L1", "pending", "2026-02-20", "2025-12-31")
        for missing in ("F1.L2", "F9.L1", "nonsense"):
            assert (
                frec.record_by_id(_KO_ROWS, missing)
                is None)  # type: ignore[arg-type]
        assert frec.render_filing_records([]) == ""

    def test_records_need_a_quote_found_in_the_filing(self, capsys):
        squashed = frec.squash(_CHARGES_RAW)
        assert frec.quote_in_filing(
            "in July 2026 we paid the fine of $5.2 billion", squashed)
        assert frec.quote_in_filing(
            "the European Commission's decision became final ... we paid the "
            "fine", squashed)
        assert not frec.quote_in_filing(
            "the court entered judgment for us", squashed)
        assert not frec.quote_in_filing("we paid the fine", squashed)

        kept = _charges_validated({
            "legal_matters": [
                _matter("EC Android fine", "judgment", _EC_FINE_QUOTE),
                _matter(
                    "Made-up case", "pending",
                    "the court has not yet ruled on the motion"), {
                        "name": "No quote",
                        "status": "pending"
                    }
            ]
        })
        assert [r["name"]
                for r in kept["legal_matters"]] == ["EC Android fine"]
        out = capsys.readouterr().out
        assert "quote_not_in_filing" in out and "missing_quote" in out

    def test_one_off_items_keep_only_the_filings_own_period(self):
        quarter = (
            "We recognized a loss of $ 12.5 billion related to the "
            "change in fair value")
        year_to_date = (
            "For the six months ended June 27, 2026 we "
            "recognized $ 14.1 billion")
        kept = _charges_validated({
            "one_off_items": [
                _charge("O1", "$12.5 billion", quarter),
                _charge("O2", "$14.1 billion", year_to_date, period_months=6),
                _charge(
                    "O3", "$12.5 billion", quarter, period_end="2025-06-28"),
                _charge("O4", "$12.5 billion", quarter, period_months="three")
            ]
        })
        assert [r["description"] for r in kept["one_off_items"]] == ["O1"]
        annual = _charges_validated(
            {"one_off_items": [_charge("O1", "$12.5 billion", quarter)]},
            form="10-K")
        assert annual["one_off_items"] == []

    def test_transaction_completed_before_the_period_is_dropped(self, capsys):
        quote = (
            "the European Commission's decision became final, and in "
            "July 2026")
        deal = {
            "name": "Old sale",
            "type": "divestiture",
            "status": "completed",
            "quote": quote,
            "detail": None
        }
        kept = _charges_validated({
            "transactions": [{
                **deal, "date": "2025-09-15"
            }, {
                **deal, "name": "This quarter",
                "date": "2026-05"
            }, {
                **deal, "name": "Open deal",
                "status": "pending",
                "date": "2025-01-10"
            }, {
                **deal, "name": "Undated close",
                "date": None
            }, {
                **deal, "name": "Note issue",
                "type": "financing",
                "status": "pending",
                "date": None
            }]
        })
        assert [r["name"] for r in kept["transactions"]
                ] == ["This quarter", "Open deal", "Note issue"]
        out = capsys.readouterr().out
        assert "not_own_period" in out and "missing_date" in out
        annual = _charges_validated(
            {"transactions": [{
                **deal, "date": "2025-09-15"
            }]}, form="10-K", report_date="2025-12-31")
        assert len(annual["transactions"]) == 1

    def test_rendered_records_say_they_are_unverified_and_show_the_quote(self):
        row = SimpleNamespace(
            filename="GOOGL_10-Q_2026-07-23.txt", form="10-Q",
            filing_date=date(2026, 7, 23), report_date=date(2026, 6, 30),
            records={
                "legal_matters": [
                    _matter("EC Android fine", "judgment", _EC_FINE_QUOTE)
                ],
                "one_off_items": [_charge("O1", "$12.5 billion", "q " * 150)]
            })
        text = frec.render_filing_records([row])  # type: ignore[list-item]
        assert "can be wrong or incomplete" in text
        assert (
            "F1.L1 legal matter: EC Android fine -- status: judgment. "
            "Filing text: \"the European Commission's decision became "
            'final"') in text
        assert "(3 months ended 2026-06-27)" in text
        assert text.split("\n")[2].startswith("  F1.O1 unusual item")
        assert text.split("\n")[2].endswith('..."')

    def test_status_date_on_the_period_end(self, capsys):
        kept = _meta_validated({
            "legal_matters": [
                _youth_case(name="Period end", status_date="2026-06-30"),
                _youth_case(name="Event date", status_date="2026-05-04"),
                _youth_case(name="Partial", status_date="2026-06")
            ]
        })
        dates = {r["name"]: r["status_date"] for r in kept["legal_matters"]}
        assert dates == {
            "Period end": None,
            "Event date": "2026-05-04",
            "Partial": "2026-06"
        }
        assert "status_date_is_period_end_cleared" in capsys.readouterr().out

        kept = _meta_validated(
            {"legal_matters": [_youth_case(status_date="2026-06-30")]},
            report_date=None)
        assert kept["legal_matters"][0]["status_date"] == "2026-06-30"

        quote = (
            "On March 31, 2026, the court entered judgment against the "
            "Company in the Example Patent Litigation")
        kept = _validated({
            "legal_matters": [{
                "name": "Example Patent Litigation",
                "status": "appeal",
                "status_date": "2026-03-31",
                "quote": quote
            }]
        }, _PATENT_RAW, form="10-Q", filing_date="2026-04-30",
                          report_date="2026-03-31")
        assert kept["legal_matters"][0]["status_date"] == "2026-03-31"

    def test_amounts_are_checked_against_the_quote(self, capsys):
        kept = _restructuring_validated({
            "one_off_items": [
                _restructuring("$(8,888) million"),
                _restructuring("$(1,180) million"),
                _restructuring("1,180")
            ]
        })
        assert [r["amount_text"]
                for r in kept["one_off_items"]] == ["$(1,180) million"]
        out = capsys.readouterr().out
        assert "amount_unreadable" in out and "amount_not_in_quote" in out

        kept = _restructuring_validated({
            "legal_matters": [{
                "name": "Example Class Action",
                "status": "pending",
                "status_date": None,
                "detail": None,
                "quote": (
                    "Plaintiffs in the Example Class Action seek "
                    "damages of "
                    "billions of dollars and the matter remains "
                    "pending"),
                "exposure_text": "billions of dollars"
            }]
        })
        assert kept["legal_matters"][0]["exposure_text"] is None
        assert "'amount_unreadable_cleared': 1" in capsys.readouterr().out

        guidance = {
            "metric": "capital expenditures",
            "value_text": "$280",
            "period_covered": "fiscal 2027",
            "quote": "We expect capital expenditures of $280 in "
            "fiscal 2027"
        }
        assert (
            _restructuring_validated({"guidance": [guidance]
                                      })["guidance"] == [])
        assert "amount_without_unit" in capsys.readouterr().out
        assert "value_text" in frec.RECORDS_ONLY_PROMPT
        assert "per share" in frec.RECORDS_ONLY_PROMPT

    def test_missing_categories(self):
        assert frec.missing_categories(None) == []
        assert (
            frec.missing_categories({key: []
                                     for key in frec.CATEGORIES}) == [])
        data: dict[str, Any] = {
            key: []
            for key in frec.CATEGORIES if key != "guidance"
        }
        data["one_off_items"] = None
        data["unusual_items"] = []
        assert frec.missing_categories(data) == ["one_off_items", "guidance"]

    def test_records_summary_and_agent_prompts(self):
        prompt = frec.RECORDS_ONLY_PROMPT
        assert list(frec.CATEGORIES) == [
            "one_off_items", "guidance", "transactions", "legal_matters"
        ]
        assert "with these four keys" in prompt
        assert (
            prompt.index('"one_off_items"') < prompt.index('"guidance"') <
            prompt.index('"transactions"') < prompt.index('"legal_matters"'))
        for gone in ('"commitments"', '"results"', '"line"', '"part_of"',
                     '"id"', '"next_event"', "prior_id", "PREVIOUS FILING",
                     "RECORDS FROM THE PREVIOUS FILING",
                     "every one the filing names",
                     "fair-value changes that appear every period"):
            assert gone not in prompt
        for words in ("the most material first",
                      "notes on other income (expense)",
                      "IS an unusual item even when such a line appears every",
                      "keeps a minus sign",
                      "the amount claimed, sought, assessed or awarded",
                      "the largest such amount",
                      "accrued, paid, escrowed or reserved",
                      "never a valuation",
                      "never the period end of this report",
                      "use a table row only when no sentence states it",
                      "period header words"):
            assert words in prompt
        assert not hasattr(frec, "RECORDS_PROMPT")

        for prompt in (filing_summary._10K_SYSTEM_PROMPT,
                       filing_summary._10Q_SYSTEM_PROMPT):
            assert filing_summary._CAPITAL_ALLOCATION_BULLET in prompt
            assert frec.RECORDS_MARKER not in prompt
        bullet = filing_summary._CAPITAL_ALLOCATION_BULLET
        for words in ("capital expenditures",
                      "share repurchases and dividends", "cash and debt",
                      "new debt", "guarantees",
                      "gap between operating income and net income"):
            assert words in bullet
        assert (
            filing_summary._SUMMARY_MAX_TOKENS
            < filing_summary._RETRY_MAX_TOKENS)
        assert (
            filing_summary._RECORDS_MAX_TOKENS
            < filing_summary._RETRY_MAX_TOKENS)
        assert "commitments" not in filing_module._SYSTEM_PROMPT
        assert "reported-result" not in filing_module._SYSTEM_PROMPT
        assert "next:" not in risk_module._SYSTEM_PROMPT
        assert "below operating income" in debate_common.UNUSUAL_ITEMS_RULE
        assert [key for key, _ in debate_common._FILING_PERIOD_TITLES] == [
            debate_common.YEAR_TO_DATE_UNUSUAL_KEY,
            debate_common.PRIOR_YEAR_UNUSUAL_KEY
        ]


class TestFilingSummaries:

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_records_only_extraction_uses_the_backfill_tier(self):
        partial = _RecordingRouter([
            (f"{frec.RECORDS_MARKER}\n{json.dumps({'guidance': []})}", None)
        ])
        assert (
            await filing_summary.extract_filing_records(
                _INTC_Q2_RAW,
                form="10-Q",
                ticker="INTC",
                filing_date="2026-07-24",
                llm_router=partial  # type: ignore[arg-type]
            ) is None)
        complete = {key: [] for key in frec.CATEGORIES}
        router = _RecordingRouter([
            (f"{frec.RECORDS_MARKER}\n{json.dumps(complete)}", None)
        ])
        records = await filing_summary.extract_filing_records(
            _INTC_Q2_RAW,
            form="10-Q",
            ticker="INTC",
            filing_date="2026-07-24",
            llm_router=router  # type: ignore[arg-type]
        )
        assert records is not None and records.served_by == "p/m"
        assert records.records == {
            "one_off_items": [],
            "guidance": [],
            "transactions": [],
            "legal_matters": []
        }
        assert router.calls[0]["tier"] == LLMTier.CHEAP
        assert (
            await filing_summary.extract_filing_records(
                _INTC_Q2_RAW,
                form="10-Q",
                ticker="INTC",
                filing_date="2026-07-24",
                llm_router=_RecordingRouter([(None, None)
                                             ])  # type: ignore[arg-type]
            ) is None)

    async def test_summary_cut_at_the_limit_is_retried_with_a_larger_limit(
            self, capsys):
        router = _RecordingRouter([
            ("Summary cut mid", "FinishReason.MAX_TOKENS"),
            ("Full summary.", "FinishReason.STOP")
        ])
        out = await filing_summary.summarize_filing(
            _CHARGES_RAW,
            form="10-Q",
            ticker="INTC",
            filing_date="2026-07-24",
            llm_router=router  # type: ignore[arg-type]
        )
        assert (out.text, out.truncated) == ("Full summary.", False)
        assert not hasattr(out, "records")
        assert [c["max_tokens"] for c in router.calls] == [12288, 16384]
        assert frec.RECORDS_MARKER not in (
            router.calls[0]["messages"][0].content)
        assert "filing_summary.output_limit_retry" in capsys.readouterr().out

        still_cut = _RecordingRouter([("Cut", "length"),
                                      ("Cut again", "length")])
        out = await filing_summary.summarize_filing(
            _CHARGES_RAW,
            form="10-Q",
            ticker="INTC",
            filing_date="2026-07-24",
            llm_router=still_cut  # type: ignore[arg-type]
        )
        assert (out.text, out.truncated) == ("Cut again", True)
        assert "filing_summary.output_truncated" in capsys.readouterr().out

    async def test_records_only_call_keeps_the_cap_and_names_the_model(
            self, capsys):
        records_reply = _records_reply({
            **{
                key: []
                for key in frec.CATEGORIES
            }, "legal_matters": [
                _matter("EC Android fine", "judgment", _EC_FINE_QUOTE)
            ]
        }, "")
        router = _RecordingRouter([("cut", "FinishReason.MAX_TOKENS"),
                                   (records_reply, "FinishReason.STOP")])
        big = _CHARGES_RAW + "x" * filing_summary.RECORDS_MAX_CHARS
        records = await filing_summary.extract_filing_records(
            big,
            form="10-Q",
            ticker="GOOGL",
            filing_date="2026-07-23",
            llm_router=router,  # type: ignore[arg-type]
            report_date="2026-06-30")
        assert records is not None and records.served_by == "p/m"
        assert records.records["legal_matters"][0]["name"] == (
            "EC Android fine")
        sent = router.calls[0]["messages"][1].content
        assert len(sent) == filing_summary.RECORDS_MAX_CHARS
        assert router.calls[0]["tier"] == LLMTier.CHEAP
        out = capsys.readouterr().out
        assert "filing_summary.records_served_by" in out and "p/m" in out
        assert [c["max_tokens"] for c in router.calls] == [10240, 16384]


class _ChunkRetriever:

    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks

    async def retrieve(self, query: str, *,
                       ticker: str | None = None) -> list[RetrievedChunk]:
        return self.chunks


def _filing_chunk(source: str, filed: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{source}-{text[:5]}", text=text, score=0.5,
        category=DocumentCategory.FILING, ticker="KO", source=source,
        content_date=filed)


async def _no_filing_records(ticker: str) -> list[object]:
    return []


async def _no_quarter(ticker: str) -> None:
    return None


async def _ko_period(
        ticker: str, form: str, filing_date: str) -> FilingPeriod | None:
    reports = {"2026-07-29": "2026-07-03", "2026-02-20": "2025-12-31"}
    if filing_date not in reports:
        return None
    return FilingPeriod(
        form, filing_date, reports[filing_date], "0000021344-26-1")


_KO_10K = (
    "**Summary of Coca-Cola Co. 2025 Form 10-K** Core Financial Results "
    "(2025 vs. 2024). Environmental: Lawsuits in Baltimore (MD) and Los "
    "Angeles allege plastic packaging harms; public nuisance claims remain "
    "active.")
_KO_10Q = (
    "**Q2 2026 Financial Results** Revenue rose 7% to $13.4B. Net income "
    "grew 16% to $4.4B. 2. **Baltimore Plastic Packaging Lawsuit**: Filed "
    "June 2024, the suit alleged environmental harm from plastic packaging. "
    "The court dismissed all claims except public nuisance in July 2025, and "
    "the **remaining claim was dismissed on July 23, 2026**. **New/Updated "
    "Risk Factors** - **Global Minimum Tax**: rules apply.")
_KO_OLD_10Q = "This quarter, Coca-Cola reported revenue of $12.455B (+5% YoY)."


class TestSearchFilingsTool:

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_header_once_periods_from_edgar_and_failing_open(self):
        lookups: list[tuple[str, str, str]] = []

        async def _period(
                ticker: str, form: str, filed: str) -> FilingPeriod | None:
            lookups.append((ticker, form, filed))
            if filed == "2026-07-29":
                return FilingPeriod(form, filed, "2026-07-03", "a")
            return None

        async def _records(ticker: str) -> list[Any]:
            return _KO_ROWS

        async def _quarter(ticker: str) -> dict[str, Any]:
            return {
                "text": "Latest quarter (SEC XBRL, ...): revenue $12.47B "
                "vs $11.13B (+12%)"
            }

        tool = SearchFilingsTool(
            ticker="KO",
            retriever=_ChunkRetriever(
                [  # type: ignore[arg-type]
                    _filing_chunk(
                        "KO_10-Q_2026-07-29", "2026-07-29", "Q2 results."
                    ),
                    _filing_chunk(
                        "KO_10-Q_2026-07-29", "2026-07-29", "Legal."
                    ),
                    _filing_chunk(
                        "KO_10-K_2026-02-20", "2026-02-20", "Annual."
                    )
                ]
            ),
            period_lookup=_period,
            records_loader=_records,
            quarter_lookup=_quarter
        )
        first = await tool.run(query="results")
        assert first.startswith("Latest quarter (SEC XBRL, ...)")
        assert (
            "F1.L1 legal matter: Baltimore suit -- status: dismissed" in first)
        assert (
            "[10-Q; period: quarter ended 2026-07-03; NEWEST filing in "
            "these results]") in first
        assert (
            "[10-K; period: not available from SEC EDGAR; OLDER filing"
            in first)
        second = await tool.run(query="legal")
        assert second.startswith(
            "(Latest-quarter SEC XBRL figures and filing records were listed")
        assert "Baltimore suit" not in second.split("Filing search results")[0]
        assert sorted(set(lookups)) == [("KO", "10-K", "2026-02-20"),
                                        ("KO", "10-Q", "2026-07-29")]
        assert len(lookups) == 2

        async def _boom(*args: Any) -> Any:
            raise RuntimeError("db down")

        tool = SearchFilingsTool(
            ticker="KO",
            retriever=_ChunkRetriever([]),  # type: ignore
            period_lookup=_boom,
            records_loader=_boom,
            quarter_lookup=_boom)
        assert (await
                tool.run(query="x")).startswith("No filing passages found")

    async def test_passages_listed_newest_first_with_form_period_and_recency(
            self):
        tool = SearchFilingsTool(
            ticker="KO",
            retriever=_ChunkRetriever(
                [  # type: ignore[arg-type]
                    _filing_chunk("KO_10-K_2026-02-20", "2026-02-20", _KO_10K),
                    _filing_chunk(
                        "KO_10-Q_2025-10-23", "2025-10-23", _KO_OLD_10Q
                    ),
                    _filing_chunk("KO_10-Q_2026-07-29", "2026-07-29", _KO_10Q)
                ]
            ),
            period_lookup=_ko_period,
            records_loader=_no_filing_records,
            quarter_lookup=_no_quarter
        )
        result = await tool.run(query="legal proceedings")
        lines = result.split("\n")
        assert lines[1] == NEWEST_FILING_GUIDANCE
        assert lines[2].startswith(
            "- (KO) [KO_10-Q_2026-07-29] (filed 2026-07-29) [10-Q; period: "
            "quarter ended 2026-07-03; NEWEST filing in these results]: ")
        assert lines[3].startswith(
            "- (KO) [KO_10-K_2026-02-20] (filed 2026-02-20) [10-K; period: "
            "fiscal year ended 2025-12-31; OLDER filing -- may be superseded "
            "by the 10-Q filed 2026-07-29]")
        assert (
            "[KO_10-Q_2025-10-23] (filed 2025-10-23) [10-Q; period: not "
            "available from SEC EDGAR") in lines[4]


def _unusual(
        description: str, amount: str, end: str, months: Any = "3",
        line: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "description": description,
        "amount_text": amount,
        "period_end": end,
        "period_months": months,
        "quote": f"we recognized {amount} for {description.lower()}"
    }
    if line:
        out["line"] = line
    return out


_META_Q2_2026 = _Row(
    "META_10-Q_2026-07-30.txt", "10-Q", date(2026, 7, 30), date(2026, 6, 30), {
        "one_off_items": [
            _unusual("Severance expenses", "$1.18 billion", "2026-06-30")
        ],
        "results": [{
            "metric": "net_income",
            "period_months": 6,
            "period_end": "2026-06-30",
            "amount_text": "$ 42.62 billion",
            "prior_amount_text": "$34.98 billion",
            "quote": "Net income was $42.62 billion"
        }, {
            "metric": "revenue",
            "period_months": 3,
            "period_end": "2026-06-30",
            "amount_text": "$58.3 billion",
            "prior_amount_text": None,
            "quote": "Revenue was $58.3 billion"
        }, {
            "metric": "ebitda",
            "period_months": 3,
            "period_end": "2026-06-30",
            "amount_text": "$1 billion",
            "quote": "x"
        }],
        "commitments": [{
            "name": "Leases not yet commenced",
            "kind": "lease",
            "amount_text": "$ 24.5 billion",
            "as_of": "2026-06-30",
            "prior_amount_text": "$20.1 billion",
            "quote": "q",
            "prior_id": None
        }, {
            "name": "Guarantee of a joint venture's debt",
            "kind": "guarantee",
            "amount_text": "$2 billion",
            "as_of": "",
            "prior_amount_text": None,
            "quote": "q"
        }, {
            "name": "",
            "kind": "other",
            "amount_text": "$1 billion"
        }]
    })
_META_Q1_2026 = _Row(
    "META_10-Q_2026-04-30.txt", "10-Q", date(2026, 4, 30), date(2026, 3, 31), {
        "one_off_items": [
            _unusual(
                "Discrete income tax benefit", "$8.03 billion", "2026-03-31",
                line="tax"),
            _unusual(
                "Six-month thing", "$9 billion", "2026-03-31", months="6")
        ]
    })
_META_FY_2025 = _Row(
    "META_10-K_2026-01-29.txt", "10-K", date(2026, 1, 29), date(2025, 12, 31),
    {
        "one_off_items": [
            _unusual(
                "OBBBA tax charge", "$15.93 billion", "2025-12-31",
                months="12")
        ]
    })
_META_Q3_2025 = _Row(
    "META_10-Q_2025-10-30.txt", "10-Q", date(2025, 10, 30), date(2025, 9, 30),
    {
        "one_off_items": [
            _unusual(
                "OBBBA discrete tax charge", "$15.93 billion", "2025-09-30")
        ]
    })
_META_Q2_2025 = _Row(
    "META_10-Q_2025-07-31.txt", "10-Q", date(2025, 7, 31), date(2025, 6, 30), {
        "one_off_items": [
            _unusual(
                "Unrealized loss on equity securities", "$511 million",
                "2025-06-30"),
            _unusual(
                "Six-month loss", "$700 million", "2025-06-30", months="6")
        ]
    })
_META = [
    _META_Q2_2025, _META_Q3_2025, _META_FY_2025, _META_Q1_2026, _META_Q2_2026
]


class TestUnusualItemLines:

    def test_year_to_date_and_prior_year_items(self):
        duplicate = _Row(
            "META_10-Q_2026-05-01.txt", "10-Q", date(2026, 4, 29),
            date(2026, 3, 31), {
                "one_off_items": [
                    _unusual(
                        "Same benefit again", "$8.03 billion", "2026-03-31",
                        months=None),
                    _unusual("Other", "$5 million", "2026-03-31", months=None)
                ]
            })
        lines = year_to_date_unusual_item_lines([*_META, duplicate])
        assert len(lines) == 2
        assert lines[1].startswith(
            "Other: $5 million (period ended 2026-03-31)")

        assert year_to_date_unusual_item_lines(_META[:3]) == []
        assert year_to_date_unusual_item_lines(_META[:4]) == []
        assert (
            year_to_date_unusual_item_lines(
                [_META_Q3_2025, _META_Q1_2026, _META_Q2_2026]) == [])
        assert year_to_date_unusual_item_lines([]) == []
        old_10k = _Row(
            "META_10-K_2025-01-30.txt", "10-K", date(2025, 1, 30),
            date(2024, 12, 31))
        assert (
            year_to_date_unusual_item_lines(
                [old_10k, _META_Q2_2025, _META_Q2_2026]) == [])

        assert prior_year_unusual_item_lines(_META) == [
            "Unrealized loss on equity securities: $511 million (3 months "
            'ended 2025-06-30) [10-Q filed 2025-07-31] Filing text: "we '
            "recognized $511 million for unrealized loss on equity "
            'securities"'
        ]
        assert prior_year_unusual_item_lines(_META[:4]) == []
        assert prior_year_unusual_item_lines([]) == []

    def test_year_to_date_window_from_record_periods(self):
        newest = _Row(
            "XOM_10-Q_2026-08-03.txt", "10-Q", date(2026, 8, 3),
            date(2026, 6, 30),
            {"one_off_items": [_unusual("Q2 charge", "$1.2B", "2026-06-30")]})
        q1 = _Row(
            "XOM_10-Q_2026-05-04.txt", "10-Q", date(2026, 5, 4), None, {
                "one_off_items": [
                    _unusual("Hedge loss", "-$706 million", "2026-03-31"),
                    _unusual("Unknown span", "$9M", "2026-03-31", None)
                ]
            })
        annual = _Row(
            "XOM_10-K_2026-02-18.txt", "10-K", date(2026, 2, 18), None, {
                "one_off_items": [
                    _unusual("Impairment", "$1.6B", "2025-12-31", "12")
                ]
            })
        prior_q3 = _Row(
            "XOM_10-Q_2025-11-03.txt", "10-Q", date(2025, 11, 3), None,
            {"one_off_items": [_unusual("Old", "$5M", "2025-09-30")]})
        with capture_logs() as logs:
            lines = year_to_date_unusual_item_lines(
                [newest, q1, annual, prior_q3])
        assert lines == [
            "Hedge loss: -$706 million (3 months ended 2026-03-31) "
            '[10-Q filed 2026-05-04] Filing text: "we recognized -$706 '
            'million for hedge loss"'
        ]
        assert [e["event"]
                for e in logs] == ["filing.ytd_window_from_record_periods"]
        assert logs[0]["year_end"] == "2025-12-31"
        assert logs[0]["items"] == 1

        no_annual_period = _Row(
            annual.filename, "10-K", annual.filing_date, None, {})
        with capture_logs() as logs:
            assert (
                year_to_date_unusual_item_lines([newest, q1,
                                                 no_annual_period]) == [])
        assert [e["event"] for e in logs] == ["filing.ytd_window_unknown"]


class _SessionOf:

    def __init__(self, session) -> None:
        self.session = session

    def __call__(self) -> "_SessionOf":
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return None


class TestBackfillScript:

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_dry_run_and_apply(self, monkeypatch, capsys):
        from scripts import backfill_filing_records as backfill

        llm_calls: list[str] = []
        stored: list[str] = []

        async def _pending_done(ticker, redo=False, latest=None, filenames=()):
            return [("KO_10-Q_2026-07-29.txt", "10-Q", "2026-07-29")]

        async def _raw(ticker, filename, form, filing_date):
            return _INTC_Q2_RAW, None, "2026-07-03"

        async def _extract(raw, **kwargs):
            llm_calls.append(kwargs["filing_date"])
            return filing_summary.ExtractedRecords(dict(_NO_RECORDS), "p/m")

        async def _store(**kwargs):
            stored.append(kwargs["filename"])
            return True

        monkeypatch.setattr(backfill, "_pending", _pending_done)
        monkeypatch.setattr(backfill, "_raw_text", _raw)
        monkeypatch.setattr(backfill, "extract_filing_records", _extract)
        monkeypatch.setattr(backfill, "store_filing_records", _store)

        counts = await backfill.run(["KO"], apply=False)
        assert counts == {"pending": 1, "stored": 0, "failed": 0}
        assert llm_calls == [] and stored == []
        assert "would process KO_10-Q_2026-07-29.txt" in (
            capsys.readouterr().out)

        counts = await backfill.run(["KO"], apply=True)
        assert counts == {"pending": 1, "stored": 1, "failed": 0}
        assert stored == ["KO_10-Q_2026-07-29.txt"]

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_filings_that_have_records_are_skipped(
            self, monkeypatch, db_session):
        from scripts import backfill_filing_records as backfill

        async def _filenames(ticker, category):
            return [
                "KO_10-Q_2026-07-29.txt", "KO_10-K_2026-02-20.txt", "note.txt"
            ]

        await FilingRecordRepository(db_session).upsert(
            ticker="KO", filename="KO_10-K_2026-02-20.txt", form="10-K",
            filing_date=date(2026, 2, 20), report_date=None, records={})
        monkeypatch.setattr(
            backfill, "get_existing_document_filenames", _filenames)
        monkeypatch.setattr(backfill, "SessionLocal", _SessionOf(db_session))
        assert await backfill._pending("KO") == [
            ("KO_10-Q_2026-07-29.txt", "10-Q", "2026-07-29")
        ]

    @pytest.mark.usefixtures("fresh_sec_caches")
    async def test_waits_before_a_large_filing(self, monkeypatch):
        from scripts import backfill_filing_records as backfill

        sizes = {
            "JPM_10-Q_2026-08-06.txt": backfill.LARGE_FILING_CHARS + 1,
            "JPM_10-Q_2026-05-01.txt": 1000
        }
        slept: list[float] = []

        async def _pending(ticker, redo=False, latest=None, filenames=()):
            return [(name, "10-Q", name[-14:-4]) for name in sizes]

        async def _raw(ticker, filename, form, filing_date):
            return "x" * sizes[filename], None, None

        async def _extract(raw, **kwargs):
            return filing_summary.ExtractedRecords(dict(_NO_RECORDS), "p/m")

        async def _store(**kwargs):
            return True

        async def _sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(backfill, "_pending", _pending)
        monkeypatch.setattr(backfill, "_raw_text", _raw)
        monkeypatch.setattr(backfill, "extract_filing_records", _extract)
        monkeypatch.setattr(backfill, "store_filing_records", _store)
        monkeypatch.setattr(backfill.asyncio, "sleep", _sleep)
        counts = await backfill.run(["JPM"], apply=True)
        assert counts["stored"] == 2
        assert slept == [backfill.IDLE_WAIT_SECONDS]

    async def test_latest_does_not_slide_back_to_older_filings(
            self, monkeypatch, db_session):
        from scripts import backfill_filing_records as backfill

        names = [
            "KO_10-Q_2025-07-25.txt", "KO_10-Q_2025-10-23.txt",
            "KO_10-K_2026-02-20.txt", "KO_10-Q_2026-04-30.txt"
        ]

        async def _filenames(ticker, category):
            return names

        repo = FilingRecordRepository(db_session)
        for name in names[2:]:
            ticker, form, filed = frec.parse_filing_filename(name) or (
                "", "", "")
            await repo.upsert(
                ticker=ticker, filename=name, form=form,
                filing_date=date.fromisoformat(filed), report_date=None,
                records={})
        monkeypatch.setattr(
            backfill, "get_existing_document_filenames", _filenames)
        monkeypatch.setattr(backfill, "SessionLocal", _SessionOf(db_session))
        assert await backfill._pending("KO", latest=2) == []
        assert [p[0] for p in await backfill._pending("KO", latest=3)
                ] == [names[1]]
        redo = await backfill._pending("KO", redo=True, latest=2)
        assert [p[0] for p in redo] == names[2:]

    async def test_filename_redoes_exact_filings(self, monkeypatch, capsys):
        from scripts import backfill_filing_records as backfill

        documents = [
            "INTC_10-Q_2026-04-24.txt", "INTC_10-Q_2026-07-24.txt",
            "INTC_10-K_2026-01-23.txt"
        ]
        extracted: list[str] = []

        async def _filenames(ticker, category):
            return documents

        async def _raw(ticker, filename, form, filing_date):
            return "raw", None, "2026-03-28"

        async def _extract(raw, **kwargs):
            extracted.append(kwargs["filing_date"])
            return filing_summary.ExtractedRecords(
                {key: []
                 for key in frec.CATEGORIES}, "google/gemini-3.8")

        async def _store(**kwargs):
            return True

        monkeypatch.setattr(
            backfill, "get_existing_document_filenames", _filenames)
        monkeypatch.setattr(backfill, "_raw_text", _raw)
        monkeypatch.setattr(backfill, "extract_filing_records", _extract)
        monkeypatch.setattr(backfill, "store_filing_records", _store)
        assert [
            p[0] for p in await backfill._pending(
                "INTC", redo=True, filenames=["INTC_10-Q_2026-04-24.txt"])
        ] == ["INTC_10-Q_2026-04-24.txt"]
        counts = await backfill.run(
            ["INTC"], apply=True, redo=True,
            filenames=["INTC_10-Q_2026-04-24.txt", "INTC_10-Q_2099-01-01.txt"])
        assert counts == {"pending": 1, "stored": 1, "failed": 0}
        assert extracted == ["2026-04-24"]
        out = capsys.readouterr().out
        assert "served by google/gemini-3.8" in out
        assert "NOT FOUND INTC_10-Q_2099-01-01.txt" in out
        assert backfill._filename_tickers(
            ["AMZN_10-Q_2026-04-30.txt", "AMZN_10-Q_2026-07-31.txt",
             "x"]) == ["AMZN"]

    def test_cli_accepts_repeated_filenames(self, monkeypatch):
        from scripts import backfill_filing_records as backfill

        seen: dict[str, Any] = {}

        async def _run(tickers, **kwargs):
            seen["tickers"] = tickers
            seen.update(kwargs)
            return {"pending": 0, "stored": 0, "failed": 0}

        monkeypatch.setattr(backfill, "run", _run)
        assert (
            backfill.main([
                "--filename", "AMZN_10-Q_2026-04-30.txt", "--filename",
                "AMZN_10-Q_2026-07-31.txt", "--redo", "--tier",
                "highest_accuracy"
            ]) == 0)
        assert seen["tickers"] == ["AMZN"]
        assert seen["filenames"] == [
            "AMZN_10-Q_2026-04-30.txt", "AMZN_10-Q_2026-07-31.txt"
        ]
        assert seen["tier"] == LLMTier.HIGHEST_ACCURACY
        assert seen["redo"] is True


class TestRegenerateSummariesScript:

    async def test_saves_first_and_replaces_only_a_deleted_document(
            self, monkeypatch, tmp_path, capsys):
        from scripts import regenerate_filing_summaries as regen

        created: list[str] = []
        existing = {"KO_10-K_2026-02-20.txt"}

        async def _raw(ticker, filename, form, filing_date):
            return _CHARGES_RAW, None, "2025-12-31"

        async def _summarize(raw, **kwargs):
            assert "prior" not in kwargs
            return filing_summary.FilingSummary("New summary.")

        async def _doc_id(ticker, filename):
            return "11111111-1111-1111-1111-111111111111"

        async def _filenames(ticker, category):
            return sorted(existing)

        async def _create(**kwargs):
            created.append(kwargs["filename"])
            return "id"

        monkeypatch.setattr(regen, "_raw_text", _raw)
        monkeypatch.setattr(regen, "summarize_filing", _summarize)
        monkeypatch.setattr(regen, "_document_id", _doc_id)
        monkeypatch.setattr(
            regen, "get_existing_document_filenames", _filenames)
        monkeypatch.setattr(regen, "create_system_document", _create)

        name = "KO_10-K_2026-02-20.txt"
        assert await regen.generate(name, tmp_path) is True
        assert (tmp_path /
                f"{name}.summary.txt").read_text() == ("New summary.")
        assert "current document id 11111111" in capsys.readouterr().out
        assert await regen.apply(name, tmp_path) is False
        assert created == []
        existing.clear()
        assert await regen.apply(name, tmp_path) is True
        assert created == [name]
        assert not (tmp_path / f"{name}.records.json").exists()
        assert await regen.apply("KO_10-Q_2026-04-30.txt", tmp_path) is False

    async def test_continues_after_a_failed_document(
            self, monkeypatch, tmp_path, capsys):
        from scripts import regenerate_filing_summaries as regen

        name = "KO_10-K_2026-02-20.txt"
        (tmp_path / f"{name}.summary.txt").write_text("New summary.")

        async def _filenames(ticker, category):
            return []

        async def _create(**kwargs):
            if kwargs["filename"].startswith("KO_10-Q"):
                raise RuntimeError("core down")
            return "id"

        monkeypatch.setattr(
            regen, "get_existing_document_filenames", _filenames)
        monkeypatch.setattr(regen, "create_system_document", _create)

        other = "KO_10-Q_2026-04-30.txt"
        (tmp_path / f"{other}.summary.txt").write_text("s")

        original = regen.apply

        async def _apply(filename):
            return await original(filename, tmp_path)

        monkeypatch.setattr(regen, "apply", _apply)
        failed = await regen.run([other, name], do_apply=True)
        out = capsys.readouterr().out
        assert failed == 1
        assert f"FAILED {other}: RuntimeError: core down" in out
        assert f"stored {name}" in out
        assert not hasattr(regen, "_prior")


def _golden_chunk(chunk_id: str, category: str,
                  text: str = "Passage text.") -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "text": f"{text} {chunk_id}",
        "document_id": "d",
        "ticker": "AMD",
        "category": category,
        "vector": [0.0, 1.0]
    }


class _QuestionRouter:

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, tier, messages, **kwargs):
        text = messages[-1].content
        self.calls.append(text)
        return LLMResponse(
            content=f"What does {text} say?", provider="fake", model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1))


def _load_golden_set_script():
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("scripts.generate_rag_golden_set")
    finally:
        sys.path.remove(str(_SCRIPTS))


class TestRagGoldenSet:

    async def test_generate_requested_uncovered_categories_or_raise(
            self, monkeypatch):
        import ai.evaluation.rag_recall as gs

        chunks = [
            _golden_chunk("n1", "news"),
            _golden_chunk("n2", "news"),
            _golden_chunk("r1", "risk_analysis"),
            _golden_chunk("e1", "earnings_call")
        ]

        async def _sample(size):
            return [dict(c) for c in chunks]

        async def _keep_all(items):
            return items

        monkeypatch.setattr(gs, "_sample_indexed_chunks", _sample)
        monkeypatch.setattr(gs, "_select_chunks_for_golden_set", _keep_all)
        monkeypatch.setattr(gs, "_inter_chunk_delay_seconds", lambda: 0.0)
        router = _QuestionRouter()
        result = await gs.generate_golden_set(
            sample_size=10, llm_router=router,
            categories=frozenset({"news", "risk_analysis"}),
            skip_chunk_ids=frozenset({"n2"}))
        assert sorted(p.chunk_id for p in result.pairs) == ["n1", "r1"]
        assert len(router.calls) == 2

        async def _sample(size):
            return [_golden_chunk("e1", "earnings_call")]

        monkeypatch.setattr(gs, "_sample_indexed_chunks", _sample)
        with pytest.raises(ValueError, match="filters"):
            await gs.generate_golden_set(
                sample_size=1, llm_router=_QuestionRouter(),
                categories=frozenset({"news"}))

    def test_delay_survives_zero_mistral_keys(self, monkeypatch):
        import ai.evaluation.rag_recall as gs

        monkeypatch.setattr(gs, "mistral_key_count", lambda: 0)
        assert gs._inter_chunk_delay_seconds() == (
            gs._INTER_CHUNK_DELAY_SECONDS_PER_KEY)

    async def test_script_merges_new_chunks_and_rejects_unknown_category(self):
        from ai.evaluation.rag_recall import GoldenQAPair

        script = _load_golden_set_script()

        def pair(chunk_id, question, category="news"):
            return GoldenQAPair(
                question=question, chunk_id=chunk_id, category=category)

        existing = [pair("a", "old a", "earnings_call"), pair("b", "old b")]
        new = [pair("b", "new b"), pair("c", "new c"), pair("c", "dup c")]
        merged = script.merge_pairs(existing, new)
        assert [(p.chunk_id, p.question) for p in merged] == [("a", "old a"),
                                                              ("b", "old b"),
                                                              ("c", "new c")]
        assert script.category_counts(merged) == {
            "earnings_call": 1,
            "news": 2
        }

        assert (
            await
            script.main(["--category", "gossip", "--output",
                         "/tmp/x.json"]) == 2)
