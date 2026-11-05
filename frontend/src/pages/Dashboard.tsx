import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowUpRight,
  Bot,
  CheckCircle2,
  CircleAlert,
  Coins,
  Database,
  FileText,
  GitBranch,
  Layers3,
  Play,
  RefreshCw,
  Workflow,
} from "lucide-react";
import {
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  Spinner,
} from "../components/ui";
import WorkflowMini from "../components/WorkflowMini";
import { useDashboard } from "../lib/dashboard";
import { duration, money, number } from "../lib/format";
import { useNewRun } from "../lib/run-dialog";

const colors = [
  "#9d70c7",
  "#76b4bd",
  "#e48c98",
  "#d6b566",
  "#7e96cd",
  "#94b693",
];
export default function Dashboard() {
  const { data, loading, error, refresh } = useDashboard();
  const { open } = useNewRun();
  const [lines, setLines] = useState({ completed: true, failed: true });
  if (loading && !data)
    return (
      <div className="dashboard-loading">
        <Spinner />
        <span>Loading your workspace…</span>
      </div>
    );
  if (!data)
    return (
      <Card>
        <EmptyState
          title="Your dashboard is temporarily unavailable"
          description={error || "Please try again."}
          action={
            <Button onClick={refresh}>
              <RefreshCw size={16} />
              Try again
            </Button>
          }
        />
      </Card>
    );
  const total = data.token_usage.reduce((sum, item) => sum + item.tokens, 0);
  const metrics = [
    {
      icon: Layers3,
      label: "Active runs",
      value: number(data.metrics.active_runs),
      hint: "Running now",
    },
    {
      icon: Workflow,
      label: "Queued tasks",
      value: number(data.metrics.queued_tasks),
      hint: "Waiting for a worker",
    },
    {
      icon: Coins,
      label: "Tokens today",
      value:
        data.metrics.tokens_today === null
          ? "Unknown"
          : number(data.metrics.tokens_today),
      hint: data.metrics.unknown_usage_calls_today
        ? `${data.metrics.unknown_usage_calls_today} calls awaiting usage · UTC`
        : "Reported usage · UTC",
    },
    {
      icon: CircleAlert,
      label: "Errors · 24h",
      value: number(data.metrics.errors_24h),
      hint: "Failed workflow runs",
    },
    {
      icon: Activity,
      label: "System readiness",
      value:
        data.metrics.system_health.status === "ready" ? "Ready" : "Unavailable",
      hint: "API & database",
    },
  ];
  return (
    <div className="dashboard-content">
      <ErrorNotice
        message={error ? `${error} Showing the last received data.` : ""}
      />
      <div className="metrics-grid">
        {metrics.map((metric) => (
          <Card key={metric.label} className="metric-card">
            <span className="metric-icon">
              <metric.icon size={24} strokeWidth={1.7} />
            </span>
            <div>
              <strong className="metric-value">{metric.value}</strong>
              <span className="metric-label">{metric.label}</span>
              <span className="metric-hint">{metric.hint}</span>
            </div>
          </Card>
        ))}
      </div>
      <div className="dashboard-charts">
        <Card className="activity-card">
          <div className="card-heading">
            <h2>Run activity</h2>
            <div className="card-tools">
              <span className="period-label">Last 24 hours · UTC</span>
              <Button
                variant="ghost"
                aria-label="Refresh dashboard"
                onClick={refresh}
              >
                <RefreshCw size={16} />
              </Button>
            </div>
          </div>
          <div className="chart-legend">
            {(["completed", "failed"] as const).map((key) => (
              <button
                key={key}
                aria-pressed={lines[key]}
                onClick={() =>
                  setLines((current) => ({ ...current, [key]: !current[key] }))
                }
                className={!lines[key] ? "legend-muted" : ""}
              >
                <span
                  style={{
                    background: key === "completed" ? "#8060b4" : "#65b3b8",
                  }}
                />
                {key === "completed" ? "Completed" : "Failed"}
              </button>
            ))}
          </div>
          <div className="activity-chart">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={data.activity}
                margin={{ top: 12, right: 12, bottom: 4, left: -15 }}
              >
                <CartesianGrid stroke="#eae9ee" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 11, fill: "#85818c" }}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={45}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fontSize: 11, fill: "#85818c" }}
                  tickLine={false}
                  axisLine={false}
                  width={42}
                />
                <Tooltip
                  contentStyle={{
                    borderRadius: 10,
                    border: "1px solid #e5e1ec",
                    fontSize: 12,
                  }}
                />
                <Line
                  name="Completed"
                  hide={!lines.completed}
                  type="monotone"
                  dataKey="completed"
                  stroke="#8060b4"
                  strokeWidth={2.6}
                  dot={false}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
                <Line
                  name="Failed"
                  hide={!lines.failed}
                  type="monotone"
                  dataKey="failed"
                  stroke="#65b3b8"
                  strokeWidth={2.6}
                  dot={false}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
        <Card className="token-card">
          <div className="card-heading">
            <h2>Token usage by agent</h2>
            <span className="period-label">Today · UTC</span>
          </div>
          {total ? (
            <div className="donut-content">
              <div className="donut-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={data.token_usage}
                      dataKey="tokens"
                      nameKey="agent_name"
                      innerRadius="59%"
                      outerRadius="88%"
                      paddingAngle={1}
                      stroke="#fff"
                      strokeWidth={2}
                      isAnimationActive={false}
                    >
                      {data.token_usage.map((item, index) => (
                        <Cell
                          key={item.agent_id ?? item.agent_name}
                          fill={colors[index % colors.length]}
                        />
                      ))}
                    </Pie>
                    <Tooltip
                      formatter={(value) => number(Number(value))}
                      contentStyle={{ borderRadius: 10, fontSize: 12 }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="donut-center">
                  <strong>{number(total)}</strong>
                  <span>reported tokens</span>
                </div>
              </div>
              <div className="donut-legend">
                {data.token_usage.map((item, index) => (
                  <div key={item.agent_id ?? item.agent_name}>
                    <span
                      className="legend-dot"
                      style={{ background: colors[index % colors.length] }}
                    />
                    <span className="legend-name" title={item.agent_name}>
                      {item.agent_name}
                    </span>
                    <strong>{Math.round((item.tokens / total) * 100)}%</strong>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <EmptyState
              icon={Coins}
              title="No reported usage yet"
              description="Agent usage will appear after workflows make LLM calls."
            />
          )}
          {data.token_usage_complete === false && (
            <p className="chart-note">
              Chart shows a bounded sample. Total usage is calculated
              separately.
            </p>
          )}
          {data.metrics.unknown_usage_calls_today > 0 && (
            <p className="chart-note">
              Calls with unknown usage are excluded from token shares.
            </p>
          )}
        </Card>
      </div>
      <div className="dashboard-bottom">
        <Card className="workspace-card">
          <div className="card-heading">
            <h2>Active agents</h2>
            <Link className="subtle-link" to="/agents">
              View all <ArrowUpRight size={14} />
            </Link>
          </div>
          {data.active_agents.length ? (
            <div className="agent-overview-list">
              {data.active_agents.map((agent, index) => (
                <Link
                  to="/agents"
                  className="agent-overview-row"
                  key={agent.id}
                >
                  <span className={`agent-avatar agent-avatar-${index % 3}`}>
                    <Bot size={20} />
                  </span>
                  <div className="agent-overview-name">
                    <strong>{agent.name}</strong>
                    <span>
                      {agent.active_runs
                        ? `${agent.active_runs} active runs`
                        : agent.waiting_runs
                          ? "Human input requested"
                          : "Available for workflows"}
                    </span>
                  </div>
                  <span className="agent-model">{agent.model_name}</span>
                  <span className="agent-tool-count">
                    {agent.tools_count} tools
                  </span>
                  <Badge
                    status={
                      agent.active_runs
                        ? "running"
                        : agent.waiting_runs
                          ? "waiting_human"
                          : "ready"
                    }
                  />
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Bot}
              title="Your agents will appear here"
              description="Create an agent and choose its model and tools."
              action={
                <Link className="button button-secondary" to="/agents">
                  Create an agent
                </Link>
              }
            />
          )}
          <div className="card-heading recent-heading">
            <h2>Recent runs</h2>
            <Link className="subtle-link" to="/runs">
              View all <ArrowUpRight size={14} />
            </Link>
          </div>
          {data.recent_runs.length ? (
            <div className="table-wrap">
              <table className="data-table recent-runs-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Workflow</th>
                    <th>Status</th>
                    <th>Duration</th>
                    <th>Est. cost</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_runs.map((run) => (
                    <tr key={run.id}>
                      <td>
                        <Link to={`/runs/${run.id}`} className="run-link">
                          #{run.id.slice(0, 8)}
                        </Link>
                      </td>
                      <td>
                        <span className="truncate" title={run.workflow_name}>
                          {run.workflow_name}
                        </span>
                      </td>
                      <td>
                        <Badge status={run.status} />
                      </td>
                      <td>{duration(run.duration_seconds)}</td>
                      <td>{money(run.cost, run.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="inline-empty">
              <Play size={18} />
              <span>
                No runs yet. Start a saved workflow to see its progress here.
              </span>
              <Button variant="ghost" onClick={() => open()}>
                New run
              </Button>
            </div>
          )}
        </Card>
        <Card className="preview-card">
          <div className="card-heading">
            <h2>Workflow preview</h2>
            <Link className="subtle-link" to="/workflows">
              Open builder <ArrowUpRight size={14} />
            </Link>
          </div>
          {data.workflow_preview ? (
            <div className="workflow-preview-box">
              <span className="workflow-preview-title">
                <GitBranch size={13} />
                {data.workflow_preview.name}
              </span>
              <WorkflowMini
                nodes={data.workflow_preview.nodes}
                edges={data.workflow_preview.edges}
              />
            </div>
          ) : (
            <div className="workflow-preview-box">
              <EmptyState
                icon={Workflow}
                title="Build your first workflow"
                description="Connect an agent, conditions and human approvals."
                action={
                  <Link className="button button-secondary" to="/workflows">
                    Open workflow builder
                  </Link>
                }
              />
            </div>
          )}
          <div className="card-heading knowledge-heading">
            <h2>Knowledge & alerts</h2>
            <Link className="subtle-link" to="/knowledge">
              Details <ArrowUpRight size={14} />
            </Link>
          </div>
          <div className="knowledge-grid">
            <Link to="/knowledge">
              <span className="small-purple-icon">
                <Database size={18} />
              </span>
              <div>
                <strong>Knowledge search</strong>
                <span>
                  {data.knowledge.configured
                    ? "Adapter configured"
                    : "Not connected"}
                </span>
              </div>
            </Link>
            <Link to="/knowledge">
              <span className="small-purple-icon">
                <FileText size={18} />
              </span>
              <div>
                <strong>Documents</strong>
                <span>Not measured</span>
              </div>
            </Link>
            <Link to="/observability">
              <span className="small-purple-icon">
                <CheckCircle2 size={18} />
              </span>
              <div>
                <strong>API & database</strong>
                <span>
                  {data.metrics.system_health.status === "ready"
                    ? "Ready"
                    : "Unavailable"}
                </span>
              </div>
            </Link>
            <Link to="/runs?status=waiting_human">
              <span className="small-purple-icon">
                <CircleAlert size={18} />
              </span>
              <div>
                <strong>Needs attention</strong>
                <span>
                  {data.alerts.filter((alert) => alert.kind !== "info").length}{" "}
                  workspace alerts
                </span>
              </div>
            </Link>
          </div>
        </Card>
      </div>
      <div className="dashboard-footer">
        <span className="live-dot" />
        Live workspace data
        <span>
          Updated{" "}
          {new Date(data.generated_at).toLocaleTimeString("en-GB", {
            timeZone: "UTC",
            hour: "2-digit",
            minute: "2-digit",
          })}{" "}
          UTC
        </span>
      </div>
    </div>
  );
}
