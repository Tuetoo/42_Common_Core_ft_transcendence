import { backendFetch } from "@/app/lib/backend";
import type { Organization } from "./types";

// GET /organizations (auth-service): the organizations the user belongs to.
// Used to share notes and documents. A failure only hides the sharing
// options, so it does not break the page.
export async function loadOrganizations(accessToken: string): Promise<Organization[]> {
  try {
    const res = await backendFetch("/organizations", accessToken, { cache: "no-store" });
    if (!res.ok) return [];
    const { data }: { data: Organization[] } = await res.json();
    return data.map(({ id, name }) => ({ id, name }));
  } catch {
    return [];
  }
}
