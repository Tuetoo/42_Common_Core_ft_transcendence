"use server";

import { revalidatePath } from "next/cache";
import { call, jsonInit } from "@/app/lib/session";
import { boundedText, CURRENCIES, NAME_MAX_LENGTH, normalizeTicker, positiveDecimal, UUID_RE } from "@/app/lib/format";

// Every form state carries the submitted values back on an error: React
// resets a form after its action runs, and the forms use these values as
// their defaults so the user doesn't have to type everything again.
type FormValues = Record<string, string>;

function valuesOf(formData: FormData, names: string[]): FormValues {
  const values: FormValues = {};
  for (const name of names) {
    const value = formData.get(name);
    if (typeof value === "string") values[name] = value;
  }
  return values;
}

const CASH_TICKER = "CASH";

async function send(
  path: string,
  method: "POST" | "PATCH",
  body: unknown,
  fallback: string
): Promise<string | null> {
  const result = await call(path, jsonInit(method, body), fallback);
  return result.ok ? null : result.error;
}

export type CreatePortfolioState = {
  error?: string;
  values?: FormValues;
};

// `sharing` is "private" or the id of an organization to share with: the
// backend stores that as organization_id + visibility "organization".
export async function createPortfolio(
  _prevState: CreatePortfolioState,
  formData: FormData
): Promise<CreatePortfolioState> {
  const values = valuesOf(formData, ["name", "currency", "sharing"]);
  const name = boundedText(formData.get("name"), NAME_MAX_LENGTH);
  const currency = formData.get("currency") || "USD";
  const sharing = formData.get("sharing") || "private";

  if (!name) {
    return { error: `Please enter a name of 1 to ${NAME_MAX_LENGTH} characters`, values };
  }
  if (typeof currency !== "string" || !(CURRENCIES as readonly string[]).includes(currency)) {
    return { error: `Currency must be one of ${CURRENCIES.join(", ")}`, values };
  }
  if (typeof sharing !== "string" || (sharing !== "private" && !UUID_RE.test(sharing))) {
    return { error: "Please choose who can see this portfolio", values };
  }

  const shared = sharing !== "private";
  const error = await send(
    "/portfolios",
    "POST",
    {
      name,
      currency,
      visibility: shared ? "organization" : "private",
      ...(shared ? { organization_id: sharing } : {}),
    },
    "Could not create portfolio"
  );
  if (error) return { error, values };

  revalidatePath("/portfolios");
  return {};
}

export type UpdatePortfolioState = {
  error?: string;
  success?: boolean;
  values?: FormValues;
};

// PATCH /portfolios/{id} accepts name and visibility. The organization a
// portfolio belongs to can only be chosen when it is created.
export async function updatePortfolio(
  portfolioId: string,
  _prevState: UpdatePortfolioState,
  formData: FormData
): Promise<UpdatePortfolioState> {
  const values = valuesOf(formData, ["name", "visibility"]);
  const name = boundedText(formData.get("name"), NAME_MAX_LENGTH);
  const visibility = formData.get("visibility");

  if (!name) {
    return { error: `Please enter a name of 1 to ${NAME_MAX_LENGTH} characters`, values };
  }
  if (visibility !== null && visibility !== "private" && visibility !== "organization") {
    return { error: "Please choose who can see this portfolio", values };
  }

  const error = await send(
    `/portfolios/${encodeURIComponent(portfolioId)}`,
    "PATCH",
    { name, ...(visibility ? { visibility } : {}) },
    "Could not update portfolio"
  );
  if (error) return { error, values };

  revalidatePath(`/portfolios/${portfolioId}`);
  revalidatePath("/portfolios");
  return { success: true };
}

export type AddHoldingState = {
  error?: string;
  values?: FormValues;
};

export async function addHolding(
  portfolioId: string,
  _prevState: AddHoldingState,
  formData: FormData
): Promise<AddHoldingState> {
  const values = valuesOf(formData, ["ticker", "quantity", "average_cost"]);
  const ticker = normalizeTicker(formData.get("ticker"));
  const quantity = positiveDecimal(formData.get("quantity"));
  const averageCost = positiveDecimal(formData.get("average_cost"));

  if (!ticker) {
    return { error: "Ticker must be 1-10 characters: letters, digits, '.' or '-'", values };
  }
  if (ticker === CASH_TICKER) {
    return { error: "CASH is reserved for deposits and withdrawals; record those as transactions", values };
  }
  if (!quantity || !averageCost) {
    return { error: "Quantity and average cost must be numbers greater than 0 (at most 18 decimals)", values };
  }

  const error = await send(
    `/portfolios/${encodeURIComponent(portfolioId)}/holdings`,
    "POST",
    { ticker, quantity, average_cost: averageCost },
    "Could not add holding"
  );
  if (error) return { error, values };

  revalidatePath(`/portfolios/${portfolioId}`);
  return {};
}

export type AddTransactionState = {
  error?: string;
  values?: FormValues;
};

const TRANSACTION_TYPES = ["buy", "sell", "deposit", "withdrawal"];

export async function addTransaction(
  portfolioId: string,
  _prevState: AddTransactionState,
  formData: FormData
): Promise<AddTransactionState> {
  const values = valuesOf(formData, ["transaction_type", "ticker", "quantity", "price", "transaction_date"]);
  const transactionType = formData.get("transaction_type");
  if (typeof transactionType !== "string" || !TRANSACTION_TYPES.includes(transactionType)) {
    return { error: "Please choose a transaction type", values };
  }
  // Deposits and withdrawals move cash: the backend books them under the
  // reserved ticker CASH, with the amount in `quantity` and no price.
  const isCash = transactionType === "deposit" || transactionType === "withdrawal";
  const ticker = isCash ? CASH_TICKER : normalizeTicker(formData.get("ticker"));
  const quantity = positiveDecimal(formData.get("quantity"));
  const price = isCash ? null : positiveDecimal(formData.get("price"));

  if (!ticker) {
    return { error: "Ticker must be 1-10 characters: letters, digits, '.' or '-'", values };
  }
  if (!isCash && ticker === CASH_TICKER) {
    return { error: "CASH is reserved for deposits and withdrawals", values };
  }
  if (!quantity) {
    return { error: `${isCash ? "Amount" : "Quantity"} must be a number greater than 0`, values };
  }
  if (!isCash && !price) {
    return { error: "Price must be a number greater than 0", values };
  }

  // The browser turns the datetime-local value (the user's wall clock) into
  // an exact instant and sends it as `transaction_date_iso`. This server may
  // run in another time zone (UTC in Docker), so it must not parse the local
  // value itself. Without JavaScript only the local value arrives; it is
  // then read as UTC.
  const iso = formData.get("transaction_date_iso");
  const local = formData.get("transaction_date");
  const instant =
    typeof iso === "string" && iso
      ? new Date(iso)
      : typeof local === "string" && local
        ? new Date(`${local}Z`)
        : null;
  if (!instant || Number.isNaN(instant.getTime())) {
    return { error: "Please pick the date and time of the transaction", values };
  }
  if (instant.getTime() > Date.now() + 24 * 60 * 60 * 1000) {
    return { error: "The date can't be more than 1 day in the future", values };
  }

  const error = await send(
    `/portfolios/${encodeURIComponent(portfolioId)}/transactions`,
    "POST",
    {
      ticker,
      transaction_type: transactionType,
      quantity,
      ...(isCash ? {} : { price }),
      transaction_date: instant.toISOString(),
    },
    "Could not record transaction"
  );
  if (error) return { error, values };

  revalidatePath(`/portfolios/${portfolioId}`);
  return {};
}
