import argparse
import asyncio
import os
import sys
import time
from typing import Any


THRESHOLDS_SECONDS = (30.0, 60.0, 90.0)
_SLOTS = ["GOOGLE_API_KEY"] + [f"GOOGLE_API_KEY_{i}" for i in range(2, 11)]

_EVIDENCE = (
    "[market] Over the past 30 days, AAPL exhibits an upward price trend, "
    "moving from an early September close near 324.96 to 333.02 by "
    "September 30, 2026. The stock reached a period high of 345.34 and a "
    "period low of 309.90. Volume patterns show higher activity during "
    "mid-September sell-offs and recovery days, such as spikes on "
    "September 9 and September 18, but generally settled into moderate "
    "levels toward the end of the month.\n\n[news] Recent sentiment and "
    "developments for Apple (AAPL) in late September and early October "
    "2026 are centered around the opening of the John Ternus leadership "
    "era, highlighted by strong consumer interest in premium devices. Key "
    "hardware milestones include the upcoming iPhone Duo foldable—with "
    "Counterpoint projecting around 6 million units sold this year and "
    "pre-orders slated for October 12—alongside the September rollouts of "
    "the Apple Watch Series 12, Ultra 4, and AirPods 5. On the operational "
    "and strategic fronts, Apple launched a payment service in India, "
    "while supply chain reports indicate the A20 Pro chip will adopt WMCM "
    "packaging. Additionally, security and software updates include "
    "patches for a CoreGraphics zero-day vulnerability, complementing "
    "ongoing legal proceedings and hardware developments.\n\n[filing] In its "
    "Q3 2026 Form 10-Q, Apple reported quarterly: revenue of $109.4B (+16% "
    "YoY) and quarterly: operating income of $35.7B (+27% YoY). Key risk "
    "factors highlighted by management include intensifying supply "
    "constraints and rising component costs (advanced semiconductors, "
    "NAND, and DRAM), expanding global regulatory scrutiny and compliance "
    "costs (such as EU Digital Markets Act rules and data "
    "privacy/localization laws), ongoing antitrust investigations and "
    "litigation (including the U.S. DOJ lawsuit), and compute capacity "
    "constraints for AI and cloud infrastructure. Forward outlook cautions "
    "that component supply constraints and cost pressures may intensify, "
    "potentially impacting margins and revenue if price increases do not "
    "fully offset them.\n\n[technical] AAPL exhibits a mixed technical "
    "outlook with a generally resilient trend. The stock is currently "
    "trading above both its 20-day SMA of 331.35 and 50-day SMA of 322.01, "
    "indicating an underlying upward momentum. However, the 14-period RSI "
    "sits in a neutral zone at 54.4, while the MACD displays a slight "
    "bearish posture with the MACD line at 4.428 below the signal line at "
    "4.890 and a negative histogram of -0.462. Bollinger Bands place the "
    "middle band at 331.35 with an upper band of 346.05 and lower band of "
    "316.65, alongside an ATR of 7.24.\n\n[fundamental] Apple (AAPL) trades "
    "at an elevated valuation with a trailing P/E of 37.71 and a forward "
    "P/E of 34.75, alongside a high price-to-book ratio of 45.25. The "
    "company demonstrates steady top-line expansion with a YoY annual "
    "revenue growth rate of 6.4%. Profitability metrics remain robust, "
    "anchored by a gross margin of 46.9%, an operating margin of 32.0%, "
    "and a net profit margin of 26.9%.\n\n[risk] Apple (AAPL) exhibits "
    "moderate risk characteristics with an annualized volatility of 27.0% "
    "and a maximum drawdown of 11.0% over its recent 62-day trading "
    "window. Sector-level and idiosyncratic risks include ongoing "
    "regulatory scrutiny under the EU Digital Markets Act (including a "
    "€500M fine under appeal and pending Article 6 findings), the U.S. DOJ "
    "antitrust lawsuit regarding smartphone markets, and Epic Games "
    "injunction compliance disputes. Additional exposures involve "
    "component cost pressures, memory chip pricing, and dependence on "
    "global supply chains.\n\nNamed risk events (sourced): EU Digital "
    "Markets Act (via search_filings); U.S. DOJ (via search_filings); Epic "
    "Games (via search_filings)\n\n[macro] The U.S. macroeconomic backdrop "
    "is currently characterized by a restrictive monetary policy stance, "
    "with the effective federal funds rate at 3.88% following a 25 basis "
    "point rate hike on September 17, 2026, and the 10-year Treasury yield "
    "elevated at 5.26%, reflecting ongoing inflationary pressures (CPI up "
    "3.35% year-over-year as of August 2026). For Apple Inc. (AAPL), this "
    "high interest rate and elevated yield environment presents a nuanced "
    "operating context. Higher discount rates increase the cost of capital "
    "and can compress valuation multiples for mega-cap technology growth "
    "companies. Furthermore, elevated borrowing costs and restrictive "
    "financial conditions may weigh on discretionary consumer spending, "
    "potentially tempering upgrade cycles for premium hardware products "
    "like iPhones and Macs. However, Apple's robust balance sheet, massive "
    "cash reserves, and strong pricing power provide a significant buffer "
    "against macroeconomic tightening.\n\nLive FRED this run: DFF 3.88% "
    "(2026-09-29); DGS10 5.26% (2026-09-29)\n\n[portfolio] Adding 5 shares "
    "of AAPL would establish a position concentration of 7.9% of the "
    "portfolio (costing 1665.10, fully covered by the available cash of "
    "7698.95 with zero shortfall). Alternatively, adding 10 shares of AAPL "
    "would result in a position concentration of 14.6% of the portfolio "
    "(costing 3330.20, also fully covered by available cash with zero "
    "shortfall). Both scenarios are well within current cash constraints, "
    "but increase portfolio exposure.\n\n[sentiment] Market sentiment for "
    "Apple (AAPL) is positive, driven by strong consumer interest in "
    "upcoming premium hardware like the foldable iPhone Duo and recent "
    "device rollouts including the Apple Watch Series 12 and AirPods 5. "
    "This optimism is further supported by strategic expansions such as "
    "the payment service launch in India and the smooth transition into "
    "the John Ternus leadership era.")
_BULL = (
    "Apple demonstrates exceptional operational execution, posting "
    "quarterly revenue of $109.4B (+16% YoY) and quarterly operating "
    "income of $35.7B (+27% YoY) in its Q3 2026 Form 10-Q [filing], backed "
    "by strong overall profitability including a 46.9% gross margin and a "
    "32.0% operating margin [fundamental]. Growth momentum is poised to "
    "continue under the John Ternus leadership era with strong consumer "
    "interest in premium devices, including the upcoming iPhone Duo "
    "foldable—which Counterpoint projects will sell around 6 million units "
    "this year with pre-orders opening October 12—alongside the recent "
    "rollouts of the Apple Watch Series 12, Ultra 4, and AirPods 5, and a "
    "new payment service launch in India [news]. Technically, the stock "
    "displays resilient upward momentum, closing at 333.02 by September "
    "30, 2026 [market] and holding above both its 20-day SMA of 331.35 and "
    "50-day SMA of 322.01 [technical].")
_BEAR = (
    "Apple trades at an elevated valuation, carrying a trailing P/E of "
    "37.71, a forward P/E of 34.75, and a price-to-book ratio of 45.25 "
    "[fundamental]. These valuation multiples are vulnerable to "
    "compression in the current macroeconomic climate, where restrictive "
    "monetary policy, a 3.88% federal funds rate, and an elevated 10-year "
    "Treasury yield of 5.26% increase the cost of capital and could weigh "
    "on consumer discretionary spending for premium hardware upgrades "
    "[macro]. Furthermore, management explicitly warns of intensifying "
    "supply constraints and rising costs for advanced semiconductors, "
    "NAND, and DRAM, which threaten to squeeze margins alongside "
    "significant legal and regulatory overhead—including the U.S. DOJ "
    "antitrust lawsuit, EU Digital Markets Act compliance, and compute "
    "capacity constraints for AI infrastructure [filing, risk]. "
    "Technically, momentum shows underlying weakness, as the MACD line "
    "sits at 4.428 below its signal line of 4.890 with a negative "
    "histogram of -0.462 [technical].")


def _keys() -> list[tuple[str, str]]:
    keys = [(name, os.environ[name]) for name in _SLOTS
            if os.environ.get(name)]
    if not keys:
        print("No Google API key found in the environment.", file=sys.stderr)
        sys.exit(1)
    return keys


_HEALTH_MODEL_SETTINGS = (
    "LLM_GOOGLE_MODEL_CHEAP", "LLM_GOOGLE_MODEL_CHEAP_FALLBACK",
    "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY",
    "LLM_GOOGLE_MODEL_HIGHEST_ACCURACY_FALLBACK")


async def _ping(client: Any, model: str) -> str | None:
    try:
        resp = await client.aio.models.generate_content(
            model=model, contents=[{
                "role": "user",
                "parts": [{
                    "text": "Reply with: pong"
                }]
            }])
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:200]}"
    return None if (resp.text or "").strip() else "empty answer"


async def health_check() -> int:
    from config import settings
    from google import genai

    keys = _keys()
    cheap = settings.LLM_GOOGLE_MODEL_CHEAP
    print(f"--- per-key check ({len(keys)} key(s), model {cheap}) ---")
    failures = 0
    healthy: list[Any] = []
    for slot, key in keys:
        client = genai.Client(api_key=key)
        error = await _ping(client, cheap)
        print(
            f"  {slot:<16} {'OK' if error is None else 'FAILED -- ' + error}")
        if error is None:
            healthy.append(client)
        else:
            failures += 1
    models = list(
        dict.fromkeys(
            str(getattr(settings, name)) for name in _HEALTH_MODEL_SETTINGS))
    print(f"\n--- per-model check ({len(models)} model ID(s)) ---")
    if not healthy:
        print("  skipped: no key answered")
        return failures + len(models)
    for model in models:
        error = await _ping(healthy[0], model)
        print(
            f"  {model:<28} {'OK' if error is None else 'FAILED -- ' + error}")
        failures += error is not None
    return failures


def _user_content() -> str:
    return (
        "Ticker: AAPL\nRound: 1\n\n"
        f"Research agent evidence:\n{_EVIDENCE}\n\n"
        f"Bull argument: {_BULL}\n\nBear argument: {_BEAR}")


async def _one(client: Any, model: str, config: Any,
               cap: float) -> dict[str, Any]:
    from google.genai import errors as genai_errors

    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model, contents=_user_content(), config=config),
            timeout=cap)
    except TimeoutError:
        return {
            "status": f"no answer within {cap:.0f}s",
            "seconds": time.monotonic() - started
        }
    except genai_errors.APIError as exc:
        return {
            "status": f"HTTP {exc.code}",
            "seconds": time.monotonic() - started
        }
    except Exception as exc:
        return {
            "status": type(exc).__name__,
            "seconds": time.monotonic() - started
        }
    usage = resp.usage_metadata
    return {
        "status": "ok",
        "seconds": time.monotonic() - started,
        "thinking": (
            getattr(usage, "thoughts_token_count", None) if usage else None),
        "output": (
            getattr(usage, "candidates_token_count", None) if usage else None),
        "finish": (
            resp.candidates[0].finish_reason if resp.candidates else None)
    }


DESCRIPTION = "Owner-run probe: Gemini latency on Judge-sized requests"


async def main() -> None:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--model", default=None)
    parser.add_argument("--per-key", type=int, default=2)
    parser.add_argument("--cap", type=float, default=120.0)
    parser.add_argument(
        "--health", action="store_true",
        help="check every key and configured model ID with one tiny request")
    args = parser.parse_args()
    if args.health:
        failures = await health_check()
        print(f"\n{failures} failure(s).")
        sys.exit(1 if failures else 0)

    from ai.agents.judge import _build_system_prompt
    from ai.llm.google_provider import _thinking_config_for
    from config import settings
    from google import genai
    from google.genai import types

    model = args.model or settings.LLM_GOOGLE_MODEL_HIGHEST_ACCURACY

    config = types.GenerateContentConfig(
        system_instruction=_build_system_prompt(False, False,
                                                False), temperature=0.2,
        max_output_tokens=1024, response_mime_type="application/json",
        thinking_config=_thinking_config_for(model))
    keys = _keys()
    total = len(keys) * max(1, args.per_key)
    print(
        f"{model}: {total} request(s) over {len(keys)} key(s), "
        f"cap {args.cap:.0f}s\n")
    results: list[dict[str, Any]] = []
    for _round in range(max(1, args.per_key)):
        for slot, key in keys:
            result = await _one(
                genai.Client(api_key=key), model, config, args.cap)
            result["key"] = slot
            results.append(result)
            extra = ""
            if result["status"] == "ok":
                extra = (
                    f"  thinking={result['thinking']}  "
                    f"output={result['output']}"
                    f"  finish={result['finish']}")
            print(
                f"  #{len(results):>2} {slot:<16} {result['seconds']:6.1f}s  "
                f"{result['status']}{extra}", flush=True)
            await asyncio.sleep(5.0)

    answered = [r["seconds"] for r in results if r["status"] == "ok"]
    errors = [r for r in results if r["status"].startswith("HTTP")]
    print(
        f"\nAnswered {len(answered)}/{len(results)}; HTTP errors "
        f"{len(errors)} "
        "(503/429 are not latency and are left out below).")
    if answered:
        ordered = sorted(answered)
        print(
            f"Answer time: median {ordered[len(ordered) // 2]:.1f}s, "
            f"max {ordered[-1]:.1f}s")
    timed = [r for r in results if not r["status"].startswith("HTTP")]
    for limit in THRESHOLDS_SECONDS:
        kept = sum(
            1 for r in timed if r["status"] == "ok" and r["seconds"] <= limit)
        lost = len(timed) - kept
        waited = sum(
            min(r["seconds"], limit) for r in timed
            if not (r["status"] == "ok" and r["seconds"] <= limit))
        print(
            f"  timeout {limit:>4.0f}s: answered {kept}/{len(timed)}, "
            f"timed out {lost} (time spent on them: {waited:.0f}s)")
    print("\nSend this output to Claude.")


if __name__ == "__main__":
    asyncio.run(main())
