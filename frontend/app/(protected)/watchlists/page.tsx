import { backendFetch, requireOk } from "@/app/lib/backend";
import { can, getMe } from "@/app/lib/session";
import { CreateWatchlistForm } from "./create-watchlist-form";
import { AddTickerForm } from "./add-ticker-form";
import { RemoveTickerButton } from "./remove-ticker-button";

type WatchlistItem = {
  id: string;
  ticker: string;
};

type Watchlist = {
  id: string;
  name: string;
  items: WatchlistItem[];
};

// GET /watchlists returns all of the user's watchlists in one list (it is
// not paginated), so there is no pager here.
export default async function WatchlistsPage() {
  const { me, accessToken } = await getMe();
  // Every watchlist change needs portfolio:write (domains/portfolio/api.py).
  const canWrite = can(me, "portfolio:write");
  const res = await backendFetch("/watchlists", accessToken, { cache: "no-store" });

  requireOk(res, "Failed to load watchlists");

  const { data: watchlists }: { data: Watchlist[] } = await res.json();

  return (
    <div className="page">
      <div>
        <p className="page-eyebrow">Workspace</p>
        <h1 className="page-title">Watchlists</h1>
      </div>

      <div className="flex flex-col gap-4">
        {watchlists.length === 0 ? (
          <p className="empty-state">No watchlists yet.</p>
        ) : (
          watchlists.map((wl) => (
            <div key={wl.id} className="panel flex flex-col gap-3">
              <h2 className="section-title break-words">{wl.name}</h2>

              {wl.items.length === 0 ? (
                <p style={{ fontSize: 12.5, color: "var(--text-faint)" }}>No tickers yet.</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {wl.items.map((item) =>
                    canWrite ? (
                      <RemoveTickerButton key={item.id} watchlistId={wl.id} ticker={item.ticker} />
                    ) : (
                      <span key={item.id} className="pill pill-accent mono">
                        {item.ticker}
                      </span>
                    )
                  )}
                </div>
              )}

              {canWrite && <AddTickerForm watchlistId={wl.id} />}
            </div>
          ))
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Create a new watchlist</h2>
        {canWrite ? (
          <CreateWatchlistForm />
        ) : (
          <p className="empty-state">Your role ({me.system_role}) is read-only.</p>
        )}
      </div>
    </div>
  );
}
