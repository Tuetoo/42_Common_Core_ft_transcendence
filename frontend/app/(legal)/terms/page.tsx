import type { Metadata } from "next";
import Link from "next/link";
import { CONTACT_URL, LAST_UPDATED } from "../legal";

export const metadata: Metadata = {
  title: "Terms of Service · AlphaMind",
};

export default function TermsOfServicePage() {
  return (
    <article className="legal">
      <div>
        <p className="page-eyebrow">Legal</p>
        <h1 className="page-title">Terms of Service</h1>
        <p className="page-subtitle">Last updated: {LAST_UPDATED}</p>
      </div>

      <h2>1. Accepting these terms</h2>
      <p>
        By creating an account or using AlphaMind you agree to these terms and to the{" "}
        <Link href="/privacy">Privacy Policy</Link>. AlphaMind is a student project built by the AlphaMind team for
        the ft_transcendence course at 42, run for development, testing and evaluation. It is not a commercial
        service.
      </p>

      <h2>2. What AlphaMind offers</h2>
      <ul>
        <li>AI-assisted research on US stocks: specialist AI agents, a Bull and Bear debate and a final verdict.</li>
        <li>Records of your own portfolios, transactions and watchlists.</li>
        <li>Research notes, comments, document uploads and questions answered from those documents.</li>
        <li>Collaboration: organizations, shared notes with live editing, friends and notifications.</li>
      </ul>

      <h2>3. Not investment advice</h2>
      <ul>
        <li>
          Analyses and answers produced by AI can be wrong or out of date. Their accuracy is not guaranteed.
        </li>
        <li>Nothing on AlphaMind is financial, investment, legal or tax advice.</li>
        <li>
          AlphaMind does not execute real trades and is not connected to any broker. Portfolios and transactions are
          records you enter yourself.
        </li>
        <li>You are solely responsible for your own decisions.</li>
      </ul>

      <h2>4. Your account</h2>
      <ul>
        <li>Give accurate information when you register.</li>
        <li>Keep your password safe; you are responsible for what happens under your account.</li>
        <li>Use one account per person, for ordinary personal use.</li>
        <li>Accounts whose email address is not verified within 7 days of registering are deleted.</li>
      </ul>

      <h2>5. Acceptable use</h2>
      <p>You agree not to:</p>
      <ul>
        <li>
          upload content that is illegal, infringes someone else&apos;s rights, is malicious, or contains other
          people&apos;s personal information;
        </li>
        <li>try to break into or disrupt the system, or get around its rate limits or permissions;</li>
        <li>
          send large numbers of automated requests. Programmatic access goes through the public API, which needs an
          API key and is rate limited.
        </li>
      </ul>

      <h2>6. Your content</h2>
      <ul>
        <li>
          You keep the rights to the content you create. You allow AlphaMind to process it as needed to provide the
          service, including sending it to the AI services listed in the{" "}
          <Link href="/privacy">Privacy Policy</Link>.
        </li>
        <li>
          Content you share with an organization can be viewed or edited by its members, depending on their role.
        </li>
      </ul>

      <h2>7. Moderation</h2>
      <p>
        Content that breaks these terms may be hidden or deleted, and the account that posted it may be deactivated.
      </p>

      <h2>8. Availability</h2>
      <p>
        The service may not always be available. It may change or stop at any time, and data may be reset during
        maintenance.
      </p>

      <h2>9. Limitation of liability</h2>
      <p>
        AlphaMind is provided &quot;as is&quot;, without any warranty. The team is not liable for any loss that
        results from using AlphaMind or from not being able to use it.
      </p>

      <h2>10. Changes to these terms</h2>
      <p>These terms may be updated. The date at the top of the page shows when they last changed.</p>

      <h2>11. Contact</h2>
      <p>
        Questions about these terms go to the team through the{" "}
        <a href={CONTACT_URL} target="_blank" rel="noopener noreferrer">
          project&apos;s GitHub Issues page
        </a>
        .
      </p>
    </article>
  );
}
