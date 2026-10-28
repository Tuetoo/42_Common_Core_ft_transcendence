"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { call, jsonInit } from "@/app/lib/session";
import { SYSTEM_ROLES, UUID_RE } from "@/app/lib/format";
import { EMAIL_MAX, EMAIL_RE, USERNAME_RE, isTimezone } from "@/app/(public)/field-rules";

// Result of a one-click action (a button or a small select form).
// Only types may be shared from a "use server" file; every exported
// function here is a server action.
export type ActionResult = {
  error?: string;
  message?: string;
};


function refresh(userId: string) {
  revalidatePath("/admin/users");
  revalidatePath(`/admin/users/${userId}`);
}

// PATCH /admin/users/{id} with { role } (user:manage).
export async function changeUserRole(
  userId: string,
  _prev: ActionResult,
  formData: FormData
): Promise<ActionResult> {
  const role = formData.get("role");
  if (!UUID_RE.test(userId)) return { error: "Invalid user id" };
  if (typeof role !== "string" || !(SYSTEM_ROLES as readonly string[]).includes(role)) {
    return { error: "Choose one of: guest, user, moderator, admin" };
  }
  const result = await call(`/admin/users/${userId}`, jsonInit("PATCH", { role }), "Could not change the role");
  if (!result.ok) return { error: result.error };
  refresh(userId);
  return { message: `Role changed to ${role}` };
}

// POST /admin/users/{id}/deactivate or /reactivate (user:deactivate).
export async function setUserActive(userId: string, active: boolean): Promise<ActionResult> {
  if (!UUID_RE.test(userId)) return { error: "Invalid user id" };
  const result = await call(
    `/admin/users/${userId}/${active ? "reactivate" : "deactivate"}`,
    { method: "POST" },
    active ? "Could not reactivate the user" : "Could not deactivate the user"
  );
  if (!result.ok) return { error: result.error };
  refresh(userId);
  return {};
}

// DELETE /admin/users/{id} (user:manage), 204. The account is anonymised
// and kept as "deleted"; from the detail page we go back to the list.
export async function deleteUser(userId: string, backToList: boolean): Promise<ActionResult> {
  if (!UUID_RE.test(userId)) return { error: "Invalid user id" };
  const result = await call(`/admin/users/${userId}`, { method: "DELETE" }, "Could not delete the user");
  if (!result.ok) return { error: result.error };
  refresh(userId);
  if (backToList) redirect("/admin/users");
  return {};
}

// DELETE /admin/users/{id}/2fa (user:manage), 204.
export async function resetUserTwoFactor(userId: string): Promise<ActionResult> {
  if (!UUID_RE.test(userId)) return { error: "Invalid user id" };
  const result = await call(`/admin/users/${userId}/2fa`, { method: "DELETE" }, "Could not reset two-factor authentication");
  if (!result.ok) return { error: result.error };
  refresh(userId);
  return { message: "Two-factor authentication removed" };
}

// Moderators cannot list users (GET /admin/users needs user:manage), so
// they act on an account by its id.
export type AccountByIdState = ActionResult & { userId?: string };

export async function setUserActiveById(_prev: AccountByIdState, formData: FormData): Promise<AccountByIdState> {
  const userId = String(formData.get("user_id") ?? "").trim();
  const intent = formData.get("intent");
  if (!UUID_RE.test(userId)) {
    return { userId, error: "Enter a user id (a UUID such as 3f2b…-…)" };
  }
  if (intent !== "deactivate" && intent !== "reactivate") {
    return { userId, error: "Unknown action" };
  }
  const result = await call(`/admin/users/${userId}/${intent}`, { method: "POST" }, `Could not ${intent} the user`);
  if (!result.ok) return { userId, error: result.error };
  const { data } = (await result.res.json()) as { data: { username: string; is_active: boolean } };
  revalidatePath("/admin/users");
  return { userId, message: `${data.username} is now ${data.is_active ? "active" : "inactive"}.` };
}

export type EditUserValues = {
  username: string;
  email: string;
  is_verified: boolean;
  first_name: string;
  last_name: string;
  bio: string;
  timezone: string;
  country: string;
};

export type EditUserState = {
  error?: string;
  success?: boolean;
  values?: EditUserValues;
};

// PATCH /admin/users/{id} (user:manage) with the account and profile
// fields. Username, email and is_verified are only sent when they changed:
// a new email without is_verified makes the backend send a new
// verification email, which an unchanged checkbox must not suppress.
export async function updateUserByAdmin(
  userId: string,
  original: { username: string; email: string; is_verified: boolean },
  _prev: EditUserState,
  formData: FormData
): Promise<EditUserState> {
  const text = (name: string) => String(formData.get(name) ?? "").trim();
  const values: EditUserValues = {
    username: text("username"),
    email: text("email"),
    is_verified: formData.get("is_verified") === "on",
    first_name: text("first_name"),
    last_name: text("last_name"),
    bio: text("bio"),
    timezone: text("timezone"),
    country: text("country"),
  };

  if (!UUID_RE.test(userId)) return { values, error: "Invalid user id" };
  if (!USERNAME_RE.test(values.username)) {
    return { values, error: "Username: 3-32 characters, letters, digits, _ . - only" };
  }
  if (values.email.length > EMAIL_MAX || !EMAIL_RE.test(values.email)) {
    return { values, error: "Enter a valid email address" };
  }
  if (values.first_name.length > 100 || values.last_name.length > 100) {
    return { values, error: "First and last name: at most 100 characters each" };
  }
  if (values.bio.length > 500) return { values, error: "Bio: at most 500 characters" };
  if (values.timezone && (values.timezone.length > 64 || !isTimezone(values.timezone))) {
    return { values, error: "Timezone must be an IANA name such as Europe/Paris" };
  }
  if (values.country.length > 100) return { values, error: "Country: at most 100 characters" };

  const body: Record<string, unknown> = {
    first_name: values.first_name || null,
    last_name: values.last_name || null,
    bio: values.bio || null,
    timezone: values.timezone || null,
    country: values.country || null,
  };
  if (values.username !== original.username) body.username = values.username;
  if (values.email.toLowerCase() !== original.email.toLowerCase()) body.email = values.email;
  if (values.is_verified !== original.is_verified) body.is_verified = values.is_verified;

  const result = await call(`/admin/users/${userId}`, jsonInit("PATCH", body), "Could not save the user");
  if (!result.ok) return { values, error: result.error };
  refresh(userId);
  return { success: true };
}

// Moderation routes of the core service (content:moderate):
//   POST   /moderation/notes/{id}/hide | /unhide    DELETE /moderation/notes/{id}     (204)
//   POST   /moderation/comments/{id}/hide | /unhide DELETE /moderation/comments/{id}  (204)
const KINDS = ["note", "comment"] as const;
const INTENTS = ["hide", "unhide", "delete"] as const;
type Kind = (typeof KINDS)[number];
type Intent = (typeof INTENTS)[number];

async function moderate(kind: Kind, id: string, intent: Intent): Promise<ActionResult> {
  if (!KINDS.includes(kind) || !INTENTS.includes(intent)) {
    return { error: "Unknown action" };
  }
  if (!UUID_RE.test(id)) {
    return { error: `Enter a ${kind} id (a UUID)` };
  }
  const path = `/moderation/${kind}s/${id}${intent === "delete" ? "" : `/${intent}`}`;
  const result = await call(path, { method: intent === "delete" ? "DELETE" : "POST" }, `Could not ${intent} the ${kind}`);
  if (!result.ok) return { error: result.error };

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
