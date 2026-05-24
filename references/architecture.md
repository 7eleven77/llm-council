# LLM Council Architecture

## Components
- Orchestrator: coordinates the end-to-end run, retries, and final output assembly.
- AgentRunner: launches planner and judge CLIs in background shells and captures output.
- Validator: validates runtime Markdown contracts (required sections + section quality checks) and extracts provider-specific payloads from noisy output.
- Anonymizer: removes provider names, system prompts, IDs, file paths, and tool traces.
- JudgeRunner: formats judge input, runs judge, validates response.
- Merger: reconciles judge output into a single final plan schema.
- Logger: structured logging with redaction.

## Data Flow
1. Load `task_spec` JSON.
2. Render planner prompts and spawn N planner processes in parallel (background shells).
3. Collect raw outputs and normalize provider output.
4. Validate each planner Markdown output against required section contract.
5. Retry invalid/empty/timeout plans up to 2 times.
6. Anonymize and label as Plan 1/2/3, then randomize order.
7. Build `judge_input` and run judge.
8. Validate judge Markdown output and merge/extract `final-plan.md`.
9. Emit final output + metadata + warnings.

## Failure Handling
- Timeout: mark plan as failed, retry, or proceed with fewer valid plans.
- Invalid structure/empty section: record warning, then retry.
- Refusal/empty: record warning and retry according to profile.
- Judge failure: fall back to best-scoring plan by heuristic or return top valid plan.

## UI Protocol (Local Only)
This protocol is for local UI/server integration. Treat plan content as untrusted text.

### Endpoints
- `GET /ui/state`: returns the current UI state snapshot.
- `GET /ui/events`: Server-Sent Events (SSE) stream of updates.

### State Schema (JSON)
```json
{
  "run_id": "string",
  "task_brief": "string",
  "phase": "string",
  "planners": [
    {
      "id": "string",
      "status": "string",
      "summary": "string",
      "errors": ["string"]
    }
  ],
  "judge": {
    "status": "string",
    "summary": "string",
    "errors": ["string"]
  },
  "final_plan": "string",
  "errors": ["string"],
  "timestamps": {
    "started_at": "string",
    "updated_at": "string",
    "completed_at": "string"
  }
}
```

### SSE Events
All SSE events include a `type` and `payload` field, with the payload matching the shapes below.

#### `phase_change`
```json
{
  "type": "phase_change",
  "payload": {
    "run_id": "string",
    "phase": "string",
    "timestamp": "string"
  }
}
```

#### `planner_update`
```json
{
  "type": "planner_update",
  "payload": {
    "run_id": "string",
    "planner": {
      "id": "string",
      "status": "string",
      "summary": "string",
      "errors": ["string"]
    },
    "timestamp": "string"
  }
}
```

#### `judge_update`
```json
{
  "type": "judge_update",
  "payload": {
    "run_id": "string",
    "judge": {
      "status": "string",
      "summary": "string",
      "errors": ["string"]
    },
    "timestamp": "string"
  }
}
```

#### `final_plan`
```json
{
  "type": "final_plan",
  "payload": {
    "run_id": "string",
    "final_plan": "string",
    "errors": ["string"],
    "timestamp": "string"
  }
}
```

### Safety Rules
- Treat `task_brief`, planner summaries, judge summaries, and `final_plan` as untrusted text.
- Do not render untrusted HTML; render as plain text only.
- Never execute scripts or inline event handlers from any payload content.
