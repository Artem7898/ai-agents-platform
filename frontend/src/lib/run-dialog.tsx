import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { ArrowUpRight, Play } from "lucide-react";
import { api, ApiError } from "./api";
import type { Paginated, Workflow } from "./types";
import {
  Button,
  EmptyState,
  ErrorNotice,
  Field,
  Modal,
  Spinner,
} from "../components/ui";

const RunDialogContext = createContext<{
  open: (workflowId?: string) => void;
} | null>(null);
export function NewRunProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [visible, setVisible] = useState(false);
  const [selected, setSelected] = useState("");
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [input, setInput] = useState('{\n  "prompt": ""\n}');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unknown, setUnknown] = useState(false);
  const key = useRef("");
  const payload = useRef<{
    workflow_id: string;
    input_data: Record<string, unknown>;
  } | null>(null);
  const open = (id?: string) => {
    key.current = crypto.randomUUID();
    payload.current = null;
    setSelected(id ?? "");
    setError("");
    setUnknown(false);
    setInput('{\n  "prompt": ""\n}');
    setVisible(true);
  };
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    setLoading(true);
    api<Paginated<Workflow>>("workflows/?limit=200", {
      signal: controller.signal,
    })
      .then(async (value) => {
        let rows = value.results;
        if (selected && !rows.some((row) => row.id === selected)) {
          const current = await api<Workflow>(`workflows/${selected}/`, {
            signal: controller.signal,
          });
          rows = [current, ...rows];
        }
        setWorkflows(rows);
      })
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
    // The selected workflow is the dialog's initial selection; changing it needs no request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (!unknown) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(input);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
          throw new Error();
      } catch {
        setError("Input must be a valid JSON object.");
        return;
      }
      if (!selected) {
        setError("Choose a workflow first.");
        return;
      }
      payload.current = {
        workflow_id: selected,
        input_data: parsed as Record<string, unknown>,
      };
    }
    setBusy(true);
    try {
      const result = await api<{ run_id: string; status: string }>("execute/", {
        method: "POST",
        body: payload.current,
        headers: { "Idempotency-Key": key.current },
      });
      setVisible(false);
      navigate(`/runs/${result.run_id}`);
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Unable to queue this run.",
      );
      if (
        error instanceof ApiError &&
        (error.status === 0 || error.status >= 500)
      )
        setUnknown(true);
      else {
        setUnknown(false);
        key.current = crypto.randomUUID();
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <RunDialogContext.Provider value={{ open }}>
      {children}
      {visible && (
        <Modal
          title="Start a new run"
          onClose={() => {
            if (!busy) setVisible(false);
          }}
        >
          <p className="modal-description">
            Run a saved workflow with a new input. Execution starts when a
            worker picks up the command.
          </p>
          {loading ? (
            <div className="loading-block">
              <Spinner />
            </div>
          ) : workflows.length === 0 ? (
            <EmptyState
              icon={Play}
              title="Create your first workflow"
              description="A saved workflow is needed before starting a run."
              action={
                <Button
                  variant="primary"
                  onClick={() => {
                    setVisible(false);
                    navigate("/workflows");
                  }}
                >
                  Open workflow builder <ArrowUpRight size={16} />
                </Button>
              }
            />
          ) : (
            <form onSubmit={submit}>
              <ErrorNotice message={error} />
              {unknown && (
                <div className="notice">
                  The response was interrupted. Retrying sends the same command
                  safely. You can also check Runs before starting another
                  request.
                </div>
              )}
              <Field label="Workflow">
                <select
                  value={selected}
                  onChange={(event) => setSelected(event.target.value)}
                  disabled={busy || unknown}
                  required
                >
                  <option value="">Select a workflow</option>
                  {workflows.map((workflow) => (
                    <option value={workflow.id} key={workflow.id}>
                      {workflow.name} · v{workflow.version}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Input JSON"
                hint="Only a JSON object is accepted. Keep API keys and credentials out of workflow inputs."
              >
                <textarea
                  className="code-input"
                  rows={7}
                  value={input}
                  disabled={busy || unknown}
                  onChange={(event) => setInput(event.target.value)}
                  spellCheck={false}
                />
              </Field>
              <div className="modal-actions">
                <Button disabled={busy} onClick={() => setVisible(false)}>
                  Cancel
                </Button>
                <Button variant="primary" type="submit" disabled={busy}>
                  {busy ? <Spinner /> : <Play size={16} />}
                  {unknown ? "Retry request" : "Start run"}
                </Button>
              </div>
            </form>
          )}
        </Modal>
      )}
    </RunDialogContext.Provider>
  );
}
export function useNewRun() {
  const value = useContext(RunDialogContext);
  if (!value) throw new Error("NewRunProvider is required");
  return value;
}
