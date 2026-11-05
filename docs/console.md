# AgentOps browser console

The console at `/console/` provides a browser interface for the existing workflow platform. Its light dashboard, purple navigation and card layout follow the supplied AgentOps reference. The cards use workspace data: the application does not populate production counters with the screenshot's example values.

## Build and start

Use the normal Python/PostgreSQL setup from the [README](../README.md). Install Node.js 24 and the locked frontend dependencies, then build the browser assets from the repository root:

```sh
npm --prefix frontend ci
npm --prefix frontend run build
uv run --frozen python manage.py migrate --noinput
uv run --frozen python manage.py collectstatic --noinput
```

The build writes generated assets under `src/console/static/console/`. Keep the source and `frontend/package-lock.json` in Git; generated assets and browser-test output are ignored. The application Docker build performs the frontend build before packaging the web release. A wheel built outside Docker must also be preceded by the frontend build if that wheel is intended to serve the console.

Inter is self-hosted with the console; browsers do not need a third-party font CDN. Its SIL Open Font License is distributed at `/static/console/licenses/Inter-OFL.txt` and retained in `frontend/public/licenses/Inter-OFL.txt`.

Start the web process and worker as usual, then open `http://127.0.0.1:8000/console/`. The worker remains a separate process: a successfully queued run is not evidence that a worker is consuming commands.

For frontend development, run the Django/ASGI web process on `127.0.0.1:8000`, then:

```sh
npm --prefix frontend run dev
```

Open the development server's `/console/` URL. Vite forwards `/console/api/` to the local backend; browser requests remain on the development server's origin. Keep the proxy local and use a trusted HTTPS reverse proxy for a deployed environment.

## Sign-in and permissions

Sign in with an existing active Django username and password. Users are provisioned through the existing administrator controls; this release does not add public registration or password-reset email delivery. A user can change their password in Settings. Creating a local administrator continues to use `python manage.py createsuperuser`.

The console uses a server-side Django session with an HttpOnly cookie. Its dedicated `/console/api/` routes enforce CSRF on writes. The bootstrap session response provides a masked CSRF value held in browser memory; API tokens, provider credentials and passwords are not stored in localStorage or sessionStorage. Signing out invalidates the browser session.

The existing `/api/v1/` and `/api/v2/` interfaces continue to require their Token authentication. Signing into the browser console does not grant session access to those token endpoints.

Ordinary users see their own resources. Existing staff privileges apply to administrative resource views and operational statistics. Dashboard usage and run summaries remain personal to the authenticated owner, including for staff. Historical invocation accounting follows its frozen billing owner rather than the current owner of an edited workflow.

Configure a shared cache such as Redis for login rate limiting across multiple web processes. The local-memory development cache only limits attempts within each process. Cookie security and trusted reverse-proxy settings still follow the [production deployment guide](../README.md#production).

## Screens and data boundaries

- **Dashboard:** active runs, queued work, reported usage, run activity, token attribution, recent runs, an actual workflow preview and derived alerts. API/database readiness is labeled as readiness, not an invented uptime or availability percentage.
- **Agents:** create, edit, filter and delete owned agents; select an allowed runtime, exact model and explicit tool allowlist.
- **Workflow Builder:** inspect and edit actual definitions, nodes and edges, save validated graphs and queue a run. Unsaved drafts require confirmation before navigating away. Graph positions are a browser layout; they do not add unsupported fields to the persisted workflow contract.
- **Runs / Traces:** inspect persisted status, inputs, outputs, events and invocation accounting. Cancellation and human-input resume use the existing queued-command rules. Displaying a timeline does not provide arbitrary event replay or rollback of external effects.
- **Knowledge & Memory:** show the current knowledge-search integration boundary. An unconfigured search adapter is shown as unconnected. The console does not invent an indexed-document count, memory service or MCP connection.
- **Observability:** personal operational signals and, for staff, the existing protected aggregate operation data. Worker monitoring, alert delivery and infrastructure metrics still require deployment configuration.
- **Billing:** inspect plans, subscription periods, capped overage invoices and payment records; use supported subscription and staff attestation actions. Billing remains disabled by default. There is no payment-provider checkout or automatic charge until a provider is selected and integrated.
- **Settings:** profile and password controls, with runtime administration available under the existing staff permissions. Runtime secrets remain server-side; a credential alias is not a request to paste an API key into the browser.

Unknown token usage and costs remain unknown. Mixed currencies are not added into one invented amount. Request counts refer to dispatched transport attempts, including retries and repairs; they are not a count of successful workflows. Chart timestamps and period boundaries follow the labels shown in the interface.

## Checks

Run the frontend type check, unit tests and production build:

```sh
npm --prefix frontend run typecheck
npm --prefix frontend run format:check
npm --prefix frontend test
npm --prefix frontend run build
```

The optional browser integration test uses an isolated Django test database, real session authentication and real CRUD/queue endpoints. It creates synthetic local users and workflow records; it does not call a real LLM or payment processor. Build the assets and install the Playwright Chromium browser before enabling it:

```sh
npm --prefix frontend exec -- playwright install chromium
CONSOLE_BROWSER_TESTS=1 DJANGO_SETTINGS_MODULE=config.settings.test \
  uv run --frozen --all-extras pytest tests/integration/test_console_browser.py
```

The database role must be allowed to create the test database. A local system Chromium can be selected with `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium`. Browser screenshots and failure details are written to the ignored `frontend/test-results/` directory. Test credentials are generated for the isolated fixture and supplied only to the browser-test subprocess.

The suite checks sign-in, cookie/CSRF boundaries, durable agent and workflow changes, cycle validation, unsaved-draft navigation, harmless run queue/cancellation, human-input queueing, empty/error recovery and mobile layout. The outage case deliberately injects an HTTP failure and then retries against the real backend; positive authentication and CRUD cases use the real endpoints. Automated tests and screenshots do not establish real-provider model quality, production infrastructure health or payment collection.
