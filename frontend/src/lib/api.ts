let csrfToken = "";

export function setCsrfToken(value: string) {
  csrfToken = value;
}

export class ApiError extends Error {
  status: number;
  data: unknown;
  constructor(status: number, message: string, data?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

function errorMessage(data: unknown): string {
  if (typeof data === "string") return data.slice(0, 700);
  if (Array.isArray(data))
    return data.map(errorMessage).join(" ").slice(0, 700);
  if (data && typeof data === "object") {
    const record = data as Record<string, unknown>;
    if (record.detail) return errorMessage(record.detail);
    if (record.code) return String(record.code).replaceAll("_", " ");
    return Object.entries(record)
      .map(
        ([key, value]) => `${key.replaceAll("_", " ")}: ${errorMessage(value)}`,
      )
      .join(" · ")
      .slice(0, 700);
  }
  return "The request could not be completed. Please try again.";
}

export interface ApiOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export async function api<T = unknown>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  if (path.startsWith("/") || path.includes("://") || path.startsWith("."))
    throw new ApiError(0, "Invalid application endpoint.");
  const method = options.method ?? "GET";
  let response: Response;
  try {
    response = await fetch(`/console/api/${path}`, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
      headers: {
        Accept: "application/json",
        ...(options.body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
        ...(!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())
          ? { "X-CSRFToken": csrfToken }
          : {}),
        ...options.headers,
      },
      ...(options.body !== undefined
        ? { body: JSON.stringify(options.body) }
        : {}),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(
      0,
      "Unable to reach the server. Check your connection and try again.",
    );
  }
  const data: unknown =
    response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    if (
      response.status === 401 ||
      (response.status === 403 &&
        typeof data === "object" &&
        data !== null &&
        "detail" in data &&
        data.detail === "Authentication credentials were not provided.")
    ) {
      window.dispatchEvent(new Event("console:session-expired"));
    }
    throw new ApiError(
      response.status,
      data ? errorMessage(data) : "The server could not complete this request.",
      data,
    );
  }
  return data as T;
}
