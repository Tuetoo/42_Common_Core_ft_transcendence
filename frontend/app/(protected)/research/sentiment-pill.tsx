import type { SentimentFields } from "./types";

const PILL_CLASS = {
  positive: "pill pill-up",
  negative: "pill pill-down",
  neutral: "pill",
} as const;

// The backend scores notes and comments in the background after each
// create/edit (domains/research/sentiment.py): label positive / neutral /
// negative and a score from -1 to 1. Until that runs, both are null.
export function SentimentPill({ item }: { item: SentimentFields }) {
  const label = item.sentiment_label;
  if (!label) {
    return (
      <span className="pill opacity-70" title="Sentiment is computed in the background">
        sentiment pending
      </span>
    );
  }
  const score = item.sentiment_score;
  const scoreText = score === null ? "" : `${score > 0 ? "+" : ""}${score.toFixed(2)}`;
  return (
    <span
      className={PILL_CLASS[label]}
      title={`Sentiment: ${label}${scoreText ? `, score ${scoreText} on a scale from -1 to +1` : ""}`}
    >
      {label}
      {scoreText && <span className="ml-1 opacity-80">{scoreText}</span>}
    </span>
  );
}
