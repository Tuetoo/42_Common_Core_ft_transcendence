import type { Metadata } from "next";
import Link from "next/link";
import { LAST_UPDATED } from "../legal";

export const metadata: Metadata = {
  title: "Privacy Policy · AlphaMind",
};

export default function PrivacyPolicyPage() {
  return (
    <article className="legal">
      <div>
        <p className="page-eyebrow">Legal</p>
        <h1 className="page-title">Privacy Policy</h1>
        <p className="page-subtitle">Last updated: {LAST_UPDATED}</p>
      </div>

      <p>
        This policy explains what information AlphaMind collects, how it is used, who it is shared with and how long
        it is kept. It describes what the system actually does today.
      </p>

      <h2>1. Who we are</h2>
      <p>
        AlphaMind is a student project built by the AlphaMind team for the ft_transcendence course at 42. It is an
        AI-assisted investment research platform covering US stocks. It is run for development, testing and
        evaluation, not as a commercial service. AlphaMind does not give investment advice, does not execute real
        trades and is not connected to any broker: portfolios and transactions are records you enter yourself.
      </p>

      <h2>2. Information we collect</h2>
      <h3>Account</h3>
      <ul>
        <li>Your email address, username and password.</li>
        <li>
          Passwords are never stored in readable form: they are hashed with bcrypt, using a unique salt for each
          password.
        </li>
      </ul>
      <h3>Profile</h3>
      <ul>
        <li>Optional: first name, last name, bio, time zone and country.</li>
        <li>An optional profile picture (PNG, JPEG, WebP or GIF, up to 5 MB).</li>
      </ul>
      <h3>Signing in with Google or GitHub</h3>
      <p>
        If you sign in with Google or GitHub, that service sends us your basic account details. We only use and keep
        your email address and your account ID on that service, to create your AlphaMind account or link it to an
        existing one. Your name on that service is not stored.
      </p>
      <h3>Two-factor authentication</h3>
      <p>If you turn on two-factor authentication, its secret key (TOTP) is stored encrypted.</p>
      <h3>Content you create</h3>
      <ul>
        <li>Portfolios, holdings, transactions and watchlists.</li>
        <li>Research notes, comments and uploaded documents (PDF, TXT or MD, up to 20 MB).</li>
        <li>AI analyses you run, and whether you followed or declined their recommendations.</li>
      </ul>
      <h3>Organizations and friends</h3>
      <p>
        Your organization memberships and invitations, your friend connections, and whether you are currently online
        (shown to your friends and to administrators).
      </p>
      <h3>Technical data</h3>
      <ul>
        <li>
          Each sign-in session records your IP address and browser information (user agent), used for security and
          to limit repeated sign-in attempts.
        </li>
        <li>
          The gateway&apos;s access logs record your IP address, browser information, the page or API path and the
          response status. They do not record the query string of a URL, so sign-in tokens are not written to the
          access logs.
        </li>
      </ul>

      <h2>3. How we use it</h2>
      <ul>
        <li>To run the service: signing you in, storing and showing your content, and sending you notifications.</li>
        <li>
          For the AI features:
          <ul>
            <li>
              When you analyze a stock, market data, public filings and news, and the holdings of the portfolio you
              chose, are sent to an AI model to produce the analysis.
            </li>
            <li>Research notes and comments are sent to an AI model to score their sentiment.</li>
            <li>
              When you ask a question on the Ask page, your question and the relevant parts of documents are sent to
              an AI model to write the answer.
            </li>
            <li>Uploaded documents are split into passages and turned into embeddings so they can be searched.</li>
          </ul>
        </li>
        <li>For security: rate limiting, detecting abuse and recording errors.</li>
        <li>
          To send email: verifying your email address, resetting your password and organization invitations. If you
          turn on email notifications in your account settings (they are off by default), your notifications are
          also sent to your email address.
        </li>
      </ul>
      <p>There is no advertising, and your information is not sold.</p>

      <h2>4. Third-party services</h2>
      <ul>
        <li>
          <strong>Google</strong>: the Gemini API (the main AI model and embeddings) and Google sign-in.
        </li>
        <li>
          <strong>GitHub</strong>: GitHub sign-in.
        </li>
        <li>
          <strong>Mistral AI</strong>: a backup AI model, when it is configured. The system can also be configured to
          use other AI providers, such as OpenAI or Anthropic; they are off unless an operator adds their keys.
        </li>
        <li>
          <strong>Market data and news</strong>: Yahoo Finance, SEC EDGAR, FRED (Federal Reserve economic data),
          NewsAPI and Tavily (web search). Only ticker symbols and search terms are sent to them, never your personal
          information.
        </li>
        <li>
          <strong>Email</strong>: messages are sent through an SMTP server. In a local copy, email is caught by a
          local test mail server (MailHog) and is never delivered.
        </li>
        <li>
          <strong>Hosting</strong>: the online version runs on a virtual machine at Amazon Web Services (AWS), at
          alphamind-research.duckdns.org; the domain name is provided by DuckDNS. A local copy started with Docker
          (https://localhost:8443) keeps all of its data on the computer that runs it; the AI, market-data and
          sign-in services above are still used.
        </li>
      </ul>

      <h2>5. Cookies and browser storage</h2>
      <ul>
        <li>
          <code>access_token</code> and <code>refresh_token</code> keep you signed in. These cookies cannot be read
          by JavaScript on the page (httpOnly), are only sent over HTTPS in production, use SameSite=Lax, and are
          session cookies that your browser clears when it closes. The refresh token is never readable by page
          scripts. A short-lived access token (valid for 15 minutes) is given to the page so it can open live
          connections, such as notifications and the shared note editor.
        </li>
        <li>
          <code>alphamind_oauth_nonce</code> exists only for the few minutes of a Google or GitHub sign-in, to check
          that the sign-in finishes in the same browser that started it. It is deleted whether the sign-in succeeds
          or fails.
        </li>
      </ul>
      <p>
        There are no advertising or tracking cookies and no third-party analytics, so there is no cookie banner.
      </p>

      <h2>6. How long we keep it</h2>
      <ul>
        <li>
          Accounts that, within 7 days of registering, never verified their email, never signed in and have no
          linked Google or GitHub account are deleted automatically.
        </li>
        <li>Read notifications are deleted after 90 days. Unread ones are kept.</li>
        <li>
          Sign-in session records (IP address and browser information) are removed when you next sign in, once the
          session has been expired for more than 7 days. Records of an account that does not sign in again are kept.
        </li>
        <li>
          Server log files are kept for about 14 days. Each container&apos;s own Docker log is limited by size (up to
          100 MB), not by age.
        </li>
        <li>Backups are kept for about 7 days. The newest 3 backups are always kept, even if they are older.</li>
      </ul>
      <h3>Deleting your account</h3>
      <p>
        You cannot delete your account yourself at the moment; an administrator does it. To ask for it, contact the
        AlphaMind team. When an account is deleted:
      </p>
      <ul>
        <li>its email address and username are replaced with anonymous values;</li>
        <li>its profile fields and profile picture are cleared;</li>
        <li>its Google or GitHub links are removed;</li>
        <li>the account is deactivated and can no longer sign in.</li>
      </ul>
      <p>
        Content created earlier, such as portfolio records, stays in the system but is no longer linked to anyone
        who can be identified.
      </p>

      <h2>7. Security</h2>
      <ul>
        <li>Every connection from a browser or from outside to the system uses HTTPS.</li>
        <li>
          Passwords are hashed with bcrypt. Two-factor authentication (TOTP) is available, and sign-in, registration
          and password reset are rate limited.
        </li>
        <li>
          Accounts have one of four roles: guest, user, moderator or admin. Moderators and admins can deactivate
          accounts and hide or delete inappropriate content.
        </li>
      </ul>

      <h2>8. Your choices</h2>
      <h3>In the app, on your own</h3>
      <ul>
        <li>View and change your profile and profile picture.</li>
        <li>Delete your own notes, documents and watchlist entries.</li>
        <li>Leave an organization.</li>
        <li>Turn two-factor authentication on or off.</li>
      </ul>
      <h3>Anything else</h3>
      <p>
        For other requests, such as deleting your account or asking what information is stored about you, contact
        the AlphaMind team.
      </p>

      <h2>9. Changes to this policy</h2>
      <p>
        This policy may be updated. The date at the top of the page shows when it last changed. See also the{" "}
        <Link href="/terms">Terms of Service</Link>.
      </p>

      <h2>10. Contact</h2>
      <p>
        Questions about this policy go to the AlphaMind team, the students who build and run the project.
      </p>
    </article>
  );
}
