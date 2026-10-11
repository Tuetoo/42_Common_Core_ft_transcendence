// The backend sends money and quantities as Decimal strings, sometimes in
// exponent form ("1E+4") or with long repeating decimals
// ("151.6666666666666666666666667"). For display only — never feed the
// result back into a calculation or a request.
export function formatNumber(value: string | number | null | undefined, maxDecimals = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toLocaleString("en-US", { maximumFractionDigits: maxDecimals });
}
