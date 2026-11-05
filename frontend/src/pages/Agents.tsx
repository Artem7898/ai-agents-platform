import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Bot,
  Cpu,
  Pencil,
  Plus,
  Search,
  ShieldCheck,
  Trash2,
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

type Agent = {
  id: string;
  owner: number | null;
  name: string;
  model_name: string;
  runtime_id: string | null;
  system_prompt: string;
  tool_ids: string[];
  temperature: number;
  status: "draft" | "active" | "archived";
  updated_at: string;
};
type Runtime = {
  id: string;
  owner: number | null;
  name: string;
  allowed_models: string[];
  enabled: boolean;
  capabilities: { tools?: boolean };
};
type Tool = { id: string; name: string; description: string };
type Page<T> = { results: T[]; count: number; next: string | null };
type AgentForm = {
  name: string;
  model_name: string;
  runtime_id: string;
  system_prompt: string;
  tool_ids: string;
  temperature: number;
  status: Agent["status"];
};
const blank: AgentForm = {
  name: "",
  model_name: "",
  runtime_id: "",
  system_prompt: "",
  tool_ids: "",
  temperature: 0.7,
  status: "draft",
};
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";

export default function Agents() {
  const { session } = useSession();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [count, setCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editor, setEditor] = useState<Agent | "new" | null>(null);
  const [form, setForm] = useState<AgentForm>(blank);
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<Agent | null>(null);
  const [revision, setRevision] = useState(0);
  const limit = 24;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.all([
      api<Page<Agent>>(`agents/?limit=${limit}&offset=${offset}`, {
        signal: controller.signal,
      }),
      api<Page<Runtime>>("runtimes/?limit=200", { signal: controller.signal }),
    ])
      .then(([agentData, runtimeData]) => {
        setAgents(agentData.results);
        setCount(agentData.count);
        setRuntimes(runtimeData.results);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [offset, revision]);

  useEffect(() => {
    const controller = new AbortController();
    api<Tool[] | { results: Tool[] }>("tools/", { signal: controller.signal })
      .then((result) =>
        setTools(Array.isArray(result) ? result : result.results),
      )
      .catch(() => {
        /* UUID entry remains available if no catalog is published. */
      });
    return () => controller.abort();
  }, []);

  const visible = useMemo(
    () =>
      agents.filter(
        (agent) =>
          (status === "all" || agent.status === status) &&
          `${agent.name} ${agent.model_name}`
            .toLowerCase()
            .includes(search.toLowerCase()),
      ),
    [agents, search, status],
  );
  const ownerId =
    typeof editor === "object" && editor ? editor.owner : session?.user?.id;
  const availableRuntimes = runtimes.filter(
    (runtime) => runtime.enabled && runtime.owner === ownerId,
  );
  const selectedRuntime = runtimes.find(
    (runtime) => runtime.id === form.runtime_id,
  );
  const selectedTools = form.tool_ids.split(/[\s,]+/).filter(Boolean);

  function openEditor(agent?: Agent) {
    setEditor(agent ?? "new");
    setFormError("");
    setForm(
      agent
        ? {
            name: agent.name,
            model_name: agent.model_name,
            runtime_id: agent.runtime_id ?? "",
            system_prompt: agent.system_prompt,
            tool_ids: agent.tool_ids.join("\n"),
            temperature: agent.temperature,
            status: agent.status,
          }
        : { ...blank },
    );
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setFormError("");
    if (
      selectedTools.some(
        (id) =>
          !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
            id,
          ),
      )
    ) {
      setFormError("Tool IDs must be UUIDs, separated by commas or new lines.");
      return;
    }
    if (new Set(selectedTools).size !== selectedTools.length) {
      setFormError("Each tool can be selected only once.");
      return;
    }
    setSaving(true);
    try {
      const editing = editor !== "new" && editor !== null;
      await api(editing ? `agents/${editor.id}/` : "agents/", {
        method: editing ? "PATCH" : "POST",
        body: {
          ...form,
          name: form.name.trim(),
          model_name: form.model_name.trim(),
          runtime_id: form.runtime_id || null,
          tool_ids: selectedTools,
        },
      });
      setEditor(null);
      setNotice(editing ? "Agent updated." : "Agent created.");
      setRevision((value) => value + 1);
    } catch (error) {
      setFormError(message(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setSaving(true);
    setFormError("");
    try {
      await api(`agents/${deleting.id}/`, { method: "DELETE" });
      setDeleting(null);
      setNotice("Agent deleted.");
      setRevision((value) => value + 1);
    } catch (error) {
      setFormError(message(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="as-page">
      <PageHeader
        title="Agents"
        description="Give every agent a model, a purpose and a clear set of permissions."
        actions={
          <Button variant="primary" onClick={() => openEditor()}>
            <Plus size={17} /> New agent
          </Button>
        }
      />
      <div className="as-mini-stats">
        <Card>
          <span className="as-stat-icon">
            <Bot size={20} />
          </span>
          <div>
            <strong>{loading || error ? "—" : count}</strong>
            <span>Configured agents</span>
          </div>
        </Card>
        <Card>
          <span className="as-stat-icon as-mint">
            <Cpu size={20} />
          </span>
          <div>
            <strong>
              {loading || error
                ? "—"
                : runtimes.filter((item) => item.enabled).length}
            </strong>
            <span>Enabled runtimes · loaded</span>
          </div>
        </Card>
        <Card>
          <span className="as-stat-icon as-amber">
            <ShieldCheck size={20} />
          </span>
          <div>
            <strong>Allowlisted</strong>
            <span>Tool access is explicit</span>
          </div>
        </Card>
      </div>
      {notice && (
        <div className="as-success" role="status">
          {notice}
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      <Card className="as-agents-panel">
        <div className="as-toolbar">
          <label className="as-search">
            <Search size={17} />
            <input
              aria-label="Filter agents on this page"
              placeholder="Filter agents on this page…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            aria-label="Filter agent status"
            className="as-select as-select-compact"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="draft">Draft</option>
            <option value="archived">Archived</option>
          </select>
        </div>
        {loading ? (
          <div className="as-loading">
            <Spinner />
          </div>
        ) : error ? (
          <EmptyState
            icon={Bot}
            title="Agent data is unavailable"
            description="Refresh the list after the connection is restored."
            action={
              <Button onClick={() => setRevision((value) => value + 1)}>
                Try again
              </Button>
            }
          />
        ) : visible.length ? (
          <div className="as-agent-grid">
            {visible.map((agent, index) => (
              <article className="as-agent-card" key={agent.id}>
                <div className="as-agent-card-top">
                  <span className={`as-agent-avatar as-avatar-${index % 3}`}>
                    <Bot size={24} />
                  </span>
                  <Badge status={agent.status} />
                </div>
                <h3>{agent.name}</h3>
                <p className="as-agent-purpose">
                  {agent.system_prompt || "No system instructions added."}
                </p>
                <div className="as-agent-facts">
                  <span>
                    <Cpu size={15} />
                    {agent.model_name}
                  </span>
                  <span>
                    <ShieldCheck size={15} />
                    {agent.tool_ids.length} tools allowed
                  </span>
                </div>
                <div className="as-agent-card-bottom">
                  <span>
                    {session?.user?.is_staff && agent.owner !== session.user.id
                      ? `Owner #${agent.owner ?? "legacy"}`
                      : agent.runtime_id
                        ? (runtimes.find((item) => item.id === agent.runtime_id)
                            ?.name ?? "Managed runtime")
                        : "Server default"}
                  </span>
                  <div>
                    <button
                      className="as-icon-button"
                      aria-label={`Edit ${agent.name}`}
                      onClick={() => openEditor(agent)}
                    >
                      <Pencil size={16} />
                    </button>
                    <button
                      className="as-icon-button as-destructive"
                      aria-label={`Delete ${agent.name}`}
                      onClick={() => {
                        setFormError("");
                        setDeleting(agent);
                      }}
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={Bot}
            title={
              agents.length
                ? "No agents match your filters"
                : "Your first agent starts here"
            }
            description={
              agents.length
                ? "Try a different name, model or status."
                : "Create an agent and connect it to a workflow when you are ready."
            }
            action={
              agents.length ? undefined : (
                <Button variant="primary" onClick={() => openEditor()}>
                  <Plus size={16} /> Create agent
                </Button>
              )
            }
          />
        )}
        <div className="as-pagination">
          <span>
            {error
              ? "List unavailable"
              : count
                ? `${offset + 1}–${Math.min(offset + agents.length, count)} of ${count} agents`
                : "No agents yet"}
          </span>
          <div>
            <Button
              variant="secondary"
              disabled={offset === 0 || loading}
              onClick={() => setOffset((value) => Math.max(0, value - limit))}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              disabled={offset + limit >= count || loading}
              onClick={() => setOffset((value) => value + limit)}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>
      {editor && (
        <Modal
          title={editor === "new" ? "Create an agent" : "Edit agent"}
          onClose={() => {
            if (!saving) setEditor(null);
          }}
          wide
        >
          <form className="as-form" onSubmit={save}>
            {formError && <ErrorNotice message={formError} />}
            <div className="as-form-row">
              <Field label="Agent name">
                <input
                  className="as-input"
                  autoFocus
                  required
                  maxLength={128}
                  value={form.name}
                  onChange={(event) =>
                    setForm({ ...form, name: event.target.value })
                  }
                  placeholder="Research Agent"
                />
              </Field>
              <Field label="Status">
                <select
                  className="as-select"
                  value={form.status}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      status: event.target.value as Agent["status"],
                    })
                  }
                >
                  <option value="draft">Draft</option>
                  <option value="active">Active</option>
                  <option value="archived">Archived</option>
                </select>
              </Field>
            </div>
            <div className="as-form-row">
              <Field
                label="Runtime"
                hint="An agent can use an enabled runtime belonging to its owner."
              >
                <select
                  className="as-select"
                  value={form.runtime_id}
                  onChange={(event) => {
                    const runtime = runtimes.find(
                      (item) => item.id === event.target.value,
                    );
                    setForm({
                      ...form,
                      runtime_id: event.target.value,
                      model_name: runtime?.allowed_models[0] ?? form.model_name,
                    });
                  }}
                >
                  <option value="">Default server configuration</option>
                  {availableRuntimes.map((runtime) => (
                    <option key={runtime.id} value={runtime.id}>
                      {runtime.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Model">
                <input
                  className="as-input"
                  required
                  maxLength={128}
                  list="agent-model-options"
                  value={form.model_name}
                  onChange={(event) =>
                    setForm({ ...form, model_name: event.target.value })
                  }
                  placeholder="Exact model name"
                />
                <datalist id="agent-model-options">
                  {selectedRuntime?.allowed_models.map((model) => (
                    <option key={model} value={model} />
                  ))}
                </datalist>
              </Field>
            </div>
            <Field
              label="System instructions"
              hint="Describe the agent’s role, expected output and boundaries."
            >
              <textarea
                className="as-textarea"
                rows={5}
                value={form.system_prompt}
                onChange={(event) =>
                  setForm({ ...form, system_prompt: event.target.value })
                }
                placeholder="You are a research assistant. Cite your sources and flag uncertainty."
              />
            </Field>
            <Field
              label={`Temperature · ${form.temperature.toFixed(1)}`}
              hint="The selected runtime must support temperature for this setting to be sent."
            >
              <input
                className="as-range"
                type="range"
                min={0}
                max={2}
                step={0.1}
                value={form.temperature}
                onChange={(event) =>
                  setForm({ ...form, temperature: Number(event.target.value) })
                }
              />
            </Field>
            <fieldset className="as-fieldset">
              <legend>Allowed tools</legend>
              <p className="as-note">
                Only these tools can be called by this agent. Empty means no
                tool access.
              </p>
              {tools.length ? (
                <div className="as-tool-options">
                  {tools.map((tool) => (
                    <label key={tool.id}>
                      <input
                        type="checkbox"
                        checked={selectedTools.includes(tool.id)}
                        disabled={Boolean(
                          selectedRuntime &&
                          !selectedRuntime.capabilities.tools &&
                          !selectedTools.includes(tool.id),
                        )}
                        onChange={(event) => {
                          const next = event.target.checked
                            ? [...selectedTools, tool.id]
                            : selectedTools.filter((id) => id !== tool.id);
                          setForm({ ...form, tool_ids: next.join("\n") });
                        }}
                      />
                      <span>
                        <strong>{tool.name}</strong>
                        <small>{tool.description}</small>
                      </span>
                    </label>
                  ))}
                </div>
              ) : (
                <textarea
                  className="as-textarea as-monospace"
                  rows={2}
                  value={form.tool_ids}
                  onChange={(event) =>
                    setForm({ ...form, tool_ids: event.target.value })
                  }
                  aria-label="Allowed tool UUIDs"
                  placeholder="Tool UUIDs, one per line"
                />
              )}
              {selectedTools.length > 0 && (
                <div>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => setForm({ ...form, tool_ids: "" })}
                  >
                    Clear tool access
                  </Button>
                </div>
              )}
            </fieldset>
            {selectedRuntime && !selectedRuntime.capabilities.tools && (
              <p className="as-note">
                This runtime has tool calling disabled. Remove existing tool IDs
                before saving.
              </p>
            )}
            <div className="as-modal-actions">
              <Button
                type="button"
                variant="secondary"
                disabled={saving}
                onClick={() => setEditor(null)}
              >
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={saving}>
                {saving ? "Saving…" : "Save agent"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {deleting && (
        <Modal
          title="Delete agent?"
          onClose={() => {
            if (!saving) setDeleting(null);
          }}
        >
          <div className="as-form">
            <p>
              Delete <strong>{deleting.name}</strong>? Definitions used by
              existing records may be protected by the server.
            </p>
            {formError && <ErrorNotice message={formError} />}
            <div className="as-modal-actions">
              <Button
                variant="secondary"
                disabled={saving}
                onClick={() => setDeleting(null)}
              >
                Keep agent
              </Button>
              <Button variant="danger" disabled={saving} onClick={remove}>
                {saving ? "Deleting…" : "Delete agent"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
