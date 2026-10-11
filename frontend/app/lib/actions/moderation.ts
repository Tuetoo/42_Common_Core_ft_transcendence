"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";
import type { ActionResult } from "./admin";

// Moderation routes of the core service (content:moderate):
//   POST   /moderation/notes/{id}/hide | /unhide    DELETE /moderation/notes/{id}     (204)
//   POST   /moderation/comments/{id}/hide | /unhide DELETE /moderation/comments/{id}  (204)
const KINDS = ["note", "comment"] as const;
const INTENTS = ["hide", "unhide", "delete"] as const;
type Kind = (typeof KINDS)[number];
type Intent = (typeof INTENTS)[number];

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function moderate(kind: Kind, id: string, intent: Intent): Promise<ActionResult> {
  if (!KINDS.includes(kind) || !INTENTS.includes(intent)) {
    return { error: "Unknown action" };
  }
  if (!UUID_RE.test(id)) {
    return { error: `Enter a ${kind} id (a UUID)` };
  }
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  const path = `/moderation/${kind}s/${id}${intent === "delete" ? "" : `/${intent}`}`;
  let res: Response;
  try {
    res = await backendFetch(path, accessToken, { method: intent === "delete" ? "DELETE" : "POST" });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }
  if (res.status === 401) {
    return { error: "Your session expired. Please log in again." };
  }
  if (!res.ok) {
    return { error: await extractErrorMessage(res, `Could not ${intent} the ${kind}`) };
  }

  revalidatePath("/admin/moderation");
  revalidatePath("/research", "layout");
  const done = { hide: "hidden", unhide: "visible again", delete: "deleted" }[intent];
  return { message: `The ${kind} is ${done}.` };
}

// Bound per row: moderateContent.bind(null, "note", note.id, "hide").
export async function moderateContent(kind: Kind, id: string, intent: Intent): Promise<ActionResult> {
  return moderate(kind, id, intent);
}

export type ModerateByIdState = ActionResult & { kind?: string; id?: string };

// The "act by id" form: kind and id are typed in, intent comes from the
// button that was pressed.
export async function moderateById(_prev: ModerateByIdState, formData: FormData): Promise<ModerateByIdState> {
  const kind = String(formData.get("kind") ?? "");
  const id = String(formData.get("id") ?? "").trim();
  const intent = String(formData.get("intent") ?? "");
  const result = await moderate(kind as Kind, id, intent as Intent);
  return { ...result, kind, id };
}
