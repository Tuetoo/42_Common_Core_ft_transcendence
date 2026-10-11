import Link from "next/link";
import { AskPanel } from "./ask-panel";

export default function AskPage() {
  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">
          <Link href="/research" className="link-btn" style={{ padding: 0 }}>
            Research
          </Link>{" "}
          / Ask
        </p>
        <h1 className="page-title">Ask your documents</h1>
        <p className="page-subtitle">
          Answers come only from indexed filings, news, research and your own uploads, with numbered sources.
        </p>
      </div>
      <AskPanel />
    </div>
  );
}
