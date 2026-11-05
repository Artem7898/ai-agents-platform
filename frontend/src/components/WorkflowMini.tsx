import { useId } from "react";
import type { WorkflowEdge, WorkflowNode } from "../lib/types";

export default function WorkflowMini({
  nodes,
  edges,
}: {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}) {
  const id = useId().replaceAll(":", "");
  const visible = nodes.slice(0, 10);
  const depth = new Map<string, number>();
  const visit = (nodeId: string, seen = new Set<string>()): number => {
    if (seen.has(nodeId)) return 0;
    if (depth.has(nodeId)) return depth.get(nodeId)!;
    seen.add(nodeId);
    const parents = edges.filter(
      (edge) =>
        edge.target === nodeId &&
        visible.some((node) => node.id === edge.source),
    );
    const level = parents.length
      ? Math.max(...parents.map((edge) => visit(edge.source, new Set(seen)))) +
        1
      : 0;
    depth.set(nodeId, level);
    return level;
  };
  visible.forEach((node) => visit(node.id));
  const maxDepth = Math.max(0, ...depth.values());
  const columns = Array.from({ length: maxDepth + 1 }, (_, level) =>
    visible.filter((node) => depth.get(node.id) === level),
  );
  const positions = new Map(
    visible.map((node) => {
      const group = columns[depth.get(node.id) ?? 0];
      return [
        node.id,
        {
          x: 30 + (depth.get(node.id) ?? 0) * 145,
          y: 90 + (group.indexOf(node) - (group.length - 1) / 2) * 72,
        },
      ];
    }),
  );
  const width = Math.max(470, (maxDepth + 1) * 145 + 30);
  return (
    <svg
      className="workflow-mini"
      viewBox={`0 0 ${width} 220`}
      role="img"
      aria-label="Saved workflow graph"
    >
      <defs>
        <marker
          id={`arrow-${id}`}
          viewBox="0 0 10 10"
          refX="8"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M0 0L10 5L0 10" fill="#80798f" />
        </marker>
      </defs>
      {edges.map((edge, index) => {
        const from = positions.get(edge.source),
          to = positions.get(edge.target);
        if (!from || !to) return null;
        return (
          <path
            key={index}
            d={`M ${from.x + 106} ${from.y + 18} C ${from.x + 125} ${from.y + 18} ${to.x - 18} ${to.y + 18} ${to.x - 6} ${to.y + 18}`}
            fill="none"
            stroke="#a59cb6"
            strokeWidth="1.6"
            markerEnd={`url(#arrow-${id})`}
          />
        );
      })}
      {visible.map((node) => {
        const position = positions.get(node.id)!;
        const human = node.kind === "hitl_approval";
        return (
          <g key={node.id} transform={`translate(${position.x},${position.y})`}>
            <title>
              {node.id} · {node.kind}
            </title>
            {human && (
              <g transform="translate(-4,-37)">
                <rect
                  width="120"
                  height="23"
                  rx="5"
                  fill="#fff5cd"
                  stroke="#e6d494"
                />
                <text
                  x="60"
                  y="15"
                  textAnchor="middle"
                  fill="#9f8128"
                  fontSize="9"
                >
                  Human approval
                </text>
              </g>
            )}
            <rect
              width="106"
              height="36"
              rx="7"
              fill={human ? "#f2eafc" : "#f8f5fd"}
              stroke={human ? "#8e6ac0" : "#b9a8d3"}
            />
            <text
              x="53"
              y="22"
              textAnchor="middle"
              fill="#504163"
              fontSize="11"
            >
              {node.id.length > 16 ? node.id.slice(0, 15) + "…" : node.id}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
