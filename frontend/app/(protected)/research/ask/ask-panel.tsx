"use client";

import { useEffect, useRef, useState } from "react";
import { TICKER_PATTERN, TICKER_TITLE } from "@/app/lib/format";
import { DOCUMENT_CATEGORIES, QUESTION_MAX, tickerError } from "../limits";

type Citation = {
  index: number;
  title: string;
  category: string;
  ticker: string | null;
  content_date: string | null;
  snippet: string;
};

type Status = "idle" | "streaming" | "done" | "error";

// Server-Sent Events arrive as blocks separated by a blank line:
//   event: token
//   data: {"text": "..."}
// The network can split or merge blocks arbitrarily, so complete blocks are
// taken off the front of a buffer and the unfinished tail waits for more.
function takeEvents(buffer: string): { events: { event: string; data: unknown }[]; rest: string } {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events = blocks.flatMap((block) => {
    let event = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    try {
      return data ? [{ event, data: JSON.parse(data) }] : [];
    } catch {
      return [];
    }
  });
  return { events, rest };
}

export function AskPanel() {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<"ask" | "draft">("ask");
  const [ticker, setTicker] = useState("");
  const [categories, setCategories] = useState<string[]>([]);
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Leaving the page cancels the answer, so the server stops generating it.
  useEffect(() => () => abortRef.current?.abort(), []);

  function toggleCategory(category: string) {
    setCategories((prev) => (prev.includes(category) ? prev.filter((c) => c !== category) : [...prev, category]));
  }

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim() || status === "streaming") return;
    const badTicker = tickerError(ticker);
    if (badTicker) {
      setError(badTicker);
      setStatus("error");
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    setAnswer("");
    setCitations([]);
    setError(null);
    setCopied(false);
    setStatus("streaming");

    let res: Response;
    try {
      res = await fetch("/research/ask/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, mode, ticker, categories: categories.length ? categories : null }),
        signal: controller.signal,
      });
    } catch {
      setStatus(controller.signal.aborted ? "done" : "error");
      if (!controller.signal.aborted) setError("Could not reach the server.");
      return;
    }

    if (!res.ok || !res.body) {
      const body = await res.json().catch(() => ({}));
      setError(
        res.status === 429 && body.retryAfter
          ? `You're asking too quickly. Try again in ${body.retryAfter} seconds.`
          : body.message ?? "Could not get an answer."
      );
      setStatus("error");
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        // stream: true keeps a multi-byte character that was split across
        // two chunks from turning into garbage.
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = takeEvents(buffer);
        buffer = rest;
        for (const { event, data } of events) {
          const payload = data as Record<string, unknown>;
          if (event === "sources") setCitations(payload.citations as Citation[]);
          else if (event === "token") setAnswer((prev) => prev + (payload.text as string));
          else if (event === "done") {
            // The final event carries the whole answer — use it as the
            // source of truth in case any token was missed.
            setAnswer(payload.answer as string);
            setCitations(payload.citations as Citation[]);
            finished = true;
          } else if (event === "error") {
            setError(payload.message as string);
            setStatus("error");
            return;
          }
        }
      }
      if (finished) {
        setStatus("done");
      } else {
        setError("The answer was cut off. Please try again.");
        setStatus("error");
      }
    } catch {
      // Reading throws when the user pressed Stop; keep what arrived.
      setStatus(controller.signal.aborted ? "done" : "error");
      if (!controller.signal.aborted) setError("The connection dropped before the answer finished.");
    }
  }

  async function copyAnswer() {
    await navigator.clipboard.writeText(answer);
    setCopied(true);
  }

  const streaming = status === "streaming";

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={ask} className="form">
        <div className="field" style={{ maxWidth: 640 }}>
          <label htmlFor="question">{mode === "ask" ? "Question" : "What should the paragraph cover?"}</label>
          <textarea
            id="question"
            autoComplete="off"
            rows={3}
            maxLength={QUESTION_MAX}
            required
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={
              mode === "ask"
                ? "e.g. What did NVIDIA say about data center demand?"
                : "e.g. NVIDIA's data center growth and its risks"
            }
          />
        </div>
        <div className="filter-row">
          <div className="field">
            <label htmlFor="mode">Mode</label>
            <select id="mode" autoComplete="off" value={mode} onChange={(e) => setMode(e.target.value as "ask" | "draft")}>
              <option value="ask">Ask — answer a question</option>
              <option value="draft">Draft — write a note paragraph</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="ask-ticker">Ticker (optional)</label>
            <input
              id="ask-ticker"
              autoComplete="off"
              type="text"
              maxLength={10}
              pattern={TICKER_PATTERN}
              title={TICKER_TITLE}
              placeholder="All"
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
            />
          </div>
        </div>
        <fieldset className="flex flex-wrap gap-x-4 gap-y-2" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
            Only search these document types (optional)
          </legend>
          {DOCUMENT_CATEGORIES.map((c) => (
            <label key={c} className="flex items-center gap-2" style={{ fontSize: 13 }}>
              <input autoComplete="off" type="checkbox" checked={categories.includes(c)} onChange={() => toggleCategory(c)} />
              {c.replaceAll("_", " ")}
            </label>
          ))}
        </fieldset>
        <div className="flex gap-2">
          <button type="submit" disabled={streaming} className="btn btn-primary">
            {streaming ? "Answering…" : mode === "ask" ? "Ask" : "Write draft"}
          </button>
          {streaming && (
            <button type="button" className="btn btn-ghost" onClick={() => abortRef.current?.abort()}>
              Stop
            </button>
          )}
        </div>
      </form>

      {error && (
        <p aria-live="polite" className="form-message error">
          {error}
        </p>
      )}

      {(answer || streaming) && (
        <div className="section">
          <div className="flex items-center justify-between">
            <h2 className="section-title">{mode === "ask" ? "Answer" : "Draft"}</h2>
            {status === "done" && answer && (
              <button type="button" className="link-btn" onClick={copyAnswer}>
                {copied ? "Copied" : "Copy"}
              </button>
            )}
          </div>
          <div className="panel" aria-live="polite" aria-busy={streaming}>
            <p style={{ margin: 0, fontSize: 14, lineHeight: 1.65, whiteSpace: "pre-wrap" }}>
              {answer}
              {streaming && <span className="stream-caret" aria-hidden="true" />}
            </p>
          </div>
        </div>
      )}

      {citations.length > 0 && (
        <div className="section">
          <h2 className="section-title">Sources</h2>
          <div className="row-list">
            {citations.map((c) => (
              <div key={c.index} className="row-item">
                <span className="row-title">
                  <span className="mono" style={{ color: "var(--accent-strong)" }}>
                    [{c.index}]
                  </span>{" "}
                  {c.title}
                </span>
                <span className="row-meta">
                  {c.category.replaceAll("_", " ")}
                  {c.ticker && ` · ${c.ticker}`}
                  {c.content_date && ` · ${c.content_date}`}
                </span>
                <span className="row-meta" style={{ fontStyle: "italic" }}>
                  {c.snippet}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
