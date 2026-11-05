export const number = (value: number) =>
  new Intl.NumberFormat("en", {
    notation: value >= 1000000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
export function money(
  value: number | string | null,
  currency: string | null,
  minor = false,
) {
  if (value === null || !currency) return "—";
  try {
    const formatter = new Intl.NumberFormat("en", {
      style: "currency",
      currency,
      maximumFractionDigits: 6,
    });
    const digits =
      new Intl.NumberFormat("en", {
        style: "currency",
        currency,
      }).resolvedOptions().maximumFractionDigits ?? 2;
    return formatter.format(Number(value) / (minor ? 10 ** digits : 1));
  } catch {
    return `${value} ${currency}`;
  }
}
export function duration(seconds: number | null) {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}h ${minutes % 60}m`
    : `${minutes}m ${Math.round(seconds % 60)}s`;
}
