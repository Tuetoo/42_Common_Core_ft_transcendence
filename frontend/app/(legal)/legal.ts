// Shared by the Privacy Policy and Terms of Service pages.

// A fixed string on purpose: a date computed with new Date() could differ
// between the server render and the browser and cause a hydration error.
// Change it whenever either page's content changes.
export const LAST_UPDATED = "2026-10-12";
