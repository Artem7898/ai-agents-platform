import { expect, test, type Page } from "@playwright/test";
import { env } from "node:process";

const base = env.CONSOLE_E2E_BASE_URL;
const username = env.CONSOLE_E2E_USERNAME;
const password = env.CONSOLE_E2E_PASSWORD;
if (!base || !username || !password) {
  throw new Error(
    "Run the console browser suite through the isolated Django test wrapper.",
  );
}
if (!["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname)) {
  throw new Error(
    "Console browser mutations are restricted to a local test server.",
  );
}
const url = (path: string) => `${base}${path}`;

async function signIn(
  page: Page,
  login = username!,
  loginPassword = password!,
) {
  await page.goto(url("/console/"));
  await page.getByLabel("Username", { exact: true }).fill(login);
  await page.getByLabel("Password", { exact: true }).fill(loginPassword);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /dashboard/i }).first(),
  ).toBeVisible();
}

test("real password sign-in preserves cookie, CSRF and token API boundaries", async ({
  page,
  context,
}, testInfo) => {
  await signIn(page);
  const cookie = (await context.cookies()).find(
    (item) => item.name === "sessionid",
  );
  expect(cookie?.httpOnly).toBe(true);
  expect(
    await page.evaluate(() => document.cookie.includes("sessionid=")),
  ).toBe(false);
  const storedCredentials = await page.evaluate(() =>
    [localStorage, sessionStorage].flatMap((storage) =>
      Object.keys(storage).filter((key) =>
        /(token|password|credential)/i.test(key),
      ),
    ),
  );
  expect(storedCredentials).toEqual([]);
  const tokenApi = await context.request.get(url("/api/v1/agents/"));
  expect(tokenApi.status()).toBe(401);
  const noCsrf = await context.request.post(url("/console/api/agents/"), {
    data: { name: "Rejected CSRF request" },
  });
  expect(noCsrf.status()).toBe(403);
  if (env.CONSOLE_E2E_OTHER_AGENT_ID) {
    const other = await context.request.get(
      url(`/console/api/agents/${env.CONSOLE_E2E_OTHER_AGENT_ID}/`),
    );
    expect(other.status()).toBe(404);
  }
  const dashboard = await context.request.get(url("/console/api/dashboard/"));
  expect(dashboard.ok()).toBe(true);
  const data = await dashboard.json();
  expect(data.metrics.system_health.uptime_percent).toBeNull();
  expect(data.knowledge.documents_count).toBeNull();
  await expect(page.getByText("Run activity", { exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("console-desktop-1440.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 1920, height: 1200 });
  await page.screenshot({
    path: testInfo.outputPath("console-desktop-1920.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("agent creation and editing persist through browser reload", async ({
  page,
}) => {
  await signIn(page);
  await page.goto(url("/console/agents"));
  await page.getByRole("button", { name: "New agent", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const name = `Browser-created agent ${Date.now()}`;
  await dialog.getByLabel("Agent name", { exact: true }).fill(name);
  await dialog
    .getByRole("combobox", { name: "Exact model name", exact: true })
    .fill("gpt-4o");
  await dialog
    .getByLabel(/^System instructions/)
    .fill("Synthetic browser test. Do not call any external service.");
  await dialog.getByRole("button", { name: "Save agent", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await page.getByRole("button", { name: `Edit ${name}`, exact: true }).click();
  const revised = `${name} edited`;
  await page
    .getByRole("dialog")
    .getByLabel("Agent name", { exact: true })
    .fill(revised);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save agent", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: revised, exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: revised, exact: true }),
  ).toBeVisible();
});

test("an empty workspace provides honest onboarding", async ({ page }) => {
  expect(env.CONSOLE_E2E_EMPTY_USERNAME).toBeTruthy();
  await signIn(page, env.CONSOLE_E2E_EMPTY_USERNAME!);
  await expect(
    page.getByText("No reported usage yet", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Your agents will appear here", { exact: true }),
  ).toBeVisible();
  await page.goto(url("/console/agents"));
  await expect(
    page.getByText("Your first agent starts here", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create agent", exact: true }),
  ).toBeVisible();
});

test("a dashboard outage offers a working retry", async ({ page }) => {
  let recovering = false;
  await page.route("**/console/api/dashboard/", async (route) => {
    if (recovering) await route.continue();
    else
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"code":"temporary_test_failure"}',
      });
  });
  await signIn(page);
  await expect(
    page.getByText("Your dashboard is temporarily unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  recovering = true;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("Run activity", { exact: true })).toBeVisible();
});

test("screens expose actual resources and honest disconnected states", async ({
  page,
}) => {
  await signIn(page);
  for (const [path, heading] of [
    ["workflows", /workflow/i],
    ["runs", /runs|traces/i],
    ["billing", /billing|subscription/i],
    ["observability", /observability/i],
    ["knowledge", /knowledge|memory/i],
    ["settings", /settings/i],
  ] as const) {
    await page.goto(url(`/console/${path}`));
    await expect(
      page.getByRole("heading", { name: heading }).first(),
    ).toBeVisible();
    await expect(page.locator("body")).not.toContainText(
      "Unexpected Application Error",
    );
  }
  await page.goto(url("/console/knowledge"));
  await expect(
    page.getByText(/not connected|not configured|not available/i).first(),
  ).toBeVisible();
});

test("workflow builder saves a real graph and rejects a cycle", async ({
  page,
  context,
}) => {
  await signIn(page);
  await page.goto(url("/console/workflows"));
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /^Condition/ })
    .click();
  const name = `Browser-created flow ${Date.now()}`;
  await page.getByLabel("Workflow name", { exact: true }).fill(name);
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/console/api/workflows/") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const response = await saved;
  expect(response.status()).toBe(201);
  const definition = await response.json();
  await expect(
    page.getByText(/Workflow saved\. Existing runs keep/),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Workflow name", { exact: true })).toHaveValue(
    name,
  );
  const persisted = await context.request.get(
    url(`/console/api/workflows/${definition.id}/`),
  );
  expect((await persisted.json()).nodes).toHaveLength(1);
  await page
    .getByRole("button", { name: "Advanced JSON", exact: true })
    .click();
  const condition = {
    kind: "condition",
    config: { condition: { path: "input.enabled", op: "truthy" } },
  };
  await page.getByLabel("Advanced workflow JSON", { exact: true }).fill(
    JSON.stringify({
      nodes: [
        { ...condition, id: "a" },
        { ...condition, id: "b" },
      ],
      edges: [
        { source: "a", target: "b" },
        { source: "b", target: "a" },
      ],
    }),
  );
  await page.getByRole("button", { name: "Apply JSON", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(/acyclic/i);
});

test("workflow navigation protects an unsaved draft", async ({ page }) => {
  await signIn(page);
  await page.goto(url("/console/workflows"));
  await page
    .getByLabel("Workflow name", { exact: true })
    .fill("Unsaved browser draft");
  await page.getByRole("link", { name: "Agents", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "Discard unsaved changes?" });
  await dialog
    .getByRole("button", { name: "Keep editing", exact: true })
    .click();
  await expect(page.getByLabel("Workflow name", { exact: true })).toHaveValue(
    "Unsaved browser draft",
  );
  await page.getByRole("link", { name: "Agents", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "Discard unsaved changes?" });
  await dialog
    .getByRole("button", { name: "Discard changes", exact: true })
    .click();
  await expect(page).toHaveURL(/\/console\/agents$/);
  await expect(
    page.getByRole("heading", { name: "Agents", exact: true }).first(),
  ).toBeVisible();
});

test("a harmless workflow is queued and cancelled through real commands", async ({
  page,
  context,
}) => {
  expect(env.CONSOLE_E2E_SAFE_WORKFLOW_ID).toBeTruthy();
  await signIn(page);
  await page
    .getByRole("button", { name: "New run", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("combobox", { name: "Workflow", exact: true })
    .selectOption(env.CONSOLE_E2E_SAFE_WORKFLOW_ID!);
  await dialog.getByLabel(/^Input JSON/).fill('{"enabled":true}');
  const queued = page.waitForResponse(
    (response) =>
      response.url().endsWith("/console/api/execute/") &&
      response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Start run", exact: true }).click();
  const response = await queued;
  expect(response.status()).toBe(202);
  const { run_id } = await response.json();
  await expect(page).toHaveURL(new RegExp(`/console/runs/${run_id}$`));
  await page.getByRole("button", { name: "Cancel run", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Request cancellation", exact: true })
    .click();
  await expect
    .poll(async () => {
      const run = await context.request.get(
        url(`/console/api/runs/${run_id}/`),
      );
      return (await run.json()).status;
    })
    .toBe("cancelled");
});

test("human approval is queued without pretending the worker has finished", async ({
  page,
}) => {
  expect(env.CONSOLE_E2E_WAITING_RUN_ID).toBeTruthy();
  await signIn(page);
  await page.goto(url(`/console/runs/${env.CONSOLE_E2E_WAITING_RUN_ID}`));
  await page
    .getByRole("button", { name: "Review & respond", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByLabel(/^Response JSON/)
    .fill('{"approved":true}');
  const resumed = page.waitForResponse(
    (response) =>
      response.url().endsWith("/resume/") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Submit response", exact: true })
    .click();
  expect((await resumed).status()).toBe(202);
  await expect(page.getByText(/Your response was queued/)).toBeVisible();
});

test("mobile navigation fits a narrow viewport and closes after selection", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await page
    .getByRole("button", { name: "Open navigation", exact: true })
    .click();
  await page.getByRole("link", { name: "Agents", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Agents", exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open navigation", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page
    .getByRole("button", { name: "Open navigation", exact: true })
    .click();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Open navigation", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("console-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("desktop dashboard renders the reference structure with real workspace data", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1200 });
  await signIn(page);
  await expect(page.getByText(/run activity/i).first()).toBeVisible();
  await expect(page.getByText(/token usage by agent/i).first()).toBeVisible();
  await expect(page.getByText(/recent runs/i).first()).toBeVisible();
  await expect(page.getByText(/workflow preview/i).first()).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("console-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });
});
