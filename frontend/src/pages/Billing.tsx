import { useEffect, useState, type FormEvent } from "react";
import {
  CalendarClock,
  CheckCircle2,
  CreditCard,
  FileCheck2,
  Info,
  Receipt,
  RefreshCw,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import { api } from "../lib/api";
import { useSession } from "../lib/session";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  Field,
  Modal,
  PageHeader,
  Spinner,
} from "../components/ui";
import "./agents-settings.css";

type Plan = {
  id: string;
  code: string;
  version: string;
  name: string;
  price_minor: number;
  currency: string;
  included_tokens: number;
  included_requests: number;
  overage_per_million_tokens_minor: number;
  overage_per_request_minor: number;
  enabled: boolean;
};
type Subscription = {
  id: string;
  owner: number;
  mode: string;
  plan: string;
  status: string;
  cancel_at_period_end: boolean;
  next_overage_limit_minor: number;
};
type Invoice = {
  id: string;
  period: string;
  kind: string;
  status: string;
  amount_minor: number;
  currency: string;
  raw_overage_minor: string | null;
  settlement_kind: string;
  issued_at: string;
  paid_at: string | null;
};
type Period = {
  id: string;
  subscription: string;
  plan: string;
  starts_at: string;
  ends_at: string;
  paid_at: string | null;
  closed_at: string | null;
  overage_limit_minor: number;
  used_tokens: number;
  reserved_tokens: number;
  used_calls: number;
  reserved_calls: number;
  invoices: Invoice[];
};
type Payment = {
  id: string;
  invoice: string;
  mode: string;
  amount_minor: number;
  currency: string;
  recorded_at: string;
};
type Page<T> = { results: T[]; count: number };
type Action =
  | { kind: "subscribe"; plan: Plan }
  | { kind: "cap"; subscription: Subscription; plan: Plan }
  | { kind: "cancel"; subscription: Subscription }
  | { kind: "renew"; subscription: Subscription }
  | { kind: "close"; period: Period }
  | { kind: "payment"; invoice: Invoice; owner?: number };
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";
const digits = (currency: string) => {
  try {
    return (
      new Intl.NumberFormat("en", {
        style: "currency",
        currency,
      }).resolvedOptions().maximumFractionDigits ?? 2
    );
  } catch {
    return 2;
  }
};
function money(value: number, currency: string) {
  const major = value / 10 ** digits(currency);
  try {
    return new Intl.NumberFormat("en", {
      style: "currency",
      currency,
      maximumFractionDigits: digits(currency),
    }).format(major);
  } catch {
    return `${major.toFixed(digits(currency))} ${currency}`;
  }
}
function minorUnits(value: string, currency: string) {
  const precision = digits(currency);
  if (
    !new RegExp(`^\\d+${precision ? `(?:\\.\\d{0,${precision}})?` : ""}$`).test(
      value,
    )
  )
    throw new Error(
      `Enter a nonnegative ${currency} amount with no more than ${precision} decimal places.`,
    );
  const [whole, fraction = ""] = value.split(".");
  const amount = Number(whole + fraction.padEnd(precision, "0"));
  if (!Number.isSafeInteger(amount) || amount > 10 ** 12)
    throw new Error("The amount is outside the supported billing limit.");
  return amount;
}
const date = (value: string) =>
  new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(value));
const number = (value: number) => new Intl.NumberFormat("en").format(value);

export default function Billing() {
  const { session } = useSession();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [periods, setPeriods] = useState<Period[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [payments, setPayments] = useState<Payment[]>([]);
  const [invoiceCount, setInvoiceCount] = useState(0);
  const [paymentCount, setPaymentCount] = useState(0);
  const [invoiceOffset, setInvoiceOffset] = useState(0);
  const [paymentOffset, setPaymentOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [tab, setTab] = useState("invoices");
  const [action, setAction] = useState<Action | null>(null);
  const [cap, setCap] = useState("0");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [invoiceLookup, setInvoiceLookup] = useState("");
  const [lookupLoading, setLookupLoading] = useState(false);
  const [payment, setPayment] = useState({
    provider: "manual",
    merchant_account: "",
    external_payment_id: "",
    evidence_ref: "",
  });

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.all([
      api<Page<Plan>>("billing-plans/?limit=100", {
        signal: controller.signal,
      }),
      api<Page<Subscription>>("billing-subscriptions/?scope=mine&limit=100", {
        signal: controller.signal,
      }),
      api<Page<Period>>("billing-periods/?scope=mine&limit=100", {
        signal: controller.signal,
      }),
      api<Page<Invoice>>(
        `billing-invoices/?scope=mine&limit=20&offset=${invoiceOffset}`,
        { signal: controller.signal },
      ),
      api<Page<Payment>>(
        `billing-payments/?scope=mine&limit=20&offset=${paymentOffset}`,
        { signal: controller.signal },
      ),
    ])
      .then(
        ([
          planData,
          subscriptionData,
          periodData,
          invoiceData,
          paymentData,
        ]) => {
          setPlans(planData.results);
          setSubscriptions(subscriptionData.results);
          setPeriods(periodData.results);
          setInvoices(invoiceData.results);
          setInvoiceCount(invoiceData.count);
          setPayments(paymentData.results);
          setPaymentCount(paymentData.count);
        },
      )
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision, invoiceOffset, paymentOffset]);

  const subscription = subscriptions.find(
    (item) =>
      item.owner === session?.user?.id && item.mode === session?.billing?.mode,
  );
  const plan = plans.find((item) => item.id === subscription?.plan);
  const ownedPeriods = periods.filter(
    (period) => period.subscription === subscription?.id,
  );
  const now = Date.now();
  const currentPeriod = ownedPeriods.find(
    (period) =>
      Boolean(period.paid_at) &&
      new Date(period.starts_at).getTime() <= now &&
      new Date(period.ends_at).getTime() > now,
  );
  const enabled = Boolean(session?.billing?.enabled);
  const isStaff = Boolean(session?.user?.is_staff);

  function openAction(next: Action) {
    setAction(next);
    setFormError("");
    setConfirmed(false);
    setCap(
      next.kind === "cap"
        ? (
            next.subscription.next_overage_limit_minor /
            10 ** digits(next.plan.currency)
          ).toFixed(digits(next.plan.currency))
        : "0",
    );
    setPayment({
      provider: "manual",
      merchant_account: "",
      external_payment_id: "",
      evidence_ref: "",
    });
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!action) return;
    setSaving(true);
    setFormError("");
    try {
      if (action.kind === "subscribe")
        await api("billing-subscriptions/", {
          method: "POST",
          body: {
            plan_id: action.plan.id,
            overage_limit_minor: minorUnits(cap, action.plan.currency),
          },
        });
      else if (action.kind === "cap")
        await api(
          `billing-subscriptions/${action.subscription.id}/set-overage-limit/`,
          {
            method: "POST",
            body: {
              overage_limit_minor: minorUnits(cap, action.plan.currency),
            },
          },
        );
      else if (action.kind === "cancel")
        await api(`billing-subscriptions/${action.subscription.id}/cancel/`, {
          method: "POST",
          body: {},
        });
      else if (action.kind === "renew")
        await api(`billing-subscriptions/${action.subscription.id}/renew/`, {
          method: "POST",
          body: {},
        });
      else if (action.kind === "close")
        await api(`billing-periods/${action.period.id}/close/`, {
          method: "POST",
          body: {},
        });
      else {
        if (!confirmed)
          throw new Error(
            "Confirm that you independently verified the payment.",
          );
        if (
          !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(payment.provider) ||
          !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(
            payment.merchant_account,
          ) ||
          [payment.external_payment_id, payment.evidence_ref].some(
            (value) => !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(value),
          )
        )
          throw new Error(
            "Payment references must be bounded identifiers without spaces, credentials or raw provider payloads.",
          );
        await api(`billing-invoices/${action.invoice.id}/record-payment/`, {
          method: "POST",
          body: {
            ...payment,
            amount_minor: action.invoice.amount_minor,
            currency: action.invoice.currency,
          },
        });
      }
      const descriptions = {
        subscribe:
          "Subscription requested. Fund its base invoice to activate the period.",
        cap: "The spending limit for newly created periods was updated.",
        cancel:
          "Cancellation scheduled. Funded service remains available until its period ends.",
        renew:
          "The next period’s base invoice is ready. No payment was collected.",
        close: "The period was closed and its overage invoice is ready.",
        payment: "Payment attestation recorded.",
      };
      setNotice(descriptions[action.kind]);
      setAction(null);
      setRevision((value) => value + 1);
    } catch (error) {
      setFormError(message(error));
    } finally {
      setSaving(false);
    }
  }

  async function lookupInvoice(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        invoiceLookup.trim(),
      )
    ) {
      setError("Enter the complete invoice UUID.");
      return;
    }
    setLookupLoading(true);
    try {
      const invoice = await api<Invoice>(
        `billing-invoices/${invoiceLookup.trim()}/`,
      );
      const period = await api<Period>(`billing-periods/${invoice.period}/`);
      const ownerSubscription = await api<Subscription>(
        `billing-subscriptions/${period.subscription}/`,
      );
      if (ownerSubscription.mode !== session.billing.mode)
        throw new Error(
          "This invoice belongs to another billing environment. It cannot be changed from the current environment.",
        );
      if (invoice.status !== "open" || invoice.amount_minor === 0)
        throw new Error(
          "Only an open invoice with a positive balance can receive a payment attestation.",
        );
      openAction({ kind: "payment", invoice, owner: ownerSubscription.owner });
    } catch (error) {
      setError(message(error));
    } finally {
      setLookupLoading(false);
    }
  }

  const actionTitle = !action
    ? ""
    : {
        subscribe: "Start subscription",
        cap: "Future spending limit",
        cancel: "Schedule cancellation?",
        renew: "Request the next period?",
        close: "Close billing period?",
        payment: "Record verified payment",
      }[action.kind];
  return (
    <div className="as-page">
      <PageHeader
        title="Subscription & billing"
        description="Understand your included usage, spending limits and payment records."
        actions={
          <Button
            variant="secondary"
            onClick={() => setRevision((value) => value + 1)}
            disabled={loading}
          >
            <RefreshCw size={16} /> Refresh
          </Button>
        }
      />
      <div className="as-info-banner">
        <ShieldCheck size={20} />
        <p>
          <strong>
            {session?.billing?.mode === "live"
              ? "Live billing records"
              : "Test billing environment"}
          </strong>{" "}
          · Payments are verified externally and recorded by an administrator.
          Checkout and automatic card charges are not connected.
        </p>
      </div>
      {!enabled && (
        <div className="as-warning-banner">
          <Info size={20} />
          <p>
            Subscription enforcement is disabled by the server operator.
            Starting or renewing a subscription is unavailable; existing records
            and financial cleanup remain accessible.
          </p>
        </div>
      )}
      {notice && (
        <div className="as-success" role="status">
          <CheckCircle2 size={18} />
          {notice}
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      {loading && !plans.length ? (
        <div className="as-loading">
          <Spinner />
        </div>
      ) : error && !plans.length ? null : (
        <>
          <div className="as-billing-overview">
            <Card>
              <div className="as-card-heading">
                <div>
                  <span className="as-eyebrow">Your subscription</span>
                  <h2>{plan?.name ?? "No active plan"}</h2>
                </div>
                <span className="as-stat-icon">
                  <CreditCard size={23} />
                </span>
              </div>
              {subscription ? (
                <>
                  <div className="as-plan-current-price">
                    {plan ? money(plan.price_minor, plan.currency) : "—"}
                    <span>/ calendar month</span>
                  </div>
                  <div className="as-inline-status">
                    <Badge status={subscription.status} />
                    {subscription.cancel_at_period_end && (
                      <span className="as-note">Cancellation scheduled</span>
                    )}
                  </div>
                  <p className="as-copy">
                    {currentPeriod
                      ? `Funded through ${date(currentPeriod.ends_at)} (UTC).`
                      : "No funded period is currently available. An active label alone does not confirm paid access."}
                  </p>
                  <div className="as-button-row">
                    <Button
                      variant="secondary"
                      disabled={!enabled || subscription.cancel_at_period_end}
                      onClick={() =>
                        openAction({ kind: "renew", subscription })
                      }
                    >
                      Request renewal
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={subscription.cancel_at_period_end}
                      onClick={() =>
                        openAction({ kind: "cancel", subscription })
                      }
                    >
                      Cancel subscription
                    </Button>
                  </div>
                </>
              ) : (
                <p className="as-copy">
                  Choose a published plan below. Your subscription begins with
                  an internal invoice and requires a confirmed payment.
                </p>
              )}
            </Card>
            <Card>
              <div className="as-card-title">
                <Wallet size={20} />
                <h2>Additional spending limit</h2>
              </div>
              <strong className="as-large-value">
                {currentPeriod && plan
                  ? money(currentPeriod.overage_limit_minor, plan.currency)
                  : "—"}
              </strong>
              <p className="as-copy">
                Maximum extra charge authorized for the current period. Actual
                usage is preserved; the invoice remains within this limit.
              </p>
              {subscription && plan && (
                <>
                  <p className="as-note">
                    New periods:{" "}
                    {money(
                      subscription.next_overage_limit_minor,
                      plan.currency,
                    )}{" "}
                    maximum additional charge.
                  </p>
                  <Button
                    variant="secondary"
                    onClick={() =>
                      openAction({ kind: "cap", subscription, plan })
                    }
                  >
                    Set future limit
                  </Button>
                </>
              )}
            </Card>
          </div>
          {currentPeriod && plan && (
            <Card>
              <div className="as-card-heading">
                <div>
                  <h2>Current period usage</h2>
                  <p>
                    {date(currentPeriod.starts_at)} –{" "}
                    {date(currentPeriod.ends_at)} · UTC
                  </p>
                </div>
                <Badge status="active" />
              </div>
              <div className="as-usage-grid">
                <div>
                  <span>Reported tokens</span>
                  <strong>{number(currentPeriod.used_tokens)}</strong>
                  <small>{number(plan.included_tokens)} included</small>
                  <progress
                    max={Math.max(
                      plan.included_tokens,
                      currentPeriod.used_tokens,
                      1,
                    )}
                    value={currentPeriod.used_tokens}
                    aria-label="Reported token usage"
                  />
                </div>
                <div>
                  <span>Dispatched requests</span>
                  <strong>{number(currentPeriod.used_calls)}</strong>
                  <small>
                    {number(plan.included_requests)} included · retries also
                    count
                  </small>
                  <progress
                    max={Math.max(
                      plan.included_requests,
                      currentPeriod.used_calls,
                      1,
                    )}
                    value={currentPeriod.used_calls}
                    aria-label="Dispatched request usage"
                  />
                </div>
                <div>
                  <span>Unsettled reservations</span>
                  <strong>{number(currentPeriod.reserved_tokens)}</strong>
                  <small>
                    Tokens held · {number(currentPeriod.reserved_calls)}{" "}
                    requests awaiting dispatch
                  </small>
                  <p className="as-note">
                    Unknown provider usage remains held until reconciliation.
                  </p>
                </div>
              </div>
            </Card>
          )}
          {!subscription && (
            <section>
              <div className="as-section-heading">
                <h2>Choose a plan</h2>
                {isStaff && (
                  <a
                    className="as-text-link"
                    href="/admin/billing/planversion/"
                  >
                    Manage plan versions →
                  </a>
                )}
              </div>
              {plans.some((item) => item.enabled) ? (
                <div className="as-plan-grid">
                  {plans
                    .filter((item) => item.enabled)
                    .map((item) => (
                      <Card key={item.id}>
                        <span className="as-eyebrow">
                          {item.code} · {item.version}
                        </span>
                        <h3>{item.name}</h3>
                        <div className="as-plan-current-price">
                          {money(item.price_minor, item.currency)}
                          <span>/ month</span>
                        </div>
                        <ul className="as-plan-features">
                          <li>
                            <CheckCircle2 size={16} />
                            {number(item.included_tokens)} tokens included
                          </li>
                          <li>
                            <CheckCircle2 size={16} />
                            {number(item.included_requests)} dispatched requests
                          </li>
                          <li>
                            <CheckCircle2 size={16} />
                            {money(
                              item.overage_per_million_tokens_minor,
                              item.currency,
                            )}{" "}
                            / extra million tokens
                          </li>
                          <li>
                            <CheckCircle2 size={16} />
                            {money(
                              item.overage_per_request_minor,
                              item.currency,
                            )}{" "}
                            / extra request
                          </li>
                        </ul>
                        <Button
                          variant="primary"
                          disabled={!enabled}
                          onClick={() =>
                            openAction({ kind: "subscribe", plan: item })
                          }
                        >
                          Choose plan
                        </Button>
                      </Card>
                    ))}
                </div>
              ) : (
                <Card>
                  <EmptyState
                    icon={CreditCard}
                    title="No plans published yet"
                    description="Your administrator can publish a versioned subscription plan with included usage and explicit overage rates."
                  />
                </Card>
              )}
            </section>
          )}
          {isStaff && (
            <Card>
              <details className="as-admin-payment">
                <summary>
                  <ShieldCheck size={18} />
                  Record a verified payment for another account
                </summary>
                <form className="as-form" onSubmit={lookupInvoice}>
                  <p className="as-note">
                    Look up the exact invoice from independently verified
                    payment evidence. The account and environment are checked
                    before opening the attestation form.
                  </p>
                  <div className="as-lookup-row">
                    <Field label="Invoice UUID">
                      <input
                        className="as-input as-monospace"
                        required
                        value={invoiceLookup}
                        onChange={(event) =>
                          setInvoiceLookup(event.target.value)
                        }
                        placeholder="Complete invoice identifier"
                      />
                    </Field>
                    <Button type="submit" disabled={lookupLoading}>
                      {lookupLoading ? "Looking up…" : "Find invoice"}
                    </Button>
                  </div>
                </form>
              </details>
            </Card>
          )}
          <Card>
            <div
              className="as-tabs as-card-tabs"
              role="tablist"
              aria-label="Billing history"
            >
              {["invoices", "periods", "payments"].map((value) => (
                <button
                  key={value}
                  role="tab"
                  aria-selected={tab === value}
                  className={tab === value ? "active" : ""}
                  onClick={() => setTab(value)}
                >
                  {value === "invoices" ? (
                    <Receipt size={17} />
                  ) : value === "periods" ? (
                    <CalendarClock size={17} />
                  ) : (
                    <FileCheck2 size={17} />
                  )}
                  {value[0].toUpperCase() + value.slice(1)}
                </button>
              ))}
            </div>
            {tab === "invoices" && (
              <>
                {invoices.length ? (
                  <div className="as-table-wrap">
                    <table className="as-data-table">
                      <thead>
                        <tr>
                          <th>Invoice</th>
                          <th>Issued</th>
                          <th>Type</th>
                          <th>Amount</th>
                          <th>Status</th>
                          <th>
                            <span className="as-sr-only">Actions</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {invoices.map((invoice) => (
                          <tr key={invoice.id}>
                            <td>
                              <span className="as-monospace" title={invoice.id}>
                                {invoice.id.slice(0, 8)}
                              </span>
                            </td>
                            <td>{date(invoice.issued_at)}</td>
                            <td>
                              {invoice.kind === "base"
                                ? "Subscription"
                                : "Metered overage"}
                            </td>
                            <td>
                              <strong>
                                {money(invoice.amount_minor, invoice.currency)}
                              </strong>
                            </td>
                            <td>
                              <Badge status={invoice.status} />
                              {invoice.settlement_kind === "zero_balance" && (
                                <small className="as-table-detail">
                                  Zero balance · no payment needed
                                </small>
                              )}
                            </td>
                            <td>
                              {isStaff &&
                                invoice.status === "open" &&
                                invoice.amount_minor > 0 && (
                                  <Button
                                    variant="secondary"
                                    onClick={() =>
                                      openAction({ kind: "payment", invoice })
                                    }
                                  >
                                    Record payment
                                  </Button>
                                )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyState
                    icon={Receipt}
                    title="No invoices yet"
                    description="Subscription and metered overage invoices will appear here."
                  />
                )}
                <div className="as-pagination">
                  <span>
                    {invoiceCount} invoice{invoiceCount === 1 ? "" : "s"} ·
                    amounts in their stated currency
                  </span>
                  <div>
                    <Button
                      variant="secondary"
                      disabled={invoiceOffset === 0 || loading}
                      onClick={() =>
                        setInvoiceOffset((value) => Math.max(0, value - 20))
                      }
                    >
                      Previous
                    </Button>
                    <Button
                      variant="secondary"
                      disabled={invoiceOffset + 20 >= invoiceCount || loading}
                      onClick={() => setInvoiceOffset((value) => value + 20)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              </>
            )}
            {tab === "periods" &&
              (ownedPeriods.length ? (
                <div className="as-table-wrap">
                  <table className="as-data-table">
                    <thead>
                      <tr>
                        <th>Period · UTC</th>
                        <th>Base payment</th>
                        <th>Reported tokens</th>
                        <th>Requests</th>
                        <th>Overage limit</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ownedPeriods.map((period) => {
                        const periodPlan = plans.find(
                          (item) => item.id === period.plan,
                        );
                        const ended = new Date(period.ends_at).getTime() <= now;
                        return (
                          <tr key={period.id}>
                            <td>
                              {date(period.starts_at)} – {date(period.ends_at)}
                            </td>
                            <td>
                              <Badge
                                status={period.paid_at ? "paid" : "pending"}
                              >
                                {period.paid_at ? "Paid" : "Unfunded"}
                              </Badge>
                            </td>
                            <td>{number(period.used_tokens)}</td>
                            <td>{number(period.used_calls)}</td>
                            <td>
                              {periodPlan
                                ? money(
                                    period.overage_limit_minor,
                                    periodPlan.currency,
                                  )
                                : "—"}
                            </td>
                            <td>
                              {period.closed_at ? (
                                <Badge status="completed">Closed</Badge>
                              ) : ended && period.paid_at ? (
                                <Button
                                  variant="secondary"
                                  onClick={() =>
                                    openAction({ kind: "close", period })
                                  }
                                >
                                  Close period
                                </Button>
                              ) : (
                                <Badge status={ended ? "expired" : "active"}>
                                  {ended ? "Ended" : "Open"}
                                </Badge>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState
                  icon={CalendarClock}
                  title="No billing periods yet"
                  description="A subscription creates a calendar period with frozen prices and a fixed spending limit."
                />
              ))}
            {tab === "payments" && (
              <>
                {payments.length ? (
                  <div className="as-table-wrap">
                    <table className="as-data-table">
                      <thead>
                        <tr>
                          <th>Payment record</th>
                          <th>Recorded · UTC</th>
                          <th>Invoice</th>
                          <th>Environment</th>
                          <th>Amount</th>
                        </tr>
                      </thead>
                      <tbody>
                        {payments.map((item) => (
                          <tr key={item.id}>
                            <td className="as-monospace" title={item.id}>
                              {item.id.slice(0, 8)}
                            </td>
                            <td>{date(item.recorded_at)}</td>
                            <td className="as-monospace" title={item.invoice}>
                              {item.invoice.slice(0, 8)}
                            </td>
                            <td>
                              <Badge status={item.mode}>{item.mode}</Badge>
                            </td>
                            <td>{money(item.amount_minor, item.currency)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyState
                    icon={FileCheck2}
                    title="No confirmed payments"
                    description="Externally verified payments recorded by staff will appear here."
                  />
                )}
                <div className="as-pagination">
                  <span>
                    {paymentCount} payment record{paymentCount === 1 ? "" : "s"}
                  </span>
                  <div>
                    <Button
                      variant="secondary"
                      disabled={paymentOffset === 0 || loading}
                      onClick={() =>
                        setPaymentOffset((value) => Math.max(0, value - 20))
                      }
                    >
                      Previous
                    </Button>
                    <Button
                      variant="secondary"
                      disabled={paymentOffset + 20 >= paymentCount || loading}
                      onClick={() => setPaymentOffset((value) => value + 20)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              </>
            )}
          </Card>
          <p className="as-footnote">
            Internal billing records · Overage is rounded once per period ·
            Unknown usage must be reconciled before closing.
          </p>
        </>
      )}
      {action && (
        <Modal
          title={actionTitle}
          onClose={() => {
            if (!saving) setAction(null);
          }}
        >
          <form className="as-form" onSubmit={submit}>
            {formError && <ErrorNotice message={formError} />}
            {action.kind === "subscribe" && (
              <>
                <p>
                  <strong>{action.plan.name}</strong> costs{" "}
                  {money(action.plan.price_minor, action.plan.currency)} per
                  calendar month. This creates a base invoice; no payment is
                  collected by this console.
                </p>
                <Field
                  label={`Maximum additional charge (${action.plan.currency})`}
                  hint="Zero means no paid overage. This cap is fixed for the period once created."
                >
                  <input
                    className="as-input"
                    type="number"
                    min={0}
                    step={10 ** -digits(action.plan.currency)}
                    required
                    value={cap}
                    onChange={(event) => setCap(event.target.value)}
                  />
                </Field>
                <p className="as-note">
                  Rates:{" "}
                  {money(
                    action.plan.overage_per_million_tokens_minor,
                    action.plan.currency,
                  )}{" "}
                  per extra million tokens and{" "}
                  {money(
                    action.plan.overage_per_request_minor,
                    action.plan.currency,
                  )}{" "}
                  per extra dispatched request.
                </p>
              </>
            )}
            {action.kind === "cap" && (
              <>
                <p>
                  This limit applies only when a later period is created.
                  Existing current or future periods keep their agreed limits.
                </p>
                <Field
                  label={`Maximum additional charge (${action.plan.currency})`}
                >
                  <input
                    className="as-input"
                    type="number"
                    min={0}
                    step={10 ** -digits(action.plan.currency)}
                    required
                    value={cap}
                    onChange={(event) => setCap(event.target.value)}
                  />
                </Field>
              </>
            )}
            {action.kind === "cancel" && (
              <p>
                New renewals will stop and unpaid base invoices will be voided.
                Already funded periods remain usable until they end. Existing
                overage obligations and unresolved usage are retained.
              </p>
            )}
            {action.kind === "renew" && (
              <p>
                Prepare the next calendar period and its base invoice. The
                server checks outstanding overage debt first. No payment is
                collected automatically.
              </p>
            )}
            {action.kind === "close" && (
              <p>
                Close {date(action.period.starts_at)} –{" "}
                {date(action.period.ends_at)} and issue its overage invoice. The
                period must have ended and all invocation reservations must be
                settled. The invoice will stay within its authorized spending
                limit.
              </p>
            )}
            {action.kind === "payment" && (
              <>
                <div className="as-warning-banner">
                  <Info size={19} />
                  <p>
                    This is an operator attestation of an externally verified
                    payment. It does not contact a payment provider or charge a
                    card.
                  </p>
                </div>
                <p className="as-note">
                  Invoice:{" "}
                  <span className="as-monospace">{action.invoice.id}</span>
                  <br />
                  Owner: #{action.owner ?? session.user?.id} ·{" "}
                  {session.billing.mode} environment
                </p>
                <Field label="Exact invoice amount">
                  <input
                    className="as-input"
                    readOnly
                    value={money(
                      action.invoice.amount_minor,
                      action.invoice.currency,
                    )}
                  />
                </Field>
                <Field label="Provider identifier">
                  <input
                    className="as-input"
                    required
                    maxLength={64}
                    value={payment.provider}
                    onChange={(event) =>
                      setPayment({ ...payment, provider: event.target.value })
                    }
                  />
                </Field>
                <Field label="Merchant account identifier">
                  <input
                    className="as-input"
                    required
                    maxLength={128}
                    value={payment.merchant_account}
                    onChange={(event) =>
                      setPayment({
                        ...payment,
                        merchant_account: event.target.value,
                      })
                    }
                  />
                </Field>
                <Field label="External payment ID">
                  <input
                    className="as-input"
                    required
                    maxLength={128}
                    value={payment.external_payment_id}
                    onChange={(event) =>
                      setPayment({
                        ...payment,
                        external_payment_id: event.target.value,
                      })
                    }
                  />
                </Field>
                <Field
                  label="Verification evidence reference"
                  hint="Use a non-secret reference. Never include card data or credentials."
                >
                  <input
                    className="as-input"
                    required
                    maxLength={128}
                    value={payment.evidence_ref}
                    onChange={(event) =>
                      setPayment({
                        ...payment,
                        evidence_ref: event.target.value,
                      })
                    }
                  />
                </Field>
                <label className="as-checkbox-label">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) => setConfirmed(event.target.checked)}
                  />
                  I independently verified this exact payment, amount, currency
                  and invoice.
                </label>
              </>
            )}
            <div className="as-modal-actions">
              <Button
                type="button"
                variant="secondary"
                disabled={saving}
                onClick={() => setAction(null)}
              >
                Back
              </Button>
              <Button
                type="submit"
                variant={action.kind === "cancel" ? "danger" : "primary"}
                disabled={saving || (action.kind === "payment" && !confirmed)}
              >
                {saving
                  ? "Saving…"
                  : action.kind === "payment"
                    ? "Record verified payment"
                    : action.kind === "subscribe"
                      ? "Create base invoice"
                      : action.kind === "close"
                        ? "Close period"
                        : "Confirm"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
