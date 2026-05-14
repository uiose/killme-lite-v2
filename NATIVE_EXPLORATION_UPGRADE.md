# Native Exploration Upgrade

This note records the second-pass upgrade made after reviewing the critique that the first exploration mode was still too close to the original adversarial decision pipeline.

## Problem diagnosed

The first implementation separated `agenda_mode = decision | exploration`, but it still had several convergence pressures:

- Roles were still primarily framed as adversarial reviewers.
- Chair scheduling could behave like a linear pipe: hypotheses -> gaps -> research paths -> decision node.
- Exploration had a strong implicit endpoint: convert to decision mode.
- `/auto` kept the decision-mode cap of 3 steps.
- Merger sorting rewarded high support and high confidence, which can suppress rare but valuable anomalies.
- Clone outputs were independent only, which is useful for review but weak for collaborative exploration.
- State used flat lists and could not express relations between hypotheses, findings, gaps and evidence.

## Implemented changes

### 1. Exploration as first-class agenda

Exploration can now remain open. `decision_candidates` are optional future entry points, not the default endpoint.

New state fields:

```json
{
  "exploration_status": "open | paused | synthesized",
  "decision_candidates": [],
  "anomalies": [],
  "exploration_nodes": [],
  "exploration_edges": []
}
```

### 2. Relationship graph

The state now supports a lightweight exploration graph:

- `exploration_nodes`: hypotheses, findings, threads, gaps, anomalies, focus nodes, candidate nodes.
- `exploration_edges`: support, challenge, relates_to, fills_gap, opens_thread, refines, etc.

This makes it possible to express relationships such as:

- finding A supports hypothesis H1;
- anomaly X challenges hypothesis H2;
- research thread T may fill coverage gap G;
- decision candidate D is derived from a specific branch.

### 3. Diversity/anomaly-first Merger

In decision mode, Merger still sorts by severity, confidence, evidence and support count.

In exploration mode, Merger uses a different priority:

- preserve anomalies and edge cases;
- preserve single-source or minority observations;
- preserve coverage gaps and research threads;
- avoid collapsing multiple branches into one strongest point.

### 4. Visible clone mode

New command:

```text
/clone-mode visible
```

Modes:

- `independent`: each clone sees the same frozen base state.
- `visible`: later clones in the same group can see earlier sibling outputs and extend them.

State patches are still held until Merger finishes, so visible mode supports brainstorming without mid-run state pollution.

### 5. Deeper exploration auto limit

`/auto` limits are now agenda-specific:

- decision mode: max 3 steps;
- exploration mode: max 12 steps.

This gives open research enough room to generate hypotheses, scan gaps, propose research routes, mark anomalies and update the map.

### 6. New exploration commands

```text
/map
/focus <branch-or-subquestion>
/clone-mode <independent|visible>
```

`/map` gives a compact view of the exploration graph. `/focus` lets users branch, backtrack or deepen a subquestion without closing the session.

### 7. Prompt changes

Agent prompts were updated so exploration is not just a field-name patch:

- Chair: graph-based exploration scheduling; no default decision endpoint.
- Executioner: boundary/anomaly scanner in exploration mode.
- Defender: hypothesis/mechanism cartographer in exploration mode.
- Builder: research operations and source-route designer in exploration mode.
- Merger: diversity/anomaly-first exploration merge.
- Judge: remains unavailable for verdicts in exploration; may only suggest candidate decision nodes when asked.

## Deliberately not added yet

Active web retrieval was not built into the runtime. The project still keeps `evidence_requests` separate from `evidence_items` so sources remain explicit, reproducible and auditable. A future retrieval adapter can be added on top of that boundary.

## Test status

```text
44 passed
```
