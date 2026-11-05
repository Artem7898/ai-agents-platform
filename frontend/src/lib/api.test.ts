import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrfToken } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken("");
});
describe("same-origin browser API", () => {
  it("sends protected writes with a memory-only CSRF token and no API credential", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response('{"saved":true}', {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    setCsrfToken("masked-csrf-example");
    await expect(
      api("agents/", {
        method: "POST",
        body: { name: "Research" },
        headers: { "Idempotency-Key": "example-command" },
      }),
    ).resolves.toEqual({ saved: true });
    const [path, options] = fetch.mock.calls[0];
    expect(path).toBe("/console/api/agents/");
    expect(options.credentials).toBe("same-origin");
    expect(options.headers["X-CSRFToken"]).toBe("masked-csrf-example");
    expect(options.headers.Authorization).toBeUndefined();
    expect(options.headers["Idempotency-Key"]).toBe("example-command");
    expect(Object.keys(localStorage)).toEqual([]);
    expect(Object.keys(sessionStorage)).toEqual([]);
  });
  it.each(["https://external.example/collect", "/agents/", "../session/"])(
    "rejects destinations outside the console namespace: %s",
    async (path) => {
      const fetch = vi.fn();
      vi.stubGlobal("fetch", fetch);
      await expect(api(path)).rejects.toBeInstanceOf(ApiError);
      expect(fetch).not.toHaveBeenCalled();
    },
  );
  it("distinguishes expired authentication from a staff permission denial", async () => {
    const expired = vi.fn();
    window.addEventListener("console:session-expired", expired);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response('{"detail":"Staff required"}', { status: 403 }),
        )
        .mockResolvedValueOnce(
          new Response(
            '{"detail":"Authentication credentials were not provided."}',
            { status: 401 },
          ),
        ),
    );
    await expect(api("runtimes/")).rejects.toMatchObject({
      status: 403,
      message: "Staff required",
    });
    expect(expired).not.toHaveBeenCalled();
    await expect(api("agents/")).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledOnce();
    window.removeEventListener("console:session-expired", expired);
  });
  it("keeps cancellation distinct from an unknown network outcome", async () => {
    const abort = new DOMException("Cancelled", "AbortError");
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockRejectedValueOnce(abort)
        .mockRejectedValueOnce(new TypeError("Network down")),
    );
    await expect(api("runs/")).rejects.toBe(abort);
    await expect(
      api("execute/", { method: "POST", body: {} }),
    ).rejects.toMatchObject({ status: 0 });
  });
  it("accepts an empty successful deletion response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 204 })),
    );
    await expect(
      api("agents/example/", { method: "DELETE" }),
    ).resolves.toBeNull();
  });
});
