import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight,
  Check,
  CheckCheck,
  ClipboardCheck,
  FileText,
  Info,
  Pencil,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  Field,
  PageHeader,
  Spinner,
} from "../components/ui";
import { api, ApiError } from "../lib/api";
import "./triage.css";

export type TriageCategory =
  "Facturación" | "Acceso" | "Soporte técnico" | "Otros";

export interface TriageRun {
  id: string;
  status: string;
  ticket_id: string;
  text: string;
  classification: { category: TriageCategory; reason: string } | null;
  confirmation: {
    approved: boolean;
    category: TriageCategory;
    note: string;
  } | null;
  waiting_for_approval: boolean;
  created_at: string;
  updated_at: string;
  receipt: Record<string, unknown> | null;
}

export interface TriageRunsResponse {
  enabled: boolean;
  categories: TriageCategory[];
  results: TriageRun[];
}

interface DecisionDraft {
  correcting: boolean;
  category: TriageCategory;
  note: string;
  error: string;
  submitted: boolean;
}

const POLL_DELAY_MS = 3000;
const MAX_POLLS = 40;
const terminalStatuses = new Set([
  "succeeded",
  "completed",
  "failed",
  "cancelled",
  "canceled",
  "expired",
]);
const statusLabels: Record<string, string> = {
  pending: "En cola",
  created: "Creada",
  queued: "En cola",
  running: "En curso",
  waiting: "Pendiente de revisión",
  waiting_for_approval: "Pendiente de revisión",
  waiting_approval: "Pendiente de revisión",
  waiting_human: "En revisión humana",
  paused: "En pausa",
  succeeded: "Completada",
  completed: "Completada",
  failed: "Fallida",
  cancelled: "Cancelada",
  canceled: "Cancelada",
  expired: "Caducada",
};

function statusLabel(run: TriageRun) {
  if (run.waiting_for_approval) return "Pendiente de revisión";
  return statusLabels[run.status] ?? run.status;
}

function requestError(error: unknown, saving: boolean) {
  if (error instanceof ApiError) {
    if (error.status === 0)
      return "No se pudo conectar con el servidor. Comprueba tu conexión y vuelve a intentarlo.";
    if (error.status === 401 || error.status === 403)
      return "Tu sesión o tus permisos no permiten esta acción. Vuelve a iniciar sesión si es necesario.";
    if (error.status === 409)
      return "La solicitud cambió o ya recibió una decisión. Actualiza para comprobar su estado.";
  }
  return saving
    ? "No se pudo guardar la decisión. Tus cambios se conservan; puedes volver a intentarlo."
    : "No se pudieron actualizar las solicitudes. Vuelve a intentarlo.";
}

const receiptLabels: Record<string, string> = {
  id: "Identificador del recibo",
  receipt_id: "Identificador del recibo",
  idempotency_key: "Clave de guardado",
  payload_hash: "Huella de la decisión",
  ticket_id: "Solicitud",
  run_id: "Ejecución",
  category: "Categoría definitiva",
  final_category: "Categoría definitiva",
  note: "Nota del operador",
  operator_note: "Nota del operador",
  approved: "Confirmada",
  saved_at: "Fecha de guardado",
  created_at: "Fecha de creación",
  saved: "Guardado",
};

function receiptValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (value === true) return "Sí";
  if (value === false) return "No";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export default function Triage() {
  const [data, setData] = useState<TriageRunsResponse | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [refreshCycle, setRefreshCycle] = useState(0);
  const [pollStopped, setPollStopped] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, DecisionDraft>>({});
  const [busyRuns, setBusyRuns] = useState<Set<string>>(new Set());
  const pending = useRef(new Map<string, AbortController>());
  const requestKeys = useRef(
    new Map<string, { payload: string; key: string }>(),
  );
  const mounted = useRef(true);
  const correctionSelect = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    mounted.current = true;
    const requests = pending.current;
    return () => {
      mounted.current = false;
      requests.forEach((controller) => controller.abort());
      requests.clear();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setPollStopped(false);

    const load = async () => {
      if (!active) return;
      setRefreshing(true);
      let keepPolling = true;
      try {
        const response = await api<TriageRunsResponse>("triage/runs/", {
          signal: controller.signal,
        });
        if (!active) return;
        setData(response);
        setSelectedId((current) =>
          response.results.some((run) => run.id === current)
            ? current
            : (response.results[0]?.id ?? ""),
        );
        setLoadError("");
        keepPolling =
          response.enabled &&
          response.results.some(
            (run) => !run.receipt && !terminalStatuses.has(run.status),
          );
      } catch (error) {
        if (!active || controller.signal.aborted) return;
        setLoadError(requestError(error, false));
      } finally {
        if (active) {
          setLoading(false);
          setRefreshing(false);
          polls += 1;
          if (keepPolling && polls < MAX_POLLS)
            timer = setTimeout(load, POLL_DELAY_MS);
          else if (keepPolling) setPollStopped(true);
        }
      }
    };
    void load();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [refreshCycle]);

  const selected = data?.results.find((run) => run.id === selectedId);
  const draft = selected
    ? (drafts[selected.id] ?? {
        correcting: false,
        category: selected.classification?.category ?? "Otros",
        note: "",
        error: "",
        submitted: false,
      })
    : null;
  const correcting = draft?.correcting ?? false;

  useEffect(() => {
    if (correcting) correctionSelect.current?.focus();
  }, [correcting, selectedId]);

  const updateDraft = (change: Partial<DecisionDraft>) => {
    if (!selected || !draft) return;
    setDrafts((current) => ({
      ...current,
      [selected.id]: { ...draft, ...change },
    }));
  };

  const submitDecision = async (event?: FormEvent<HTMLFormElement>) => {
    event?.preventDefault();
    if (
      !selected ||
      !draft ||
      !data?.enabled ||
      !selected.waiting_for_approval ||
      !selected.classification ||
      selected.confirmation ||
      draft.submitted ||
      pending.current.has(selected.id)
    )
      return;
    const category = draft.correcting
      ? draft.category
      : selected.classification.category;
    if (!data.categories.includes(category)) {
      updateDraft({ error: "Selecciona una categoría válida." });
      return;
    }
    const id = selected.id;
    const body = {
      approved: true,
      category,
      note: draft.correcting ? draft.note.trim() : "",
    };
    const payload = JSON.stringify(body);
    let request = requestKeys.current.get(id);
    if (!request || request.payload !== payload) {
      request = { payload, key: crypto.randomUUID() };
      requestKeys.current.set(id, request);
    }
    const controller = new AbortController();
    pending.current.set(id, controller);
    setBusyRuns((current) => new Set(current).add(id));
    setDrafts((current) => ({ ...current, [id]: { ...draft, error: "" } }));
    try {
      await api<{ run_id: string; status: string }>(
        `triage/runs/${encodeURIComponent(id)}/confirm/`,
        {
          method: "POST",
          body,
          headers: { "Idempotency-Key": request.key },
          signal: controller.signal,
        },
      );
      if (!mounted.current || controller.signal.aborted) return;
      setDrafts((current) => ({
        ...current,
        [id]: { ...current[id], submitted: true, error: "" },
      }));
      setRefreshCycle((current) => current + 1);
    } catch (error) {
      if (!mounted.current || controller.signal.aborted) return;
      setDrafts((current) => ({
        ...current,
        [id]: { ...current[id], error: requestError(error, true) },
      }));
    } finally {
      pending.current.delete(id);
      if (mounted.current)
        setBusyRuns((current) => {
          const next = new Set(current);
          next.delete(id);
          return next;
        });
    }
  };

  const busy = selected ? busyRuns.has(selected.id) : false;
  const canReview = Boolean(
    selected?.waiting_for_approval &&
    selected.classification &&
    !selected.confirmation &&
    !selected.receipt &&
    !draft?.submitted,
  );

  return (
    <div className="triage-page" lang="es">
      <PageHeader
        title="Revisar solicitudes"
        description="Confirma la categoría propuesta o corrígela antes de guardarla."
        actions={
          <Button
            onClick={() => setRefreshCycle((current) => current + 1)}
            disabled={refreshing}
            aria-label="Actualizar solicitudes"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Actualizar
          </Button>
        }
      />
      <ErrorNotice message={loadError} />
      {loading ? (
        <Card className="triage-loading">
          <Spinner />
          <span>Cargando solicitudes…</span>
        </Card>
      ) : data && !data.enabled ? (
        <Card>
          <EmptyState
            icon={Info}
            title="La demostración de solicitudes está desactivada"
            description="El administrador debe activar la demostración para que aparezcan las solicitudes y sus decisiones."
          />
        </Card>
      ) : data && data.results.length === 0 ? (
        <Card>
          <EmptyState
            icon={ClipboardCheck}
            title="No hay solicitudes para revisar"
            description="Aquí aparecerán tus ejecuciones de clasificación. Actualiza la lista cuando se haya iniciado una solicitud ficticia."
          />
        </Card>
      ) : selected && draft ? (
        <>
          <div className="triage-demo-note">
            <Info size={18} aria-hidden="true" />
            <p>
              <strong>Demostración con solicitudes ficticias.</strong> El modelo
              propone una categoría; tú decides cuál se guarda.
            </p>
          </div>
          <div className="triage-layout">
            <Card className="triage-inbox">
              <div className="triage-section-heading">
                <h3>Solicitudes</h3>
                <span>{data!.results.length}</span>
              </div>
              <p className="triage-inbox-description">
                Tus últimas 50 ejecuciones como máximo
              </p>
              <ul
                className="triage-run-list"
                aria-label="Solicitudes disponibles"
              >
                {data!.results.map((run) => (
                  <li key={run.id}>
                    <button
                      className={`triage-run ${selectedId === run.id ? "is-selected" : ""}`}
                      onClick={() => setSelectedId(run.id)}
                      aria-pressed={selectedId === run.id}
                    >
                      <span className="triage-run-top">
                        <strong>
                          {run.ticket_id || "Solicitud sin referencia"}
                        </strong>
                        {run.receipt ? (
                          <CheckCheck
                            size={17}
                            aria-label="Con recibo guardado"
                          />
                        ) : (
                          <ArrowRight size={16} aria-hidden="true" />
                        )}
                      </span>
                      <span className="triage-run-preview">{run.text}</span>
                      <span className="triage-run-bottom">
                        <span>{statusLabel(run)}</span>
                        {run.classification && (
                          <span>{run.classification.category}</span>
                        )}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
            <div className="triage-detail">
              <Card className="triage-request-card">
                <div className="triage-detail-heading">
                  <div>
                    <span className="triage-eyebrow">ENTRADA FICTICIA</span>
                    <h3>{selected.ticket_id || "Solicitud sin referencia"}</h3>
                  </div>
                  <Badge status={selected.status}>
                    {statusLabel(selected)}
                  </Badge>
                </div>
                <p className="triage-request-text">{selected.text}</p>
                <dl className="triage-run-metadata">
                  <div>
                    <dt>ID de ejecución</dt>
                    <dd>
                      <code>{selected.id}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Estado</dt>
                    <dd>
                      <code>{selected.status}</code>
                    </dd>
                  </div>
                </dl>
              </Card>
              <Card className="triage-recommendation-card">
                <div className="triage-icon-heading">
                  <span className="triage-step-icon">
                    <Sparkles size={20} aria-hidden="true" />
                  </span>
                  <div>
                    <span className="triage-eyebrow">PROPUESTA DEL MODELO</span>
                    <h3>Recomendación</h3>
                  </div>
                </div>
                {selected.classification ? (
                  <>
                    <span className="triage-category">
                      {selected.classification.category}
                    </span>
                    <p className="triage-reason">
                      {selected.classification.reason}
                    </p>
                  </>
                ) : (
                  <p className="triage-copy">
                    La clasificación aún no está disponible. El estado se
                    actualizará automáticamente.
                  </p>
                )}
              </Card>
              <Card className="triage-decision-card">
                <div className="triage-icon-heading">
                  <span className="triage-step-icon triage-human-icon">
                    <ClipboardCheck size={20} aria-hidden="true" />
                  </span>
                  <div>
                    <span className="triage-eyebrow">REVISIÓN HUMANA</span>
                    <h3>Tu decisión</h3>
                  </div>
                </div>
                {canReview ? (
                  <form
                    className="triage-decision-form"
                    onSubmit={submitDecision}
                  >
                    <ErrorNotice message={draft.error} />
                    {draft.correcting ? (
                      <>
                        <Field label="Categoría definitiva">
                          <select
                            ref={correctionSelect}
                            value={draft.category}
                            onChange={(event) =>
                              updateDraft({
                                category: event.target.value as TriageCategory,
                              })
                            }
                            disabled={busy}
                            required
                          >
                            {data!.categories.map((category) => (
                              <option key={category} value={category}>
                                {category}
                              </option>
                            ))}
                          </select>
                        </Field>
                        <Field
                          label="Motivo de la corrección"
                          hint="Opcional. Esta nota se enviará con tu decisión."
                        >
                          <textarea
                            value={draft.note}
                            onChange={(event) =>
                              updateDraft({ note: event.target.value })
                            }
                            disabled={busy}
                            maxLength={1000}
                            rows={3}
                          />
                        </Field>
                        <div className="triage-actions">
                          <Button
                            type="submit"
                            variant="primary"
                            disabled={busy}
                          >
                            {busy ? (
                              <Spinner />
                            ) : (
                              <Check size={17} aria-hidden="true" />
                            )}
                            {busy ? "Enviando…" : "Guardar corrección"}
                          </Button>
                          <Button
                            onClick={() =>
                              updateDraft({ correcting: false, error: "" })
                            }
                            disabled={busy}
                          >
                            Cancelar
                          </Button>
                        </div>
                      </>
                    ) : (
                      <>
                        <p className="triage-copy">
                          ¿Es correcta la categoría propuesta?
                        </p>
                        <div className="triage-actions">
                          <Button
                            type="submit"
                            variant="primary"
                            disabled={busy}
                          >
                            {busy ? (
                              <Spinner />
                            ) : (
                              <Check size={17} aria-hidden="true" />
                            )}
                            {busy ? "Enviando…" : "Confirmar"}
                          </Button>
                          <Button
                            onClick={() =>
                              updateDraft({ correcting: true, error: "" })
                            }
                            disabled={busy}
                          >
                            <Pencil size={16} aria-hidden="true" />
                            Corregir
                          </Button>
                        </div>
                      </>
                    )}
                  </form>
                ) : selected.confirmation || draft.submitted ? (
                  <div className="triage-submitted" role="status">
                    <Check size={19} aria-hidden="true" />
                    <div>
                      <strong>
                        {selected.receipt
                          ? "Decisión registrada"
                          : "Decisión recibida"}
                      </strong>
                      <p>
                        {selected.receipt
                          ? "El recibo guardado está disponible a continuación."
                          : "Esperando el recibo guardado por el servidor."}
                      </p>
                      {selected.confirmation && (
                        <p>
                          Categoría definitiva: {selected.confirmation.category}
                        </p>
                      )}
                      {selected.confirmation?.note && (
                        <p>Nota: {selected.confirmation.note}</p>
                      )}
                    </div>
                  </div>
                ) : (
                  <p className="triage-copy">
                    {terminalStatuses.has(selected.status)
                      ? "Esta ejecución no tiene una revisión pendiente."
                      : "La revisión estará disponible cuando el modelo termine de clasificar la solicitud."}
                  </p>
                )}
              </Card>
              <Card className="triage-receipt-card">
                <div className="triage-icon-heading">
                  <span
                    className={`triage-step-icon ${selected.receipt ? "triage-saved-icon" : "triage-empty-icon"}`}
                  >
                    <FileText size={20} aria-hidden="true" />
                  </span>
                  <div>
                    <span className="triage-eyebrow">RESULTADO PERSISTIDO</span>
                    <h3>
                      {selected.receipt
                        ? "Recibo guardado"
                        : "Recibo pendiente"}
                    </h3>
                  </div>
                </div>
                {selected.receipt ? (
                  <dl className="triage-receipt">
                    {Object.entries(selected.receipt).map(([key, value]) => (
                      <div key={key}>
                        <dt>
                          {receiptLabels[key] ?? key.replaceAll("_", " ")}
                        </dt>
                        <dd>{receiptValue(value)}</dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="triage-copy">
                    El recibo aparecerá aquí cuando el servidor complete y
                    guarde la decisión.
                  </p>
                )}
              </Card>
            </div>
          </div>
          {pollStopped && (
            <p className="triage-poll-note" role="status">
              Se pausó la actualización automática. Pulsa «Actualizar» para
              consultar el estado más reciente.
            </p>
          )}
        </>
      ) : null}
    </div>
  );
}
