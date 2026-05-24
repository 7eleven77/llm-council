#!/usr/bin/env python3
import argparse
import json
import os
import queue
from pathlib import Path
import random
import re
import shutil
import shlex
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
import webbrowser
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import ui_server

RETRY_LIMIT = 2
DEFAULT_TIMEOUT_SEC = 180
MIN_TIMEOUT_SEC = 5
MAX_TIMEOUT_SEC = 3600
DEFAULT_UI_KEEPALIVE_SEC = 20 * 60
DEFAULT_UI_SESSION_TTL_SEC = 30 * 60

CODEX_MODEL = "gpt-5.2-codex"
CODEX_REASONING = "xhigh"
CLAUDE_MODEL = "opus"
GEMINI_MODEL = "gemini-3-pro-preview"

PROFILE_SETTINGS: Dict[str, Dict[str, Any]] = {
    "fast": {
        "planner_timeout": 120,
        "judge_timeout": 120,
        "retry_limit": 0,
        "planner_hint": "Keep the plan concise. Prefer short, direct bullets.",
    },
    "balanced": {
        "planner_timeout": 240,
        "judge_timeout": 240,
        "retry_limit": 2,
        "planner_hint": "Keep the plan concise but complete.",
    },
    "deep": {
        "planner_timeout": 420,
        "judge_timeout": 420,
        "retry_limit": 3,
        "planner_hint": "Be thorough and explicit. Include more detailed reasoning.",
    },
}
WORKFLOW_SETTINGS: Dict[str, Dict[str, Any]] = {
    "quick-decide": {
        "profile": "fast",
        "plan_only": True,
        "min_valid_planners": 2,
        "workflow_hint": "Prioritize speed and concise options.",
    },
    "full-council": {
        "profile": "balanced",
        "plan_only": False,
        "min_valid_planners": 2,
        "workflow_hint": "Balance quality and speed.",
    },
    "risk-review": {
        "profile": "deep",
        "plan_only": False,
        "min_valid_planners": 3,
        "workflow_hint": "Prioritize risk analysis and failure modes.",
    },
}
RECOMMENDED_MODELS: Dict[str, List[str]] = {
    "codex": ["gpt-5.2-codex", "gpt-5-codex"],
    "claude": ["opus", "sonnet"],
    "gemini": ["gemini-3-pro-preview", "gemini-2.5-pro"],
    "opencode": ["openai/gpt-5.2-codex", "anthropic/claude-sonnet-4-5"],
}
WORKFLOW_CHOICES: Tuple[str, ...] = tuple(WORKFLOW_SETTINGS.keys())
PROFILE_CHOICES: Tuple[str, ...] = tuple(PROFILE_SETTINGS.keys())
DEFAULT_RUNTIME_DEFAULTS: Dict[str, Any] = {
    "workflow": "full-council",
    "profile": "balanced",
    "timeout_sec": DEFAULT_TIMEOUT_SEC,
    "min_valid_planners": 2,
    "plan_only": False,
}

@dataclass
class AgentConfig:
    name: str
    kind: str
    command: Optional[str] = None
    output_format: str = "text"
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    agent: Optional[str] = None
    attach: Optional[str] = None
    cli_format: Optional[str] = None
    prompt_mode: str = "arg"
    extra_args: List[str] = field(default_factory=list)
    fallback_models: List[str] = field(default_factory=list)
    auth_mode: str = "login"
    api_env_var: Optional[str] = None
    enabled: bool = True

@dataclass
class AgentResult:
    name: str
    raw_output: str
    data: Optional[Dict[str, Any]]
    valid: bool
    error: Optional[str]


@dataclass
class RunningAgent:
    config: AgentConfig
    prompt: str
    start_time: float
    process: Any


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def extract_json_array(text: str) -> Optional[List[Any]]:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def extract_agent_response(config: AgentConfig, raw: str) -> str:
    kind = (config.kind or config.name).lower()
    if kind == "codex":
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event") or event.get("type")
            if kind == "turn.completed":
                content = event.get("content")
                if isinstance(content, str):
                    return content
                message = event.get("message")
                if isinstance(message, dict):
                    msg_content = message.get("content")
                    if isinstance(msg_content, str):
                        return msg_content
            if kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict):
                    if item.get("type") in ("agent_message", "assistant_message"):
                        text = item.get("text")
                        if isinstance(text, str):
                            return text
        return raw

    if kind == "claude":
        envelope = extract_json(raw)
        if isinstance(envelope, dict):
            result = envelope.get("result")
            if isinstance(result, str):
                return result
            message = envelope.get("message")
            if isinstance(message, dict):
                content_list = message.get("content")
                if isinstance(content_list, list):
                    for block in content_list:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            return block["text"]
        events = extract_json_array(raw)
        if events is None:
            return raw
        if isinstance(events, list):
            for item in reversed(events):
                if isinstance(item, dict) and item.get("type") == "result":
                    result = item.get("result")
                    if isinstance(result, str):
                        return result
            for item in reversed(events):
                if isinstance(item, dict) and item.get("type") == "assistant":
                    msg = item.get("message")
                    if isinstance(msg, dict):
                        content_list = msg.get("content")
                        if isinstance(content_list, list):
                            for block in content_list:
                                if isinstance(block, dict) and isinstance(block.get("text"), str):
                                    return block["text"]
        return raw

    if kind == "gemini":
        envelope = extract_json(raw)
        if envelope is None:
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                return raw
        if isinstance(envelope, dict):
            for key in ("response", "completion", "content", "output", "text"):
                value = envelope.get(key)
                if isinstance(value, str):
                    return value
            content = envelope.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        return item["text"]
        return raw

    if kind == "opencode":
        # Prefer OpenCode JSON event stream output when --format json is used.
        text_parts: List[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            direct_text = event.get("text")
            if isinstance(direct_text, str):
                text_parts.append(direct_text)
                continue
            part = event.get("part")
            if isinstance(part, dict):
                part_text = part.get("text")
                if isinstance(part_text, str):
                    text_parts.append(part_text)
        if text_parts:
            return "".join(text_parts).strip()
        envelope = extract_json(raw)
        if envelope is None:
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                envelope = None
        if isinstance(envelope, dict):
            for key in ("response", "completion", "content", "output", "text", "message"):
                value = envelope.get(key)
                if isinstance(value, str):
                    return value
            content = envelope.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        return item["text"]
        if isinstance(envelope, list):
            for item in reversed(envelope):
                if isinstance(item, dict):
                    for key in ("content", "text", "message", "output"):
                        value = item.get(key)
                        if isinstance(value, str):
                            return value
        return raw

    return raw


def _build_command_and_input(config: AgentConfig, prompt: str) -> Tuple[List[str], Optional[str]]:
    kind = (config.kind or config.name).lower()
    if kind == "codex":
        model = config.model or CODEX_MODEL
        reasoning = config.reasoning_effort or CODEX_REASONING
        args = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-m",
            model,
            "-c",
            f"model_reasoning_effort={reasoning}",
        ]
        args.extend(config.extra_args)
        args.append(prompt)
        return (
            args,
            None,
        )
    if kind == "gemini":
        model = config.model or GEMINI_MODEL
        args = ["gemini", "--output-format", "json"]
        if model:
            args.extend(["--model", model])
        args.extend(config.extra_args)
        args.extend(["-p", prompt])
        return (
            args,
            None,
        )
    if kind == "claude":
        model = config.model or CLAUDE_MODEL
        args = [
            "claude",
            "--output-format",
            "json",
            "--model",
            model,
            "--max-turns",
            "1",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--tools",
            "",
            "--disable-slash-commands",
        ]
        args.extend(config.extra_args)
        args.extend(["-p", prompt])
        return (
            args,
            None,
        )
    if kind == "opencode":
        args = ["opencode", "run"]
        args.extend(config.extra_args)
        if config.model:
            args.extend(["--model", config.model])
        if config.agent:
            args.extend(["--agent", config.agent])
        if config.cli_format:
            args.extend(["--format", config.cli_format])
        if config.attach:
            args.extend(["--attach", config.attach])
        args.append(prompt)
        return (args, None)
    if not config.command:
        raise ValueError(f"custom agent '{config.name}' requires a command")
    args = shlex.split(config.command)
    if config.extra_args:
        args.extend(config.extra_args)
    if (config.prompt_mode or "stdin").lower() == "stdin":
        return (args, prompt + "\n")
    return (args + [prompt], None)


def spawn_cli_agent(config: AgentConfig, prompt: str) -> RunningAgent:
    args, stdin_payload = _build_command_and_input(config, prompt)
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_payload is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if stdin_payload is not None and process.stdin:
        process.stdin.write(stdin_payload)
        process.stdin.close()
    return RunningAgent(config=config, prompt=prompt, start_time=time.time(), process=process)


def collect_cli_output(running: RunningAgent, timeout_sec: int) -> str:
    try:
        stdout, stderr = running.process.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        _kill_running_agent(running)
        stdout, stderr = running.process.communicate()
        raise TimeoutError(f"{running.config.name} timed out") from exc
    combined = stdout or ""
    if stderr:
        combined = combined + "\n" + stderr
    return combined


def _kill_running_agent(running: RunningAgent) -> None:
    try:
        os.killpg(running.process.pid, signal.SIGKILL)
    except OSError:
        try:
            running.process.kill()
        except OSError:
            pass


def collect_cli_output_until(running: RunningAgent, deadline_monotonic: float) -> str:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        if running.process.poll() is not None:
            stdout, stderr = running.process.communicate()
            combined = stdout or ""
            if stderr:
                combined = combined + "\n" + stderr
            return combined
        _kill_running_agent(running)
        running.process.communicate()
        raise TimeoutError(f"{running.config.name} timed out")
    try:
        stdout, stderr = running.process.communicate(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _kill_running_agent(running)
        stdout, stderr = running.process.communicate()
        raise TimeoutError(f"{running.config.name} timed out") from exc
    combined = stdout or ""
    if stderr:
        combined = combined + "\n" + stderr
    return combined


def anonymize_text(text: str) -> str:
    patterns = [
        r"codex",
        r"claude",
        r"gemini",
        r"opencode",
        r"openai",
        r"anthropic",
        r"google",
        r"gpt[-_\\w]*",
        r"sk-[A-Za-z0-9]{10,}",
        r"system prompt",
        r"tool trace",
        r"trace id",
    ]
    pattern = re.compile("|".join(patterns), flags=re.IGNORECASE)
    return pattern.sub("[REDACTED]", text)


def validate_markdown_plan(text: str) -> Tuple[bool, Optional[str]]:
    required = [
        "# Plan",
        "## Overview",
        "## Scope",
        "## Phases",
        "## Testing Strategy",
        "## Risks",
        "## Rollback Plan",
        "## Edge Cases",
    ]
    missing = [header for header in required if header not in text]
    if missing:
        return False, "missing headers: " + ", ".join(missing)
    section_requirements = {
        "## Overview": 20,
        "## Scope": 20,
        "## Phases": 30,
        "## Testing Strategy": 20,
        "## Risks": 20,
        "## Rollback Plan": 15,
        "## Edge Cases": 15,
    }
    for heading, min_len in section_requirements.items():
        body = _extract_markdown_section(text, heading)
        if len(body.strip()) < min_len:
            return False, f"section too short: {heading}"
    if "### Phase" not in text:
        return False, "phases section must contain at least one '### Phase' heading"
    return True, None


def validate_markdown_judge(text: str) -> Tuple[bool, Optional[str]]:
    required = [
        "# Judge Report",
        "## Scores",
        "## Comparative Analysis",
        "## Missing Steps",
        "## Contradictions",
        "## Improvements",
        "## Final Plan",
    ]
    missing = [header for header in required if header not in text]
    if missing:
        return False, "missing headers: " + ", ".join(missing)
    section_requirements = {
        "## Scores": 12,
        "## Comparative Analysis": 20,
        "## Missing Steps": 12,
        "## Contradictions": 12,
        "## Improvements": 12,
    }
    for heading, min_len in section_requirements.items():
        body = _extract_markdown_section(text, heading)
        if len(body.strip()) < min_len:
            return False, f"section too short: {heading}"
    final_plan_body = _extract_markdown_section(text, "## Final Plan")
    if "# Plan" not in final_plan_body:
        return False, "final plan section must include a '# Plan' block"
    return True, None


def _extract_markdown_section(text: str, heading: str) -> str:
    match = re.search(
        rf"^{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    return match.group(1).strip()


def _ui_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ui_deadline_from_now(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _ui_truncate(text: str, max_len: int = 600) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len].rstrip() + "…"


def _ui_update_timestamp(state: Dict[str, Any], timestamp: str) -> None:
    timestamps = state.get("timestamps")
    if not isinstance(timestamps, dict):
        timestamps = {}
    if "started_at" not in timestamps:
        timestamps["started_at"] = timestamp
    timestamps["updated_at"] = timestamp
    state["timestamps"] = timestamps


def _ui_emit(ui_instance: Optional["ui_server.UIServer"], event_type: str, payload: Dict[str, Any]) -> None:
    if not ui_instance:
        return
    ui_instance.broadcast({"type": event_type, "payload": payload})


def _ui_set_session_state(
    ui_state: Optional["ui_server.UIState"],
    ui_instance: Optional["ui_server.UIServer"],
    keep_open: bool,
    deadline: Optional[str],
    timestamp: str,
) -> None:
    if not ui_state:
        return
    def mutator(state: Dict[str, Any]) -> None:
        state["keep_open"] = keep_open
        state["ui_deadline"] = deadline or ""
        _ui_update_timestamp(state, timestamp)
    ui_state.mutate(mutator)
    _ui_emit(
        ui_instance,
        "session_update",
        {"keep_open": keep_open, "ui_deadline": deadline or "", "timestamp": timestamp},
    )


def _ui_set_phase(
    ui_state: Optional["ui_server.UIState"],
    ui_instance: Optional["ui_server.UIServer"],
    phase: str,
    timestamp: str,
) -> None:
    if not ui_state:
        return
    def mutator(state: Dict[str, Any]) -> None:
        state["phase"] = phase
        _ui_update_timestamp(state, timestamp)
    ui_state.mutate(mutator)
    _ui_emit(ui_instance, "phase_change", {"phase": phase, "timestamp": timestamp})


def _ui_upsert_planner(
    ui_state: Optional["ui_server.UIState"],
    ui_instance: Optional["ui_server.UIServer"],
    planner_id: str,
    status: str,
    summary: str,
    errors: Optional[List[str]],
    timestamp: str,
) -> None:
    if not ui_state:
        return
    entry = {"id": planner_id, "status": status, "summary": summary, "errors": errors or []}
    def mutator(state: Dict[str, Any]) -> None:
        planners = state.get("planners")
        if not isinstance(planners, list):
            planners = []
        index = next((i for i, item in enumerate(planners) if item.get("id") == planner_id), None)
        if index is None:
            planners.append(entry)
        else:
            planners[index] = entry
        state["planners"] = planners
        _ui_update_timestamp(state, timestamp)
    ui_state.mutate(mutator)
    _ui_emit(ui_instance, "planner_update", {"planner": entry, "timestamp": timestamp})


def _ui_update_judge(
    ui_state: Optional["ui_server.UIState"],
    ui_instance: Optional["ui_server.UIServer"],
    status: str,
    summary: str,
    errors: Optional[List[str]],
    timestamp: str,
) -> None:
    if not ui_state:
        return
    judge_entry = {"status": status, "summary": summary, "errors": errors or []}
    def mutator(state: Dict[str, Any]) -> None:
        state["judge"] = judge_entry
        _ui_update_timestamp(state, timestamp)
    ui_state.mutate(mutator)
    _ui_emit(ui_instance, "judge_update", {"judge": judge_entry, "timestamp": timestamp})


def _ui_set_final_plan(
    ui_state: Optional["ui_server.UIState"],
    ui_instance: Optional["ui_server.UIServer"],
    final_plan: str,
    timestamp: str,
) -> None:
    if not ui_state:
        return
    def mutator(state: Dict[str, Any]) -> None:
        state["final_plan"] = final_plan
        _ui_update_timestamp(state, timestamp)
    ui_state.mutate(mutator)
    _ui_emit(ui_instance, "final_plan", {"final_plan": final_plan, "timestamp": timestamp})


def _ui_action_result(
    ui_instance: Optional["ui_server.UIServer"],
    action: str,
    status: str,
    message: str,
    url: Optional[str],
    timestamp: str,
) -> None:
    if not ui_instance:
        return
    payload = {"action": action, "status": status, "message": message, "timestamp": timestamp}
    if url:
        payload["url"] = url
    _ui_emit(ui_instance, "action_result", payload)


class _KeepaliveController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keep_open = False

    def set_keep_open(self, value: bool) -> None:
        with self._lock:
            self.keep_open = value

    def should_keep_open(self) -> bool:
        with self._lock:
            return self.keep_open


def _parse_ui_deadline(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _start_ui_session_timer(
    ui_instance: "ui_server.UIServer",
    ui_state: "ui_server.UIState",
    stop_event: threading.Event,
    keepalive: Optional[_KeepaliveController],
) -> None:
    def run() -> None:
        while not stop_event.is_set():
            state = ui_state.get()
            keep_open = bool(state.get("keep_open"))
            if keepalive and keepalive.should_keep_open():
                keep_open = True
            if not keep_open:
                deadline = _parse_ui_deadline(state.get("ui_deadline"))
                if deadline and datetime.now(timezone.utc) >= deadline:
                    _ui_action_result(
                        ui_instance,
                        "session",
                        "expired",
                        "session expired",
                        None,
                        _ui_timestamp(),
                    )
                    stop_event.set()
                    ui_instance.shutdown()
                    break
            time.sleep(1)

    thread = threading.Thread(target=run, name="ui-session-timer", daemon=True)
    thread.start()


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _build_refine_prompt(plan_template: str, task_brief: str, final_plan: str, context: str) -> str:
    notes = context.strip() if context else "No extra context provided."
    return (
        "You are refining a plan. Return only updated plan markdown that follows the template below.\n\n"
        f"Task brief:\n{task_brief}\n\n"
        f"Template:\n{plan_template}\n\n"
        f"Current plan:\n{final_plan}\n\n"
        f"Refinement request:\n{notes}\n"
    )


def _rebuild_ui_state_from_run(run_dir: Path) -> Dict[str, Any]:
    planners = []
    for plan_path in sorted(run_dir.glob("plan-*.md")):
        name = plan_path.stem[len("plan-") :]
        if name.endswith("-attempt1") or name.endswith("-attempt2") or name.endswith("-attempt3"):
            continue
        planners.append(
            {
                "id": name,
                "status": "complete",
                "summary": load_text(str(plan_path)),
                "errors": [],
            }
        )
    judge_path = run_dir / "judge.md"
    final_path = run_dir / "final-plan.md"
    return {
        "run_id": run_dir.name,
        "task_brief": "",
        "phase": "complete",
        "planners": planners,
        "judge": {
            "status": "complete" if judge_path.exists() else "unknown",
            "summary": load_text(str(judge_path)) if judge_path.exists() else "",
            "errors": [],
        },
        "final_plan": load_text(str(final_path)) if final_path.exists() else "",
        "errors": [],
        "config_agents": [],
        "config_judge": "",
        "model_catalog": {"items": [], "updated_at": ""},
        "runtime_defaults": dict(DEFAULT_RUNTIME_DEFAULTS),
        "runtime_options": _runtime_defaults_options(),
        "run_settings": {},
        "keep_open": False,
        "ui_deadline": _ui_deadline_from_now(DEFAULT_UI_SESSION_TTL_SEC),
        "timestamps": {"started_at": "", "updated_at": _ui_timestamp()},
    }


def _next_numbered_final_plan_path(run_dir: Path) -> Path:
    pattern = re.compile(r"^final-plan-(\d+)\.md$")
    max_num = 0
    for path in run_dir.glob("final-plan-*.md"):
        match = pattern.match(path.name)
        if not match:
            continue
        max_num = max(max_num, int(match.group(1)))
    return run_dir / f"final-plan-{max_num + 1}.md"


def _handle_ui_actions(
    ui_instance: "ui_server.UIServer",
    ui_state: Optional["ui_server.UIState"],
    run_dir: Path,
    task_spec: Dict[str, Any],
    args: argparse.Namespace,
    config_path: Path,
    stop_event: threading.Event,
    keepalive: Optional[_KeepaliveController] = None,
    judge: Optional[AgentConfig] = None,
    plan_template: Optional[str] = None,
) -> None:
    def sync_config_state() -> Dict[str, Any]:
        config_data = _load_agents_config_runtime(config_path)
        planners = config_data.get("planners") if isinstance(config_data.get("planners"), list) else []
        if not planners and ui_state:
            snapshot_agents = ui_state.get().get("config_agents")
            if isinstance(snapshot_agents, list):
                planners = []
                for item in snapshot_agents:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    planners.append(
                        {
                            "name": name,
                            "kind": str(item.get("kind") or "custom"),
                            "model": str(item.get("model") or ""),
                            "enabled": bool(item.get("enabled", True)),
                        }
                    )
                config_data["planners"] = planners
        runtime_defaults = _runtime_defaults_from_config(config_data)
        config_data["runtime_defaults"] = runtime_defaults
        agent_rows: List[Dict[str, Any]] = []
        for item in planners:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            agent_rows.append(
                {
                    "name": name,
                    "kind": str(item.get("kind") or "custom"),
                    "model": str(item.get("model") or ""),
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        judge_obj = config_data.get("judge") if isinstance(config_data.get("judge"), dict) else {}
        judge_name = str(judge_obj.get("name") or "")
        catalog = refresh_model_catalog(config_data)
        if ui_state:
            def mutator(state: Dict[str, Any]) -> None:
                state["config_agents"] = agent_rows
                state["config_judge"] = judge_name
                state["model_catalog"] = catalog
                state["runtime_defaults"] = runtime_defaults
                state["runtime_options"] = _runtime_defaults_options()
                _ui_update_timestamp(state, _ui_timestamp())
            ui_state.mutate(mutator)
        return config_data

    sync_config_state()

    while not stop_event.is_set():
        try:
            action = ui_instance.actions.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            path = action.path
            payload = action.payload or {}
            if path == "/api/save":
                final_plan = _coerce_text(payload.get("final_plan"))
                save_path = _next_numbered_final_plan_path(run_dir)
                save_path.write_text(final_plan, encoding="utf-8")
                _ui_action_result(
                    ui_instance,
                    "save",
                    "saved",
                    f"Saved at {save_path.resolve()}!",
                    None,
                    _ui_timestamp(),
                )
                continue
            if path == "/api/accept":
                final_plan = _coerce_text(payload.get("final_plan"))
                accept_path = run_dir / "final-plan-accepted.md"
                accept_path.write_text(final_plan, encoding="utf-8")
                final_path = run_dir / "final-plan.md"
                final_path.write_text(final_plan, encoding="utf-8")
                _ui_set_final_plan(ui_state, ui_instance, final_plan, _ui_timestamp())
                _ui_action_result(
                    ui_instance,
                    "accept",
                    "accepted",
                    "accepted plan and closing UI",
                    None,
                    _ui_timestamp(),
                )
                stop_event.set()
                ui_instance.shutdown()
                continue
            if path == "/api/refine":
                if not judge or not plan_template:
                    _ui_action_result(ui_instance, "refine", "failed", "refine unavailable", None, _ui_timestamp())
                    continue
                context = _coerce_text(payload.get("context")).strip()
                final_plan = _coerce_text(payload.get("final_plan")).strip()
                if not final_plan:
                    _ui_action_result(ui_instance, "refine", "failed", "no plan to refine", None, _ui_timestamp())
                    continue
                start_ts = _ui_timestamp()
                _ui_update_judge(
                    ui_state,
                    ui_instance,
                    status="running",
                    summary="refining…",
                    errors=[],
                    timestamp=start_ts,
                )
                task_brief = build_task_brief(task_spec)
                prompt = _build_refine_prompt(plan_template, task_brief, final_plan, context)
                running = spawn_cli_agent(judge, prompt)
                try:
                    refine_timeout = _coerce_int(
                        getattr(args, "timeout", DEFAULT_TIMEOUT_SEC),
                        DEFAULT_TIMEOUT_SEC,
                        minimum=MIN_TIMEOUT_SEC,
                        maximum=MAX_TIMEOUT_SEC,
                    )
                    raw = collect_cli_output(running, refine_timeout)
                except TimeoutError as exc:
                    _ui_update_judge(
                        ui_state,
                        ui_instance,
                        status="failed",
                        summary=str(exc),
                        errors=[str(exc)],
                        timestamp=_ui_timestamp(),
                    )
                    _ui_action_result(ui_instance, "refine", "failed", str(exc), None, _ui_timestamp())
                    continue
                normalized = extract_agent_response(judge, raw).strip()
                valid, err = validate_markdown_plan(normalized)
                if not valid:
                    _ui_update_judge(
                        ui_state,
                        ui_instance,
                        status="needs-fix",
                        summary=normalized,
                        errors=[err] if err else [],
                        timestamp=_ui_timestamp(),
                    )
                    _ui_action_result(ui_instance, "refine", "failed", err or "invalid plan", None, _ui_timestamp())
                    continue
                refined_name = f"final-plan-refined-{time.strftime('%Y%m%d-%H%M%S')}.md"
                refined_path = run_dir / refined_name
                refined_path.write_text(normalized, encoding="utf-8")
                final_path = run_dir / "final-plan.md"
                final_path.write_text(normalized, encoding="utf-8")
                _ui_set_final_plan(ui_state, ui_instance, normalized, _ui_timestamp())
                _ui_update_judge(
                    ui_state,
                    ui_instance,
                    status="complete",
                    summary=normalized,
                    errors=[],
                    timestamp=_ui_timestamp(),
                )
                _ui_action_result(ui_instance, "refine", "complete", "refined plan saved", None, _ui_timestamp())
                continue
            if path == "/api/keepalive":
                keep_open = bool(payload.get("keep_open"))
                if keepalive:
                    keepalive.set_keep_open(keep_open)
                deadline = "" if keep_open else _ui_deadline_from_now(DEFAULT_UI_SESSION_TTL_SEC)
                _ui_set_session_state(ui_state, ui_instance, keep_open, deadline, _ui_timestamp())
                status = "enabled" if keep_open else "disabled"
                _ui_action_result(ui_instance, "keepalive", status, f"keep open {status}", None, _ui_timestamp())
                continue
            if path == "/api/models-refresh":
                sync_config_state()
                _ui_action_result(ui_instance, "models-refresh", "ok", "model catalog refreshed", None, _ui_timestamp())
                continue
            if path == "/api/runtime-defaults":
                config_data = _load_agents_config_runtime(config_path)
                current = _runtime_defaults_from_config(config_data)
                update = payload.get("runtime_defaults")
                if isinstance(update, dict):
                    merged = dict(current)
                    merged.update(update)
                elif isinstance(payload, dict):
                    merged = dict(current)
                    merged.update(payload)
                else:
                    _ui_action_result(ui_instance, "runtime-defaults", "failed", "invalid defaults payload", None, _ui_timestamp())
                    continue
                normalized = _normalize_runtime_defaults(merged)
                planners = config_data.get("planners") if isinstance(config_data.get("planners"), list) else []
                enabled_planners = [
                    item
                    for item in planners
                    if isinstance(item, dict) and bool(item.get("enabled", True))
                ]
                max_valid = len(enabled_planners)
                clipped = False
                if max_valid > 0 and normalized["min_valid_planners"] > max_valid:
                    normalized["min_valid_planners"] = max_valid
                    clipped = True
                config_data["runtime_defaults"] = normalized
                _save_agents_config_runtime(config_path, config_data)
                sync_config_state()
                clip_note = " (min-valid adjusted to enabled planner count)" if clipped else ""
                message = (
                    f"defaults saved: {normalized['workflow']} / {normalized['profile']}, "
                    f"timeout {normalized['timeout_sec']}s{clip_note}"
                )
                _ui_action_result(ui_instance, "runtime-defaults", "ok", message, None, _ui_timestamp())
                continue
            if path == "/api/agent-add":
                config_data = _load_agents_config_runtime(config_path)
                planners = config_data.get("planners") if isinstance(config_data.get("planners"), list) else []
                agent = payload.get("agent")
                if not isinstance(agent, dict):
                    _ui_action_result(ui_instance, "agent-add", "failed", "missing agent payload", None, _ui_timestamp())
                    continue
                name = str(agent.get("name") or "").strip()
                if not name:
                    _ui_action_result(ui_instance, "agent-add", "failed", "agent name required", None, _ui_timestamp())
                    continue
                if any(isinstance(item, dict) and str(item.get("name") or "").strip() == name for item in planners):
                    _ui_action_result(ui_instance, "agent-add", "failed", "agent already exists", None, _ui_timestamp())
                    continue
                planners.append(agent)
                config_data["planners"] = planners
                if not isinstance(config_data.get("judge"), dict) and planners:
                    config_data["judge"] = planners[0]
                _save_agents_config_runtime(config_path, config_data)
                sync_config_state()
                _ui_action_result(ui_instance, "agent-add", "ok", f"agent {name} added", None, _ui_timestamp())
                continue
            if path == "/api/agent-remove":
                config_data = _load_agents_config_runtime(config_path)
                planners = config_data.get("planners") if isinstance(config_data.get("planners"), list) else []
                name = str(payload.get("name") or "").strip()
                if not name:
                    _ui_action_result(ui_instance, "agent-remove", "failed", "agent name required", None, _ui_timestamp())
                    continue
                new_planners = [item for item in planners if not (isinstance(item, dict) and str(item.get("name") or "").strip() == name)]
                if len(new_planners) == len(planners):
                    _ui_action_result(ui_instance, "agent-remove", "failed", "agent not found", None, _ui_timestamp())
                    continue
                config_data["planners"] = new_planners
                judge = config_data.get("judge")
                if isinstance(judge, dict) and str(judge.get("name") or "").strip() == name:
                    config_data["judge"] = new_planners[0] if new_planners else None
                _save_agents_config_runtime(config_path, config_data)
                sync_config_state()
                _ui_action_result(ui_instance, "agent-remove", "ok", f"agent {name} removed", None, _ui_timestamp())
                continue
            if path == "/api/agent-toggle":
                config_data = _load_agents_config_runtime(config_path)
                planners = config_data.get("planners") if isinstance(config_data.get("planners"), list) else []
                name = str(payload.get("name") or "").strip()
                enabled = bool(payload.get("enabled", True))
                changed = False
                for item in planners:
                    if isinstance(item, dict) and str(item.get("name") or "").strip() == name:
                        item["enabled"] = enabled
                        changed = True
                        break
                if not changed:
                    _ui_action_result(ui_instance, "agent-toggle", "failed", "agent not found", None, _ui_timestamp())
                    continue
                _save_agents_config_runtime(config_path, config_data)
                sync_config_state()
                _ui_action_result(ui_instance, "agent-toggle", "ok", f"agent {name} {'enabled' if enabled else 'disabled'}", None, _ui_timestamp())
                continue
            if path == "/api/judge-set":
                config_data = _load_agents_config_runtime(config_path)
                planners = config_data.get("planners") if isinstance(config_data.get("planners"), list) else []
                name = str(payload.get("name") or "").strip()
                target = next(
                    (item for item in planners if isinstance(item, dict) and str(item.get("name") or "").strip() == name),
                    None,
                )
                if not target:
                    _ui_action_result(ui_instance, "judge-set", "failed", "agent not found", None, _ui_timestamp())
                    continue
                config_data["judge"] = dict(target)
                _save_agents_config_runtime(config_path, config_data)
                sync_config_state()
                _ui_action_result(ui_instance, "judge-set", "ok", f"judge set to {name}", None, _ui_timestamp())
                continue
            _ui_action_result(ui_instance, "unknown", "ignored", f"unhandled action: {path}", None, _ui_timestamp())
        except Exception as exc:
            _ui_action_result(ui_instance, "error", "failed", str(exc), None, _ui_timestamp())


def render_planner_prompt(task_spec: Dict[str, Any], plan_template: str, prompt_template: str) -> str:
    brief = build_task_brief(task_spec)
    prompt = prompt_template.replace("{{TASK_BRIEF}}", brief)
    return prompt.replace("{{PLAN_TEMPLATE}}", plan_template)


def render_judge_prompt(task_spec: Dict[str, Any], plans: List[Dict[str, Any]], judge_template: str, prompt_template: str) -> str:
    brief = build_task_brief(task_spec)
    plans_block = "\n\n".join(f"### {p['label']}\n\n{p['plan']}" for p in plans)
    prompt = prompt_template.replace("{{TASK_BRIEF}}", brief)
    prompt = prompt.replace("{{PLANS_MD}}", plans_block)
    return prompt.replace("{{JUDGE_TEMPLATE}}", judge_template)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def resolve_path(relative_path: str) -> str:
    base_dir = Path(__file__).resolve().parent
    return str((base_dir / relative_path).resolve())


def get_run_root() -> Path:
    env_override = os.environ.get("LLM_COUNCIL_RUN_ROOT")
    if env_override:
        return Path(env_override).expanduser().resolve()
    cwd = Path.cwd().resolve()
    # If running from the llm-council repository root, avoid nested llm-council/llm-council/runs.
    if (cwd / "scripts" / "llm_council.py").exists() and (cwd / "references").exists():
        return cwd / "runs"
    return cwd / "llm-council" / "runs"


def slugify(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not cleaned:
        return "run"
    return cleaned[:max_len].strip("-")


def unique_run_dir(run_root: Path, base_name: str) -> Path:
    candidate = run_root / base_name
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = run_root / f"{base_name}-{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def maybe_trash_empty_dir(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        return
    if any(path.iterdir()):
        return
    trash_bin = shutil.which("trash")
    if not trash_bin:
        return
    subprocess.run([trash_bin, str(path)], check=False)


def get_default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "llm-council" / "agents.json"
    return Path.home() / ".config" / "llm-council" / "agents.json"


def load_agent_config_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    data = load_json(str(path))
    if isinstance(data, dict) and "agents" in data and isinstance(data["agents"], dict):
        return data["agents"]
    if isinstance(data, dict) and ("planners" in data or "judge" in data):
        return data
    return None


def _load_agents_config_runtime(config_path: Path) -> Dict[str, Any]:
    data = load_agent_config_file(config_path)
    if data:
        planners = data.get("planners") or []
        judge = data.get("judge")
        if isinstance(planners, list):
            runtime_defaults = _runtime_defaults_from_config(data)
            return {"planners": planners, "judge": judge, "runtime_defaults": runtime_defaults}
    return {"planners": [], "judge": None, "runtime_defaults": dict(DEFAULT_RUNTIME_DEFAULTS)}


def _save_agents_config_runtime(config_path: Path, payload: Dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        payload["runtime_defaults"] = _runtime_defaults_from_config(payload)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    write_json(str(tmp_path), payload)
    os.replace(tmp_path, config_path)


def _agent_names_from_config(config_data: Dict[str, Any]) -> List[str]:
    planners = config_data.get("planners") or []
    names: List[str] = []
    for item in planners:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _refresh_model_catalog_for_agent(agent: Dict[str, Any]) -> Dict[str, Any]:
    name = str(agent.get("name") or "agent")
    kind = str(agent.get("kind") or "custom").lower()
    selected = str(agent.get("model") or "")
    recommended = RECOMMENDED_MODELS.get(kind, [])
    source = "fallback"
    available: List[str] = []
    warning = ""

    if kind == "opencode" and shutil.which("opencode"):
        try:
            completed = subprocess.run(
                ["opencode", "models"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
            # Most outputs include provider/model tokens; keep simple extraction.
            candidates: List[str] = []
            for line in lines:
                for token in line.split():
                    if "/" in token and not token.startswith("http"):
                        candidates.append(token.strip("`,"))
            if candidates:
                available = sorted(set(candidates))
                source = "live"
        except Exception:
            warning = "live model lookup failed"

    if not available:
        available = list(recommended)
    if not available and selected:
        available = [selected]
    if not warning and selected and available and selected not in available:
        warning = "selected model not in discovered list"

    return {
        "agent": name,
        "kind": kind,
        "selected_model": selected,
        "available_models": available,
        "recommended_models": recommended,
        "source": source,
        "warning": warning,
        "timestamp": _ui_timestamp(),
    }


def refresh_model_catalog(config_data: Dict[str, Any]) -> Dict[str, Any]:
    planners = config_data.get("planners") or []
    judge = config_data.get("judge")
    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for agent in planners:
        if not isinstance(agent, dict):
            continue
        name = str(agent.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append(_refresh_model_catalog_for_agent(agent))
    if isinstance(judge, dict):
        name = str(judge.get("name") or "").strip()
        if name and name not in seen:
            entries.append(_refresh_model_catalog_for_agent(judge))
    return {"items": entries, "updated_at": _ui_timestamp()}


def configure_agents(config_path: Path) -> None:
    def prompt_text(label: str, default: Optional[str] = None) -> str:
        suffix = f" (default: {default})" if default else ""
        value = input(f"{label}{suffix}: ").strip()
        return value if value else (default or "")

    def prompt_choice(label: str, choices: List[str], default_idx: int = 1) -> int:
        while True:
            raw = input(f"{label} (default: {default_idx}): ").strip()
            if not raw:
                return default_idx
            try:
                value = int(raw)
            except ValueError:
                print("Please enter a number.")
                continue
            if 1 <= value <= len(choices):
                return value
            print(f"Choose a number between 1 and {len(choices)}.")

    def prompt_yes_no(label: str, default_yes: bool = True) -> bool:
        default = "Y/n" if default_yes else "y/N"
        raw = input(f"{label} [{default}] ").strip().lower()
        if not raw:
            return default_yes
        return raw in ("y", "yes")

    def prompt_model(kind: str, default_model: str) -> str:
        recommended = RECOMMENDED_MODELS.get(kind, [])
        if recommended:
            print(f"Recommended {kind} models: {', '.join(recommended)}")
        return prompt_text(f"{kind.capitalize()} model", default_model)

    print("Council setup")
    if prompt_yes_no("Use default council (Codex CLI + Claude CLI + Gemini CLI)?", default_yes=True):
        planners = [
            {
                "name": "codex-1",
                "kind": "codex",
                "model": CODEX_MODEL,
                "reasoning_effort": CODEX_REASONING,
                "auth_mode": "login",
            },
            {"name": "claude-2", "kind": "claude", "model": CLAUDE_MODEL, "auth_mode": "login"},
            {"name": "gemini-3", "kind": "gemini", "model": GEMINI_MODEL, "auth_mode": "login"},
        ]
        judge = planners[0]
    else:
        count_raw = prompt_text("How many planners?", "3")
        try:
            planner_count = max(1, int(count_raw))
        except ValueError:
            planner_count = 3

        planners = []
        for idx in range(1, planner_count + 1):
            print(f"\nPlanner {idx}")
            kinds = ["codex", "claude", "gemini", "opencode", "custom"]
            for i, kind in enumerate(kinds, start=1):
                print(f"{i}) {kind}")
            choice = prompt_choice("Choose CLI", kinds, default_idx=1)
            kind = kinds[choice - 1]

            default_name = f"{kind}-{idx}"
            name = prompt_text("Planner name", default_name) or default_name

            planner: Dict[str, Any] = {"name": name, "kind": kind}
            if kind == "codex":
                planner["model"] = prompt_model("codex", CODEX_MODEL)
                planner["reasoning_effort"] = prompt_text("Reasoning effort", CODEX_REASONING)
            elif kind == "claude":
                planner["model"] = prompt_model("claude", CLAUDE_MODEL)
            elif kind == "gemini":
                planner["model"] = prompt_model("gemini", GEMINI_MODEL)
            elif kind == "opencode":
                print(
                    "Opencode provider/model (note: run 'opencode models' in another terminal to see available models)"
                )
                recommended = RECOMMENDED_MODELS.get("opencode", [])
                if recommended:
                    print(f"Recommended opencode models: {', '.join(recommended)}")
                model = prompt_text("Provider/model", "")
                while not model:
                    model = prompt_text("Provider/model", "")
                planner["model"] = model
            else:
                planner["command"] = prompt_text("Command", "")
                while not planner["command"]:
                    planner["command"] = prompt_text("Command", "")
                prompt_mode = prompt_text("Prompt mode (arg|stdin)", "arg").lower()
                planner["prompt_mode"] = "stdin" if prompt_mode == "stdin" else "arg"

            auth_mode = prompt_text("Auth mode (login|api)", "login").lower().strip()
            if auth_mode not in ("login", "api"):
                auth_mode = "login"
            planner["auth_mode"] = auth_mode
            if auth_mode == "api":
                planner["api_env_var"] = prompt_text("API env var name", f"{kind.upper()}_API_KEY")

            fallback = prompt_text("Fallback model (optional)", "").strip()
            if fallback:
                planner["fallback_models"] = [fallback]

            planners.append(planner)

        print("\nWhich model should be the judge?")
        for i, planner in enumerate(planners, start=1):
            model = planner.get("model")
            label = f"{planner['name']} ({planner['kind']}"
            if model:
                label += f": {model}"
            label += ")"
            print(f"{i}) {label}")
        judge_idx = prompt_choice("Select judge", planners, default_idx=1)
        judge = planners[judge_idx - 1]

    print("\nDefault run settings")
    workflow_default = prompt_text("Workflow (quick-decide|full-council|risk-review)", "full-council").strip()
    if workflow_default not in WORKFLOW_SETTINGS:
        workflow_default = "full-council"
    profile_default = prompt_text("Profile (fast|balanced|deep)", str(_workflow_setting(workflow_default, "profile", "balanced"))).strip()
    if profile_default not in PROFILE_SETTINGS:
        profile_default = str(_workflow_setting(workflow_default, "profile", "balanced"))
    timeout_default = _coerce_int(
        prompt_text("Max timeout seconds", str(DEFAULT_TIMEOUT_SEC)),
        DEFAULT_TIMEOUT_SEC,
        minimum=MIN_TIMEOUT_SEC,
        maximum=MAX_TIMEOUT_SEC,
    )
    min_valid_default = _coerce_int(
        prompt_text("Min valid planner outputs", str(_workflow_setting(workflow_default, "min_valid_planners", 2))),
        int(_workflow_setting(workflow_default, "min_valid_planners", 2)),
        minimum=1,
        maximum=16,
    )
    plan_only_default = prompt_yes_no("Default to plan-only mode?", default_yes=bool(_workflow_setting(workflow_default, "plan_only", False)))
    runtime_defaults = _normalize_runtime_defaults(
        {
            "workflow": workflow_default,
            "profile": profile_default,
            "timeout_sec": timeout_default,
            "min_valid_planners": min_valid_default,
            "plan_only": plan_only_default,
        }
    )
    planner_count = max(1, len(planners))
    if runtime_defaults["min_valid_planners"] > planner_count:
        runtime_defaults["min_valid_planners"] = planner_count

    payload = {"planners": planners, "judge": judge, "runtime_defaults": runtime_defaults}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    print(f"\nSaving config to {config_path}.")
    write_json(str(tmp_path), payload)
    os.replace(tmp_path, config_path)
    print("Saved.")


def build_task_brief(task_spec: Dict[str, Any]) -> str:
    lines = []
    task = (task_spec.get("task") or "").strip()
    lines.append(f"Task: {task}")
    constraints = task_spec.get("constraints") or []
    if constraints:
        lines.append("Constraints:")
        for item in constraints:
            lines.append(f"- {item}")
    repo = task_spec.get("repo_context") or {}
    if repo:
        root = repo.get("root")
        paths = repo.get("paths") or []
        notes = repo.get("notes")
        if root:
            lines.append(f"Repo root: {root}")
        if paths:
            lines.append("Relevant paths:")
            for path in paths:
                lines.append(f"- {path}")
        if notes:
            lines.append(f"Notes: {notes}")
    return "\n".join(lines).strip()


def _normalize_agent_spec(spec: Any, fallback_name: str) -> AgentConfig:
    if isinstance(spec, str):
        data = {"name": spec, "kind": spec}
    elif isinstance(spec, dict):
        data = spec
    else:
        raise ValueError("agent spec must be an object or string")
    name = str(data.get("name") or fallback_name).strip()
    kind = str(data.get("kind") or data.get("cli") or data.get("type") or name).strip().lower()
    output_format = str(data.get("output_format") or "text").strip()
    model = data.get("model")
    reasoning_effort = data.get("reasoning_effort") or data.get("reasoning")
    agent = data.get("agent")
    attach = data.get("attach")
    cli_format = data.get("format") or data.get("cli_format")
    command = data.get("command")
    prompt_mode = data.get("prompt_mode") or "arg"
    extra_args = data.get("extra_args") or []
    if not isinstance(extra_args, list):
        extra_args = [str(extra_args)]
    extra_args = [str(item) for item in extra_args]
    fallback_models = data.get("fallback_models") or []
    if not isinstance(fallback_models, list):
        fallback_models = [str(fallback_models)]
    fallback_models = [str(item) for item in fallback_models if str(item).strip()]
    auth_mode = str(data.get("auth_mode") or "login").strip().lower()
    api_env_var = data.get("api_env_var")
    if api_env_var is not None:
        api_env_var = str(api_env_var).strip() or None
    enabled = bool(data.get("enabled", True))
    if kind == "opencode" and not cli_format:
        cli_format = "json"
    return AgentConfig(
        name=name,
        kind=kind,
        command=command,
        output_format=output_format,
        model=model,
        reasoning_effort=reasoning_effort,
        agent=agent,
        attach=attach,
        cli_format=cli_format,
        prompt_mode=prompt_mode,
        extra_args=extra_args,
        fallback_models=fallback_models,
        auth_mode=auth_mode,
        api_env_var=api_env_var,
        enabled=enabled,
    )


def load_agent_configs(task_spec: Dict[str, Any], config_path: Optional[Path] = None) -> Tuple[List[AgentConfig], AgentConfig]:
    agents_spec = task_spec.get("agents")
    if not agents_spec:
        config_path = config_path or get_default_config_path()
        config_spec = load_agent_config_file(config_path)
        if config_spec:
            agents_spec = config_spec

    if not agents_spec:
        raise ValueError(
            "Uh oh! Your models are not configured. Please run `./setup.sh` to select your models. "
            "You can override or change these models at any time by running the setup script again."
        )

    if isinstance(agents_spec, list):
        planner_specs = agents_spec
        judge_spec = None
    elif isinstance(agents_spec, dict):
        planner_specs = agents_spec.get("planners") or agents_spec.get("agents") or []
        judge_spec = agents_spec.get("judge")
    else:
        raise ValueError("agents must be a list or object with planners")

    if not planner_specs:
        raise ValueError("agents.planners must include at least one agent")

    planners: List[AgentConfig] = []
    seen = set()
    for idx, spec in enumerate(planner_specs, start=1):
        agent = _normalize_agent_spec(spec, f"planner-{idx}")
        if not agent.enabled:
            continue
        if agent.name in seen:
            agent.name = f"{agent.name}-{idx}"
        seen.add(agent.name)
        planners.append(agent)

    if len(planners) < 2:
        raise ValueError("At least two planner agents are required")

    if judge_spec:
        judge = _normalize_agent_spec(judge_spec, "judge")
    else:
        primary = planners[0]
        judge = AgentConfig(
            name=f"{primary.name}-judge",
            kind=primary.kind,
            command=primary.command,
            output_format=primary.output_format,
            model=primary.model,
            reasoning_effort=primary.reasoning_effort,
            agent=primary.agent,
            attach=primary.attach,
            cli_format=primary.cli_format,
            prompt_mode=primary.prompt_mode,
            extra_args=list(primary.extra_args),
            fallback_models=list(primary.fallback_models),
            auth_mode=primary.auth_mode,
            api_env_var=primary.api_env_var,
            enabled=primary.enabled,
        )

    return planners, judge


def _unique_fallback_models(config: AgentConfig) -> List[str]:
    current = str(config.model or "").strip()
    values: List[str] = []
    for item in config.fallback_models:
        value = str(item or "").strip()
        if not value:
            continue
        if current and value == current:
            continue
        if value in values:
            continue
        values.append(value)
    return values


def _run_with_fallbacks(
    config: AgentConfig,
    prompt: str,
    validator: Any,
    deadline_monotonic: float,
    *,
    start_with_primary: bool = True,
) -> Tuple[str, str, bool, Optional[str], Optional[str]]:
    attempts: List[AgentConfig] = []
    if start_with_primary:
        attempts.append(config)
    attempts.extend(replace(config, model=model) for model in _unique_fallback_models(config))
    if not attempts:
        return "", "", False, f"{config.name} failed", None
    last_error: Optional[str] = None
    last_raw = ""
    last_text = ""
    for candidate in attempts:
        try:
            running = spawn_cli_agent(candidate, prompt)
            raw = collect_cli_output_until(running, deadline_monotonic)
        except TimeoutError as exc:
            last_error = str(exc)
            last_raw = ""
            last_text = ""
            continue
        except Exception as exc:
            last_error = f"{candidate.name} failed to start: {exc}"
            last_raw = ""
            last_text = ""
            continue

        normalized = extract_agent_response(candidate, raw).strip()
        valid, err = validator(normalized)
        last_raw = raw
        last_text = normalized
        if valid:
            used_model = candidate.model
            if used_model == config.model:
                used_model = None
            return raw, normalized, True, None, used_model
        model_label = candidate.model or "default model"
        last_error = f"{candidate.name}: invalid output from {model_label}"
        if err:
            last_error = f"{last_error} ({err})"
    return last_raw, last_text, False, last_error or f"{config.name} failed", None


def run_planners(
    task_spec: Dict[str, Any],
    planners: List[AgentConfig],
    planner_prompt_template: str,
    plan_template: str,
    timeout_sec: int,
    retry_limit: int,
    prompt_hint: str,
    run_dir: str,
    ui_state: Optional["ui_server.UIState"] = None,
    ui_instance: Optional["ui_server.UIServer"] = None,
) -> List[AgentResult]:
    results: List[AgentResult] = []
    remaining = planners[:]
    attempt = 0
    while remaining and attempt <= retry_limit:
        running_map: Dict[str, RunningAgent] = {}
        prompt_map: Dict[str, str] = {}
        current_batch = remaining[:]
        attempt_deadline = time.monotonic() + max(float(timeout_sec), 0.1)
        for planner in current_batch:
            prompt = render_planner_prompt(task_spec, plan_template, planner_prompt_template)
            prompt = f"{prompt}\n\nStyle constraint:\n- {prompt_hint}\n"
            prompt_map[planner.name] = prompt
            timestamp = _ui_timestamp()
            _ui_upsert_planner(
                ui_state,
                ui_instance,
                planner_id=planner.name,
                status="running",
                summary="starting…",
                errors=[],
                timestamp=timestamp,
            )
            try:
                running_map[planner.name] = spawn_cli_agent(planner, prompt)
            except Exception as exc:
                _ui_upsert_planner(
                    ui_state,
                    ui_instance,
                    planner_id=planner.name,
                    status="failed",
                    summary="failed to launch",
                    errors=[str(exc)],
                    timestamp=_ui_timestamp(),
                )
                results.append(
                    AgentResult(
                        name=planner.name,
                        raw_output="",
                        data={"path": str(Path(run_dir) / f"plan-{planner.name}-attempt{attempt + 1}.md"), "text": ""},
                        valid=False,
                        error=f"{planner.name} failed to start: {exc}",
                    )
                )

        remaining = []
        for planner in current_batch:
            if planner.name not in running_map:
                if attempt < retry_limit:
                    remaining.append(planner)
                continue

            entry = running_map[planner.name]
            try:
                raw = collect_cli_output_until(entry, attempt_deadline)
                normalized = extract_agent_response(entry.config, raw).strip()
                valid, err = validate_markdown_plan(normalized)
                plan_text = normalized
            except TimeoutError as exc:
                raw = ""
                plan_text = ""
                valid, err = False, str(exc)

            selected_model: Optional[str] = None
            if not valid:
                fallback_raw, fallback_text, fallback_valid, fallback_err, fallback_model = _run_with_fallbacks(
                    planner,
                    prompt_map[planner.name],
                    validate_markdown_plan,
                    attempt_deadline,
                    start_with_primary=False,
                )
                if fallback_valid:
                    raw = fallback_raw
                    plan_text = fallback_text
                    valid = True
                    err = None
                    selected_model = fallback_model
                else:
                    if fallback_raw:
                        raw = fallback_raw
                    if fallback_text:
                        plan_text = fallback_text
                    if fallback_err:
                        err = fallback_err

            plan_path = Path(run_dir) / f"plan-{planner.name}-attempt{attempt + 1}.md"
            write_attempt = attempt > 0 or not valid
            if write_attempt:
                plan_path.write_text(plan_text, encoding="utf-8")
            if valid:
                final_path = Path(run_dir) / f"plan-{planner.name}.md"
                final_path.write_text(plan_text, encoding="utf-8")
            timestamp = _ui_timestamp()
            timed_out = bool(err and "timed out" in err)
            status = "complete" if valid else ("timed-out" if timed_out else "needs-fix")
            errors = [err] if err else []
            summary = plan_text
            if valid and selected_model:
                summary = f"[fallback model: {selected_model}]\n\n{summary}"
            _ui_upsert_planner(
                ui_state,
                ui_instance,
                planner_id=planner.name,
                status=status,
                summary=summary or ("error" if errors else ""),
                errors=errors,
                timestamp=timestamp,
            )
            result = AgentResult(
                name=planner.name,
                raw_output=raw,
                data={"path": str(plan_path if write_attempt else final_path), "text": plan_text},
                valid=valid,
                error=err,
            )
            results.append(result)
            if not valid and attempt < retry_limit:
                retry_timestamp = _ui_timestamp()
                _ui_upsert_planner(
                    ui_state,
                    ui_instance,
                    planner_id=planner.name,
                    status="retrying",
                    summary="retry scheduled",
                    errors=[err] if err else [],
                    timestamp=retry_timestamp,
                )
                remaining.append(planner)

        attempt += 1
    return results


def run_judge(
    task_spec: Dict[str, Any],
    plans: List[Dict[str, Any]],
    judge: AgentConfig,
    judge_prompt_template: str,
    judge_template: str,
    timeout_sec: int,
    run_dir: str,
    prompt_hint: str,
    ui_state: Optional["ui_server.UIState"] = None,
    ui_instance: Optional["ui_server.UIServer"] = None,
) -> AgentResult:
    prompt = render_judge_prompt(task_spec, plans, judge_template, judge_prompt_template)
    prompt = f"{prompt}\n\nStyle constraint:\n- {prompt_hint}\n"
    start_timestamp = _ui_timestamp()
    _ui_update_judge(
        ui_state,
        ui_instance,
        status="running",
        summary="starting…",
        errors=[],
        timestamp=start_timestamp,
    )
    deadline = time.monotonic() + max(float(timeout_sec), 0.1)
    raw, judge_text, valid, err, selected_model = _run_with_fallbacks(
        judge,
        prompt,
        validate_markdown_judge,
        deadline,
    )
    judge_path = Path(run_dir) / "judge.md"
    judge_path.write_text(judge_text, encoding="utf-8")
    finish_timestamp = _ui_timestamp()
    timed_out = bool(err and "timed out" in err)
    status = "complete" if valid else ("failed" if timed_out else "needs-fix")
    errors = [err] if err else []
    summary = judge_text
    if valid and selected_model:
        summary = f"[fallback model: {selected_model}]\n\n{summary}"
    _ui_update_judge(
        ui_state,
        ui_instance,
        status=status,
        summary=summary or ("error" if errors else ""),
        errors=errors,
        timestamp=finish_timestamp,
    )
    return AgentResult(
        name=judge.name,
        raw_output=raw,
        data={"path": str(judge_path), "text": judge_text},
        valid=valid,
        error=err,
    )


def extract_final_plan(judge_text: str) -> str:
    marker = "## Final Plan"
    if marker not in judge_text:
        return judge_text
    after = judge_text.split(marker, 1)[1]
    plan_start = after.find("# Plan")
    if plan_start == -1:
        return after.strip()
    return after[plan_start:].strip()


def _profile_setting(profile: str, key: str, fallback: Any) -> Any:
    values = PROFILE_SETTINGS.get(profile, {})
    return values.get(key, fallback)


def _workflow_setting(workflow: str, key: str, fallback: Any) -> Any:
    values = WORKFLOW_SETTINGS.get(workflow, {})
    return values.get(key, fallback)


def _coerce_int(value: Any, fallback: int, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = fallback
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _normalize_runtime_defaults(raw: Any) -> Dict[str, Any]:
    defaults = dict(DEFAULT_RUNTIME_DEFAULTS)
    if not isinstance(raw, dict):
        return defaults

    workflow = str(raw.get("workflow") or defaults["workflow"]).strip()
    if workflow not in WORKFLOW_SETTINGS:
        workflow = defaults["workflow"]
    defaults["workflow"] = workflow

    fallback_profile = str(_workflow_setting(workflow, "profile", defaults["profile"]))
    profile = str(raw.get("profile") or fallback_profile).strip()
    if profile not in PROFILE_SETTINGS:
        profile = fallback_profile if fallback_profile in PROFILE_SETTINGS else defaults["profile"]
    defaults["profile"] = profile

    timeout_default = _coerce_int(
        raw.get("timeout_sec"),
        defaults["timeout_sec"],
        minimum=MIN_TIMEOUT_SEC,
        maximum=MAX_TIMEOUT_SEC,
    )
    defaults["timeout_sec"] = timeout_default

    workflow_min = _coerce_int(_workflow_setting(workflow, "min_valid_planners", defaults["min_valid_planners"]), 2, minimum=1)
    min_valid = _coerce_int(raw.get("min_valid_planners"), workflow_min, minimum=1, maximum=16)
    defaults["min_valid_planners"] = min_valid

    plan_only = bool(raw.get("plan_only", defaults["plan_only"]))
    defaults["plan_only"] = plan_only
    return defaults


def _runtime_defaults_from_config(config_data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config_data, dict):
        return dict(DEFAULT_RUNTIME_DEFAULTS)
    return _normalize_runtime_defaults(config_data.get("runtime_defaults"))


def _runtime_defaults_options() -> Dict[str, List[str]]:
    return {"workflows": list(WORKFLOW_CHOICES), "profiles": list(PROFILE_CHOICES)}


def _recommendation_for_result(result: AgentResult, config: AgentConfig) -> Optional[str]:
    if not result.error:
        return None
    model_label = config.model or "default model"
    if "timed out" in result.error:
        if config.fallback_models:
            return f"{config.name}: timed out on {model_label}. Try fallback model {config.fallback_models[0]}."
        return f"{config.name}: timed out on {model_label}. Consider a faster model or fast profile."
    return f"{config.name}: invalid output from {model_label}. Consider a more reliable model for structured plans."


def _build_agent_check_prompt(
    task_spec: Dict[str, Any],
    final_plan: str,
    judge_text: str,
    alternatives: List[Dict[str, Any]],
) -> str:
    alt_section = "\n\n".join(
        f"{item.get('label', 'Plan')}:\n{str(item.get('plan') or '')[:900]}" for item in alternatives[:3]
    )
    return (
        "You are reviewing a judge decision in a multi-agent planning run.\n"
        "Reply with exactly 4 short markdown bullets:\n"
        "- Verdict: agree/disagree with one sentence\n"
        "- Risk: one concrete risk\n"
        "- Improvement: one concrete improvement\n"
        "- Confidence: 1-5 with one sentence\n"
        "Do not include anything else.\n\n"
        f"Task brief:\n{build_task_brief(task_spec)}\n\n"
        f"Judge report (excerpt):\n{judge_text[:1600]}\n\n"
        f"Final plan:\n{final_plan[:2000]}\n\n"
        f"Alternatives:\n{alt_section}\n"
    )


def run_agent_checks(
    task_spec: Dict[str, Any],
    planners: List[AgentConfig],
    judge: AgentConfig,
    final_plan: str,
    judge_text: str,
    alternatives: List[Dict[str, Any]],
    timeout_sec: int,
    run_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []
    prompt = _build_agent_check_prompt(task_spec, final_plan, judge_text, alternatives)
    reviewers = [agent for agent in planners if agent.name != judge.name]
    if not reviewers:
        return checks, warnings
    for reviewer in reviewers:
        try:
            running = spawn_cli_agent(reviewer, prompt)
            raw = collect_cli_output(running, timeout_sec)
            text = extract_agent_response(reviewer, raw).strip()
            if not text:
                text = "No response."
                warnings.append(f"{reviewer.name}: empty agent check response")
            checks.append({"agent": reviewer.name, "kind": reviewer.kind, "response": text})
            (run_dir / f"agent-check-{reviewer.name}.md").write_text(text, encoding="utf-8")
        except TimeoutError:
            warnings.append(f"{reviewer.name}: agent check timed out")
            checks.append({"agent": reviewer.name, "kind": reviewer.kind, "response": "Timed out."})
        except Exception as exc:
            warnings.append(f"{reviewer.name}: agent check failed ({exc})")
            checks.append({"agent": reviewer.name, "kind": reviewer.kind, "response": "Failed."})
    if checks:
        checks_md = []
        for item in checks:
            checks_md.append(f"## {item['agent']}\n\n{item['response']}\n")
        (run_dir / "agent-checks.md").write_text("\n".join(checks_md), encoding="utf-8")
        write_json(str(run_dir / "agent-checks.json"), checks)
    return checks, warnings


def _agent_command_name(config: AgentConfig) -> Optional[str]:
    kind = (config.kind or "").lower()
    if kind in ("codex", "claude", "gemini", "opencode"):
        return kind
    if config.command:
        parts = shlex.split(config.command)
        if parts:
            return parts[0]
    return None


def _validate_agent_runtime(config: AgentConfig) -> Optional[str]:
    cmd = _agent_command_name(config)
    if not cmd:
        return f"{config.name}: no command configured"
    if shutil.which(cmd) is None:
        return f"{config.name}: command '{cmd}' not found in PATH"
    if config.auth_mode == "api":
        if not config.api_env_var:
            return f"{config.name}: auth_mode=api requires api_env_var"
        if not os.environ.get(config.api_env_var):
            return f"{config.name}: env var {config.api_env_var} is not set"
    return None


def collect_agent_runtime_errors(planners: List[AgentConfig], judge: AgentConfig) -> List[str]:
    errors: List[str] = []
    seen: Dict[str, AgentConfig] = {}
    for cfg in planners + [judge]:
        if cfg.name in seen:
            continue
        seen[cfg.name] = cfg
        err = _validate_agent_runtime(cfg)
        if err:
            errors.append(err)
    return errors


def test_agents_config(config_path: Path) -> int:
    config_spec = load_agent_config_file(config_path)
    if not config_spec:
        print(f"No config found at {config_path}. Run setup first.", file=sys.stderr)
        return 2
    task_spec = {"task": "agent test", "agents": config_spec}
    try:
        planners, judge = load_agent_configs(task_spec)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    errors = collect_agent_runtime_errors(planners, judge)

    if errors:
        print("Agent check failed:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
        return 3

    print("Agent check passed.")
    for cfg in planners:
        mode = cfg.auth_mode
        model = cfg.model or "default"
        print(f"- planner {cfg.name}: kind={cfg.kind} model={model} auth={mode}")
    jmodel = judge.model or "default"
    print(f"- judge {judge.name}: kind={judge.kind} model={jmodel} auth={judge.auth_mode}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="llm-council")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run")
    run.add_argument("--spec", required=True, help="Path to task spec JSON")
    run.add_argument("--out", required=False, help="Path to write final plan Markdown")
    run.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Global timeout cap in seconds. Defaults to configured runtime defaults.",
    )
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--config", required=False, help="Path to agents config JSON")
    run.add_argument(
        "--workflow",
        choices=list(WORKFLOW_CHOICES),
        default=None,
        help="Workflow template controlling defaults for profile and planning mode",
    )
    run.add_argument(
        "--profile",
        choices=list(PROFILE_CHOICES),
        default=None,
        help="Execution profile for timeouts, retries, and prompt depth",
    )
    mode_group = run.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--plan-only",
        action="store_true",
        help="Run planners and shortlist only; skip judge and final synthesis",
    )
    mode_group.add_argument(
        "--full-run",
        action="store_true",
        help="Force full run even for quick-decide workflow",
    )
    run.add_argument(
        "--min-valid-planners",
        type=int,
        default=None,
        help="Minimum number of valid planner outputs required to continue",
    )
    run.add_argument("--no-ui", action="store_true", help="Disable the live UI server")
    run.add_argument(
        "--ui-keepalive-seconds",
        type=int,
        default=DEFAULT_UI_KEEPALIVE_SEC,
        help="Keep the UI server alive for N seconds after completion (0 to disable)",
    )

    ui = sub.add_parser("ui")
    ui.add_argument("--run-dir", required=True, help="Path to a run directory to resume")
    ui.add_argument("--no-open", action="store_true", help="Do not auto-open a browser window")
    ui.add_argument("--config", required=False, help="Path to agents config JSON for UI actions")

    configure = sub.add_parser("configure")
    configure.add_argument("--config", required=False, help="Path to write agents config JSON")

    agents = sub.add_parser("agents")
    agents_sub = agents.add_subparsers(dest="agents_cmd", required=True)
    agents_test = agents_sub.add_parser("test")
    agents_test.add_argument("--config", required=False, help="Path to agents config JSON")

    models = sub.add_parser("models")
    models_sub = models.add_subparsers(dest="models_cmd", required=True)
    models_refresh = models_sub.add_parser("refresh")
    models_refresh.add_argument("--config", required=False, help="Path to agents config JSON")

    args = parser.parse_args()

    if args.cmd == "configure":
        config_path = Path(args.config) if args.config else get_default_config_path()
        configure_agents(config_path)
        return 0
    if args.cmd == "agents":
        if args.agents_cmd == "test":
            config_path = Path(args.config) if args.config else get_default_config_path()
            return test_agents_config(config_path)
        return 2
    if args.cmd == "models":
        if args.models_cmd == "refresh":
            config_path = Path(args.config) if args.config else get_default_config_path()
            config_data = _load_agents_config_runtime(config_path)
            catalog = refresh_model_catalog(config_data)
            print(json.dumps(catalog, indent=2, sort_keys=True))
            return 0
        return 2
    if args.cmd == "ui":
        run_dir = Path(args.run_dir).expanduser().resolve()
        ui_config_path = Path(args.config) if args.config else get_default_config_path()
        snapshot_path = run_dir / "ui-state.json"
        initial_state: Dict[str, Any] = _rebuild_ui_state_from_run(run_dir)
        ui_state = ui_server.UIState(initial_state, snapshot_path=snapshot_path)
        ui_instance = ui_server.start_server(state=ui_state)
        ui_url = ui_instance.ui_url
        action_stop = threading.Event()
        keepalive = _KeepaliveController()
        action_thread = threading.Thread(
            target=_handle_ui_actions,
            args=(
                ui_instance,
                ui_state,
                run_dir,
                {},
                argparse.Namespace(timeout=DEFAULT_TIMEOUT_SEC),
                ui_config_path,
                action_stop,
                keepalive,
                None,
                None,
            ),
            name="ui-action-handler",
            daemon=True,
        )
        action_thread.start()
        _start_ui_session_timer(ui_instance, ui_state, action_stop, keepalive)
        if not args.no_open:
            webbrowser.open(ui_url)
        print(f"UI server running at {ui_url}")
        try:
            while not action_stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            ui_instance.shutdown()
        return 0

    try:
        task_spec = load_json(args.spec)
    except FileNotFoundError:
        print(
            f"Spec file not found: {args.spec}\n"
            "Uh oh! Your models are not configured. Please run `./setup.sh` to select your models. "
            "You can override or change these models at any time by running the setup script again.",
            file=sys.stderr,
        )
        return 2
    config_path = Path(args.config) if args.config else get_default_config_path()
    runtime_config = _load_agents_config_runtime(config_path)
    runtime_defaults = _runtime_defaults_from_config(runtime_config)
    prompt_text = load_text(resolve_path("../references/prompts.md"))
    planner_prompt = prompt_text.split("## Judge Prompt")[0].split("```text", 1)[1].rsplit("```", 1)[0]
    judge_prompt = prompt_text.split("## Judge Prompt", 1)[1].split("```text", 1)[1].rsplit("```", 1)[0]

    plan_template = load_text(resolve_path("../references/templates/plan.md"))
    judge_template = load_text(resolve_path("../references/templates/judge.md"))

    run_root = get_run_root()
    run_root.mkdir(parents=True, exist_ok=True)
    base_label = task_spec.get("run_id") or task_spec.get("run_label")
    if not base_label:
        task_label = slugify(task_spec.get("task") or "run")
        base_label = f"{time.strftime('%Y%m%d')}-{task_label}"
    run_dir = unique_run_dir(run_root, base_label)
    run_dir.mkdir(parents=True, exist_ok=True)

    ui_state: Optional[ui_server.UIState] = None
    ui_instance: Optional[ui_server.UIServer] = None
    keepalive = _KeepaliveController() if not args.no_ui else None

    try:
        planners, judge = load_agent_configs(task_spec, config_path=config_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    runtime_errors = collect_agent_runtime_errors(planners, judge)
    if runtime_errors:
        print("Agent runtime preflight failed:", file=sys.stderr)
        for item in runtime_errors:
            print(f"- {item}", file=sys.stderr)
        return 3
    workflow_explicit = args.workflow is not None
    workflow_name = args.workflow or str(runtime_defaults.get("workflow") or DEFAULT_RUNTIME_DEFAULTS["workflow"])
    if workflow_name not in WORKFLOW_SETTINGS:
        workflow_name = str(DEFAULT_RUNTIME_DEFAULTS["workflow"])
    workflow_profile_default = str(_workflow_setting(workflow_name, "profile", "balanced"))
    if args.profile:
        profile_name = args.profile
    elif workflow_explicit:
        profile_name = workflow_profile_default
    else:
        profile_name = str(runtime_defaults.get("profile") or workflow_profile_default)
    if profile_name not in PROFILE_SETTINGS:
        print(f"Unknown profile: {profile_name}", file=sys.stderr)
        return 2

    timeout_cap = _coerce_int(
        args.timeout if args.timeout is not None else runtime_defaults.get("timeout_sec"),
        DEFAULT_TIMEOUT_SEC,
        minimum=MIN_TIMEOUT_SEC,
        maximum=MAX_TIMEOUT_SEC,
    )
    workflow_plan_only_default = bool(_workflow_setting(workflow_name, "plan_only", False))
    if workflow_explicit:
        plan_only_default = workflow_plan_only_default
    else:
        plan_only_default = bool(runtime_defaults.get("plan_only", workflow_plan_only_default))
    if args.full_run:
        plan_only_mode = False
    elif args.plan_only:
        plan_only_mode = True
    else:
        plan_only_mode = plan_only_default

    workflow_min_valid_default = int(_workflow_setting(workflow_name, "min_valid_planners", 2))
    min_valid_default = _coerce_int(
        runtime_defaults.get("min_valid_planners"),
        workflow_min_valid_default,
        minimum=1,
        maximum=16,
    )
    if workflow_explicit:
        min_valid_default = workflow_min_valid_default
    min_valid_required = _coerce_int(
        args.min_valid_planners if args.min_valid_planners is not None else min_valid_default,
        min_valid_default,
        minimum=1,
        maximum=16,
    )
    if min_valid_required > len(planners):
        if args.min_valid_planners is not None:
            print(
                f"min-valid-planners ({min_valid_required}) cannot exceed planner count ({len(planners)}).",
                file=sys.stderr,
            )
            return 2
        print(
            f"Configured min-valid-planners ({min_valid_required}) exceeded planner count ({len(planners)}); "
            f"clamping to {len(planners)} for this run.",
            file=sys.stderr,
        )
        min_valid_required = len(planners)

    if args.seed is not None:
        random.seed(args.seed)

    planner_timeout = int(min(timeout_cap, _profile_setting(profile_name, "planner_timeout", DEFAULT_TIMEOUT_SEC)))
    judge_timeout = int(min(timeout_cap, _profile_setting(profile_name, "judge_timeout", DEFAULT_TIMEOUT_SEC)))
    retry_limit = int(_profile_setting(profile_name, "retry_limit", RETRY_LIMIT))
    prompt_hint = str(_profile_setting(profile_name, "planner_hint", "Keep the plan concise but complete."))
    workflow_hint = str(_workflow_setting(workflow_name, "workflow_hint", ""))
    if workflow_hint:
        prompt_hint = f"{prompt_hint} {workflow_hint}".strip()
    run_settings = {
        "workflow": workflow_name,
        "profile": profile_name,
        "plan_only": plan_only_mode,
        "timeout_sec": timeout_cap,
        "min_valid_planners": min_valid_required,
        "planner_timeout_sec": planner_timeout,
        "judge_timeout_sec": judge_timeout,
        "retry_limit": retry_limit,
    }

    if not args.no_ui:
        snapshot_path = run_dir / "ui-state.json"
        ui_state = ui_server.UIState(snapshot_path=snapshot_path)
        ui_instance = ui_server.start_server(state=ui_state)
        ui_url = ui_instance.ui_url
        webbrowser.open(ui_url)
        print(f"UI server running at {ui_url}")

    if ui_state:
        timestamp = _ui_timestamp()
        initial_planners = [
            {"id": planner.name, "status": "pending", "summary": "", "errors": []} for planner in planners
        ]
        initial_state = {
            "run_id": run_dir.name,
            "task_brief": build_task_brief(task_spec),
            "phase": "starting",
            "planners": initial_planners,
            "judge": {"status": "pending", "summary": "", "errors": []},
            "final_plan": "",
            "errors": [],
            "config_agents": [
                {
                    "name": planner.name,
                    "kind": planner.kind,
                    "model": planner.model or "",
                    "enabled": planner.enabled,
                }
                for planner in planners
            ],
            "config_judge": judge.name,
            "model_catalog": {"items": [], "updated_at": ""},
            "runtime_defaults": runtime_defaults,
            "runtime_options": _runtime_defaults_options(),
            "run_settings": run_settings,
            "keep_open": False,
            "ui_deadline": _ui_deadline_from_now(DEFAULT_UI_SESSION_TTL_SEC),
            "timestamps": {"started_at": timestamp, "updated_at": timestamp},
        }
        ui_state.set(initial_state)
        _ui_emit(ui_instance, "phase_change", {"phase": "starting", "timestamp": timestamp})

    if ui_instance:
        action_stop = threading.Event()
        action_thread = threading.Thread(
            target=_handle_ui_actions,
            args=(
                ui_instance,
                ui_state,
                run_dir,
                task_spec,
                args,
                config_path,
                action_stop,
                keepalive,
                judge,
                plan_template,
            ),
            name="ui-action-handler",
            daemon=True,
        )
        action_thread.start()
        _start_ui_session_timer(ui_instance, ui_state, action_stop, keepalive)

    _ui_set_phase(ui_state, ui_instance, "planning", _ui_timestamp())
    planner_results = run_planners(
        task_spec,
        planners,
        planner_prompt,
        plan_template,
        planner_timeout,
        retry_limit,
        prompt_hint,
        str(run_dir),
        ui_state=ui_state,
        ui_instance=ui_instance,
    )
    latest_valid: Dict[str, Dict[str, Any]] = {}
    for result in planner_results:
        if result.valid and result.data:
            latest_valid[result.name] = result.data
    valid_plans = list(latest_valid.values())
    metadata = {
        "used_plans": [],
        "profile": profile_name,
        "workflow": workflow_name,
        "timeouts": {"planner_sec": planner_timeout, "judge_sec": judge_timeout},
        "agents": {
            "planners": [planner.name for planner in planners],
            "judge": judge.name,
        },
        "validation": {
            "task_spec_valid": True,
            "plans_valid": {r.name: r.valid for r in planner_results},
            "judge_valid": None,
            "min_valid_required": min_valid_required,
            "valid_planners_count": len(valid_plans),
        },
        "warnings": [r.error for r in planner_results if r.error],
    }
    if len(valid_plans) < min_valid_required:
        recommendations = []
        planner_lookup = {planner.name: planner for planner in planners}
        for result in planner_results:
            planner_cfg = planner_lookup.get(result.name)
            if planner_cfg:
                rec = _recommendation_for_result(result, planner_cfg)
                if rec:
                    recommendations.append(rec)
        if not recommendations:
            recommendations.append(
                f"Only {len(valid_plans)} valid planner outputs, need at least {min_valid_required}. "
                "Try balanced/deep profile and verify agent configuration."
            )
        (run_dir / "recommendations.md").write_text("\n".join(f"- {item}" for item in recommendations), encoding="utf-8")
        metadata["validation"]["judge_valid"] = False
        metadata["warnings"] = [r.error for r in planner_results if r.error]
        partial_alternatives = [
            {"label": f"Plan {idx + 1}", "plan": anonymize_text(plan.get("text", ""))}
            for idx, plan in enumerate(valid_plans)
        ]
        failure_summary = {
            "mode": "failed",
            "reason": "insufficient_valid_plans",
            "run_dir": str(run_dir),
            "profile": profile_name,
            "workflow": workflow_name,
            "valid_planners": len(valid_plans),
            "required_valid_planners": min_valid_required,
        }
        write_json(str(run_dir / "run-metadata.json"), metadata)
        write_json(
            str(run_dir / "run.json"),
            {
                "metadata": metadata,
                "final_plan": "",
                "alternatives": partial_alternatives,
                "agent_checks": [],
                "recommendations": recommendations,
                "summary": failure_summary,
            },
        )
        if ui_state:
            _ui_set_phase(ui_state, ui_instance, "failed", _ui_timestamp())
        print("Insufficient valid planner outputs. See recommendations.md in run directory.", file=sys.stderr)
        return 3

    _ui_set_phase(ui_state, ui_instance, "judging", _ui_timestamp())
    randomized_plans = []
    for idx, plan in enumerate(valid_plans):
        labeled = {"label": f"Plan {idx + 1}", "plan": anonymize_text(plan["text"])}
        randomized_plans.append(labeled)
    random.shuffle(randomized_plans)
    alternatives_md = []
    for item in randomized_plans:
        alternatives_md.append(f"## {item['label']}\n\n{item['plan']}\n")
    (run_dir / "alternatives.md").write_text("\n".join(alternatives_md), encoding="utf-8")
    write_json(str(run_dir / "alternatives.json"), randomized_plans)

    metadata["used_plans"] = [p["label"] for p in randomized_plans]

    if plan_only_mode:
        _ui_set_phase(ui_state, ui_instance, "complete", _ui_timestamp())
        summary = {
            "mode": "plan-only",
            "run_dir": str(run_dir),
            "profile": profile_name,
            "workflow": workflow_name,
            "valid_planners": len(valid_plans),
            "alternatives_json": str(run_dir / "alternatives.json"),
        }
        write_json(str(run_dir / "run-metadata.json"), metadata)
        write_json(
            str(run_dir / "run.json"),
            {
                "metadata": metadata,
                "final_plan": "",
                "alternatives": randomized_plans,
                "agent_checks": [],
                "recommendations": [],
                "summary": summary,
            },
        )
        if args.out:
            Path(args.out).write_text((run_dir / "alternatives.md").read_text(encoding="utf-8"), encoding="utf-8")
        else:
            print((run_dir / "alternatives.md").read_text(encoding="utf-8"))
        return 0

    judge_result = run_judge(
        task_spec,
        randomized_plans,
        judge,
        judge_prompt,
        judge_template,
        judge_timeout,
        str(run_dir),
        prompt_hint,
        ui_state=ui_state,
        ui_instance=ui_instance,
    )
    if not judge_result.valid:
        recommendations = []
        judge_rec = _recommendation_for_result(judge_result, judge)
        if judge_rec:
            recommendations.append(judge_rec)
        recommendations.append("Judge output invalid. Try deep profile or assign a stronger judge model.")
        (run_dir / "recommendations.md").write_text("\n".join(f"- {item}" for item in recommendations), encoding="utf-8")
        metadata["validation"]["judge_valid"] = False
        metadata["warnings"] = [r.error for r in planner_results if r.error] + ([judge_result.error] if judge_result.error else [])
        failure_summary = {
            "mode": "failed",
            "reason": "judge_invalid",
            "run_dir": str(run_dir),
            "profile": profile_name,
            "workflow": workflow_name,
        }
        write_json(str(run_dir / "run-metadata.json"), metadata)
        write_json(
            str(run_dir / "run.json"),
            {
                "metadata": metadata,
                "final_plan": "",
                "alternatives": randomized_plans,
                "agent_checks": [],
                "recommendations": recommendations,
                "summary": failure_summary,
            },
        )
        if ui_state:
            _ui_set_phase(ui_state, ui_instance, "failed", _ui_timestamp())
        print("Judge output invalid. See recommendations.md in run directory.", file=sys.stderr)
        return 4
    metadata["validation"]["judge_valid"] = judge_result.valid
    metadata["warnings"] = [r.error for r in planner_results if r.error] + ([judge_result.error] if judge_result.error else [])

    _ui_set_phase(ui_state, ui_instance, "finalizing", _ui_timestamp())
    judge_text_full = judge_result.data.get("text", "") if judge_result.data else ""
    final_text = extract_final_plan(judge_text_full)
    agent_checks, agent_check_warnings = run_agent_checks(
        task_spec=task_spec,
        planners=planners,
        judge=judge,
        final_plan=final_text,
        judge_text=judge_text_full,
        alternatives=randomized_plans,
        timeout_sec=max(MIN_TIMEOUT_SEC, judge_timeout // 2),
        run_dir=run_dir,
    )
    if agent_check_warnings:
        metadata["warnings"].extend(agent_check_warnings)
    final_path = run_dir / "final-plan.md"
    final_path.write_text(final_text, encoding="utf-8")
    write_json(str(run_dir / "run-metadata.json"), metadata)
    write_json(
        str(run_dir / "run.json"),
        {
            "metadata": metadata,
            "final_plan": final_text,
            "alternatives": randomized_plans,
            "agent_checks": agent_checks,
            "recommendations": [],
            "summary": {"mode": "full", "run_dir": str(run_dir), "profile": profile_name, "workflow": workflow_name},
        },
    )
    _ui_set_final_plan(ui_state, ui_instance, final_text, _ui_timestamp())
    _ui_set_phase(ui_state, ui_instance, "complete", _ui_timestamp())

    if args.out:
        Path(args.out).write_text(final_text, encoding="utf-8")
    else:
        print(final_text)

    if ui_instance:
        resume_cmd = f"python scripts/llm_council.py ui --run-dir {run_dir}"
        print(f"Resume UI: {resume_cmd}")
    if ui_instance and args.ui_keepalive_seconds > 0:
        print(f"Keeping UI server alive for {args.ui_keepalive_seconds}s unless kept open...")
        start = time.time()
        while True:
            if keepalive and keepalive.should_keep_open():
                time.sleep(1)
                continue
            if time.time() - start >= args.ui_keepalive_seconds:
                break
            time.sleep(1)
        ui_instance.shutdown()

    maybe_trash_empty_dir(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
