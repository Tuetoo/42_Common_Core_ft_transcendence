import { getAccessToken } from "@/app/lib/session";
import { backendFetch, requireOk } from "@/app/lib/backend";
import { removeWatchlistItem } from "@/app/lib/actions/watchlists";
import { CreateWatchlistForm } from "./create-watchlist-form";
import { AddTickerForm } from "./add-ticker-form";

type WatchlistItem = {
  id: string;
  ticker: string;
};

type Watchlist = {
  id: string;
  name: string;
  items: WatchlistItem[];
};

export default async function WatchlistsPage() {
  const accessToken = await getAccessToken();
  const res = await backendFetch("/watchlists", accessToken!, { cache: "no-store" });

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
              <h2 className="section-title">{wl.name}</h2>

              {wl.items.length === 0 ? (
                <p style={{ fontSize: 12.5, color: "var(--text-faint)" }}>No tickers yet.</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {wl.items.map((item) => (
                    <form key={item.id} action={removeWatchlistItem.bind(null, wl.id, item.ticker)}>
                      <button
                        type="submit"
                        title={`Remove ${item.ticker}`}
                        className="pill pill-accent mono"
                        style={{ border: "none", cursor: "pointer" }}
                      >
                        {item.ticker} ×
                      </button>
                    </form>
                  ))}
                </div>
              )}

              <AddTickerForm watchlistId={wl.id} />
            </div>
          ))
        )}
      </div>

      <div className="section">
        <h2 className="section-title">Create a new watchlist</h2>
        <CreateWatchlistForm />
      </div>
    </div>
  );
}
