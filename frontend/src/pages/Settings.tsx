import { useEffect, useState, type FormEvent } from "react";
import {
  CheckCircle2,
  KeyRound,
  LockKeyhole,
  Pencil,
  Plus,
  Server,
  ShieldCheck,
  Trash2,
  UserRound,
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

type Capabilities = {
  tools: boolean;
  structured_outputs: boolean;
  streaming: boolean;
  usage: boolean;
  strict_tools: boolean;
  temperature: boolean;
  token_limit_parameter: "max_tokens" | "max_completion_tokens";
};
type Runtime = {
  id: string;
  owner: number | null;
  name: string;
  profile: "openai" | "ollama" | "openai_compatible";
  base_url: string;
  allowed_models: string[];
  capabilities: Capabilities;
  enabled: boolean;
  revision: number;
};
type RuntimeForm = {
  name: string;
  owner: string;
  profile: Runtime["profile"];
  base_url: string;
  secret_ref: string;
  models: string;
  enabled: boolean;
  capabilities: Capabilities;
};
const capabilities: Capabilities = {
  tools: false,
  structured_outputs: false,
  streaming: false,
  usage: false,
  strict_tools: false,
  temperature: false,
  token_limit_parameter: "max_completion_tokens",
};
const blankRuntime: RuntimeForm = {
  name: "",
  owner: "",
  profile: "openai",
  base_url: "https://api.openai.com/v1",
  secret_ref: "",
  models: "",
  enabled: true,
  capabilities,
};
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";

export default function Settings() {
  const { session, refresh } = useSession();
  const user = session?.user;
  const [tab, setTab] = useState("profile");
  const [firstName, setFirstName] = useState(user?.first_name ?? "");
  const [lastName, setLastName] = useState(user?.last_name ?? "");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [runtimeCount, setRuntimeCount] = useState(0);
  const [runtimeOffset, setRuntimeOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [editor, setEditor] = useState<Runtime | "new" | null>(null);
  const [form, setForm] = useState<RuntimeForm>(blankRuntime);
  const [formError, setFormError] = useState("");
  const [deleting, setDeleting] = useState<Runtime | null>(null);

  useEffect(() => {
    setFirstName(user?.first_name ?? "");
    setLastName(user?.last_name ?? "");
  }, [user?.first_name, user?.last_name]);
  useEffect(() => {
    if (tab !== "runtimes") return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<{ results: Runtime[]; count: number }>(
      `runtimes/?limit=20&offset=${runtimeOffset}`,
      { signal: controller.signal },
    )
      .then((data) => {
        setRuntimes(data.results);
        setRuntimeCount(data.count);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [tab, runtimeOffset, revision]);

  function switchTab(value: string) {
    setTab(value);
    setError("");
    setNotice("");
  }
  async function saveProfile(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    setSaving(true);
    try {
      await api("profile/", {
        method: "POST",
        body: { first_name: firstName.trim(), last_name: lastName.trim() },
      });
      await refresh();
      setNotice("Profile updated.");
    } catch (error) {
      setError(message(error));
    } finally {
      setSaving(false);
    }
  }
  async function changePassword(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (newPassword !== confirmPassword) {
      setError("The new passwords do not match.");
      return;
    }
    setSaving(true);
    try {
      await api("password/", {
        method: "POST",
        body: { old_password: oldPassword, new_password: newPassword },
      });
      await refresh();
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setNotice("Password changed. Your current session remains signed in.");
    } catch (error) {
      setError(message(error));
    } finally {
      setSaving(false);
    }
  }
  function openEditor(runtime?: Runtime) {
    setFormError("");
    setEditor(runtime ?? "new");
    setForm(
      runtime
        ? {
            name: runtime.name,
            owner: runtime.owner?.toString() ?? "",
            profile: runtime.profile,
            base_url: runtime.base_url,
            secret_ref: "",
            models: runtime.allowed_models.join("\n"),
            enabled: runtime.enabled,
            capabilities: { ...capabilities, ...runtime.capabilities },
          }
        : {
            ...blankRuntime,
            owner: user?.id.toString() ?? "",
            capabilities: { ...capabilities },
          },
    );
  }
  async function saveRuntime(event: FormEvent) {
    event.preventDefault();
    setFormError("");
    const models = form.models.split(/[\s,]+/).filter(Boolean);
    if (
      !models.length ||
      models.some((model) => model.length > 128) ||
      new Set(models).size !== models.length
    ) {
      setFormError(
        "Enter at least one unique model name, up to 128 characters each.",
      );
      return;
    }
    const editing = editor !== "new" && editor !== null;
    if (!editing && form.profile !== "ollama" && !form.secret_ref.trim()) {
      setFormError("Cloud runtimes require a server-side secret alias.");
      return;
    }
    if (
      form.secret_ref &&
      !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(form.secret_ref)
    ) {
      setFormError(
        "Enter a secret alias, not an API key. Use letters, numbers, periods, underscores or hyphens.",
      );
      return;
    }
    const owner = form.owner ? Number(form.owner) : null;
    if (owner !== null && (!Number.isSafeInteger(owner) || owner < 1)) {
      setFormError("The owner must be a positive user ID.");
      return;
    }
    const body: Record<string, unknown> = {
      name: form.name.trim(),
      owner,
      profile: form.profile,
      base_url: form.base_url.trim(),
      allowed_models: models,
      enabled: form.enabled,
      capabilities: form.capabilities,
    };
    if (form.secret_ref.trim()) body.secret_ref = form.secret_ref.trim();
    else if (!editing) body.secret_ref = null;
    setSaving(true);
    try {
      await api(editing ? `runtimes/${editor.id}/` : "runtimes/", {
        method: editing ? "PATCH" : "POST",
        body,
      });
      setEditor(null);
      setNotice(editing ? "Runtime updated." : "Runtime created.");
      setRevision((value) => value + 1);
    } catch (error) {
      setFormError(message(error));
    } finally {
      setSaving(false);
    }
  }
  async function removeRuntime() {
    if (!deleting) return;
    setSaving(true);
    setFormError("");
    try {
      await api(`runtimes/${deleting.id}/`, { method: "DELETE" });
      setDeleting(null);
      setNotice("Runtime deleted.");
      setRevision((value) => value + 1);
    } catch (error) {
      setFormError(message(error));
    } finally {
      setSaving(false);
    }
  }
  function setCapability(
    key: keyof Omit<Capabilities, "token_limit_parameter">,
    value: boolean,
  ) {
    setForm((previous) => ({
      ...previous,
      capabilities: {
        ...previous.capabilities,
        [key]: value,
        ...(key === "strict_tools" && value ? { tools: true } : {}),
        ...(key === "tools" && !value ? { strict_tools: false } : {}),
      },
    }));
  }

  return (
    <div className="as-page">
      <PageHeader
        title="Settings"
        description="Manage your profile, account security and approved model connections."
      />
      <div className="as-tabs" role="tablist" aria-label="Settings sections">
        {[
          { id: "profile", label: "Profile", icon: UserRound },
          { id: "security", label: "Security", icon: LockKeyhole },
          { id: "runtimes", label: "LLM runtimes", icon: Server },
        ].map((item) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            aria-controls={`settings-${item.id}`}
            className={tab === item.id ? "active" : ""}
            onClick={() => switchTab(item.id)}
          >
            <item.icon size={17} />
            {item.label}
          </button>
        ))}
      </div>
      {notice && (
        <div className="as-success" role="status">
          <CheckCircle2 size={17} />
          {notice}
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      {tab === "profile" && (
        <section
          id="settings-profile"
          role="tabpanel"
          className="as-settings-grid"
        >
          <Card>
            <div className="as-card-title">
              <UserRound size={20} />
              <h2>Your profile</h2>
            </div>
            <form className="as-form" onSubmit={saveProfile}>
              <div className="as-profile-heading">
                <span className="as-profile-avatar">
                  {(user?.first_name || user?.username || "A")
                    .slice(0, 1)
                    .toUpperCase()}
                </span>
                <div>
                  <strong>
                    {[user?.first_name, user?.last_name]
                      .filter(Boolean)
                      .join(" ") || user?.username}
                  </strong>
                  <small>
                    {user?.is_staff
                      ? "Workspace administrator"
                      : "Workspace member"}
                  </small>
                </div>
              </div>
              <Field label="Username">
                <input
                  className="as-input"
                  value={user?.username ?? ""}
                  readOnly
                />
              </Field>
              <div className="as-form-row">
                <Field label="First name">
                  <input
                    className="as-input"
                    autoComplete="given-name"
                    maxLength={150}
                    value={firstName}
                    onChange={(event) => setFirstName(event.target.value)}
                  />
                </Field>
                <Field label="Last name">
                  <input
                    className="as-input"
                    autoComplete="family-name"
                    maxLength={150}
                    value={lastName}
                    onChange={(event) => setLastName(event.target.value)}
                  />
                </Field>
              </div>
              <div>
                <Button type="submit" variant="primary" disabled={saving}>
                  {saving ? "Saving…" : "Save profile"}
                </Button>
              </div>
            </form>
          </Card>
          <Card>
            <div className="as-card-title">
              <ShieldCheck size={20} />
              <h2>Workspace access</h2>
            </div>
            <p className="as-copy">
              Your session is protected with a browser-only session cookie.
              Agent credentials stay on the server.
            </p>
            <dl className="as-definition-list">
              <div>
                <dt>Role</dt>
                <dd>{user?.is_staff ? "Administrator" : "Member"}</dd>
              </div>
              <div>
                <dt>Billing environment</dt>
                <dd>{session?.billing?.mode ?? "Unavailable"}</dd>
              </div>
              <div>
                <dt>Access scope</dt>
                <dd>
                  {user?.is_staff
                    ? "Administrative resource access"
                    : "Your own resources"}
                </dd>
              </div>
            </dl>
            {user?.is_staff && (
              <a className="as-text-link" href="/admin/auth/user/">
                Manage user accounts in admin →
              </a>
            )}
            <p className="as-note">
              Contact your administrator to provision accounts or change
              workspace permissions.
            </p>
          </Card>
        </section>
      )}
      {tab === "security" && (
        <section
          id="settings-security"
          role="tabpanel"
          className="as-settings-grid"
        >
          <Card>
            <div className="as-card-title">
              <KeyRound size={20} />
              <h2>Change password</h2>
            </div>
            <form className="as-form" onSubmit={changePassword}>
              <Field label="Current password">
                <input
                  className="as-input"
                  type="password"
                  required
                  autoComplete="current-password"
                  value={oldPassword}
                  onChange={(event) => setOldPassword(event.target.value)}
                />
              </Field>
              <Field label="New password">
                <input
                  className="as-input"
                  type="password"
                  required
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                />
              </Field>
              <Field label="Confirm new password">
                <input
                  className="as-input"
                  type="password"
                  required
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                />
              </Field>
              <p className="as-note">
                Use a unique password. The server checks the configured password
                policy before saving.
              </p>
              <div>
                <Button type="submit" variant="primary" disabled={saving}>
                  {saving ? "Updating…" : "Update password"}
                </Button>
              </div>
            </form>
          </Card>
          <Card>
            <div className="as-card-title">
              <ShieldCheck size={20} />
              <h2>Credential handling</h2>
            </div>
            <div className="as-security-points">
              <p>
                <CheckCircle2 size={18} />
                API tokens are not stored in browser storage.
              </p>
              <p>
                <CheckCircle2 size={18} />
                Changes require a valid CSRF-protected session.
              </p>
              <p>
                <CheckCircle2 size={18} />
                Provider keys are resolved by server-side secret aliases.
              </p>
            </div>
            <p className="as-note">
              Never paste model provider keys into agent instructions, tool
              arguments or runtime names.
            </p>
          </Card>
        </section>
      )}
      {tab === "runtimes" && (
        <section id="settings-runtimes" role="tabpanel">
          <Card>
            <div className="as-card-heading">
              <div>
                <h2>Model connections</h2>
                <p>
                  Approved endpoints and explicitly verified model capabilities.
                </p>
              </div>
              {user?.is_staff && (
                <Button variant="primary" onClick={() => openEditor()}>
                  <Plus size={16} /> Add runtime
                </Button>
              )}
            </div>
            {loading ? (
              <div className="as-loading">
                <Spinner />
              </div>
            ) : error ? null : runtimes.length ? (
              <div className="as-table-wrap">
                <table className="as-data-table">
                  <thead>
                    <tr>
                      <th>Runtime</th>
                      <th>Profile</th>
                      <th>Models</th>
                      <th>Status</th>
                      <th>Revision</th>
                      {user?.is_staff && (
                        <th>
                          <span className="as-sr-only">Actions</span>
                        </th>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {runtimes.map((runtime) => (
                      <tr key={runtime.id}>
                        <td>
                          <strong>{runtime.name}</strong>
                          <small className="as-table-detail">
                            {runtime.base_url}
                          </small>
                        </td>
                        <td>
                          {
                            {
                              openai: "OpenAI",
                              ollama: "Ollama",
                              openai_compatible: "OpenAI compatible",
                            }[runtime.profile]
                          }
                        </td>
                        <td>{runtime.allowed_models.join(", ")}</td>
                        <td>
                          <Badge
                            status={runtime.enabled ? "active" : "disabled"}
                          />
                        </td>
                        <td>v{runtime.revision}</td>
                        {user?.is_staff && (
                          <td>
                            <div className="as-table-actions">
                              <button
                                className="as-icon-button"
                                aria-label={`Edit ${runtime.name}`}
                                onClick={() => openEditor(runtime)}
                              >
                                <Pencil size={16} />
                              </button>
                              <button
                                className="as-icon-button as-destructive"
                                aria-label={`Delete ${runtime.name}`}
                                onClick={() => {
                                  setDeleting(runtime);
                                  setFormError("");
                                }}
                              >
                                <Trash2 size={16} />
                              </button>
                            </div>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                icon={Server}
                title="No managed runtimes yet"
                description={
                  user?.is_staff
                    ? "Add an approved endpoint and a server-side secret alias to connect a model."
                    : "Your administrator can assign a model connection to your account."
                }
              />
            )}
            <div className="as-pagination">
              <span>
                {error
                  ? "List unavailable"
                  : `${runtimeCount} runtime${runtimeCount === 1 ? "" : "s"}`}
              </span>
              <div>
                <Button
                  variant="secondary"
                  disabled={runtimeOffset === 0 || loading}
                  onClick={() =>
                    setRuntimeOffset((value) => Math.max(0, value - 20))
                  }
                >
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  disabled={runtimeOffset + 20 >= runtimeCount || loading}
                  onClick={() => setRuntimeOffset((value) => value + 20)}
                >
                  Next
                </Button>
              </div>
            </div>
          </Card>
          <div className="as-info-banner">
            <ShieldCheck size={19} />
            <p>
              Provider keys are never shown here. Endpoint allowlists and secret
              bindings are configured by the server operator. Changing or
              disabling a runtime affects subsequent calls, including resumed
              workflows.
            </p>
          </div>
        </section>
      )}
      {editor && (
        <Modal
          title={editor === "new" ? "Add model runtime" : "Edit model runtime"}
          onClose={() => {
            if (!saving) setEditor(null);
          }}
          wide
        >
          <form className="as-form" onSubmit={saveRuntime}>
            {formError && <ErrorNotice message={formError} />}
            <div className="as-form-row">
              <Field label="Runtime name">
                <input
                  className="as-input"
                  required
                  maxLength={128}
                  value={form.name}
                  onChange={(event) =>
                    setForm({ ...form, name: event.target.value })
                  }
                  autoFocus
                  placeholder="Production model endpoint"
                />
              </Field>
              <Field
                label="Owner user ID"
                hint="The runtime must belong to the agent’s owner."
              >
                <input
                  className="as-input"
                  type="number"
                  min={1}
                  step={1}
                  value={form.owner}
                  onChange={(event) =>
                    setForm({ ...form, owner: event.target.value })
                  }
                />
              </Field>
            </div>
            <div className="as-form-row">
              <Field label="Provider profile">
                <select
                  className="as-select"
                  value={form.profile}
                  onChange={(event) => {
                    const profile = event.target.value as Runtime["profile"];
                    setForm({
                      ...form,
                      profile,
                      capabilities: {
                        ...form.capabilities,
                        token_limit_parameter:
                          profile === "openai"
                            ? "max_completion_tokens"
                            : "max_tokens",
                      },
                    });
                  }}
                >
                  <option value="openai">OpenAI</option>
                  <option value="ollama">Ollama</option>
                  <option value="openai_compatible">OpenAI compatible</option>
                </select>
              </Field>
              <Field label="Status">
                <select
                  className="as-select"
                  value={form.enabled ? "enabled" : "disabled"}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      enabled: event.target.value === "enabled",
                    })
                  }
                >
                  <option value="enabled">Enabled</option>
                  <option value="disabled">Disabled</option>
                </select>
              </Field>
            </div>
            <Field
              label="API base URL"
              hint="The endpoint must be approved by the server’s URL policy."
            >
              <input
                className="as-input"
                type="url"
                required
                maxLength={2048}
                value={form.base_url}
                onChange={(event) =>
                  setForm({ ...form, base_url: event.target.value })
                }
                placeholder="https://model.example.com/v1"
              />
            </Field>
            <Field
              label="Secret alias"
              hint={
                editor === "new"
                  ? "A configured server-side alias, never an API key."
                  : "Leave blank to keep the existing secret binding."
              }
            >
              <input
                className="as-input"
                value={form.secret_ref}
                maxLength={128}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) =>
                  setForm({ ...form, secret_ref: event.target.value })
                }
                placeholder="production-model"
              />
            </Field>
            <Field
              label="Allowed models"
              hint="Exact, unique model identifiers, separated by commas or new lines."
            >
              <textarea
                className="as-textarea as-monospace"
                rows={3}
                required
                value={form.models}
                onChange={(event) =>
                  setForm({ ...form, models: event.target.value })
                }
                placeholder="model-version-1"
              />
            </Field>
            <fieldset className="as-fieldset">
              <legend>Verified capabilities</legend>
              <p className="as-note">
                Enable a feature only after testing this exact provider and
                model.
              </p>
              <div className="as-capability-grid">
                {(
                  [
                    { key: "tools", label: "Tool calls" },
                    { key: "structured_outputs", label: "Structured output" },
                    { key: "streaming", label: "Streaming" },
                    { key: "usage", label: "Token usage" },
                    { key: "strict_tools", label: "Strict tool schemas" },
                    { key: "temperature", label: "Temperature" },
                  ] as const
                ).map((item) => (
                  <label key={item.key}>
                    <input
                      type="checkbox"
                      checked={form.capabilities[item.key]}
                      onChange={(event) =>
                        setCapability(item.key, event.target.checked)
                      }
                    />
                    {item.label}
                  </label>
                ))}
              </div>
            </fieldset>
            <Field label="Output token limit parameter">
              <select
                className="as-select"
                value={form.capabilities.token_limit_parameter}
                onChange={(event) =>
                  setForm({
                    ...form,
                    capabilities: {
                      ...form.capabilities,
                      token_limit_parameter: event.target
                        .value as Capabilities["token_limit_parameter"],
                    },
                  })
                }
              >
                <option value="max_completion_tokens">
                  max_completion_tokens
                </option>
                <option value="max_tokens">max_tokens</option>
              </select>
            </Field>
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
                {saving ? "Saving…" : "Save runtime"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {deleting && (
        <Modal
          title="Delete runtime?"
          onClose={() => {
            if (!saving) setDeleting(null);
          }}
        >
          <div className="as-form">
            <p>
              Delete <strong>{deleting.name}</strong>? A runtime assigned to an
              agent is protected and cannot be deleted.
            </p>
            {formError && <ErrorNotice message={formError} />}
            <div className="as-modal-actions">
              <Button
                variant="secondary"
                disabled={saving}
                onClick={() => setDeleting(null)}
              >
                Keep runtime
              </Button>
              <Button
                variant="danger"
                disabled={saving}
                onClick={removeRuntime}
              >
                {saving ? "Deleting…" : "Delete runtime"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
