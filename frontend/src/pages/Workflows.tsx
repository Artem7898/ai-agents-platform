import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBlocker, type BlockerFunction } from "react-router-dom";
import {
  ArrowRight,
  Bot,
  Braces,
  Check,
  ChevronRight,
  CircleHelp,
  Copy,
  GitBranch,
  Layers,
  LoaderCircle,
  Network,
  Play,
  Plus,
  RotateCcw,
  Save,
  Search,
  Settings2,
  ShieldCheck,
  Trash2,
  Workflow as WorkflowIcon,
  Wrench,
  X,
} from "lucide-react";
import { api } from "../lib/api";
import { useSession } from "../lib/session";
import { useNewRun } from "../lib/run-dialog";
import {
  Button,
  Card,
  EmptyState,
  ErrorNotice,
  Field,
  Modal,
  PageHeader,
} from "../components/ui";
import "./workflows.css";

type Kind = "llm_call" | "tool_exec" | "condition" | "hitl_approval";
type Predicate = { path: string; op: string; value?: unknown };
type Edge = {
  source: string;
  target: string;
  condition?: Predicate | boolean | null;
};
type Node = {
  id: string;
  kind: Kind;
  agent_id?: string | null;
  tool_id?: string | null;
  output_schema?: { id: string; version: number } | null;
  config: Record<string, unknown>;
};
type WorkflowDefinition = {
  id: string;
  owner: number | null;
  name: string;
  version: number;
  nodes: Node[];
  edges: Edge[];
  limits: Record<string, number>;
  output_node_id: string | null;
  created_at: string;
  updated_at: string;
};
type Draft = Pick<
  WorkflowDefinition,
  "name" | "version" | "nodes" | "edges" | "limits" | "output_node_id"
>;
type Agent = {
  id: string;
  owner: number | null;
  name: string;
  model_name: string;
  status: string;
};
type Tool = {
  id: string;
  name: string;
  description: string;
  parameters_schema: Record<string, unknown>;
};
type Schema = { id: string; version: number; description: string };
type Page<T> = {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
};

const KINDS: Record<
  Kind,
  { label: string; description: string; icon: typeof Bot }
> = {
  llm_call: {
    label: "AI agent",
    description: "Generate an answer with an active agent.",
    icon: Bot,
  },
  tool_exec: {
    label: "Tool",
    description: "Execute one registered server tool.",
    icon: Wrench,
  },
  condition: {
    label: "Condition",
    description: "Choose a branch using a typed predicate.",
    icon: GitBranch,
  },
  hitl_approval: {
    label: "Human approval",
    description: "Pause until a person supplies input.",
    icon: ShieldCheck,
  },
};
const OPS = [
  "eq",
  "ne",
  "gt",
  "gte",
  "lt",
  "lte",
  "in",
  "not_in",
  "truthy",
  "falsy",
  "exists",
];
const LIMITS: Record<string, [number, number]> = {
  max_steps: [1, 10000],
  max_duration_seconds: [Number.MIN_VALUE, 3600],
  max_tool_calls: [0, 1000],
  max_tool_repairs: [0, 5],
  max_output_repairs: [0, 5],
  max_tokens: [1, 1000000],
  max_output_tokens: [1, 32000],
  tool_timeout_seconds: [Number.MIN_VALUE, 300],
};
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The operation could not be completed.";
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const uuid = (value: unknown, version?: 4) =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value,
  ) &&
  (version === undefined ||
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    ));
const emptyDraft = (): Draft => ({
  name: "Untitled workflow",
  version: 1,
  nodes: [],
  edges: [],
  limits: {},
  output_node_id: null,
});
const asDraft = (workflow: WorkflowDefinition): Draft =>
  clone({
    name: workflow.name,
    version: workflow.version,
    nodes: workflow.nodes,
    edges: workflow.edges,
    limits: workflow.limits,
    output_node_id: workflow.output_node_id,
  });
const advanced = (draft: Draft) =>
  pretty({
    nodes: draft.nodes,
    edges: draft.edges,
    limits: draft.limits,
    output_node_id: draft.output_node_id,
  });

function predicateError(value: unknown): string | null {
  if (
    !object(value) ||
    typeof value.path !== "string" ||
    !/^[\p{L}\p{N}_-]+(?:\.[\p{L}\p{N}_-]+)*$/u.test(value.path) ||
    value.path.length > 256 ||
    (value.op !== undefined && typeof value.op !== "string") ||
    !OPS.includes(String(value.op ?? "eq"))
  )
    return "Conditions need a dotted path and a supported operator.";
  if (Object.keys(value).some((key) => !["path", "op", "value"].includes(key)))
    return "A condition accepts only path, op and value.";
  if (
    ["in", "not_in"].includes(String(value.op)) &&
    !Array.isArray(value.value)
  )
    return "Membership conditions require a JSON array value.";
  return null;
}

function assertJsonTree(value: unknown) {
  const pending: [unknown, number][] = [[value, 0]];
  let visited = 0;
  while (pending.length) {
    const [item, depth] = pending.pop()!;
    if (++visited > 100000 || depth > 64)
      throw new Error("JSON is too deeply nested or complex for the editor.");
    if (typeof item === "number" && !Number.isFinite(item))
      throw new Error("JSON numbers must be finite.");
    if (Array.isArray(item))
      item.forEach((child) => pending.push([child, depth + 1]));
    else if (object(item))
      Object.values(item).forEach((child) => pending.push([child, depth + 1]));
  }
}

/** Client feedback complements the server's authoritative schema and access checks. */
export function validateDraft(draft: Draft): string | null {
  if (!draft.name.trim() || draft.name.length > 256)
    return "Enter a workflow name between 1 and 256 characters.";
  if (!Number.isInteger(draft.version) || draft.version < 1)
    return "Version must be a positive integer.";
  if (
    !Array.isArray(draft.nodes) ||
    !draft.nodes.length ||
    draft.nodes.length > 1000
  )
    return "Add between 1 and 1,000 nodes before saving.";
  if (!Array.isArray(draft.edges) || draft.edges.length > 10000)
    return "A workflow supports up to 10,000 connections.";
  const nodes = new Map<string, Node>();
  for (const node of draft.nodes) {
    if (
      !object(node) ||
      typeof node.id !== "string" ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(node.id)
    )
      return "Every node needs a unique ID using letters, numbers, underscores or hyphens.";
    if (nodes.has(node.id)) return `Node IDs must be unique: ${node.id}.`;
    if (
      typeof node.kind !== "string" ||
      !Object.hasOwn(KINDS, node.kind) ||
      !object(node.config)
    )
      return "Each node needs a supported kind and a JSON-object configuration.";
    if (
      Object.keys(node).some(
        (key) =>
          ![
            "id",
            "kind",
            "agent_id",
            "tool_id",
            "output_schema",
            "config",
          ].includes(key),
      )
    )
      return "Nodes contain an unsupported field. Layout coordinates are not workflow data.";
    if (node.kind === "llm_call" && !uuid(node.agent_id, 4))
      return `Choose an agent for ${node.id}.`;
    if (node.kind === "tool_exec" && !uuid(node.tool_id))
      return `Choose a registered tool for ${node.id}.`;
    if (node.kind === "condition") {
      const error = predicateError(node.config.condition);
      if (error) return `${node.id}: ${error}`;
    }
    if (
      node.output_schema &&
      (node.kind !== "llm_call" ||
        !object(node.output_schema) ||
        typeof node.output_schema.id !== "string" ||
        !/^[a-z][a-z0-9_]{0,63}$/.test(node.output_schema.id) ||
        !Number.isInteger(node.output_schema.version) ||
        node.output_schema.version < 1 ||
        node.output_schema.version > 1000000)
    )
      return "Structured output requires a valid registered schema reference on an AI node.";
    nodes.set(node.id, node);
  }
  const incoming = new Map(draft.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(
    draft.nodes.map((node) => [node.id, [] as string[]]),
  );
  const pairs = new Set<string>();
  for (const edge of draft.edges) {
    if (
      !object(edge) ||
      typeof edge.source !== "string" ||
      typeof edge.target !== "string" ||
      !nodes.has(edge.source) ||
      !nodes.has(edge.target)
    )
      return "Every connection must reference two existing nodes.";
    if (
      Object.keys(edge).some(
        (key) => !["source", "target", "condition"].includes(key),
      )
    )
      return "Connections accept only source, target and condition.";
    const pair = `${edge.source}:${edge.target}`;
    if (pairs.has(pair)) return "Duplicate connections are not allowed.";
    pairs.add(pair);
    if (
      typeof edge.condition === "boolean" &&
      nodes.get(edge.source)?.kind !== "condition"
    )
      return "True/false branches require a Condition source node.";
    if (
      edge.condition !== null &&
      edge.condition !== undefined &&
      typeof edge.condition !== "boolean"
    ) {
      const error = predicateError(edge.condition);
      if (error) return error;
    }
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
    outgoing.get(edge.source)?.push(edge.target);
  }
  const ready = [...incoming]
    .filter(([, count]) => count === 0)
    .map(([id]) => id);
  let visited = 0;
  while (ready.length) {
    const id = ready.pop()!;
    visited += 1;
    for (const target of outgoing.get(id) ?? []) {
      incoming.set(target, incoming.get(target)! - 1);
      if (incoming.get(target) === 0) ready.push(target);
    }
  }
  if (visited !== draft.nodes.length)
    return "Connections must form an acyclic graph. Remove the loop before saving.";
  if (draft.output_node_id !== null && !nodes.has(draft.output_node_id))
    return "The output node must exist in this workflow.";
  if (!object(draft.limits)) return "Execution limits must be a JSON object.";
  for (const [key, value] of Object.entries(draft.limits)) {
    if (
      !Object.hasOwn(LIMITS, key) ||
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      value < LIMITS[key][0] ||
      value > LIMITS[key][1]
    )
      return `Execution limit ${key} is unsupported or out of range.`;
    if (
      !["max_duration_seconds", "tool_timeout_seconds"].includes(key) &&
      !Number.isInteger(value)
    )
      return `Execution limit ${key} must be an integer.`;
  }
  return null;
}

export function parseAdvanced(text: string, draft: Draft): Draft {
  if (text.length > 1000000)
    throw new Error("The JSON definition is too large.");
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("Advanced definition must be valid JSON.");
  }
  assertJsonTree(value);
  if (
    !object(value) ||
    Object.keys(value).some(
      (key) => !["nodes", "edges", "limits", "output_node_id"].includes(key),
    )
  )
    throw new Error(
      "Advanced JSON accepts nodes, edges, limits and output_node_id only.",
    );
  const next = { ...draft, ...value } as Draft;
  const error = validateDraft(next);
  if (error) throw new Error(error);
  return next;
}

function layout(nodes: Node[], edges: Edge[]) {
  const depth = new Map(nodes.map((node) => [node.id, 0]));
  for (let pass = 0; pass < Math.min(nodes.length, 20); pass += 1) {
    let changed = false;
    for (const edge of edges) {
      if (
        !depth.has(edge.source) ||
        !depth.has(edge.target) ||
        edge.source === edge.target
      )
        continue;
      const next = Math.min(nodes.length, depth.get(edge.source)! + 1);
      if (next > depth.get(edge.target)!) {
        depth.set(edge.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const columns = new Map<number, number>();
  const positions = nodes.map((node) => {
    const column = depth.get(node.id) ?? 0;
    const row = columns.get(column) ?? 0;
    columns.set(column, row + 1);
    return { x: 36 + column * 238, y: 52 + row * 134 };
  });
  return {
    positions,
    width: Math.max(670, ...positions.map((p) => p.x + 210)),
    height: Math.max(350, ...positions.map((p) => p.y + 130)),
  };
}

function conditionLabel(value: Edge["condition"]) {
  if (value === undefined || value === null) return "Always";
  if (typeof value === "boolean") return value ? "True" : "False";
  return `${value.path} ${value.op}`;
}

export default function Workflows() {
  const { session } = useSession();
  const newRun = useNewRun();
  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>([]);
  const [total, setTotal] = useState(0);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [schemas, setSchemas] = useState<Schema[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [catalogError, setCatalogError] = useState("");
  const [success, setSuccess] = useState("");
  const [search, setSearch] = useState("");
  const [loaded, setLoaded] = useState<WorkflowDefinition | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [baseline, setBaseline] = useState("");
  const [selected, setSelected] = useState(0);
  const [configDrafts, setConfigDrafts] = useState<Record<string, string>>({});
  const [advancedText, setAdvancedText] = useState("");
  const [advancedDirty, setAdvancedDirty] = useState(false);
  const [tab, setTab] = useState<"visual" | "json">("visual");
  const [busy, setBusy] = useState(false);
  const [addNodeOpen, setAddNodeOpen] = useState(false);
  const [pending, setPending] = useState<(() => void) | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [edgeSource, setEdgeSource] = useState("");
  const [edgeTarget, setEdgeTarget] = useState("");
  const [edgeMode, setEdgeMode] = useState("always");
  const [edgePredicate, setEdgePredicate] = useState(
    '{"path":"input.approved","op":"eq","value":true}',
  );
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const dirty = Boolean(
    draft &&
    (pretty(draft) !== baseline ||
      advancedDirty ||
      Object.keys(configDrafts).length),
  );
  const shouldBlock = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) =>
      (dirty || busy) && currentLocation.pathname !== nextLocation.pathname,
    [dirty, busy],
  );
  const blocker = useBlocker(shouldBlock);

  useEffect(() => {
    if (blocker.state === "blocked" && !dirty && !busy) blocker.proceed();
  }, [blocker, dirty, busy]);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    setCatalogError("");
    const results = await Promise.allSettled([
      api<Page<WorkflowDefinition>>("workflows/?limit=100", { signal }),
      api<Page<Agent>>("agents/?limit=200", { signal }),
      api<Page<Tool>>("tools/", { signal }),
      api<Page<Schema>>("output-schemas/?limit=200", { signal }),
    ]);
    if (signal?.aborted) return;
    if (results[0].status === "fulfilled") {
      setWorkflows(results[0].value.results);
      setTotal(results[0].value.count);
    } else setError(message(results[0].reason));
    if (results[1].status === "fulfilled") setAgents(results[1].value.results);
    if (results[2].status === "fulfilled") setTools(results[2].value.results);
    if (results[3].status === "fulfilled") setSchemas(results[3].value.results);
    if (results.slice(1).some((result) => result.status === "rejected"))
      setCatalogError(
        "Some agent, tool or output-schema catalogs could not load. Retry before assigning a new resource.",
      );
    setLoading(false);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty, busy]);
  useEffect(() => {
    if (draft || !workflows.length) return;
    openWorkflow(workflows[0]);
  }, [workflows, draft]);

  const owner = loaded ? loaded.owner : session.user?.id;
  const availableAgents = agents.filter((agent) => agent.owner === owner);
  const filtered = workflows.filter((workflow) =>
    workflow.name.toLowerCase().includes(search.toLowerCase()),
  );
  const graph = useMemo(
    () => (draft ? layout(draft.nodes, draft.edges) : null),
    [draft],
  );
  const selectedNode = draft?.nodes[selected];
  const clientError = draft ? validateDraft(draft) : null;

  function openWorkflow(workflow: WorkflowDefinition) {
    const next = asDraft(workflow);
    setLoaded(workflow);
    setDraft(next);
    setBaseline(pretty(next));
    setAdvancedText(advanced(next));
    setAdvancedDirty(false);
    setConfigDrafts({});
    setSelected(0);
    setError("");
    setSuccess("");
    setTab("visual");
  }
  function changeSelection(action: () => void) {
    if (busy) return;
    if (dirty) setPending(() => action);
    else action();
  }
  function keepEditing() {
    if (busy) return;
    setPending(null);
    if (blocker.state === "blocked") blocker.reset();
  }
  function createDraft() {
    const next = emptyDraft();
    setLoaded(null);
    setDraft(next);
    setBaseline("");
    setAdvancedText(advanced(next));
    setAdvancedDirty(false);
    setConfigDrafts({});
    setSelected(0);
    setError("");
    setSuccess("");
    setTab("visual");
    setAddNodeOpen(true);
  }
  function updateDraft(next: Draft) {
    setDraft(next);
    if (!advancedDirty) setAdvancedText(advanced(next));
    setSuccess("");
  }
  function patchNode(index: number, values: Partial<Node>) {
    if (!draft) return;
    updateDraft({
      ...draft,
      nodes: draft.nodes.map((node, i) =>
        i === index ? { ...node, ...values } : node,
      ),
    });
  }
  function updateConfig(index: number, config: Record<string, unknown>) {
    if (!draft) return;
    patchNode(index, { config });
    setConfigDrafts((previous) => {
      const next = { ...previous };
      delete next[draft.nodes[index].id];
      return next;
    });
  }
  function configText(node: Node) {
    return Object.hasOwn(configDrafts, node.id)
      ? configDrafts[node.id]
      : pretty(node.config);
  }
  function addNode(kind: Kind) {
    if (!draft) return;
    const prefix = {
      llm_call: "agent",
      tool_exec: "tool",
      condition: "condition",
      hitl_approval: "approval",
    }[kind];
    let number = 1;
    while (draft.nodes.some((node) => node.id === `${prefix}_${number}`))
      number += 1;
    const node: Node = {
      id: `${prefix}_${number}`,
      kind,
      config:
        kind === "condition"
          ? { condition: { path: "input.approved", op: "eq", value: true } }
          : {},
    };
    if (kind === "llm_call")
      node.agent_id =
        availableAgents.find((agent) => agent.status === "active")?.id ?? null;
    if (kind === "tool_exec") node.tool_id = tools[0]?.id ?? null;
    updateDraft({ ...draft, nodes: [...draft.nodes, node] });
    setSelected(draft.nodes.length);
    setAddNodeOpen(false);
  }
  function removeNode(index: number) {
    if (!draft) return;
    const id = draft.nodes[index].id;
    updateDraft({
      ...draft,
      nodes: draft.nodes.filter((_, i) => i !== index),
      edges: draft.edges.filter(
        (edge) => edge.source !== id && edge.target !== id,
      ),
      output_node_id: draft.output_node_id === id ? null : draft.output_node_id,
    });
    setSelected(Math.max(0, index - 1));
    setConfigDrafts((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
  }
  function pendingDraft(): Draft {
    if (!draftRef.current) throw new Error("Choose a workflow first.");
    let next = clone(draftRef.current);
    if (advancedDirty) {
      if (Object.keys(configDrafts).length)
        throw new Error(
          "Apply node configuration changes before applying Advanced JSON.",
        );
      next = parseAdvanced(advancedText, next);
    }
    next.nodes = next.nodes.map((node) => {
      if (!Object.hasOwn(configDrafts, node.id)) return node;
      let config: unknown;
      try {
        config = JSON.parse(configDrafts[node.id]);
      } catch {
        throw new Error(`${node.id}: configuration must be valid JSON.`);
      }
      assertJsonTree(config);
      if (!object(config))
        throw new Error(`${node.id}: configuration must be a JSON object.`);
      return { ...node, config };
    });
    const validation = validateDraft(next);
    if (validation) throw new Error(validation);
    return next;
  }
  async function save() {
    setError("");
    setSuccess("");
    let next: Draft;
    try {
      next = pendingDraft();
    } catch (caught) {
      setError(message(caught));
      return;
    }
    setBusy(true);
    try {
      const saved = await api<WorkflowDefinition>(
        loaded ? `workflows/${loaded.id}/` : "workflows/",
        { method: loaded ? "PATCH" : "POST", body: next },
      );
      setWorkflows((previous) => [
        saved,
        ...previous.filter((item) => item.id !== saved.id),
      ]);
      if (!loaded) setTotal((previous) => previous + 1);
      openWorkflow(saved);
      setSuccess(
        "Workflow saved. Existing runs keep their original definition snapshot.",
      );
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }
  async function duplicate() {
    if (!draft) return;
    try {
      const next = pendingDraft();
      next.version =
        Math.max(
          next.version,
          ...workflows
            .filter(
              (item) =>
                item.name === next.name && item.owner === session.user?.id,
            )
            .map((item) => item.version),
        ) + 1;
      const needsOwnAgents = loaded?.owner !== session.user?.id;
      if (needsOwnAgents)
        next.nodes = next.nodes.map((node) =>
          node.kind === "llm_call" ? { ...node, agent_id: null } : node,
        );
      setDraft(next);
      setLoaded(null);
      setBaseline("");
      setAdvancedText(advanced(next));
      setConfigDrafts({});
      setAdvancedDirty(false);
      setSuccess(
        needsOwnAgents
          ? "A new version is ready in your workspace. Assign your own agents, then save."
          : "A new version is ready to edit. Save it before running.",
      );
    } catch (caught) {
      setError(message(caught));
    }
  }
  async function removeWorkflow() {
    if (!loaded) return;
    setBusy(true);
    setError("");
    try {
      await api(`workflows/${loaded.id}/`, { method: "DELETE" });
      setWorkflows((previous) =>
        previous.filter((item) => item.id !== loaded.id),
      );
      setTotal((previous) => Math.max(0, previous - 1));
      setDraft(null);
      setLoaded(null);
      setDeleteOpen(false);
      setBaseline("");
      setConfigDrafts({});
      setAdvancedDirty(false);
    } catch (caught) {
      setError(message(caught));
      setDeleteOpen(false);
    } finally {
      setBusy(false);
    }
  }
  function addEdge() {
    if (!draft) return;
    let condition: Edge["condition"] = null;
    try {
      if (edgeMode === "true") condition = true;
      else if (edgeMode === "false") condition = false;
      else if (edgeMode === "predicate") {
        const parsed: unknown = JSON.parse(edgePredicate);
        const error = predicateError(parsed);
        if (error) throw new Error(error);
        condition = parsed as Predicate;
      }
      const next = {
        ...draft,
        edges: [
          ...draft.edges,
          { source: edgeSource, target: edgeTarget, condition },
        ],
      };
      // Validate graph structure while allowing unfinished node assignments.
      const structural = validateDraft({
        ...next,
        nodes: next.nodes.map((node) => ({
          ...node,
          ...(node.kind === "llm_call" && !node.agent_id
            ? { agent_id: "00000000-0000-4000-8000-000000000000" }
            : {}),
          ...(node.kind === "tool_exec" && !node.tool_id
            ? { tool_id: "00000000-0000-5000-8000-000000000000" }
            : {}),
        })),
      });
      if (structural) throw new Error(structural);
      updateDraft(next);
      setError("");
    } catch (caught) {
      setError(message(caught));
    }
  }
  async function more() {
    setLoadingMore(true);
    try {
      const page = await api<Page<WorkflowDefinition>>(
        `workflows/?limit=100&offset=${workflows.length}`,
      );
      setWorkflows((previous) => [
        ...previous,
        ...page.results.filter(
          (item) => !previous.some((existing) => existing.id === item.id),
        ),
      ]);
      setTotal(page.count);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setLoadingMore(false);
    }
  }
  function applyAdvanced() {
    try {
      const next = pendingDraft();
      setDraft(next);
      setConfigDrafts({});
      setAdvancedDirty(false);
      setAdvancedText(advanced(next));
      setSelected(0);
      setError("");
      setSuccess("JSON definition applied. Save to persist your changes.");
      return true;
    } catch (caught) {
      setError(message(caught));
      return false;
    }
  }
  function applyConfiguration() {
    if (!selectedNode || !draft) return;
    try {
      const value: unknown = JSON.parse(configText(selectedNode));
      assertJsonTree(value);
      if (!object(value))
        throw new Error("Node configuration must be a JSON object.");
      if (selectedNode.kind === "condition") {
        const error = predicateError(value.condition);
        if (error) throw new Error(error);
      }
      updateConfig(selected, value);
      setError("");
    } catch (caught) {
      setError(message(caught));
    }
  }

  return (
    <div className="wf-page">
      <PageHeader
        title="Workflow Builder"
        description="Design, validate and run your agent workflows."
        actions={
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => changeSelection(createDraft)}
          >
            <Plus size={17} /> New workflow
          </Button>
        }
      />
      {error && <ErrorNotice message={error} />}
      {catalogError && (
        <div className="wf-notice">
          <CircleHelp size={17} />
          <span>{catalogError}</span>
          <button type="button" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {success && (
        <div className="wf-success" role="status">
          <Check size={17} />
          {success}
        </div>
      )}
      <div className="wf-workspace" aria-busy={busy} inert={busy}>
        <Card className="wf-library">
          <div className="wf-library-title">
            <span>Workflow library</span>
            <span className="wf-count">{total}</span>
          </div>
          <label className="wf-search">
            <Search size={16} />
            <input
              aria-label="Search loaded workflows"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search workflows..."
            />
          </label>
          {loading ? (
            <div className="wf-loading">
              <LoaderCircle className="wf-spin" size={22} />
              <span>Loading workflows</span>
            </div>
          ) : !workflows.length ? (
            <div className="wf-library-empty">
              <WorkflowIcon size={28} />
              <p>Your workflows will appear here.</p>
              <Button
                variant="secondary"
                onClick={() => changeSelection(createDraft)}
              >
                Create your first workflow
              </Button>
            </div>
          ) : (
            <div className="wf-library-items">
              {filtered.map((workflow) => (
                <button
                  key={workflow.id}
                  type="button"
                  className={`wf-library-item ${loaded?.id === workflow.id ? "is-selected" : ""}`}
                  onClick={() => changeSelection(() => openWorkflow(workflow))}
                >
                  <span className="wf-library-icon">
                    <WorkflowIcon size={19} />
                  </span>
                  <span className="wf-library-copy">
                    <strong>{workflow.name}</strong>
                    <small>
                      Version {workflow.version} · {workflow.nodes.length} nodes
                    </small>
                  </span>
                  <ChevronRight size={15} />
                </button>
              ))}
              {!filtered.length && (
                <p className="wf-muted wf-search-empty">
                  No matching loaded workflows.
                </p>
              )}
            </div>
          )}
          {workflows.length < total && (
            <Button
              variant="ghost"
              disabled={loadingMore}
              onClick={() => void more()}
            >
              {loadingMore
                ? "Loading..."
                : `Load more (${workflows.length} of ${total})`}
            </Button>
          )}
          <div className="wf-library-footer">
            <ShieldCheck size={16} />
            <span>
              Runs keep a frozen snapshot of the workflow they started with.
            </span>
          </div>
        </Card>
        {!draft ? (
          <Card className="wf-editor-empty">
            <EmptyState
              icon={WorkflowIcon}
              title="Build your first workflow"
              description="Connect AI agents, registered tools, conditions and human approvals."
              action={
                <Button variant="primary" onClick={createDraft}>
                  <Plus size={16} /> Create workflow
                </Button>
              }
            />
          </Card>
        ) : (
          <div className="wf-editor-stack">
            <Card className="wf-editor">
              <div className="wf-editor-toolbar">
                <div className="wf-editor-heading">
                  <span className="wf-heading-icon">
                    <Network size={21} />
                  </span>
                  <div>
                    <h2>{draft.name || "Untitled workflow"}</h2>
                    <span>
                      {loaded ? `Version ${draft.version}` : "New definition"}
                      {dirty ? " · Unsaved changes" : " · All changes saved"}
                    </span>
                  </div>
                </div>
                <div className="wf-editor-actions">
                  <Button
                    variant="ghost"
                    disabled={busy || !loaded}
                    onClick={() => void duplicate()}
                    title="Create a new version"
                  >
                    <Copy size={16} />
                    <span className="wf-wide-label">New version</span>
                  </Button>
                  <Button
                    variant="secondary"
                    disabled={busy || !dirty}
                    onClick={() =>
                      changeSelection(() =>
                        loaded ? openWorkflow(loaded) : setDraft(null),
                      )
                    }
                  >
                    <RotateCcw size={16} />
                    <span className="wf-wide-label">Reset</span>
                  </Button>
                  <Button
                    variant="primary"
                    disabled={busy || !dirty}
                    onClick={() => void save()}
                  >
                    {busy ? (
                      <LoaderCircle size={16} className="wf-spin" />
                    ) : (
                      <Save size={16} />
                    )}{" "}
                    Save
                  </Button>
                  <Button
                    variant="secondary"
                    disabled={busy || dirty || !loaded}
                    onClick={() => loaded && newRun.open(loaded.id)}
                    title={
                      dirty
                        ? "Save changes before starting a run"
                        : "Start a run from the saved definition"
                    }
                  >
                    <Play size={16} /> Run
                  </Button>
                </div>
              </div>
              <div className="wf-definition-fields">
                <Field label="Workflow name">
                  <input
                    value={draft.name}
                    maxLength={256}
                    onChange={(event) =>
                      updateDraft({ ...draft, name: event.target.value })
                    }
                  />
                </Field>
                <Field label="Version">
                  <input
                    type="number"
                    min={1}
                    step={1}
                    value={draft.version}
                    onChange={(event) =>
                      updateDraft({
                        ...draft,
                        version: Number(event.target.value),
                      })
                    }
                  />
                </Field>
                <Field
                  label="Output node"
                  hint="Optional; otherwise terminal-node results are returned."
                >
                  <select
                    value={draft.output_node_id ?? ""}
                    onChange={(event) =>
                      updateDraft({
                        ...draft,
                        output_node_id: event.target.value || null,
                      })
                    }
                  >
                    <option value="">Default result</option>
                    {draft.nodes.map((node) => (
                      <option key={node.id} value={node.id}>
                        {node.id}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <div className="wf-tabs">
                <button
                  type="button"
                  className={tab === "visual" ? "active" : ""}
                  onClick={() => {
                    if (!advancedDirty || applyAdvanced()) setTab("visual");
                  }}
                >
                  <WorkflowIcon size={16} /> Visual builder
                </button>
                <button
                  type="button"
                  className={tab === "json" ? "active" : ""}
                  onClick={() => {
                    if (!advancedDirty) setAdvancedText(advanced(draft));
                    setTab("json");
                  }}
                >
                  <Braces size={16} /> Advanced JSON
                </button>
                <span className="wf-validation">
                  {clientError ? (
                    <>
                      <span className="wf-validation-dot warning" /> Needs
                      configuration
                    </>
                  ) : (
                    <>
                      <span className="wf-validation-dot" /> Graph valid
                    </>
                  )}
                </span>
              </div>
              {tab === "json" ? (
                <div className="wf-json-panel">
                  <p>
                    Use the server workflow schema. IDs are stable references;
                    layout positions are not submitted. Changes remain local
                    until you save.
                  </p>
                  <textarea
                    className="wf-code"
                    aria-label="Advanced workflow JSON"
                    spellCheck={false}
                    maxLength={1000000}
                    value={advancedText}
                    onChange={(event) => {
                      setAdvancedText(event.target.value);
                      setAdvancedDirty(true);
                    }}
                  />
                  <div className="wf-json-footer">
                    <span>
                      {advancedDirty
                        ? "Unapplied JSON changes"
                        : "Definition matches the visual builder"}
                    </span>
                    <Button variant="secondary" onClick={applyAdvanced}>
                      <Check size={16} /> Apply JSON
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="wf-canvas-wrap">
                  <div className="wf-canvas-tools">
                    <span>
                      <Layers size={15} /> {draft.nodes.length} nodes{" "}
                      <span>·</span> {draft.edges.length} connections
                    </span>
                    <Button
                      variant="secondary"
                      disabled={draft.nodes.length >= 1000}
                      onClick={() => setAddNodeOpen(true)}
                    >
                      <Plus size={15} /> Add node
                    </Button>
                  </div>
                  {!draft.nodes.length ? (
                    <div className="wf-canvas-empty">
                      <span>
                        <WorkflowIcon size={36} />
                      </span>
                      <h3>Start with a node</h3>
                      <p>Add an agent, tool, condition or approval step.</p>
                      <Button
                        variant="primary"
                        onClick={() => setAddNodeOpen(true)}
                      >
                        <Plus size={16} /> Add your first node
                      </Button>
                    </div>
                  ) : (
                    <div className="wf-canvas" aria-label="Workflow graph">
                      <div
                        className="wf-map"
                        style={{ width: graph!.width, height: graph!.height }}
                      >
                        <svg
                          width={graph!.width}
                          height={graph!.height}
                          aria-hidden="true"
                        >
                          <defs>
                            <marker
                              id="wf-arrow"
                              viewBox="0 0 10 10"
                              refX="9"
                              refY="5"
                              markerWidth="6"
                              markerHeight="6"
                              orient="auto-start-reverse"
                            >
                              <path d="M 0 0 L 10 5 L 0 10 z" fill="#a69cbd" />
                            </marker>
                          </defs>
                          {draft.edges.map((edge, index) => {
                            const sourceIndex = draft.nodes.findIndex(
                              (node) => node.id === edge.source,
                            );
                            const targetIndex = draft.nodes.findIndex(
                              (node) => node.id === edge.target,
                            );
                            if (sourceIndex < 0 || targetIndex < 0) return null;
                            const source = graph!.positions[sourceIndex];
                            const target = graph!.positions[targetIndex];
                            const x1 = source.x + 174,
                              y1 = source.y + 40,
                              x2 = target.x,
                              y2 = target.y + 40;
                            const bend = Math.max(36, (x2 - x1) / 2);
                            return (
                              <g key={index}>
                                <path
                                  d={`M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`}
                                  fill="none"
                                  stroke="#a69cbd"
                                  strokeWidth="1.7"
                                  markerEnd="url(#wf-arrow)"
                                />
                                {edge.condition !== undefined &&
                                  edge.condition !== null && (
                                    <text
                                      x={(x1 + x2) / 2}
                                      y={(y1 + y2) / 2 - 10}
                                      textAnchor="middle"
                                      className="wf-edge-label"
                                    >
                                      {typeof edge.condition === "boolean"
                                        ? edge.condition
                                          ? "True"
                                          : "False"
                                        : "Predicate"}
                                    </text>
                                  )}
                              </g>
                            );
                          })}
                        </svg>
                        {draft.nodes.map((node, index) => {
                          const Icon = KINDS[node.kind].icon;
                          const agent = agents.find(
                            (item) => item.id === node.agent_id,
                          );
                          const tool = tools.find(
                            (item) => item.id === node.tool_id,
                          );
                          return (
                            <button
                              key={`${node.id}-${index}`}
                              className={`wf-node wf-node-${node.kind} ${selected === index ? "is-selected" : ""}`}
                              style={{
                                left: graph!.positions[index].x,
                                top: graph!.positions[index].y,
                              }}
                              type="button"
                              aria-pressed={selected === index}
                              onClick={() => setSelected(index)}
                            >
                              <span className="wf-node-icon">
                                <Icon size={19} />
                              </span>
                              <span className="wf-node-copy">
                                <strong>{node.id}</strong>
                                <small>
                                  {agent?.name ??
                                    tool?.name ??
                                    KINDS[node.kind].label}
                                </small>
                              </span>
                              <span className="wf-node-port input" />
                              <span className="wf-node-port output" />
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                  <div className="wf-canvas-foot">
                    <span>
                      <CircleHelp size={14} /> Select a node to configure it.
                      Connections are created below.
                    </span>
                    <span>
                      Auto layout · {draft.nodes.length ? "DAG" : "Empty graph"}
                    </span>
                  </div>
                </div>
              )}
            </Card>
            {tab === "visual" && (
              <div className="wf-detail-grid">
                <Card className="wf-node-inspector">
                  <div className="wf-section-title">
                    <Settings2 size={18} />
                    <h3>Node configuration</h3>
                    {selectedNode && (
                      <button
                        className="wf-icon-button danger"
                        type="button"
                        aria-label={`Remove node ${selectedNode.id}`}
                        onClick={() => removeNode(selected)}
                      >
                        <Trash2 size={16} />
                      </button>
                    )}
                  </div>
                  {!selectedNode ? (
                    <p className="wf-muted">
                      Select a node on the canvas to configure it.
                    </p>
                  ) : (
                    <>
                      <div className="wf-node-fields">
                        <Field
                          label="Node ID"
                          hint="Stable reference. Use Advanced JSON to rename IDs and references together."
                        >
                          <input value={selectedNode.id} readOnly />
                        </Field>
                        <Field label="Node type">
                          <select
                            value={selectedNode.kind}
                            onChange={(event) => {
                              const kind = event.target.value as Kind;
                              const replacement: Node = {
                                id: selectedNode.id,
                                kind,
                                config:
                                  kind === "condition"
                                    ? {
                                        condition: {
                                          path: "input.approved",
                                          op: "eq",
                                          value: true,
                                        },
                                      }
                                    : {},
                              };
                              if (kind === "llm_call")
                                replacement.agent_id =
                                  availableAgents.find(
                                    (agent) => agent.status === "active",
                                  )?.id ?? null;
                              if (kind === "tool_exec")
                                replacement.tool_id = tools[0]?.id ?? null;
                              updateDraft({
                                ...draft,
                                nodes: draft.nodes.map((node, index) =>
                                  index === selected ? replacement : node,
                                ),
                                edges: draft.edges.map((edge) =>
                                  edge.source === selectedNode.id &&
                                  typeof edge.condition === "boolean" &&
                                  kind !== "condition"
                                    ? { ...edge, condition: null }
                                    : edge,
                                ),
                              });
                              setConfigDrafts((previous) => {
                                const next = { ...previous };
                                delete next[selectedNode.id];
                                return next;
                              });
                            }}
                          >
                            <option value="llm_call">AI agent</option>
                            <option value="tool_exec">Tool</option>
                            <option value="condition">Condition</option>
                            <option value="hitl_approval">
                              Human approval
                            </option>
                          </select>
                        </Field>
                      </div>
                      {selectedNode.kind === "llm_call" && (
                        <>
                          <Field label="Agent">
                            <select
                              value={selectedNode.agent_id ?? ""}
                              onChange={(event) =>
                                patchNode(selected, {
                                  agent_id: event.target.value || null,
                                })
                              }
                            >
                              <option value="">Select an agent</option>
                              {selectedNode.agent_id &&
                                !availableAgents.some(
                                  (agent) => agent.id === selectedNode.agent_id,
                                ) && (
                                  <option value={selectedNode.agent_id}>
                                    Assigned agent ·{" "}
                                    {selectedNode.agent_id.slice(0, 8)}
                                  </option>
                                )}
                              {availableAgents.map((agent) => (
                                <option
                                  key={agent.id}
                                  value={agent.id}
                                  disabled={agent.status !== "active"}
                                >
                                  {agent.name} · {agent.model_name}
                                  {agent.status !== "active"
                                    ? ` (${agent.status})`
                                    : ""}
                                </option>
                              ))}
                            </select>
                          </Field>
                          <Field
                            label="Prompt template"
                            hint="Use context values such as {input.prompt} or {nodes.agent_1.result}."
                          >
                            <textarea
                              rows={3}
                              value={String(
                                selectedNode.config.prompt_template ??
                                  "{input.prompt}",
                              )}
                              disabled={Object.hasOwn(
                                configDrafts,
                                selectedNode.id,
                              )}
                              onChange={(event) =>
                                updateConfig(selected, {
                                  ...selectedNode.config,
                                  prompt_template: event.target.value,
                                })
                              }
                            />
                          </Field>
                          <Field
                            label="Structured output"
                            hint="Requires a runtime with verified structured-output support."
                          >
                            <select
                              value={
                                selectedNode.output_schema
                                  ? `${selectedNode.output_schema.id}:${selectedNode.output_schema.version}`
                                  : ""
                              }
                              onChange={(event) => {
                                const schema = schemas.find(
                                  (item) =>
                                    `${item.id}:${item.version}` ===
                                    event.target.value,
                                );
                                patchNode(selected, {
                                  output_schema: schema
                                    ? { id: schema.id, version: schema.version }
                                    : null,
                                });
                              }}
                            >
                              <option value="">Plain text response</option>
                              {selectedNode.output_schema &&
                                !schemas.some(
                                  (schema) =>
                                    schema.id ===
                                      selectedNode.output_schema!.id &&
                                    schema.version ===
                                      selectedNode.output_schema!.version,
                                ) && (
                                  <option
                                    value={`${selectedNode.output_schema.id}:${selectedNode.output_schema.version}`}
                                  >
                                    {selectedNode.output_schema.id} v
                                    {selectedNode.output_schema.version}
                                  </option>
                                )}
                              {schemas.map((schema) => (
                                <option
                                  key={`${schema.id}:${schema.version}`}
                                  value={`${schema.id}:${schema.version}`}
                                >
                                  {schema.id} · v{schema.version}
                                </option>
                              ))}
                            </select>
                          </Field>
                          {!availableAgents.length && (
                            <p className="wf-inline-hint">
                              Create an agent in Agents before configuring this
                              step.
                            </p>
                          )}
                        </>
                      )}
                      {selectedNode.kind === "tool_exec" && (
                        <>
                          <Field label="Registered tool">
                            <select
                              value={selectedNode.tool_id ?? ""}
                              onChange={(event) =>
                                patchNode(selected, {
                                  tool_id: event.target.value || null,
                                })
                              }
                            >
                              <option value="">Select a tool</option>
                              {tools.map((tool) => (
                                <option key={tool.id} value={tool.id}>
                                  {tool.name}
                                </option>
                              ))}
                            </select>
                          </Field>
                          <p className="wf-inline-hint">
                            {tools.find(
                              (tool) => tool.id === selectedNode.tool_id,
                            )?.description ??
                              "Only tools registered on this server can be executed."}{" "}
                            Supply tool arguments in the configuration object
                            below.
                          </p>
                          {tools.find(
                            (tool) => tool.id === selectedNode.tool_id,
                          )?.name === "search_knowledge_base" &&
                            !session.capabilities
                              .knowledge_search_configured && (
                              <p className="wf-inline-hint">
                                Knowledge search is not configured on this
                                server yet. Configure the endpoint before
                                running this tool.
                              </p>
                            )}
                          {tools.find(
                            (tool) => tool.id === selectedNode.tool_id,
                          ) && (
                            <details className="wf-tool-schema">
                              <summary>Tool argument schema</summary>
                              <pre>
                                {pretty(
                                  tools.find(
                                    (tool) => tool.id === selectedNode.tool_id,
                                  )!.parameters_schema,
                                )}
                              </pre>
                            </details>
                          )}
                        </>
                      )}
                      {selectedNode.kind === "condition" && (
                        <>
                          <Field label="Context path">
                            <input
                              value={String(
                                (
                                  selectedNode.config.condition as
                                    Predicate | undefined
                                )?.path ?? "",
                              )}
                              disabled={Object.hasOwn(
                                configDrafts,
                                selectedNode.id,
                              )}
                              onChange={(event) =>
                                updateConfig(selected, {
                                  ...selectedNode.config,
                                  condition: {
                                    ...(object(selectedNode.config.condition)
                                      ? selectedNode.config.condition
                                      : {}),
                                    path: event.target.value,
                                  },
                                })
                              }
                            />
                          </Field>
                          <Field label="Comparison">
                            <select
                              value={String(
                                (
                                  selectedNode.config.condition as
                                    Predicate | undefined
                                )?.op ?? "eq",
                              )}
                              disabled={Object.hasOwn(
                                configDrafts,
                                selectedNode.id,
                              )}
                              onChange={(event) =>
                                updateConfig(selected, {
                                  ...selectedNode.config,
                                  condition: {
                                    ...(object(selectedNode.config.condition)
                                      ? selectedNode.config.condition
                                      : {}),
                                    op: event.target.value,
                                  },
                                })
                              }
                            >
                              {OPS.map((op) => (
                                <option key={op} value={op}>
                                  {op}
                                </option>
                              ))}
                            </select>
                          </Field>
                          <p className="wf-inline-hint">
                            Set the typed comparison value in configuration
                            JSON. Boolean true/false branches can leave this
                            node.
                          </p>
                        </>
                      )}
                      {selectedNode.kind === "hitl_approval" && (
                        <div className="wf-approval-note">
                          <ShieldCheck size={19} />
                          <span>
                            This step pauses the run. An authorized user resumes
                            it from Runs / Traces with a JSON response.
                            Downstream steps can read{" "}
                            <code>{`{nodes.${selectedNode.id}.result}`}</code>.
                          </span>
                        </div>
                      )}
                      {selectedNode.kind !== "hitl_approval" && (
                        <>
                          <Field label="Configuration JSON">
                            <textarea
                              className="wf-code wf-config-code"
                              aria-label={`Configuration for ${selectedNode.id}`}
                              spellCheck={false}
                              rows={7}
                              maxLength={1000000}
                              value={configText(selectedNode)}
                              onChange={(event) => {
                                setConfigDrafts((previous) => ({
                                  ...previous,
                                  [selectedNode.id]: event.target.value,
                                }));
                                setSuccess("");
                              }}
                            />
                          </Field>
                          <div className="wf-config-footer">
                            <span>
                              {Object.hasOwn(configDrafts, selectedNode.id)
                                ? "Unapplied JSON changes"
                                : "Configuration applied"}
                            </span>
                            <Button
                              variant="secondary"
                              onClick={applyConfiguration}
                            >
                              <Check size={15} /> Apply
                            </Button>
                          </div>
                        </>
                      )}
                    </>
                  )}
                </Card>
                <Card className="wf-connections">
                  <div className="wf-section-title">
                    <GitBranch size={18} />
                    <h3>Connections</h3>
                    <span className="wf-count">{draft.edges.length}</span>
                  </div>
                  <div className="wf-edge-list">
                    {draft.edges.length ? (
                      draft.edges.map((edge, index) => (
                        <div
                          className="wf-edge-row"
                          key={`${edge.source}-${edge.target}-${index}`}
                        >
                          <div>
                            <code>{edge.source}</code>
                            <ArrowRight size={14} />
                            <code>{edge.target}</code>
                            <span
                              className={`wf-edge-condition ${typeof edge.condition === "boolean" ? (edge.condition ? "true" : "false") : ""}`}
                            >
                              {conditionLabel(edge.condition)}
                            </span>
                          </div>
                          <button
                            type="button"
                            className="wf-icon-button"
                            aria-label={`Remove connection ${edge.source} to ${edge.target}`}
                            onClick={() =>
                              updateDraft({
                                ...draft,
                                edges: draft.edges.filter(
                                  (_, i) => i !== index,
                                ),
                              })
                            }
                          >
                            <X size={15} />
                          </button>
                        </div>
                      ))
                    ) : (
                      <p className="wf-muted">
                        Connect nodes to define their execution order.
                      </p>
                    )}
                  </div>
                  <div className="wf-connect-form">
                    <div className="wf-node-fields">
                      <Field label="From">
                        <select
                          value={edgeSource}
                          onChange={(event) => {
                            setEdgeSource(event.target.value);
                            setEdgeMode("always");
                          }}
                        >
                          <option value="">Source node</option>
                          {draft.nodes.map((node) => (
                            <option key={node.id} value={node.id}>
                              {node.id}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="To">
                        <select
                          value={edgeTarget}
                          onChange={(event) =>
                            setEdgeTarget(event.target.value)
                          }
                        >
                          <option value="">Target node</option>
                          {draft.nodes
                            .filter((node) => node.id !== edgeSource)
                            .map((node) => (
                              <option key={node.id} value={node.id}>
                                {node.id}
                              </option>
                            ))}
                        </select>
                      </Field>
                    </div>
                    <Field label="When">
                      <select
                        value={edgeMode}
                        onChange={(event) => setEdgeMode(event.target.value)}
                      >
                        <option value="always">Always</option>
                        {draft.nodes.find((node) => node.id === edgeSource)
                          ?.kind === "condition" && (
                          <>
                            <option value="true">Condition is true</option>
                            <option value="false">Condition is false</option>
                          </>
                        )}
                        <option value="predicate">Custom predicate</option>
                      </select>
                    </Field>
                    {edgeMode === "predicate" && (
                      <Field label="Predicate JSON">
                        <textarea
                          className="wf-code"
                          rows={3}
                          value={edgePredicate}
                          maxLength={4000}
                          onChange={(event) =>
                            setEdgePredicate(event.target.value)
                          }
                        />
                      </Field>
                    )}
                    <Button
                      variant="secondary"
                      disabled={!edgeSource || !edgeTarget}
                      onClick={addEdge}
                    >
                      <Plus size={15} /> Add connection
                    </Button>
                  </div>
                </Card>
              </div>
            )}
            <div className="wf-save-note">
              <ShieldCheck size={15} />
              <span>
                Definitions are validated again by the server. Saving a workflow
                does not start a run or call a model.
              </span>
              {loaded && (
                <button type="button" onClick={() => setDeleteOpen(true)}>
                  <Trash2 size={14} /> Delete definition
                </button>
              )}
            </div>
          </div>
        )}
      </div>
      {addNodeOpen && (
        <Modal
          title="Add a workflow node"
          onClose={() => setAddNodeOpen(false)}
        >
          <p className="wf-modal-intro">
            Choose a step. Each node has a stable ID and can be connected to
            other steps.
          </p>
          <div className="wf-node-picker">
            {(Object.entries(KINDS) as [Kind, (typeof KINDS)[Kind]][]).map(
              ([kind, details]) => {
                const Icon = details.icon;
                return (
                  <button
                    key={kind}
                    type="button"
                    onClick={() => addNode(kind)}
                  >
                    <span className={`wf-picker-icon wf-picker-${kind}`}>
                      <Icon size={22} />
                    </span>
                    <span>
                      <strong>{details.label}</strong>
                      <small>{details.description}</small>
                    </span>
                    <ChevronRight size={18} />
                  </button>
                );
              },
            )}
          </div>
        </Modal>
      )}
      {(pending || blocker.state === "blocked") && (
        <Modal title="Discard unsaved changes?" onClose={keepEditing}>
          <p>
            {busy
              ? "Wait for the current save or delete request to finish before leaving this workflow."
              : "Your current definition has unsaved changes. Discard them to continue, or keep editing."}
          </p>
          <div className="wf-modal-actions">
            <Button variant="secondary" disabled={busy} onClick={keepEditing}>
              Keep editing
            </Button>
            <Button
              variant="danger"
              disabled={busy}
              onClick={() => {
                const action = pending;
                setPending(null);
                if (blocker.state === "blocked") blocker.proceed();
                else action?.();
              }}
            >
              Discard changes
            </Button>
          </div>
        </Modal>
      )}
      {deleteOpen && (
        <Modal
          title="Delete workflow definition?"
          onClose={() => setDeleteOpen(false)}
        >
          <p>
            Delete <strong>{loaded?.name}</strong>? Definitions referenced by
            runs cannot be deleted. Existing run history is preserved.
          </p>
          <div className="wf-modal-actions">
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => setDeleteOpen(false)}
            >
              Keep workflow
            </Button>
            <Button
              variant="danger"
              disabled={busy}
              onClick={() => void removeWorkflow()}
            >
              {busy ? "Deleting..." : "Delete workflow"}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
