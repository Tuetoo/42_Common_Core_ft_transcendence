"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";

export type CreateOrganizationState = {
  error?: string;
};

export async function createOrganization(
  _prevState: CreateOrganizationState,
  formData: FormData
): Promise<CreateOrganizationState> {
  const name = formData.get("name");
  const description = formData.get("description");

  if (typeof name !== "string" || !name.trim()) {
    return { error: "Please enter an organization name" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/organizations", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        description: typeof description === "string" && description.trim() ? description : null,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not create organization") };
  }

  revalidatePath("/organizations");
  return {};
}

export type InviteMemberState = {
  error?: string;
  success?: boolean;
};

export async function inviteMember(
  orgId: string,
  _prevState: InviteMemberState,
  formData: FormData
): Promise<InviteMemberState> {
  const email = formData.get("email");
  const role = formData.get("role");

  if (typeof email !== "string" || !email.trim()) {
    return { error: "Please enter an email address" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/organizations/${orgId}/invitations`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, role: role || "member" }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not send invitation") };
  }

  return { success: true };
}
