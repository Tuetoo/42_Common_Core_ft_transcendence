"use server";

import { revalidatePath } from "next/cache";
import { getAccessToken } from "@/app/lib/session";
import { backendFetch } from "@/app/lib/backend";
import { extractErrorMessage } from "@/app/lib/api-error";

export type CreatePortfolioState = {
  error?: string;
};

export async function createPortfolio(
  _prevState: CreatePortfolioState,
  formData: FormData
): Promise<CreatePortfolioState> {
  const name = formData.get("name");
  const currency = formData.get("currency");

  if (typeof name !== "string" || !name.trim()) {
    return { error: "Please enter a portfolio name" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch("/portfolios", accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, currency: currency || "USD" }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not create portfolio") };
  }

  revalidatePath("/portfolios");
  return {};
}

export type AddHoldingState = {
  error?: string;
};

export async function addHolding(
  portfolioId: string,
  _prevState: AddHoldingState,
  formData: FormData
): Promise<AddHoldingState> {
  const ticker = formData.get("ticker");
  const quantity = formData.get("quantity");
  const averageCost = formData.get("average_cost");

  if (
    typeof ticker !== "string" || !ticker.trim() ||
    typeof quantity !== "string" || !quantity ||
    typeof averageCost !== "string" || !averageCost
  ) {
    return { error: "Please fill in all fields" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/portfolios/${portfolioId}/holdings`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: ticker.toUpperCase(),
        quantity,
        average_cost: averageCost,
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not add holding") };
  }

  revalidatePath(`/portfolios/${portfolioId}`);
  return {};
}

export type AddTransactionState = {
  error?: string;
};

export async function addTransaction(
  portfolioId: string,
  _prevState: AddTransactionState,
  formData: FormData
): Promise<AddTransactionState> {
  const transactionType = formData.get("transaction_type");
  const quantity = formData.get("quantity");
  const transactionDate = formData.get("transaction_date");
  // Deposits and withdrawals move cash: the backend books them under the
  // reserved ticker CASH, with the amount in `quantity` and no price.
  const isCash = transactionType === "deposit" || transactionType === "withdrawal";
  const ticker = isCash ? "CASH" : formData.get("ticker");
  const price = isCash ? null : formData.get("price");

  if (
    typeof ticker !== "string" || !ticker.trim() ||
    typeof transactionType !== "string" ||
    typeof quantity !== "string" || !quantity ||
    (!isCash && (typeof price !== "string" || !price)) ||
    typeof transactionDate !== "string" || !transactionDate
  ) {
    return { error: "Please fill in all fields" };
  }

  const accessToken = await getAccessToken();
  if (!accessToken) {
    return { error: "You're not logged in" };
  }

  let res: Response;
  try {
    res = await backendFetch(`/portfolios/${portfolioId}/transactions`, accessToken, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: ticker.toUpperCase(),
        transaction_type: transactionType,
        quantity,
        ...(isCash ? {} : { price }),
        transaction_date: new Date(transactionDate).toISOString(),
      }),
    });
  } catch {
    return { error: "Could not reach the server. Is the backend running?" };
  }

  if (!res.ok) {
    return { error: await extractErrorMessage(res, "Could not record transaction") };
  }

  revalidatePath(`/portfolios/${portfolioId}`);
  return {};
}
