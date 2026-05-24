# Data Contracts

## Input: task_spec
- Required: `task` (string)
- Optional: `constraints` (array of strings), `repo_context`, `preferences`, `agents`
- Schema: `schemas/task_spec.schema.json`

## Planner Output (Current Runtime)
- Runtime contract: Markdown plan with required sections:
  - `# Plan`
  - `## Overview`
  - `## Scope`
  - `## Phases` (must include at least one `### Phase ...`)
  - `## Testing Strategy`
  - `## Risks`
  - `## Rollback Plan`
  - `## Edge Cases`
- Validation: required headings + minimum content per section + phase heading checks.

## Planner Output (Legacy/Optional Structured Contract)
- Structured JSON schema remains available at `schemas/council_plan.schema.json` for integrations that emit JSON.

## Judge Input
- task_spec + list of labeled plans (Plan 1/2/3) + rubric
- Schema: `schemas/judge_input.schema.json`

## Judge Output (Current Runtime)
- Runtime contract: Markdown judge report with required sections:
  - `# Judge Report`
  - `## Scores`
  - `## Comparative Analysis`
  - `## Missing Steps`
  - `## Contradictions`
  - `## Improvements`
  - `## Final Plan` (must include a `# Plan` block)
- Validation: required headings + minimum content per section + final plan block checks.

## Judge Output (Legacy/Optional Structured Contract)
- Structured JSON schema remains available at `schemas/judge_output.schema.json` for integrations that emit JSON.

## Final Output (Current Runtime)
- `run.json` and `run-metadata.json` with:
  - final plan markdown
  - alternatives
  - validation flags
  - warnings/recommendations

## Final Output (Legacy/Optional Structured Contract)
- Structured JSON schema remains available at `schemas/final_plan.schema.json` for integrations that emit JSON.
