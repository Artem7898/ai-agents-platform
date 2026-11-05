# Subscriptions and metered overage

The billing core combines a monthly subscription with an explicit, bounded allowance for paid overage. It provides immutable plan versions, calendar periods, internal invoices and recorded payment evidence. It uses the existing [invocation ledger](llm-usage.md) to meter every dispatched LLM attempt, including retries and repairs.

The payment provider is deliberately left unselected. This release does not provide a hosted checkout, provider webhook, recurring charge scheduler or automatic collection. Staff can record a payment only after verifying it outside the platform. That action is an operator attestation; it does not claim that the application checked a provider signature or bank settlement.

## Configuration and rollout

Billing is disabled by default:

```dotenv
BILLING_ENABLED=false
BILLING_MODE=test
```

Apply Django migrations before starting web and worker processes. Configure plans and exercise the complete payment, usage, closing and reconciliation flow in test mode before enabling admission enforcement. All web and worker processes must use the same billing mode and database release.

When `BILLING_ENABLED=true`, each new LLM attempt requires a paid period for the run's frozen billing owner. A missing owner, missing paid period, blocked account, exhausted allowance or outstanding prior-period debt prevents a new admission. Existing workflow execution limits and technical owner quotas continue to apply. Staff status does not bypass the workflow owner's subscription.

With billing disabled, existing usage accounting and quotas continue to work. Starting or renewing a subscription returns `503` with code `billing_disabled`; reading records, configuring plans, scheduling cancellation, setting a future overage limit, closing an existing period and recording verified payment evidence remain available. Turning the feature off does not erase reservations or prevent financial cleanup.

`BILLING_MODE` is either `test` or `live`. Accounts, periods and payment evidence retain their environment. Ordinary users see their own records in the selected mode; staff can inspect both modes. A test payment cannot fund live-mode admission. Financial mutations reject records from a different current mode. Switching the setting is not a migration of balances or subscriptions.

## Plans and money

All billing APIs require `Authorization: Token <key>`. Staff creates plans at `POST /api/v1/billing-plans/`. The values below illustrate the format, not a recommended product price:

```json
{
  "code": "example-monthly",
  "version": "2026-11",
  "name": "Example monthly plan",
  "price_minor": 1000,
  "currency": "USD",
  "included_tokens": 1000000,
  "included_requests": 1000,
  "overage_per_million_tokens_minor": 50,
  "overage_per_request_minor": 1,
  "enabled": true
}
```

Money uses integer minor units: for USD, `1000` means USD 10.00. Configure the correct minor-unit convention for the chosen currency. Floating-point values, numeric strings and booleans are rejected for monetary and quota inputs. Currency is an uppercase three-letter code; storing a code does not establish that a future payment provider supports it.

A plan's price, currency, included quantities and overage rates are immutable. Publish a new version to change them. Only `enabled` can be patched on an existing plan; disabling a plan prevents new subscriptions and renewals without changing already purchased periods. Deleting billing records is not supported.

Token and request overage rates apply independently. A zero rate permits free excess for that metric; use the separate technical owner quotas when a hard usage limit is required. Configure the rates to match the product's intended charging policy.

Subscription prices are independent of the optional provider-cost estimates in `LLMRate` and `LLMInvocation.cost`. A vendor tariff estimate does not establish the retail amount owed by the customer. This release does not reconcile actual vendor invoices or implement a wallet.

## Start and fund a subscription

An owner starts a subscription with a server-defined plan and a maximum additional charge for its first period:

```sh
curl --request POST "$PLATFORM_URL/api/v1/billing-subscriptions/" \
  --header "Authorization: Token $OWNER_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"plan_id":"11111111-1111-4111-8111-111111111111","overage_limit_minor":500}'
```

Use an actual plan UUID from `GET /api/v1/billing-plans/`. The response contains a period and its nested base invoice. The owner cannot supply another owner, price, currency, usage counters or paid status. Unknown input fields are rejected. An omitted `overage_limit_minor` defaults to zero, permitting no additional charge.

The base invoice must be paid before that period can admit LLM calls. A customer redirect, posted payment status or an `active` subscription label alone cannot establish this entitlement. Admission checks the paid period's actual start and end timestamps on every attempt, including queued and resumed workflows.

In the provider-neutral workflow, staff verifies the full payment externally and records an attestation against that exact invoice:

```sh
curl --request POST "$PLATFORM_URL/api/v1/billing-invoices/22222222-2222-4222-8222-222222222222/record-payment/" \
  --header "Authorization: Token $STAFF_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"provider":"manual","merchant_account":"example-merchant","external_payment_id":"example-payment-001","amount_minor":1000,"currency":"USD","evidence_ref":"example-verification-001"}'
```

The UUID and evidence identifiers above are synthetic. Use the actual invoice and independently checked evidence. `amount_minor` and `currency` must match the invoice exactly. Partial payments, discounts, credits and refunds are outside this flow. Do not include card data, credentials or raw provider payloads in evidence identifiers.

Payment uniqueness covers provider, merchant account, mode and external payment ID. Repeating the same matching attestation is idempotent; reusing evidence for another invoice or changing its financial facts produces a conflict. An invoice can have at most one confirmed payment. The receipt records the staff actor and timestamp. Owners can inspect their payment amount, currency, mode and time; staff-only external evidence identifiers are omitted from owner responses.

## Calendar periods, renewals and cancellation

Periods are monthly UTC intervals with a preserved day-of-month anchor. A subscription beginning January 31 can end February 28 and then March 31; it is not modeled as repeated 30-day windows. A restart after a gap begins at the new current time and establishes a new anchor rather than billing an invented past interval. Each created period retains its plan, timestamps and consent cap.

`POST /api/v1/billing-subscriptions/SUBSCRIPTION_UUID/renew/` with `{}` requests the next base invoice. Renewal does not collect money automatically. Pay that invoice through the same verified evidence procedure. A period paid in advance admits calls only when its own start time is reached.

`POST /api/v1/billing-subscriptions/SUBSCRIPTION_UUID/set-overage-limit/` accepts:

```json
{"overage_limit_minor": 750}
```

This changes the limit used when a later period is created. It does not alter an existing current or already-created future period, reset usage or retroactively authorize a larger bill. Read the period's own `overage_limit_minor` to see its fixed maximum additional charge.

`POST /api/v1/billing-subscriptions/SUBSCRIPTION_UUID/cancel/` with `{}` schedules cancellation at the end of funded service and voids unpaid base invoices. Already funded periods remain usable within their dates. New renewal requests stop. Cancellation does not erase existing usage, unknown reservations or overage debt, and it does not perform an external refund.

## Metering and closing a period

The included request allowance counts dispatched transport attempts, including failed or unknown attempts, retries and repairs. A staff reconciliation confirming that a request was never sent can reverse its dispatch count. Token usage follows reported totals; missing usage remains unknown. Admissions reserve an estimated token amount and one attempt against both the technical quotas and the paid period. Dispatch, settlement and reconciliation update the original period even after a renewal or configuration change.

Overage is calculated cumulatively for the whole period. Let `T` be measured tokens and `R` dispatched requests. The raw overage amount in minor units is:

```text
ceil((max(0, T - included_tokens) * overage_per_million_tokens_minor
      + max(0, R - included_requests) * overage_per_request_minor * 1000000)
     / 1000000)
```

The combined amount is rounded up once per period, rather than once per invocation. A new reservation must fit within the period's consent cap when projected usage includes existing holds. If actual reported usage exceeds an estimate, the full actual usage is preserved and can block further admission. The overage invoice still charges no more than the fixed consent cap: `amount_minor = min(raw_overage_minor, overage_limit_minor)`. The uncapped computed retail amount remains in `raw_overage_minor`; an excess above the authorized cap is not charged to the customer.

For example, the sample plan with 1,500,000 measured tokens and 1,700 dispatched requests has raw overage of `725` minor units. With a consent cap of `500`, its overage invoice is `500`. This retail calculation does not establish the platform's actual provider expense.

After the period ends, its owner can call `POST /api/v1/billing-periods/PERIOD_UUID/close/` with `{}`. Closing requires a paid period and no unresolved invocation reservation. A zero-token estimate can still leave an active unknown hold, so zero counters alone are not sufficient. Reconcile unknown calls through the existing [staff usage reconciliation](llm-usage.md) procedure first; do not manufacture usage to unblock billing.

Closing creates one immutable overage invoice and is idempotent. A zero amount is marked paid with `settlement_kind: "zero_balance"` and creates no fictional payment receipt. A positive invoice remains open until staff records its externally verified payment; its settlement kind then becomes `payment`.

An ended, funded period with a positive consent cap blocks new billable admissions and renewals while it has an unresolved hold or a positive capped overage amount without a paid overage invoice. This also applies when the invoice has not yet been issued. There is no grace interval in this policy. Close eligible periods and settle their overage before allowing the account to continue; paying a new base invoice does not clear old debt. Periods with a zero consent cap cannot create collectible overage debt.

Operators can use the `billing_close_periods` management command to issue eligible due invoices in the current `BILLING_MODE`, including while admission enforcement is disabled:

```sh
uv run --frozen python manage.py billing_close_periods --dry-run --limit 100
uv run --frozen python manage.py billing_close_periods --limit 100
```

`--limit` accepts 1 to 1000 periods, with a default of 100. A dry run reports candidates without closing them. Periods with counter reservations or active invocation holds are excluded before applying the limit, so an old unknown call cannot prevent later settled periods from closing. If a selected period changes before closure, the command reports a safe deferral code. It does not invent usage, collect payment or remove unresolved holds. Schedule it through the deployment's normal job system if automatic closing is required.

## API access and operations

| Endpoint | Access and behavior |
| --- | --- |
| `/api/v1/billing-plans/` | Authenticated read; staff creates versions and patches `enabled`. |
| `/api/v1/billing-subscriptions/` | Owner starts and reads subscriptions in the current mode; detail actions renew, cancel and set a future limit. |
| `/api/v1/billing-periods/` | Owner reads period counters and nested invoices; `close/` issues a due overage invoice. |
| `/api/v1/billing-invoices/` | Owner reads invoices; staff records confirmed full payment with `record-payment/`. |
| `/api/v1/billing-payments/` | Read-only payment receipts; external attestation metadata is staff-only. |

Lists are paginated, with a maximum page size of 200. Cross-owner records are not exposed to ordinary users. Financial ledgers are read-only in Django admin; staff can disable a plan or block a billing account through the supported controls. The invocation API exposes the original `billing_period` reference alongside its existing quota window. Use `GET /api/v1/llm-calls/?billing_period_id=PERIOD_UUID` to inspect a period's calls before reconciliation; the existing ownership filters still apply.

Keep protected exports and an operator audit procedure for invoice and payment evidence. These internal invoice records are not a fiscal receipt, tax invoice, payment-provider statement or refund record. Selecting a provider still requires its customer and merchant binding, hosted payment flow, verified callbacks, stable outbound idempotency, reconciliation of uncertain results, refunds, tax documents and collection policy. No real payment credentials or cloud billing integrations are configured by enabling this core.
