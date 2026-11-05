import { describe, expect, it } from "vitest";
import { parseAdvanced, validateDraft } from "./Workflows";

type Draft = Parameters<typeof validateDraft>[0];

function conditionFlow(): Draft {
  return {
    name: "Review a response",
    version: 1,
    nodes: [
      {
        id: "decision",
        kind: "condition",
        config: {
          condition: { path: "input.approved", op: "eq", value: true },
        },
      },
      { id: "review", kind: "hitl_approval", config: {} },
    ],
    edges: [{ source: "decision", target: "review", condition: true }],
    limits: { max_tokens: 32000, max_output_tokens: 1024 },
    output_node_id: "review",
  };
}

describe("workflow editor validation", () => {
  it("accepts a supported condition and human-approval flow", () => {
    expect(validateDraft(conditionFlow())).toBeNull();
  });

  it("preserves Unicode context paths supported by the server", () => {
    const flow = conditionFlow();
    flow.nodes[0].config.condition = { path: "input.решение", op: "exists" };
    expect(validateDraft(flow)).toBeNull();
  });

  it("rejects a graph cycle before a save can be sent", () => {
    const flow = conditionFlow();
    flow.edges.push({ source: "review", target: "decision" });
    expect(validateDraft(flow)).toMatch(/acyclic/);
  });

  it("rejects a Boolean branch leaving a non-condition node", () => {
    const flow = conditionFlow();
    flow.edges = [{ source: "review", target: "decision", condition: false }];
    expect(validateDraft(flow)).toMatch(/Condition source/);
  });

  it("rejects references to missing nodes and duplicate connections", () => {
    const flow = conditionFlow();
    flow.edges.push({ source: "decision", target: "missing" });
    expect(validateDraft(flow)).toMatch(/existing nodes/);
    flow.edges = [flow.edges[0], flow.edges[0]];
    expect(validateDraft(flow)).toMatch(/Duplicate connections/);
  });

  it("requires unique node identities", () => {
    const flow = conditionFlow();
    flow.nodes[1].id = "decision";
    expect(validateDraft(flow)).toMatch(/unique/);
  });

  it.each([null, "not-a-uuid", "11111111-1111-5111-8111-111111111111"])(
    "requires a UUID4 agent assignment rather than a malformed identity: %s",
    (agentId) => {
      const flow = conditionFlow();
      flow.nodes = [
        { id: "research", kind: "llm_call", agent_id: agentId, config: {} },
      ];
      flow.edges = [];
      flow.output_node_id = "research";
      expect(validateDraft(flow)).toMatch(/Choose an agent/);
    },
  );

  it("accepts a UUID4 agent identity without pretending to authorize its owner", () => {
    const flow = conditionFlow();
    flow.nodes = [
      {
        id: "research",
        kind: "llm_call",
        agent_id: "11111111-1111-4111-8111-111111111111",
        config: {},
      },
    ];
    flow.edges = [];
    flow.output_node_id = "research";
    expect(validateDraft(flow)).toBeNull();
    // The actual catalog membership and ownership remain server checks.
  });

  it("requires an array for membership comparisons", () => {
    const flow = conditionFlow();
    flow.nodes[0].config.condition = {
      path: "input.roles",
      op: "in",
      value: "reviewer",
    };
    expect(validateDraft(flow)).toMatch(/JSON array/);
    flow.nodes[0].config.condition = {
      path: "input.roles",
      op: "in",
      value: ["reviewer"],
    };
    expect(validateDraft(flow)).toBeNull();
  });

  it("validates numeric execution limits without silently coercing values", () => {
    const flow = conditionFlow();
    flow.limits = { max_tokens: 1000001 };
    expect(validateDraft(flow)).toMatch(/out of range/);
    flow.limits = { max_tool_repairs: 1.5 };
    expect(validateDraft(flow)).toMatch(/must be an integer/);
    flow.limits = { max_duration_seconds: 0 };
    expect(validateDraft(flow)).toMatch(/out of range/);
  });
});

describe("advanced workflow JSON boundary", () => {
  it("applies a valid definition without replacing metadata or persisting layout", () => {
    const flow = conditionFlow();
    const parsed = parseAdvanced(
      JSON.stringify({ ...flow, name: undefined, version: undefined }),
      flow,
    );
    expect(parsed.name).toBe(flow.name);
    expect(parsed.version).toBe(flow.version);
    expect(parsed.nodes).toEqual(flow.nodes);
  });

  it.each(["owner", "id", "name", "version"])(
    "rejects metadata injection through %s",
    (field) => {
      expect(() =>
        parseAdvanced(
          JSON.stringify({ [field]: "unexpected" }),
          conditionFlow(),
        ),
      ).toThrow(/accepts nodes/);
    },
  );

  it.each(["constructor", "__proto__"])(
    "rejects a prototype property masquerading as a node kind: %s",
    (kind) => {
      const flow = conditionFlow();
      const nodes = [{ id: "invalid", kind, config: {} }];
      expect(() =>
        parseAdvanced(
          JSON.stringify({ nodes, edges: [], output_node_id: null }),
          flow,
        ),
      ).toThrow(/supported kind/);
    },
  );

  it.each(["constructor", "__proto__"])(
    "rejects a prototype property masquerading as a limit: %s",
    (key) => {
      expect(() =>
        parseAdvanced(`{"limits":{"${key}":1}}`, conditionFlow()),
      ).toThrow(/unsupported/);
    },
  );

  it("retains a legitimate prototype-like node ID safely", () => {
    const flow = conditionFlow();
    flow.nodes[0].id = "constructor";
    flow.edges[0].source = "constructor";
    expect(validateDraft(flow)).toBeNull();
  });

  it("rejects layout coordinates and other unknown node fields", () => {
    const node = { ...conditionFlow().nodes[0], x: 42 };
    expect(() =>
      parseAdvanced(
        JSON.stringify({ nodes: [node], edges: [], output_node_id: null }),
        conditionFlow(),
      ),
    ).toThrow(/unsupported field/);
  });

  it("rejects non-finite numbers before they could be serialized as null", () => {
    expect(() =>
      parseAdvanced('{"limits":{"max_tokens":1e999}}', conditionFlow()),
    ).toThrow(/finite/);
  });

  it("rejects deeply nested JSON before a render can exhaust the stack", () => {
    const text = '{"limits":' + "[".repeat(80) + "0" + "]".repeat(80) + "}";
    expect(() => parseAdvanced(text, conditionFlow())).toThrow(/deeply nested/);
  });

  it("bounds JSON size and reports invalid syntax", () => {
    expect(() => parseAdvanced(" ".repeat(1000001), conditionFlow())).toThrow(
      /too large/,
    );
    expect(() => parseAdvanced("{", conditionFlow())).toThrow(/valid JSON/);
  });
});
