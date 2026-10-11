// Shared by the list and detail pages so an analysis status (pending,
// running, completed, failed) is colored the same everywhere.
export function statusPillClass(status: string): string {
  if (status === "completed") return "pill pill-up";
  if (status === "failed") return "pill pill-down";
  if (status === "running" || status === "pending") return "pill pill-accent";
  return "pill";
}

export function isFinished(status: string): boolean {
  return status === "completed" || status === "failed";
}
