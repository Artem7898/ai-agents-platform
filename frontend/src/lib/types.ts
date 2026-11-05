export interface User {
  id: number;
  username: string;
  first_name: string;
  last_name: string;
  is_staff: boolean;
}
export interface Session {
  authenticated: boolean;
  csrf_token: string;
  user: User | null;
  environment: "development" | "production";
  timezone: string;
  billing: { enabled: boolean; mode: "test" | "live" };
  capabilities: { knowledge_search_configured: boolean };
}
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
export interface Agent {
  id: string;
  owner: number | null;
  name: string;
  model_name: string;
  status: string;
  system_prompt: string;
  temperature: number;
  tool_ids: string[];
  runtime_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface WorkflowNode {
  id: string;
  kind: string;
  agent_id?: string | null;
  config?: Record<string, unknown>;
  [key: string]: unknown;
}
export interface WorkflowEdge {
  source: string;
  target: string;
  condition?: unknown;
}
export interface Workflow {
  id: string;
  owner: number | null;
  name: string;
  version: number;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  limits: Record<string, unknown>;
  output_node_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface DashboardData {
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
  token_usage: {
    agent_id: string | null;
    agent_name: string;
    tokens: number;
    unknown_calls: number;
  }[];
  token_usage_complete?: boolean;
  active_agents: {
    id: string;
    name: string;
    model_name: string;
    status: string;
    tools_count: number;
    active_runs: number;
    waiting_runs: number;
  }[];
  recent_runs: {
    id: string;
    workflow_name: string;
    status: string;
    created_at: string;
    updated_at: string;
    duration_seconds: number | null;
    cost: string | null;
    currency: string | null;
  }[];
  workflow_preview: {
    id: string;
    name: string;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
  } | null;
  alerts: {
    id: string;
    kind: "warning" | "info" | "error";
    title: string;
    description: string;
    href: string;
  }[];
  knowledge: {
    configured: boolean;
    index_status: string;
    documents_count: number | null;
    memory_status: string;
  };
  billing: {
    enabled: boolean;
    mode: "test" | "live";
    subscription_status: string | null;
    overage_limit_minor: number | null;
    currency: string | null;
  };
}
