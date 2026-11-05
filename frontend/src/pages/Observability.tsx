import { useEffect, useState } from "react";
import {
  Activity,
  ArrowUpRight,
  CheckCircle2,
  Clock3,
  Database,
  RefreshCw,
  Server,
  ShieldCheck,
  TriangleAlert,
  Users,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useSession } from "../lib/session";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  PageHeader,
  Spinner,
} from "../components/ui";
import "./runs-observability.css";

interface Dashboard {
  generated_at: string;
  metrics: {
    active_runs: number;
    queued_tasks: number;
    tokens_today: number | null;
    unknown_usage_calls_today: number;
    errors_24h: number;
    system_health: { status: string; label: string; uptime_percent: null };
  };
  activity: {
    hour: string;
    label: string;
    completed: number;
    failed: number;
  }[];
  alerts: {
    id: string;
    kind: string;
    title: string;
    description: string;
    href: string;
  }[];
  knowledge: {
    configured: boolean;
    index_status: string;
    documents_count: null;
    memory_status: string;
  };
}
interface Operations {
  collected_at: string;
  run_counts: Record<string, number>;
  command_counts: Record<string, number>;
  oldest_pending_age_seconds: number;
  expired_processing_count: number;
  total_tokens: number;
}
const format = new Intl.NumberFormat();
function errorText(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Monitoring data could not be loaded.";
}
function age(seconds: number) {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600)
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}
function StatusList({ values }: { values: Record<string, number> }) {
  return (
    <dl className="obs-status-list">
      {Object.entries(values).map(([key, value]) => (
        <div key={key}>
          <dt>
            <Badge status={key} />
          </dt>
          <dd>{format.format(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function Observability() {
  const { session } = useSession();
  const staff = Boolean(session.user?.is_staff);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [operations, setOperations] = useState<Operations | null>(null);
  const [error, setError] = useState("");
  const [operationsError, setOperationsError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      const results = await Promise.allSettled([
        api<Dashboard>("dashboard/", { signal: controller.signal }),
        staff
          ? api<Operations>("operations/", { signal: controller.signal })
          : Promise.resolve(null),
      ]);
      if (!active) return;
      if (results[0].status === "fulfilled") {
        setDashboard(results[0].value);
        setError("");
      } else if (!(
        results[0].reason instanceof Error &&
        results[0].reason.name === "AbortError"
      ))
        setError(errorText(results[0].reason));
      if (results[1].status === "fulfilled") {
        setOperations(results[1].value);
        setOperationsError("");
      } else if (!(
        results[1].reason instanceof Error &&
        results[1].reason.name === "AbortError"
      ))
        setOperationsError(errorText(results[1].reason));
      setLoading(false);
      timer = setTimeout(() => void load(), 15000);
    };
    void load();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [staff, revision]);
  const ready = dashboard?.metrics.system_health.status === "ready";
  const peak = Math.max(
    1,
    ...(dashboard?.activity.map((point) =>
      Math.max(point.completed, point.failed),
    ) ?? []),
  );
  return (
    <div className="obs-page">
      <PageHeader
        title="Observability"
        description="Measured activity, queue signals and operational visibility."
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
      {error && <ErrorNotice message={error} />}
      {loading && !dashboard ? (
        <div className="runs-loading">
          <Spinner />
          <span>Loading monitoring data…</span>
        </div>
      ) : dashboard ? (
        <>
          <div className="obs-metrics">
            <Card>
              <span className="obs-metric-icon">
                <Database size={20} />
              </span>
              <div>
                <strong>{ready ? "Ready" : "Unavailable"}</strong>
                <span>API / database readiness</span>
              </div>
              <span className={`obs-readiness ${ready ? "is-ready" : ""}`} />
            </Card>
            <Card>
              <span className="obs-metric-icon">
                <Activity size={20} />
              </span>
              <div>
                <strong>{format.format(dashboard.metrics.active_runs)}</strong>
                <span>Your active runs</span>
              </div>
            </Card>
            <Card>
              <span className="obs-metric-icon">
                <Clock3 size={20} />
              </span>
              <div>
                <strong>{format.format(dashboard.metrics.queued_tasks)}</strong>
                <span>Your queued tasks</span>
              </div>
            </Card>
            <Card>
              <span className="obs-metric-icon obs-metric-warning">
                <TriangleAlert size={20} />
              </span>
              <div>
                <strong>{format.format(dashboard.metrics.errors_24h)}</strong>
                <span>Your failed runs · 24h</span>
              </div>
            </Card>
          </div>
          <div className="obs-main-grid">
            <Card className="obs-activity">
              <div className="runs-section-heading">
                <div>
                  <h2>Your run activity</h2>
                  <p>Completions and failures in the last 24 hours · UTC.</p>
                </div>
                <div className="obs-chart-key">
                  <span>
                    <i />
                    Completed
                  </span>
                  <span>
                    <i />
                    Failed
                  </span>
                </div>
              </div>
              {dashboard.activity.some(
                (point) => point.completed || point.failed,
              ) ? (
                <>
                  <div
                    className="obs-bar-chart"
                    role="img"
                    aria-label="Hourly completed and failed runs in the last 24 hours"
                  >
                    {dashboard.activity.map((point) => (
                      <div
                        className="obs-bar-group"
                        key={point.hour}
                        title={`${point.label} UTC: ${point.completed} completed, ${point.failed} failed`}
                      >
                        <span
                          style={{
                            height: `${(point.completed / peak) * 100}%`,
                          }}
                        />
                        <span
                          style={{ height: `${(point.failed / peak) * 100}%` }}
                        />
                      </div>
                    ))}
                  </div>
                  <div className="obs-chart-labels">
                    <span>{dashboard.activity[0]?.label}</span>
                    <span>
                      {
                        dashboard.activity[
                          Math.floor(dashboard.activity.length / 2)
                        ]?.label
                      }
                    </span>
                    <span>{dashboard.activity.at(-1)?.label}</span>
                  </div>
                  <details className="obs-chart-data">
                    <summary>View hourly values</summary>
                    <div className="runs-table-scroll">
                      <table className="runs-table">
                        <thead>
                          <tr>
                            <th scope="col">Hour UTC</th>
                            <th scope="col">Completed</th>
                            <th scope="col">Failed</th>
                          </tr>
                        </thead>
                        <tbody>
                          {dashboard.activity.map((point) => (
                            <tr key={point.hour}>
                              <td>{point.label}</td>
                              <td>{point.completed}</td>
                              <td>{point.failed}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </details>
                </>
              ) : (
                <EmptyState
                  icon={Activity}
                  title="No completed activity yet"
                  description="This chart fills as your workflows complete or fail."
                />
              )}
            </Card>
            <Card>
              <div className="runs-section-heading">
                <h2>
                  <ShieldCheck size={18} /> Signals & attention
                </h2>
              </div>
              {dashboard.alerts.length ? (
                <ul className="obs-alerts">
                  {dashboard.alerts.map((alert) => (
                    <li key={alert.id}>
                      <span
                        className={`obs-alert-icon obs-alert-${alert.kind}`}
                      >
                        {alert.kind === "error" || alert.kind === "warning" ? (
                          <TriangleAlert size={17} />
                        ) : (
                          <Activity size={17} />
                        )}
                      </span>
                      <div>
                        <strong>{alert.title}</strong>
                        <p>{alert.description}</p>
                        {alert.href.startsWith("/") &&
                          !alert.href.startsWith("//") && (
                            <Link to={alert.href}>
                              Review <ArrowUpRight size={13} />
                            </Link>
                          )}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="obs-no-alerts">
                  <CheckCircle2 size={29} />
                  <strong>No attention signals</strong>
                  <p>No alerts are currently derived from your saved runs.</p>
                </div>
              )}
            </Card>
          </div>
          <div className="obs-context-grid">
            <Card>
              <h2 className="runs-small-heading">Usage visibility</h2>
              <div className="obs-usage-value">
                {dashboard.metrics.tokens_today === null
                  ? "Awaiting usage"
                  : format.format(dashboard.metrics.tokens_today)}
                <span>tokens today · UTC</span>
              </div>
              <p className="runs-secondary">
                {dashboard.metrics.unknown_usage_calls_today
                  ? `${format.format(dashboard.metrics.unknown_usage_calls_today)} call(s) have unknown usage. Their reservations remain held.`
                  : "Reported totals only. Estimates are not substituted for missing usage."}
              </p>
              <Link className="obs-text-link" to="/runs">
                Inspect run ledgers <ArrowUpRight size={14} />
              </Link>
            </Card>
            <Card>
              <h2 className="runs-small-heading">Knowledge integration</h2>
              <div className="obs-integration-status">
                <Database size={23} />
                <div>
                  <strong>
                    {dashboard.knowledge.configured
                      ? "Search endpoint configured"
                      : "No search endpoint configured"}
                  </strong>
                  <span>
                    Index health and document counts are not reported.
                  </span>
                </div>
              </div>
              <Link className="obs-text-link" to="/knowledge">
                Knowledge & memory <ArrowUpRight size={14} />
              </Link>
            </Card>
            <Card>
              <h2 className="runs-small-heading">Infrastructure telemetry</h2>
              <p className="obs-unreported">Not connected</p>
              <p className="runs-secondary">
                Host CPU, GPU, memory, worker heartbeat and uptime are not
                measured by this view. Readiness confirms API/database access
                only.
              </p>
            </Card>
          </div>
        </>
      ) : (
        !loading && (
          <Card>
            <EmptyState
              icon={TriangleAlert}
              title="Monitoring unavailable"
              description="Refresh to try loading your operational summary again."
            />
          </Card>
        )
      )}
      {staff ? (
        <Card className="obs-platform">
          <div className="runs-section-heading">
            <div>
              <h2>
                <Server size={18} /> Platform operations
              </h2>
              <p>
                Administrator view · all owners. These totals have a different
                scope from your charts above.
              </p>
            </div>
            <span className="obs-admin-label">Staff only</span>
          </div>
          {operationsError && <ErrorNotice message={operationsError} />}
          {operations ? (
            <>
              <div className="obs-platform-summary">
                <div>
                  <span>Oldest pending command</span>
                  <strong>{age(operations.oldest_pending_age_seconds)}</strong>
                </div>
                <div>
                  <span>Expired processing leases</span>
                  <strong
                    className={
                      operations.expired_processing_count
                        ? "obs-value-warning"
                        : ""
                    }
                  >
                    {format.format(operations.expired_processing_count)}
                  </strong>
                </div>
                <div>
                  <span>Persisted token totals</span>
                  <strong>{format.format(operations.total_tokens)}</strong>
                </div>
              </div>
              <div className="obs-status-grid">
                <section>
                  <h3>Workflow runs</h3>
                  <StatusList values={operations.run_counts} />
                </section>
                <section>
                  <h3>Queue commands</h3>
                  <StatusList values={operations.command_counts} />
                </section>
              </div>
              <p className="runs-footnote">
                Collected {new Date(operations.collected_at).toLocaleString()}.
                Queue leases do not establish an idle worker’s health.
              </p>
            </>
          ) : (
            !operationsError && (
              <div className="runs-loading">
                <Spinner />
                <span>Loading platform operations…</span>
              </div>
            )
          )}
        </Card>
      ) : (
        <div className="obs-access-note">
          <Users size={17} />
          <p>
            Platform-wide queue statistics are available to workspace
            administrators. Your personal runs and usage are shown above.
          </p>
        </div>
      )}
      {dashboard && (
        <p className="runs-footnote">
          <RefreshCw size={12} /> Updates every 15 seconds · last summary{" "}
          {new Date(dashboard.generated_at).toLocaleTimeString()}
        </p>
      )}
    </div>
  );
}
