import { expect, test } from "@playwright/test";
import { env } from "node:process";

const runId = env.TRIAGE_E2E_RUN_ID;
const base = env.CONSOLE_E2E_BASE_URL;
const username = env.CONSOLE_E2E_USERNAME;
const password = env.CONSOLE_E2E_PASSWORD;

// The existing console wrapper discovers every spec. Its fixtures deliberately
// do not enable this separate, mutating triage check.
test.skip(!runId, "Run through the isolated triage Django browser wrapper.");
if (runId) {
  if (!base || !password || username !== "triage-browser-owner") {
    throw new Error("Triage browser checks require their isolated test owner.");
  }
  if (!["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname)) {
    throw new Error("Triage browser mutations require a local test server.");
  }
  if (!/^[0-9a-f-]{36}$/i.test(runId)) {
    throw new Error("The triage fixture run must have a UUID.");
  }
}

const url = (path: string) => `${base}${path}`;
const classification = {
  category: "Facturación",
  reason:
    "La consulta menciona una factura; esta propuesta es un dato de prueba.",
};
const confirmation = {
  approved: true,
  category: "Acceso",
  note: "El problema es el acceso a la cuenta, no la factura.",
};

test("Spanish review keeps the recommendation and queues the corrected human decision", async ({
  page,
  context,
}, testInfo) => {
  const inboxUrl = url("/console/api/triage/runs/");
  const confirmUrl = url(`/console/api/triage/runs/${runId}/confirm/`);
  expect((await context.request.get(inboxUrl)).status()).toBe(401);
  await page.goto(url("/console/"));
  await page.getByLabel("Username", { exact: true }).fill(username!);
  await page.getByLabel("Password", { exact: true }).fill(password!);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /dashboard/i }).first(),
  ).toBeVisible();
  const cookies = await context.cookies();
  expect(cookies.find((cookie) => cookie.name === "sessionid")?.httpOnly).toBe(
    true,
  );
  expect(cookies.some((cookie) => cookie.name === "csrftoken")).toBe(true);
  const rejected = await context.request.post(confirmUrl, {
    data: confirmation,
  });
  expect(rejected.status()).toBe(403);

  const loaded = page.waitForResponse(
    (response) =>
      response.url() === inboxUrl && response.request().method() === "GET",
  );
  await page.goto(url("/console/triage"));
  const response = await loaded;
  expect(response.status()).toBe(200);
  const inbox = await response.json();
  expect(inbox.enabled).toBe(true);
  expect(inbox.results).toHaveLength(1);
  expect(inbox.results[0]).toMatchObject({
    id: runId,
    ticket_id: "TRIAGE-BROWSER-OWN",
    status: "waiting_human",
    classification,
    confirmation: null,
    waiting_for_approval: true,
    receipt: null,
  });
  await expect(
    page.getByRole("heading", { name: "Revisar solicitudes", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("TRIAGE-BROWSER-PRIVATE")).toHaveCount(0);
  const recommendation = page.locator(".triage-recommendation-card");
  await expect(
    recommendation.getByText("Facturación", { exact: true }),
  ).toBeVisible();
  await expect(
    recommendation.getByText(classification.reason, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recibo pendiente", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("triage-desktop-1440.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("triage-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 1440, height: 1050 });

  await page.getByRole("button", { name: "Corregir", exact: true }).click();
  const category = page.getByRole("combobox", {
    name: "Categoría definitiva",
    exact: true,
  });
  await expect(category).toBeFocused();
  await expect(category).toHaveValue(classification.category);
  await category.selectOption(confirmation.category);
  await page
    .getByLabel("Motivo de la corrección", { exact: true })
    .fill(confirmation.note);
  await expect(
    recommendation.getByText("Facturación", { exact: true }),
  ).toBeVisible();
  const submitted = page.waitForResponse(
    (result) =>
      result.url() === confirmUrl && result.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Guardar corrección", exact: true })
    .click();
  const saved = await submitted;
  expect(saved.status()).toBe(202);
  expect(saved.request().postDataJSON()).toEqual(confirmation);
  expect(saved.request().headers()["x-csrftoken"]).toBeTruthy();
  expect(saved.request().headers()["idempotency-key"]).toBeTruthy();
  expect(await saved.json()).toEqual({ run_id: runId, status: "queued" });
  await expect(
    page.getByText("Decisión recibida", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Categoría definitiva: Acceso", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(`Nota: ${confirmation.note}`, { exact: true }),
  ).toBeVisible();
  await expect(
    recommendation.getByText("Facturación", { exact: true }),
  ).toBeVisible();
  await expect(
    recommendation.getByText(classification.reason, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recibo guardado", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Recibo pendiente", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Corregir", exact: true }),
  ).toHaveCount(0);

  const persisted = await context.request.get(inboxUrl);
  expect(persisted.status()).toBe(200);
  expect((await persisted.json()).results).toEqual([
    expect.objectContaining({
      id: runId,
      status: "waiting_human",
      classification,
      confirmation,
      waiting_for_approval: false,
      receipt: null,
    }),
  ]);
  await page.reload();
  await expect(
    page.getByText("Categoría definitiva: Acceso", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recibo pendiente", exact: true }),
  ).toBeVisible();
});
