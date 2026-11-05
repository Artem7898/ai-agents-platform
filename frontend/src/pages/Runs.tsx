import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowLeft,
  ArrowUpRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  FileJson,
  List,
  PauseCircle,
  Play,
  RefreshCw,
  Search,
  Square,
  Terminal,
  TriangleAlert,
} from "lucide-react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { useNewRun } from "../lib/run-dialog";
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
import "./runs-observability.css";

interface Page<T> {
  count: number;
  results: T[];
  next: string | null;
  previous: string | null;
}
interface Run {
  id: string;
  workflow: string;
  workflow_name?: string;
  status: string;
  input_data: Record<string, unknown>;
  output_data: unknown;
  created_at: string;
  updated_at: string;
  waiting_nodes?: string[];
  cancel_requested?: boolean;
  usage?: {
    total_tokens?: number;
    prompt_tokens?: number;
    completion_tokens?: number;
    unknown_calls?: number;
  };
}
interface RunEvent {
  id: string;
  sequence: number;
  type: string;
  payload: unknown;
  timestamp: string;
}
interface EventPage {
  events: RunEvent[];
  status: string;
  next_after: number;
  has_more: boolean;
}
interface Invocation {
  id: string;
  node_id: string;
  model_name: string;
  status: string;
  operation: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  reservation_active: boolean;
  usage_status: string;
  reserved_tokens: number;
  cost: string | null;
  currency: string | null;
  billing_period: string | null;
  started_at: string;
}
const PAGE_SIZE = 20;
const CALL_PAGE_SIZE = 20;
const liveStatuses = new Set(["pending", "running", "waiting_human"]);
const statuses = [
  "pending",
  "running",
  "waiting_human",
  "completed",
  "failed",
  "cancelled",
];
const number = new Intl.NumberFormat();

function errorText(error: unknown) {
  return error instanceof Error
    ? error.message
    : "The request could not be completed. Please try again.";
}
function abortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}
function dateText(value?: string) {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString() : "—";
}
function elapsed(run: Run) {
  const start = new Date(run.created_at).getTime();
  const end = liveStatuses.has(run.status)
    ? Date.now()
    : new Date(run.updated_at).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}
function estimatedCost(cost: string | null, currency: string | null) {
  if (cost === null || !currency || !Number.isFinite(Number(cost)))
    return "Not reported";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 6,
    }).format(Number(cost));
  } catch {
    return `${cost} ${currency}`;
  }
}
function safeValue(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[Nested content truncated]";
  if (typeof value === "string")
    return value.length > 12000 ? `${value.slice(0, 12000)}…` : value;
  if (Array.isArray(value))
    return value.slice(0, 100).map((item) => safeValue(item, depth + 1));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .slice(0, 100)
        .map(([key, item]) => [
          key,
          /(?:password|secret|api[_-]?key|authorization|execution[_-]?token|lease[_-]?token|access[_-]?token|refresh[_-]?token|checkpoint|context_snapshot)/i.test(
            key,
          )
            ? "[Redacted]"
            : safeValue(item, depth + 1),
        ]),
    );
  }
  return value;
}
function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="runs-json">
      {JSON.stringify(safeValue(value), null, 2) ?? "null"}
    </pre>
  );
}

export default function Runs() {
  const params = useParams();
  const id = params.id ?? params.runId;
  return id ? <RunDetail id={id} /> : <RunList />;
}

function RunList() {
  const [searchParams, setSearchParams] = useSearchParams();
  const query = (searchParams.get("search") ?? "").slice(0, 128);
  const requestedStatus = searchParams.get("status") ?? "";
  const status = statuses.includes(requestedStatus) ? requestedStatus : "";
  const [page, setPage] = useState<Page<Run> | null>(null);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState(query);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const { open } = useNewRun();
  const navigate = useNavigate();

  useEffect(() => {
    setSearch(query);
  }, [query]);
  useEffect(() => {
    setOffset(0);
  }, [query, status]);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (search.trim() === query) return;
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          if (search.trim()) next.set("search", search.trim());
          else next.delete("search");
          return next;
        },
        { replace: true },
      );
    }, 300);
    return () => clearTimeout(timer);
  }, [search, query, setSearchParams]);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async (initial: boolean) => {
      if (initial) setLoading(true);
      try {
        const values = new URLSearchParams({
          limit: String(PAGE_SIZE),
          offset: String(offset),
        });
        if (query) values.set("search", query);
        if (status) values.set("status", status);
        const result = await api<Page<Run>>(`runs/?${values}`, {
          signal: controller.signal,
        });
        if (!active) return;
        setPage(result);
        setError("");
        if (
          result.results.some((run) => liveStatuses.has(run.status)) ||
          offset === 0
        )
          timer = setTimeout(() => void load(false), 10000);
      } catch (cause) {
        if (active && !abortError(cause)) setError(errorText(cause));
      } finally {
        if (active) setLoading(false);
      }
    };
    void load(true);
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [offset, query, status, revision]);

  return (
    <div className="runs-page">
      <PageHeader
        title="Runs / Traces"
        description="Follow every workflow from queue to result."
        actions={
          <Button variant="primary" onClick={() => open()}>
            <Play size={16} /> New run
          </Button>
        }
      />
      <Card className="runs-list-card">
        <div className="runs-toolbar">
          <label className="runs-search">
            <Search size={17} />
            <input
              aria-label="Search workflows or run IDs"
              placeholder="Search workflow or run ID…"
              maxLength={128}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            aria-label="Filter run status"
            value={status}
            onChange={(event) => {
              const value = event.target.value;
              setSearchParams(
                (previous) => {
                  const next = new URLSearchParams(previous);
                  if (value) next.set("status", value);
                  else next.delete("status");
                  return next;
                },
                { replace: true },
              );
            }}
          >
            <option value="">All statuses</option>
            {statuses.map((value) => (
              <option key={value} value={value}>
                {value.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <Button
            variant="secondary"
            onClick={() => setRevision((value) => value + 1)}
            disabled={loading}
            aria-label="Refresh runs"
          >
            <RefreshCw size={16} /> Refresh
          </Button>
        </div>
        {error && <ErrorNotice message={error} />}
        {loading && !page ? (
          <div className="runs-loading">
            <Spinner />
            <span>Loading runs…</span>
          </div>
        ) : page?.results.length ? (
          <>
            <div className="runs-table-scroll">
              <table className="runs-table">
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Workflow</th>
                    <th scope="col">Status</th>
                    <th scope="col">Started</th>
                    <th scope="col">Elapsed</th>
                    <th scope="col">
                      <span className="sr-only">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {page.results.map((run) => (
                    <tr key={run.id}>
                      <td>
                        <Link className="runs-id-link" to={`/runs/${run.id}`}>
                          <Activity size={15} /> {run.id.slice(0, 8)}
                        </Link>
                      </td>
                      <td>
                        <strong>{run.workflow_name ?? "Workflow"}</strong>
                        <span className="runs-secondary">
                          {run.workflow.slice(0, 8)}
                        </span>
                      </td>
                      <td>
                        <Badge status={run.status} />
                        {run.cancel_requested &&
                          liveStatuses.has(run.status) && (
                            <span className="runs-secondary">
                              Cancellation requested
                            </span>
                          )}
                      </td>
                      <td className="runs-nowrap">
                        {dateText(run.created_at)}
                      </td>
                      <td className="runs-nowrap">{elapsed(run)}</td>
                      <td>
                        <button
                          className="runs-icon-button"
                          aria-label={`Open run ${run.id}`}
                          onClick={() => navigate(`/runs/${run.id}`)}
                        >
                          <ArrowUpRight size={17} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="runs-pagination">
              <span>
                {number.format(offset + 1)}–
                {number.format(
                  Math.min(offset + page.results.length, page.count),
                )}{" "}
                of {number.format(page.count)} runs
              </span>
              <div>
                <Button
                  variant="secondary"
                  disabled={offset === 0 || loading}
                  onClick={() =>
                    setOffset((value) => Math.max(0, value - PAGE_SIZE))
                  }
                >
                  <ChevronLeft size={15} /> Previous
                </Button>
                <Button
                  variant="secondary"
                  disabled={!page.next || loading}
                  onClick={() => setOffset((value) => value + PAGE_SIZE)}
                >
                  Next <ChevronRight size={15} />
                </Button>
              </div>
            </div>
          </>
        ) : (
          !loading && (
            <EmptyState
              icon={Activity}
              title={
                query || status
                  ? "No matching runs"
                  : "Your first run starts here"
              }
              description={
                query || status
                  ? "Try another workflow name, run ID or status."
                  : "Launch a saved workflow to see its progress, events and token usage."
              }
              action={
                <Button variant="primary" onClick={() => open()}>
                  Start a run
                </Button>
              }
            />
          )
        )}
      </Card>
      <p className="runs-footnote">
        <Clock3 size={13} /> Elapsed time includes queue time. Dates use your
        browser’s time zone.
      </p>
    </div>
  );
}

function RunDetail({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [eventHasMore, setEventHasMore] = useState(false);
  const [eventBusy, setEventBusy] = useState(false);
  const [calls, setCalls] = useState<Page<Invocation> | null>(null);
  const [callOffset, setCallOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [resumeOpen, setResumeOpen] = useState(false);
  const [commandBusy, setCommandBusy] = useState(false);
  const [commandError, setCommandError] = useState("");
  const [resumeQueued, setResumeQueued] = useState(false);
  const [resumeUnknown, setResumeUnknown] = useState(false);
  const [resumeInput, setResumeInput] = useState('{\n  "approved": true\n}');
  const [resumeNode, setResumeNode] = useState("");
  const cursor = useRef(0);
  const eventInFlight = useRef(false);
  const resumeKey = useRef<string | null>(null);
  const resumePayload = useRef<Record<string, unknown> | null>(null);
  const controllers = useRef(new Set<AbortController>());
  const generation = useRef(0);
  const { open } = useNewRun();

  useEffect(() => {
    generation.current += 1;
    cursor.current = 0;
    eventInFlight.current = false;
    setRun(null);
    setEvents([]);
    setCalls(null);
    setCallOffset(0);
    setEventHasMore(false);
    setResumeQueued(false);
    setNotice("");
    setError("");
    setCommandError("");
    setCommandBusy(false);
    setResumeOpen(false);
    setCancelOpen(false);
    setResumeUnknown(false);
    resumeKey.current = null;
    resumePayload.current = null;
    return () => {
      generation.current += 1;
      for (const controller of controllers.current) controller.abort();
      controllers.current.clear();
    };
  }, [id]);

  const loadEvents = useCallback(
    async (signal?: AbortSignal) => {
      if (eventInFlight.current) return;
      eventInFlight.current = true;
      setEventBusy(true);
      const current = generation.current;
      try {
        const result = await api<EventPage>(
          `runs/${id}/events/?after=${cursor.current}&limit=100`,
          { signal },
        );
        if (current !== generation.current || signal?.aborted) return;
        setEvents((previous) => {
          const ids = new Set(previous.map((event) => event.id));
          return [
            ...previous,
            ...result.events.filter((event) => !ids.has(event.id)),
          ].slice(-500);
        });
        cursor.current = result.next_after;
        setEventHasMore(result.has_more);
      } finally {
        if (current === generation.current) {
          eventInFlight.current = false;
          setEventBusy(false);
        }
      }
    },
    [id],
  );

  useEffect(() => {
    const controller = new AbortController();
    controllers.current.add(controller);
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async (initial: boolean) => {
      if (initial) setLoading(true);
      const result = await Promise.allSettled([
        api<Run>(`runs/${id}/`, { signal: controller.signal }),
        api<Page<Invocation>>(
          `llm-calls/?run_id=${id}&limit=${CALL_PAGE_SIZE}&offset=${callOffset}`,
          { signal: controller.signal },
        ),
        loadEvents(controller.signal),
      ]);
      if (!active) return;
      const runResult = result[0];
      const callResult = result[1];
      if (runResult.status === "fulfilled") {
        setRun(runResult.value);
        if (runResult.value.status !== "waiting_human") {
          setResumeQueued(false);
          setResumeOpen(false);
          setResumeUnknown(false);
          resumeKey.current = null;
          resumePayload.current = null;
        }
        if (liveStatuses.has(runResult.value.status))
          timer = setTimeout(() => void load(false), 5000);
      }
      if (callResult.status === "fulfilled") setCalls(callResult.value);
      const failed = result.find(
        (item) => item.status === "rejected" && !abortError(item.reason),
      );
      setError(failed?.status === "rejected" ? errorText(failed.reason) : "");
      setLoading(false);
    };
    void load(true);
    return () => {
      active = false;
      controller.abort();
      controllers.current.delete(controller);
      clearTimeout(timer);
    };
  }, [id, callOffset, loadEvents, revision]);

  async function command(kind: "cancel" | "resume") {
    const current = generation.current;
    setCommandError("");
    setCommandBusy(true);
    try {
      if (kind === "resume") {
        if (!resumeUnknown) {
          let input: unknown;
          try {
            input = JSON.parse(resumeInput);
          } catch {
            throw new Error("Enter valid JSON for the approval response.");
          }
          if (!input || typeof input !== "object" || Array.isArray(input))
            throw new Error("The approval response must be a JSON object.");
          if ((run?.waiting_nodes?.length ?? 0) > 1 && !resumeNode)
            throw new Error("Choose the approval node to continue.");
          resumePayload.current = {
            input,
            ...(resumeNode ? { node_id: resumeNode } : {}),
          };
        }
        if (!resumePayload.current)
          throw new Error("Review the approval response before submitting.");
        resumeKey.current ??= crypto.randomUUID();
        await api(`runs/${id}/resume/`, {
          method: "POST",
          body: resumePayload.current,
          headers: { "Idempotency-Key": resumeKey.current },
        });
        if (current !== generation.current) return;
        setResumeQueued(true);
        setResumeOpen(false);
        setResumeUnknown(false);
        resumeKey.current = null;
        resumePayload.current = null;
        setNotice(
          "Your response was queued. The worker will continue the workflow.",
        );
      } else {
        await api(`runs/${id}/cancel/`, { method: "POST", body: {} });
        if (current !== generation.current) return;
        setCancelOpen(false);
        setNotice(
          "Cancellation requested. Active work stops at its next cancellation checkpoint or timeout.",
        );
      }
      setRevision((value) => value + 1);
    } catch (cause) {
      if (current === generation.current) {
        setCommandError(errorText(cause));
        if (kind === "resume") {
          const uncertain =
            cause instanceof ApiError &&
            (cause.status === 0 || cause.status >= 500);
          setResumeUnknown(uncertain);
          if (!uncertain) {
            resumeKey.current = null;
            resumePayload.current = null;
          }
        }
      }
    } finally {
      if (current === generation.current) setCommandBusy(false);
    }
  }
  async function moreEvents() {
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      await loadEvents(controller.signal);
    } catch (cause) {
      if (!abortError(cause)) setError(errorText(cause));
    } finally {
      controllers.current.delete(controller);
    }
  }

  return (
    <div className="runs-page">
      <Link className="runs-back" to="/runs">
        <ArrowLeft size={15} /> All runs
      </Link>
      <PageHeader
        title={run?.workflow_name ?? "Run details"}
        description={`Run ${id}`}
        actions={
          <>
            <Button
              variant="secondary"
              onClick={() => setRevision((value) => value + 1)}
              disabled={loading}
            >
              <RefreshCw size={15} /> Refresh
            </Button>
            {run && (
              <Button variant="primary" onClick={() => open(run.workflow)}>
                <Play size={15} /> Run again
              </Button>
            )}
          </>
        }
      />
      {error && <ErrorNotice message={error} />}
      {notice && (
        <div className="runs-notice" role="status">
          <CheckCircle2 size={17} /> {notice}
        </div>
      )}
      {loading && !run ? (
        <div className="runs-loading">
          <Spinner />
          <span>Loading run details…</span>
        </div>
      ) : run ? (
        <>
          <div className="runs-detail-summary">
            <Card>
              <span className="runs-stat-label">Status</span>
              <Badge status={run.status} />
              <span className="runs-secondary">
                {run.cancel_requested
                  ? "Cancellation requested"
                  : "Persisted workflow state"}
              </span>
            </Card>
            <Card>
              <span className="runs-stat-label">Elapsed</span>
              <strong>{elapsed(run)}</strong>
              <span className="runs-secondary">Includes time in queue</span>
            </Card>
            <Card>
              <span className="runs-stat-label">Known tokens</span>
              <strong>
                {typeof run.usage?.total_tokens === "number"
                  ? number.format(run.usage.total_tokens)
                  : "—"}
              </strong>
              <span className="runs-secondary">
                {run.usage?.unknown_calls
                  ? `${number.format(run.usage.unknown_calls)} call(s) awaiting usage`
                  : "Reported usage only"}
              </span>
            </Card>
            <Card>
              <span className="runs-stat-label">LLM ledger records</span>
              <strong>{calls ? number.format(calls.count) : "—"}</strong>
              <span className="runs-secondary">Includes denied attempts</span>
            </Card>
          </div>
          {run.status === "waiting_human" && (
            <div className="runs-approval-callout">
              <PauseCircle size={23} />
              <div>
                <strong>Human approval required</strong>
                <p>
                  {resumeQueued
                    ? "A response is queued. Waiting for the worker to continue."
                    : "Review this run, then submit the response needed by the waiting node."}
                </p>
              </div>
              <Button
                variant="primary"
                onClick={() => {
                  setCommandError("");
                  if (!resumeUnknown)
                    setResumeNode(
                      run.waiting_nodes?.length === 1
                        ? run.waiting_nodes[0]
                        : "",
                    );
                  setResumeOpen(true);
                }}
                disabled={resumeQueued}
              >
                {resumeUnknown ? "Review response retry" : "Review & respond"}
              </Button>
            </div>
          )}
          <div className="runs-detail-layout">
            <Card className="runs-events-card">
              <div className="runs-section-heading">
                <div>
                  <h2>
                    <List size={18} /> Execution timeline
                  </h2>
                  <p>Events are ordered by their saved sequence.</p>
                </div>
                {liveStatuses.has(run.status) && (
                  <span className="runs-live-label">
                    <i /> Live
                  </span>
                )}
              </div>
              {events.length ? (
                <ol className="runs-timeline">
                  {events.map((event) => (
                    <li
                      key={event.id}
                      className={
                        /failed|denied/.test(event.type)
                          ? "runs-event-error"
                          : ""
                      }
                    >
                      <span className="runs-event-dot" />
                      <details>
                        <summary>
                          <span className="runs-event-title">
                            {event.type
                              .replaceAll("_", " ")
                              .replaceAll(".", " · ")}
                          </span>
                          <span className="runs-event-meta">
                            #{event.sequence} · {dateText(event.timestamp)}
                          </span>
                        </summary>
                        <JsonBlock value={event.payload} />
                      </details>
                    </li>
                  ))}
                </ol>
              ) : (
                <EmptyState
                  icon={Terminal}
                  title="No events yet"
                  description="Events appear after the worker begins processing this run."
                />
              )}
              {eventHasMore && (
                <Button
                  variant="secondary"
                  onClick={() => void moreEvents()}
                  disabled={eventBusy}
                >
                  {eventBusy ? "Loading events…" : "Load next events"}{" "}
                  <ChevronRight size={15} />
                </Button>
              )}
              {events.length >= 500 && (
                <p className="runs-footnote">
                  Showing the latest 500 loaded events.
                </p>
              )}
            </Card>
            <div className="runs-detail-aside">
              <Card>
                <div className="runs-section-heading">
                  <h2>
                    <FileJson size={18} /> Input & result
                  </h2>
                </div>
                <details className="runs-data-section" open>
                  <summary>Workflow input</summary>
                  <JsonBlock value={run.input_data} />
                </details>
                <details
                  className="runs-data-section"
                  open={run.status === "completed"}
                >
                  <summary>Workflow result</summary>
                  {run.output_data === null ? (
                    <p className="runs-secondary">No result has been saved.</p>
                  ) : (
                    <JsonBlock value={run.output_data} />
                  )}
                </details>
              </Card>
              <Card>
                <h2 className="runs-small-heading">Run information</h2>
                <dl className="runs-metadata">
                  <div>
                    <dt>Started</dt>
                    <dd>{dateText(run.created_at)}</dd>
                  </div>
                  <div>
                    <dt>Last updated</dt>
                    <dd>{dateText(run.updated_at)}</dd>
                  </div>
                  <div>
                    <dt>Workflow</dt>
                    <dd>
                      <Link to="/workflows">
                        {run.workflow_name ?? run.workflow}
                      </Link>
                    </dd>
                  </div>
                </dl>
                {liveStatuses.has(run.status) && (
                  <Button
                    variant="danger"
                    onClick={() => {
                      setCommandError("");
                      setCancelOpen(true);
                    }}
                    disabled={Boolean(run.cancel_requested)}
                  >
                    <Square size={14} />{" "}
                    {run.cancel_requested
                      ? "Cancellation requested"
                      : "Cancel run"}
                  </Button>
                )}
              </Card>
            </div>
          </div>
          <Card className="runs-ledger-card">
            <div className="runs-section-heading">
              <div>
                <h2>
                  <Activity size={18} /> LLM call ledger
                </h2>
                <p>
                  Provider cost estimates are separate from subscription
                  invoices. Unknown usage keeps its reservation.
                </p>
              </div>
            </div>
            {calls?.results.length ? (
              <>
                <div className="runs-table-scroll">
                  <table className="runs-table">
                    <thead>
                      <tr>
                        <th scope="col">Node / model</th>
                        <th scope="col">Status</th>
                        <th scope="col">Input / output</th>
                        <th scope="col">Total tokens</th>
                        <th scope="col">Estimated cost</th>
                        <th scope="col">Billing period</th>
                      </tr>
                    </thead>
                    <tbody>
                      {calls.results.map((call) => (
                        <tr key={call.id}>
                          <td>
                            <strong>{call.node_id}</strong>
                            <span className="runs-secondary">
                              {call.model_name}
                            </span>
                          </td>
                          <td>
                            <Badge status={call.status} />
                            {call.reservation_active && (
                              <span className="runs-secondary runs-held">
                                {number.format(call.reserved_tokens)} tokens
                                held
                              </span>
                            )}
                          </td>
                          <td>
                            {call.prompt_tokens === null
                              ? "—"
                              : number.format(call.prompt_tokens)}{" "}
                            /{" "}
                            {call.completion_tokens === null
                              ? "—"
                              : number.format(call.completion_tokens)}
                          </td>
                          <td>
                            {call.total_tokens === null ? (
                              <span className="runs-muted">Unknown</span>
                            ) : (
                              number.format(call.total_tokens)
                            )}
                          </td>
                          <td className="runs-nowrap">
                            {estimatedCost(call.cost, call.currency)}
                          </td>
                          <td>
                            {call.billing_period ? (
                              <Link to="/billing" title={call.billing_period}>
                                {call.billing_period.slice(0, 8)}{" "}
                                <ArrowUpRight size={12} />
                              </Link>
                            ) : (
                              <span className="runs-muted">Not attached</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="runs-pagination">
                  <span>
                    {number.format(callOffset + 1)}–
                    {number.format(
                      Math.min(callOffset + calls.results.length, calls.count),
                    )}{" "}
                    of {number.format(calls.count)} records
                  </span>
                  <div>
                    <Button
                      variant="secondary"
                      onClick={() =>
                        setCallOffset((value) =>
                          Math.max(0, value - CALL_PAGE_SIZE),
                        )
                      }
                      disabled={callOffset === 0 || loading}
                    >
                      <ChevronLeft size={14} /> Previous
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() =>
                        setCallOffset((value) => value + CALL_PAGE_SIZE)
                      }
                      disabled={!calls.next || loading}
                    >
                      Next <ChevronRight size={14} />
                    </Button>
                  </div>
                </div>
              </>
            ) : (
              <EmptyState
                icon={Activity}
                title="No LLM calls recorded"
                description="This run may still be queued, or its workflow may not call a language model."
              />
            )}
          </Card>
        </>
      ) : (
        !loading && (
          <EmptyState
            icon={TriangleAlert}
            title="Run unavailable"
            description="The run may not exist or your account may not have access."
            action={<Link to="/runs">Return to runs</Link>}
          />
        )
      )}
      {cancelOpen && (
        <Modal
          title="Cancel this run?"
          onClose={() => {
            if (!commandBusy) setCancelOpen(false);
          }}
        >
          <p className="runs-dialog-copy">
            The worker will stop at a cancellation checkpoint or timeout.
            Completed external actions are not undone.
          </p>
          {commandError && <ErrorNotice message={commandError} />}
          <div className="runs-dialog-actions">
            <Button
              variant="secondary"
              onClick={() => setCancelOpen(false)}
              disabled={commandBusy}
            >
              Keep running
            </Button>
            <Button
              variant="danger"
              onClick={() => void command("cancel")}
              disabled={commandBusy}
            >
              {commandBusy ? "Requesting…" : "Request cancellation"}
            </Button>
          </div>
        </Modal>
      )}
      {resumeOpen && (
        <Modal
          title="Respond to approval"
          onClose={() => {
            if (!commandBusy) setResumeOpen(false);
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void command("resume");
            }}
          >
            {resumeUnknown && (
              <p className="runs-dialog-copy" role="status">
                The response was interrupted. Retry sends the same approval
                command with its original input. You can also check the timeline
                before retrying.
              </p>
            )}
            {(run?.waiting_nodes?.length ?? 0) > 1 && (
              <Field label="Waiting node">
                <select
                  required
                  value={resumeNode}
                  disabled={commandBusy || resumeUnknown}
                  onChange={(event) => {
                    setResumeNode(event.target.value);
                    resumeKey.current = null;
                  }}
                >
                  <option value="">Choose a node</option>
                  {run?.waiting_nodes?.map((node) => (
                    <option key={node} value={node}>
                      {node}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <Field
              label="Response JSON"
              hint="Provide the object expected by this workflow’s approval node."
            >
              <textarea
                className="runs-json-input"
                value={resumeInput}
                disabled={commandBusy || resumeUnknown}
                onChange={(event) => {
                  setResumeInput(event.target.value);
                  resumeKey.current = null;
                }}
                rows={8}
                maxLength={32000}
                spellCheck={false}
              />
            </Field>
            {commandError && <ErrorNotice message={commandError} />}
            <div className="runs-dialog-actions">
              <Button
                type="button"
                variant="secondary"
                onClick={() => setResumeOpen(false)}
                disabled={commandBusy}
              >
                Back
              </Button>
              <Button type="submit" variant="primary" disabled={commandBusy}>
                {commandBusy
                  ? "Submitting…"
                  : resumeUnknown
                    ? "Retry response"
                    : "Submit response"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
