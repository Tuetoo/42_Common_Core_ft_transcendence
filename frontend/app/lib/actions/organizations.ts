"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch, extractErrorMessage } from "@/app/lib/backend";
import type { ActionResult } from "./admin";

// Limits match OrganizationCreate / OrganizationUpdate / InvitationCreate in
// services/auth-service/domains/organization/schema.py.
const NAME_MAX = 128;
const DESCRIPTION_MAX = 1000;
const EMAIL_MAX = 254;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// The owner role can never be assigned (MemberRoleUpdate, InvitationCreate).
const ASSIGNABLE_ROLES = ["admin", "member", "viewer"];

type CallResult = { ok: true; res: Response } | { ok: false; error: string };

async function call(path: string, init: RequestInit, fallback: string): Promise<CallResult> {
  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { ok: false, error: "You're not logged in" };
  }
  let res: Response;
  try {
    res = await backendFetch(path, accessToken, init);
  } catch {
    return { ok: false, error: "Could not reach the server. Is the backend running?" };
  }
  if (res.status === 401) {
    return { ok: false, error: "Your session expired. Please log in again." };
  }
  if (!res.ok) {
    return { ok: false, error: await extractErrorMessage(res, fallback) };
  }
  return { ok: true, res };
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function validateDetails(name: string, description: string): string | null {
  if (!name) return "Please enter an organization name";
  if (name.length > NAME_MAX) return `The name can be at most ${NAME_MAX} characters`;
  if (description.length > DESCRIPTION_MAX) return `The description can be at most ${DESCRIPTION_MAX} characters`;
  return null;
}

function refresh(orgId: string) {
  revalidatePath("/organizations");
  revalidatePath(`/organizations/${orgId}`);
}

export type OrganizationFormState = {
  error?: string;
  success?: boolean;
  values?: { name: string; description: string };
};

// POST /organizations (organization:create), 201. Opens the new organization.
export async function createOrganization(
  _prevState: OrganizationFormState,
  formData: FormData
): Promise<OrganizationFormState> {
  const values = {
    name: String(formData.get("name") ?? "").trim(),
    description: String(formData.get("description") ?? "").trim(),
  };
  const invalid = validateDetails(values.name, values.description);
  if (invalid) return { values, error: invalid };

  const result = await call(
    "/organizations",
    jsonInit("POST", { name: values.name, description: values.description || null }),
    "Could not create organization"
  );
  if (!result.ok) return { values, error: result.error };

  const { data } = (await result.res.json()) as { data: { id: string } };
  revalidatePath("/organizations");
  redirect(`/organizations/${data.id}`);
}

// PATCH /organizations/{id} (org owner or admin). An empty description clears it.
export async function updateOrganization(
  orgId: string,
  _prevState: OrganizationFormState,
  formData: FormData
): Promise<OrganizationFormState> {
  const values = {
    name: String(formData.get("name") ?? "").trim(),
    description: String(formData.get("description") ?? "").trim(),
  };
  if (!UUID_RE.test(orgId)) return { values, error: "Invalid organization id" };
  const invalid = validateDetails(values.name, values.description);
  if (invalid) return { values, error: invalid };

  const result = await call(
    `/organizations/${orgId}`,
    jsonInit("PATCH", { name: values.name, description: values.description || null }),
    "Could not save the organization"
  );
  if (!result.ok) return { values, error: result.error };
  refresh(orgId);
  return { success: true };
}

// DELETE /organizations/{id} (owner only), 204.
export async function deleteOrganization(orgId: string): Promise<ActionResult> {
  if (!UUID_RE.test(orgId)) return { error: "Invalid organization id" };
  const result = await call(`/organizations/${orgId}`, { method: "DELETE" }, "Could not delete the organization");
  if (!result.ok) return { error: result.error };
  revalidatePath("/organizations");
  redirect("/organizations");
}

// POST /organizations/{id}/leave (any member but the owner), 204.
export async function leaveOrganization(orgId: string): Promise<ActionResult> {
  if (!UUID_RE.test(orgId)) return { error: "Invalid organization id" };
  const result = await call(`/organizations/${orgId}/leave`, { method: "POST" }, "Could not leave the organization");
  if (!result.ok) return { error: result.error };
  revalidatePath("/organizations");
  redirect("/organizations");
}

// POST /organizations/{id}/members/{user_id}/remove (owner or admin; only the owner removes admins), 204.
export async function removeMember(orgId: string, userId: string): Promise<ActionResult> {
  if (!UUID_RE.test(orgId) || !UUID_RE.test(userId)) return { error: "Invalid id" };
  const result = await call(
    `/organizations/${orgId}/members/${userId}/remove`,
    { method: "POST" },
    "Could not remove the member"
  );
  if (!result.ok) return { error: result.error };
  refresh(orgId);
  return {};
}

// PATCH /organizations/{id}/members/{user_id}/role with { role }.
export async function changeMemberRole(
  orgId: string,
  userId: string,
  _prev: ActionResult,
  formData: FormData
): Promise<ActionResult> {
  const role = formData.get("role");
  if (!UUID_RE.test(orgId) || !UUID_RE.test(userId)) return { error: "Invalid id" };
  if (typeof role !== "string" || !ASSIGNABLE_ROLES.includes(role)) {
    return { error: "Choose admin, member or viewer" };
  }
  const result = await call(
    `/organizations/${orgId}/members/${userId}/role`,
    jsonInit("PATCH", { role }),
    "Could not change the role"
  );
  if (!result.ok) return { error: result.error };
  refresh(orgId);
  return {};
}

// DELETE /organizations/{id}/invitations/{invitation_id} (owner or admin), 204.
export async function revokeInvitation(orgId: string, invitationId: string): Promise<ActionResult> {
  if (!UUID_RE.test(orgId) || !UUID_RE.test(invitationId)) return { error: "Invalid id" };
  const result = await call(
    `/organizations/${orgId}/invitations/${invitationId}`,
    { method: "DELETE" },
    "Could not revoke the invitation"
  );
  if (!result.ok) return { error: result.error };
  refresh(orgId);
  return {};
}

export type InviteMemberState = {
  error?: string;
  success?: string;
  values?: { email: string; role: string };
};

// POST /organizations/{id}/invitations with { email, role } (owner or
// admin, and the system permission organization:create), 201.
export async function inviteMember(
  orgId: string,
  _prevState: InviteMemberState,
  formData: FormData
): Promise<InviteMemberState> {
  const values = {
    email: String(formData.get("email") ?? "").trim(),
    role: String(formData.get("role") ?? "member"),
  };
  if (!UUID_RE.test(orgId)) return { values, error: "Invalid organization id" };
  if (!values.email || values.email.length > EMAIL_MAX || !EMAIL_RE.test(values.email)) {
    return { values, error: "Enter a valid email address" };
  }
  if (!ASSIGNABLE_ROLES.includes(values.role)) {
    return { values, error: "Choose admin, member or viewer" };
  }

  const result = await call(
    `/organizations/${orgId}/invitations`,
    jsonInit("POST", { email: values.email, role: values.role }),
    "Could not send invitation"
  );
  if (!result.ok) return { values, error: result.error };
  refresh(orgId);
  return { success: `Invitation sent to ${values.email}.` };
}
