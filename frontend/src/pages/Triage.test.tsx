import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ApiOptions } from "../lib/api";
import Triage from "./Triage";

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  api: vi.fn(),
}));

const categories = ["Facturación", "Acceso", "Soporte técnico", "Otros"];
const run = {
  id: "11111111-1111-4111-8111-111111111111",
  status: "waiting_for_approval",
  ticket_id: "DEMO-001",
  text: "Solicitud ficticia: se ha cobrado dos veces mi factura de prueba.",
  classification: {
    category: "Facturación",
    reason: "La solicitud describe un cargo duplicado.",
  },
  confirmation: null,
  waiting_for_approval: true,
  created_at: "2026-11-28T00:00:00Z",
  updated_at: "2026-11-28T00:00:00Z",
  receipt: null,
};

const mockedApi = vi.mocked(api);
let container: HTMLDivElement;
let root: Root;
let unmounted: boolean;

function respond(
  handler: (path: string, options: ApiOptions) => Promise<unknown>,
) {
  mockedApi.mockImplementation(
    (path, options) => handler(path, options ?? {}) as never,
  );
}

function button(label: string): HTMLButtonElement {
  const result = Array.from(container.querySelectorAll("button")).find(
    (element) => element.textContent?.trim() === label,
  );
  expect(result, `Expected button ${label}`).toBeDefined();
  return result!;
}

function field<T extends HTMLSelectElement | HTMLTextAreaElement>(
  label: string,
): T {
  const element = Array.from(container.querySelectorAll("label")).find(
    (candidate) => candidate.textContent?.includes(label),
  );
  expect(element, `Expected field ${label}`).toBeDefined();
  const input = element!.htmlFor
    ? container.querySelector<T>(`#${element!.htmlFor}`)
    : element!.querySelector<T>("select, textarea");
  expect(input).not.toBeNull();
  return input!;
}

async function click(label: string) {
  await act(async () => {
    button(label).click();
  });
}

async function change(
  element: HTMLSelectElement | HTMLTextAreaElement,
  value: string,
) {
  await act(async () => {
    const prototype =
      element instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLTextAreaElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(
      element,
      value,
    );
    element.dispatchEvent(
      new Event(element instanceof HTMLSelectElement ? "change" : "input", {
        bubbles: true,
      }),
    );
  });
}

async function renderPage() {
  await act(async () => {
    root.render(<Triage />);
  });
}

function writes() {
  return mockedApi.mock.calls.filter(
    ([, options]) => options?.method === "POST",
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  mockedApi.mockReset();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  unmounted = false;
});

afterEach(async () => {
  if (!unmounted) await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Spanish request review", () => {
  it("confirms once and displays a receipt only when the server returns it", async () => {
    let finishWrite!: (result: unknown) => void;
    let saved = false;
    respond(async (_path, options) => {
      if (options.method === "POST") {
        return new Promise((resolve) => {
          finishWrite = resolve;
        });
      }
      return {
        enabled: true,
        categories,
        results: [
          saved
            ? {
                ...run,
                status: "succeeded",
                waiting_for_approval: false,
                confirmation: {
                  approved: true,
                  category: "Facturación",
                  note: "",
                },
                receipt: {
                  receipt_id: "RECIBO-GUARDADO-EN-BACKEND",
                  ticket_id: "DEMO-001",
                  category: "Facturación",
                  idempotency_key: "decision-from-server",
                  created_at: "2026-11-28T00:01:00Z",
                },
              }
            : run,
        ],
      };
    });
    await renderPage();
    expect(container.textContent).toContain(run.text);
    expect(container.textContent).toContain(run.classification.reason);
    expect(container.textContent).not.toContain("RECIBO-GUARDADO-EN-BACKEND");

    await act(async () => {
      const confirm = button("Confirmar");
      confirm.click();
      confirm.click();
    });
    expect(writes()).toHaveLength(1);
    expect(writes()[0][0]).toBe(`triage/runs/${run.id}/confirm/`);
    expect(writes()[0][1]?.body).toEqual({
      approved: true,
      category: "Facturación",
      note: "",
    });
    expect(writes()[0][1]?.headers?.["Idempotency-Key"]).toBeTruthy();
    expect(writes()[0][1]?.signal).toBeInstanceOf(AbortSignal);

    await act(async () => finishWrite({ run_id: run.id, status: "queued" }));
    expect(container.textContent).not.toContain("RECIBO-GUARDADO-EN-BACKEND");
    saved = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(container.textContent).toContain("RECIBO-GUARDADO-EN-BACKEND");
  });

  it("preserves a correction on failure and scopes retry keys to its exact payload", async () => {
    respond(async (_path, options) => {
      if (options.method === "POST") throw new Error("No se pudo guardar");
      return { enabled: true, categories, results: [run] };
    });
    await renderPage();
    await click("Corregir");
    await change(field<HTMLSelectElement>("Categoría definitiva"), "Acceso");
    await change(
      field<HTMLTextAreaElement>("Motivo de la corrección"),
      "El problema real es el acceso a la cuenta de prueba.",
    );
    await click("Guardar corrección");

    expect(container.textContent).toContain("No se pudo guardar");
    expect(field<HTMLSelectElement>("Categoría definitiva").value).toBe(
      "Acceso",
    );
    expect(field<HTMLTextAreaElement>("Motivo de la corrección").value).toBe(
      "El problema real es el acceso a la cuenta de prueba.",
    );
    expect(writes()[0][1]?.body).toEqual({
      approved: true,
      category: "Acceso",
      note: "El problema real es el acceso a la cuenta de prueba.",
    });

    // A server refresh must not replace an operator's unsaved correction.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(field<HTMLSelectElement>("Categoría definitiva").value).toBe(
      "Acceso",
    );
    await click("Guardar corrección");
    expect(writes()[1][1]?.headers?.["Idempotency-Key"]).toBe(
      writes()[0][1]?.headers?.["Idempotency-Key"],
    );

    await change(
      field<HTMLTextAreaElement>("Motivo de la corrección"),
      "Se verificó el problema de acceso.",
    );
    await click("Guardar corrección");
    expect(writes()).toHaveLength(3);
    expect(writes()[2][1]?.headers?.["Idempotency-Key"]).not.toBe(
      writes()[1][1]?.headers?.["Idempotency-Key"],
    );
  });

  it("explains a disabled demo without inventing a request or confirmation", async () => {
    mockedApi.mockResolvedValue({ enabled: false, categories, results: [] });
    await renderPage();
    expect(container.textContent).toMatch(
      /desactiv|deshabilit|habilitad|disponible/i,
    );
    expect(container.textContent).not.toContain(run.text);
    expect(
      Array.from(container.querySelectorAll("button")).some(
        (element) => element.textContent?.trim() === "Confirmar",
      ),
    ).toBe(false);
    expect(writes()).toHaveLength(0);
  });

  it("bounds automatic polling and cancels its request scope when leaving the page", async () => {
    mockedApi.mockResolvedValue({ enabled: true, categories, results: [run] });
    await renderPage();
    const signal = mockedApi.mock.calls[0][1]?.signal;
    expect(signal?.aborted).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(180000);
    });
    const requestsAtPause = mockedApi.mock.calls.length;
    expect(requestsAtPause).toBeGreaterThan(1);
    expect(requestsAtPause).toBeLessThanOrEqual(50);
    expect(container.textContent).toContain(
      "Se pausó la actualización automática",
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180000);
    });
    expect(mockedApi.mock.calls).toHaveLength(requestsAtPause);

    await act(async () => root.unmount());
    unmounted = true;
    expect(signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores an old poll that resolves after a successful confirmation refresh", async () => {
    let finishOldPoll!: (value: unknown) => void;
    let reads = 0;
    respond(async (_path, options) => {
      if (options.method === "POST")
        return { run_id: run.id, status: "queued" };
      reads += 1;
      if (reads === 2)
        return new Promise((resolve) => {
          finishOldPoll = resolve;
        });
      return {
        enabled: true,
        categories,
        results: [
          reads === 1
            ? run
            : {
                ...run,
                status: "succeeded",
                waiting_for_approval: false,
                confirmation: {
                  approved: true,
                  category: "Facturación",
                  note: "",
                },
                receipt: {
                  receipt_id: "RECIBO-TRAS-CONFIRMACIÓN",
                  ticket_id: run.ticket_id,
                  category: "Facturación",
                  idempotency_key: "decision-from-server",
                  created_at: "2026-11-28T00:01:00Z",
                },
              },
        ],
      };
    });
    await renderPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    const oldSignal = mockedApi.mock.calls[1][1]?.signal;
    expect(oldSignal?.aborted).toBe(false);

    await click("Confirmar");
    expect(oldSignal?.aborted).toBe(true);
    expect(container.textContent).toContain("RECIBO-TRAS-CONFIRMACIÓN");
    await act(async () => {
      finishOldPoll({
        enabled: true,
        categories,
        results: [{ ...run, text: "RESPUESTA OBSOLETA" }],
      });
    });
    expect(container.textContent).toContain("RECIBO-TRAS-CONFIRMACIÓN");
    expect(container.textContent).not.toContain("RESPUESTA OBSOLETA");
  });
});
