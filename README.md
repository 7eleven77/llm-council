# LLM Council

Multi-agent planning orchestrator for AI CLIs (Codex, Claude, Gemini, OpenCode, or custom commands).

`llm-council` runs multiple planner agents in parallel, anonymizes and randomizes their plans, then uses a judge agent to produce a final plan. It also includes a local minimalist UI for live status, runtime tuning, and post-run refine/accept workflows.

Current focus: Linux-first runtime and setup flow.

## What it does

- Runs 2+ planner agents in parallel.
- Validates planner and judge markdown structure.
- Supports model fallback lists per agent.
- Applies profile-based timeout/retry behavior (`fast`, `balanced`, `deep`).
- Kills full process groups on timeout (not only parent process).
- Performs runtime preflight checks (command presence and API env vars).
- Writes complete run artifacts (`final-plan.md`, `run.json`, `run-metadata.json`, alternatives, recommendations).
- Provides a local UI with:
  - Agent pulse/status
  - Runtime default controls
  - Add/remove/toggle agents
  - Judge selection
  - Model catalog refresh
  - Final plan refine/save/accept

## Quick start (Linux)

From repo root:

```bash
./setup.sh
```

`setup.sh` will:

1. Check/install core dependencies (`python3`, `git`, `curl`, `node`, `npm`)
2. Attempt to install AI CLIs (`codex`, `claude`, `gemini`, `opencode`) via package manager and/or npm
3. Launch interactive configuration

Then run a task:

```bash
python3 scripts/llm_council.py run --spec references/task-spec.example.json
```

## Core commands

Configure agents and runtime defaults:

```bash
python3 scripts/llm_council.py configure
python3 scripts/llm_council.py configure --config /custom/path/agents.json
```

When a TTY is available, `configure` starts a full-screen setup wizard (provider-first UX).  
If TTY is unavailable, it falls back to text prompts.

Run council:

```bash
python3 scripts/llm_council.py run --spec /path/to/task-spec.json
```

Run without UI:

```bash
python3 scripts/llm_council.py run --spec /path/to/task-spec.json --no-ui
```

Resume UI for an existing run:

```bash
python3 scripts/llm_council.py ui --run-dir /path/to/run-dir
```

Agent preflight check:

```bash
python3 scripts/llm_council.py agents test
```

Refresh model catalog:

```bash
python3 scripts/llm_council.py models refresh
```

## Workflows and profiles

### Workflows

- `quick-decide`: defaults to `fast`, plan-only
- `full-council`: defaults to `balanced`, full run
- `risk-review`: defaults to `deep`, full run, higher min valid planners

### Profiles

- `fast`: short timeout windows, no retries
- `balanced`: standard timeout windows, 2 retries
- `deep`: longer timeout windows, 3 retries

Global timeout cap (`--timeout`) clamps profile timeouts.

## Runtime defaults and config path

Default config path:

- `$XDG_CONFIG_HOME/llm-council/agents.json`
- fallback: `~/.config/llm-council/agents.json`

Runtime defaults include:

- `workflow`
- `profile`
- `timeout_sec` (clamped to 5..3600)
- `min_valid_planners`
- `plan_only`

If configured `min_valid_planners` exceeds enabled planner count, runtime auto-clamps it for that run.  
If you explicitly pass `--min-valid-planners` too high, the command fails fast.

## Task spec

Minimal:

```json
{
  "task": "Implement X with tests and rollback plan."
}
```

See full example:

- `references/task-spec.example.json`

## Output artifacts

Run directories are created under:

- `./runs/...` when executed from repo root
- otherwise `./llm-council/runs/...`
- override with `LLM_COUNCIL_RUN_ROOT`

Common artifacts:

- `plan-<agent>.md`
- `alternatives.md` / `alternatives.json`
- `judge.md`
- `final-plan.md`
- `run-metadata.json`
- `run.json`
- `recommendations.md` (on failures)
- `agent-checks.md` / `agent-checks.json` (full runs)

## Validation contracts

Planner output must include:

- `# Plan`
- `## Overview`
- `## Scope`
- `## Phases` (with at least one `### Phase ...`)
- `## Testing Strategy`
- `## Risks`
- `## Rollback Plan`
- `## Edge Cases`

Judge output must include:

- `# Judge Report`
- `## Scores`
- `## Comparative Analysis`
- `## Missing Steps`
- `## Contradictions`
- `## Improvements`
- `## Final Plan` (must contain `# Plan`)

## Troubleshooting

`Uh oh! Your models are not configured`  
- Run `./setup.sh` or `python3 scripts/llm_council.py configure`

`Agent runtime preflight failed`  
- Install missing CLI command(s)
- If `auth_mode=api`, set required env var(s)

`Insufficient valid planner outputs`  
- Try `--profile balanced` or `--profile deep`
- Add `fallback_models` for planners
- Lower `--min-valid-planners` (or adjust defaults)

`Judge output invalid`  
- Use a stronger judge model
- Try `--profile deep`

## Current status

- Linux runtime flow is the primary maintained path.
- `setup.ps1` and `setup.bat` are present, but Linux setup is currently the most complete.
