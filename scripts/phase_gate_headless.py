#!/usr/bin/env python3
"""PhaseGate headless orchestration contracts and executor relay plumbing.

This module can render, launch, verify, validate, and retry one bounded executor
phase before stopping ahead of staging or commits. It provides the durable
contracts those later phases need: plan normalization, ledger/resume persistence,
validation execution, exit code mapping, deterministic git scope
capture/classification, scoped follow-up prompts, and semantic review gating.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PLAN_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1

COMMIT_MODE_NO_COMMIT = "no-commit"
COMMIT_MODE_PREPARE_COMMAND = "prepare-commit-command-only"
COMMIT_MODE_AUTO_AFTER_GATE = "auto-commit-after-gate"
SUPPORTED_COMMIT_MODES = {
    COMMIT_MODE_NO_COMMIT,
    COMMIT_MODE_PREPARE_COMMAND,
    COMMIT_MODE_AUTO_AFTER_GATE,
}
KNOWN_COMMIT_MODES = SUPPORTED_COMMIT_MODES | {COMMIT_MODE_AUTO_AFTER_GATE}

DEFAULT_OUTPUT_DIR = Path(".phase-gate")
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300
DEFAULT_OUTPUT_EXCERPT_CHARS = 4000
DEFAULT_EXECUTOR_RELAY_MODE = "local_exec"
SUPPORTED_EXECUTOR_MODELS = ("gpt-6-sol", "gpt-6-luna")
DEFAULT_EXECUTOR_LOCAL_TIMEOUT_SECONDS = 900
DEFAULT_EXECUTOR_LOCAL_REASONING_EFFORT = "xhigh"
DEFAULT_EXECUTOR_LOCAL_SERVICE_TIER = "fast"
DEFAULT_SEMANTIC_REVIEW_MODE = "disabled"
SEMANTIC_REVIEW_MODES = {"disabled", "manual_artifact", "model_review"}
DEFAULT_REVIEWER_LOCAL_TIMEOUT_SECONDS = DEFAULT_EXECUTOR_LOCAL_TIMEOUT_SECONDS
DEFAULT_REVIEWER_LOCAL_REASONING_EFFORT = DEFAULT_EXECUTOR_LOCAL_REASONING_EFFORT
DEFAULT_REVIEWER_LOCAL_SERVICE_TIER = DEFAULT_EXECUTOR_LOCAL_SERVICE_TIER
DEFAULT_REVIEW_DIFF_CHARS = 40000
DEFAULT_REVIEW_FILE_EXCERPT_CHARS = 12000
DEFAULT_ALLOW_DIRTY_APPROVED_CONTINUATION = False
REVIEW_VERDICT_REQUIRED_FIELDS = {
    "approved",
    "findings",
    "reviewed_files",
    "validation_summary",
    "residual_risks",
    "commit_eligibility",
}
TASK_ID_CLEAN_RE = re.compile(r"[^a-zA-Z0-9._-]+")
HARD_BLOCKED_REASONS = {
    "branch_drift",
    "head_drift",
    "unexpected_commit",
    "staged_changes_present",
    "preexisting_dirty_allowed_paths",
}

SCRIPT_DIR = Path(__file__).resolve().parent

EXIT_OK = 0
EXIT_PHASE_FAILED = 1
EXIT_INVALID_INPUT = 2
EXIT_CHECKPOINT = 3
EXIT_BLOCKED = 4
EXIT_TIMEOUT = 5
RELAY_EXIT_TIMEOUT = 2

EXIT_CODE_NAMES = {
    EXIT_OK: "ok",
    EXIT_PHASE_FAILED: "phase_failed",
    EXIT_INVALID_INPUT: "invalid_input",
    EXIT_CHECKPOINT: "checkpoint",
    EXIT_BLOCKED: "blocked",
    EXIT_TIMEOUT: "timeout",
}


class PhaseGateError(ValueError):
    """Base exception for invalid PhaseGate inputs."""


class PathContractError(PhaseGateError):
    """Raised when a configured path violates the PhaseGate path contract."""


class CommandContractError(PhaseGateError):
    """Raised when a validation command violates the command contract."""


class ResumeStateError(PhaseGateError):
    """Raised when a resume state file is missing data or no longer matches git."""


class GitCommandError(RuntimeError):
    """Raised when a read-only git inspection command fails."""


@dataclass(frozen=True)
class PathSpec:
    raw: str
    path: str
    is_dir: bool


@dataclass(frozen=True)
class ValidationCommand:
    id: str
    cwd: str
    required: bool
    timeout_seconds: float
    shell: bool
    argv: Optional[List[str]] = None
    command: Optional[str] = None
    manual: bool = False
    live: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def exit_code_name(code: int) -> str:
    return EXIT_CODE_NAMES.get(code, "unknown")


def _clean_id(raw: str, fallback: str) -> str:
    cleaned = TASK_ID_CLEAN_RE.sub("_", (raw or "").strip()).strip("_")
    return cleaned[:120] if cleaned else fallback


def _load_json_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PhaseGateError(f"failed to read JSON file {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PhaseGateError(f"invalid JSON in {path}: {exc.msg}") from exc


def _write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _path_is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_windows_absolute_path(value: str) -> None:
    if re.match(r"^[a-zA-Z]:[/\\]", value):
        raise PathContractError(f"path must be repo-relative POSIX, got Windows absolute path: {value!r}")


def normalize_repo_relative_path(
    value: Any,
    *,
    repo_root: Path,
    allow_dot: bool = False,
    must_exist: bool = False,
    require_dir: bool = False,
) -> str:
    if not isinstance(value, str):
        raise PathContractError(f"path must be a string, got {type(value).__name__}")

    raw = value.strip()
    if not raw:
        raise PathContractError("path must not be empty")
    if "\\" in raw:
        raise PathContractError(f"path must use POSIX separators: {value!r}")
    _reject_windows_absolute_path(raw)
    if raw.startswith("/"):
        raise PathContractError(f"path must be repo-relative, got absolute path: {value!r}")

    pure = PurePosixPath(raw)
    if any(part == ".." for part in pure.parts):
        raise PathContractError(f"path must not contain '..': {value!r}")

    normalized = str(pure)
    if normalized == ".":
        if not allow_dot:
            raise PathContractError("'.' is not allowed for this path field")
    elif normalized.startswith("../") or normalized == "..":
        raise PathContractError(f"path escapes repo root: {value!r}")

    repo_root_resolved = repo_root.resolve()
    candidate = repo_root_resolved if normalized == "." else repo_root_resolved / normalized
    resolved_candidate = candidate.resolve(strict=False)
    if not _path_is_relative_to(resolved_candidate, repo_root_resolved):
        raise PathContractError(f"path resolves outside repo root: {value!r}")
    if must_exist and not candidate.exists():
        raise PathContractError(f"path does not exist: {value!r}")
    if require_dir and not candidate.is_dir():
        raise PathContractError(f"path is not a directory: {value!r}")
    return normalized


def normalize_path_spec(value: Any, *, repo_root: Path, allow_dot: bool = False) -> PathSpec:
    if not isinstance(value, str):
        raise PathContractError(f"path must be a string, got {type(value).__name__}")
    raw = value.strip()
    is_dir = raw.endswith("/") or raw == "."
    normalized = normalize_repo_relative_path(raw.rstrip("/") or raw, repo_root=repo_root, allow_dot=allow_dot)
    return PathSpec(raw=raw, path=normalized, is_dir=is_dir)


def path_matches_spec(path: str, spec: PathSpec) -> bool:
    normalized_path = str(PurePosixPath(path.strip()))
    if spec.path == ".":
        return True
    if spec.is_dir:
        return normalized_path == spec.path or normalized_path.startswith(spec.path + "/")
    return normalized_path == spec.path


def path_matches_any(path: str, specs: Sequence[PathSpec]) -> bool:
    return any(path_matches_spec(path, spec) for spec in specs)


def _normalize_path_list(
    raw_items: Any,
    *,
    field_name: str,
    repo_root: Path,
    allow_dot: bool = False,
) -> List[PathSpec]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise PathContractError(f"{field_name} must be an array")
    specs: List[PathSpec] = []
    for index, item in enumerate(raw_items, start=1):
        try:
            specs.append(normalize_path_spec(item, repo_root=repo_root, allow_dot=allow_dot))
        except PathContractError as exc:
            raise PathContractError(f"{field_name}[{index}]: {exc}") from exc
    return specs


def _ensure_string_list(raw_items: Any, *, field_name: str) -> List[str]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise PhaseGateError(f"{field_name} must be an array")
    normalized: List[str] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, str):
            raise PhaseGateError(f"{field_name}[{index}] must be a string")
        normalized.append(item)
    return normalized


def _normalize_validation_command(
    raw: Any,
    *,
    index: int,
    repo_root: Path,
) -> ValidationCommand:
    if not isinstance(raw, dict):
        raise CommandContractError(f"validation[{index}] must be an object")

    command_id = _clean_id(str(raw.get("id") or ""), f"validation_{index:03d}")
    cwd = normalize_repo_relative_path(
        raw.get("cwd", "."),
        repo_root=repo_root,
        allow_dot=True,
        must_exist=True,
        require_dir=True,
    )
    required = bool(raw.get("required", True))
    timeout_seconds = raw.get("timeout_seconds", DEFAULT_VALIDATION_TIMEOUT_SECONDS)
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise CommandContractError(f"validation[{index}].timeout_seconds must be a number")
    if float(timeout_seconds) <= 0:
        raise CommandContractError(f"validation[{index}].timeout_seconds must be greater than zero")

    shell = bool(raw.get("shell", False))
    manual = bool(raw.get("manual", False))
    live = bool(raw.get("live", False))
    if shell:
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise CommandContractError(
                f"validation[{index}] with shell=true must include non-empty command"
            )
        if "argv" in raw:
            raise CommandContractError(f"validation[{index}] cannot combine shell=true with argv")
        return ValidationCommand(
            id=command_id,
            cwd=cwd,
            required=required,
            timeout_seconds=float(timeout_seconds),
            shell=True,
            command=command,
            manual=manual,
            live=live,
        )

    if "command" in raw:
        raise CommandContractError(
            f"validation[{index}] shell command requires explicit shell=true"
        )
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv:
        raise CommandContractError(f"validation[{index}] must include non-empty argv")
    normalized_argv: List[str] = []
    for argv_index, item in enumerate(argv, start=1):
        if not isinstance(item, str) or not item:
            raise CommandContractError(
                f"validation[{index}].argv[{argv_index}] must be a non-empty string"
            )
        normalized_argv.append(item)

    return ValidationCommand(
        id=command_id,
        cwd=cwd,
        required=required,
        timeout_seconds=float(timeout_seconds),
        shell=False,
        argv=normalized_argv,
        manual=manual,
        live=live,
    )


def render_validation_command(command: ValidationCommand) -> str:
    cwd_display = "." if command.cwd == "." else command.cwd
    if command.shell:
        assert command.command is not None
        return f"(cd {shlex.quote(cwd_display)} && {command.command})"
    assert command.argv is not None
    return f"(cd {shlex.quote(cwd_display)} && {shlex.join(command.argv)})"


def _format_prompt_list(items: Sequence[str], *, empty: str = "- None.") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def _path_specs_as_prompt_items(phase: Dict[str, Any], field: str) -> List[str]:
    items: List[str] = []
    for spec in _specs_from_phase(phase, field):
        suffix = "/" if spec.is_dir and spec.path != "." else ""
        items.append(f"{spec.path}{suffix}")
    return items


def render_executor_prompt(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    repo_root: Path,
    relay_mode: str = DEFAULT_EXECUTOR_RELAY_MODE,
    local_sandbox: str = "workspace-write",
) -> str:
    """Render the PhaseGate kickoff prompt for a single executor phase."""

    validation_lines = [
        render_validation_command(_validation_command_from_mapping(command))
        for command in phase.get("validation", [])
    ]
    final_report_json = {
        "phase_id": phase.get("id"),
        "status": "completed | failed | blocked",
        "files_changed": [],
        "files_inspected": [],
        "validations_run": [
            {"command": "<command>", "result": "passed | failed | skipped"}
        ],
        "smoke_checks": [],
        "skipped_checks": [],
        "deviations": [],
        "commit_made": False,
        "staged_files": [],
    }
    relay_contract = (
        "Relay mode: disabled. The prompt is persisted for manual handoff; "
        "PhaseGate will not launch an executor for this run."
    )
    if relay_mode == "local_exec":
        relay_contract = (
            "Relay mode: local_exec. The executor runs via local Codex CLI in the target "
            f"repo with sandbox={local_sandbox}."
        )

    return "\n".join(
        [
            "You are the executor agent for PhaseGate headless automation.",
            "",
            f"Target repo: {repo_root.resolve()}",
            f"Working directory: {repo_root.resolve()}",
            f"Branch: {plan.get('branch')}",
            f"Run: {plan.get('run_name')}",
            "",
            f"Current phase: {phase.get('name')} ({phase.get('id')})",
            "",
            "Objective:",
            str(phase.get("objective") or "").strip(),
            "",
            "Non-goals:",
            _format_prompt_list(phase.get("non_goals", [])),
            "",
            "Read first:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "read_first")),
            "",
            "Allowed write paths:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "allowed_write_paths")),
            "",
            "Expected pre-existing dirty paths:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "expected_dirty_paths")),
            "",
            "Do not modify:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "forbidden_paths")),
            "",
            "Relay contract:",
            f"- {relay_contract}",
            "- Honor the write scope regardless of relay mode.",
            "",
            "Hard rules:",
            "- Do not commit.",
            "- Do not stage changes unless explicitly asked.",
            "- Do not touch unrelated repos or unrelated files.",
            "- You are not alone in the codebase. Preserve user/orchestrator changes and work with any dirty files you encounter.",
            "- Keep changes narrowly scoped to this phase.",
            "- Do not add temporary planning or orchestration artifacts to git history unless explicitly instructed.",
            "",
            "Validation to run:",
            _format_prompt_list(validation_lines),
            "",
            "Final report format:",
            "- Current branch and git status summary.",
            "- Files changed.",
            "- What changed.",
            "- Validation commands run and exact pass/fail results.",
            "- Smoke/live checks run, skipped, or blocked.",
            "- Deviations from the phase plan.",
            "- Confirmation that no commit was made and no files were staged.",
            "- Include this structured JSON report in a fenced json block:",
            "```json",
            json.dumps(final_report_json, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def _single_line_excerpt(value: Any, *, limit: int = 600) -> str:
    text = " ".join(_coerce_output_text(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + " ... [truncated]"


def _format_inline_list(items: Sequence[Any], *, empty: str = "none") -> str:
    normalized = [str(item) for item in items if str(item)]
    return ", ".join(normalized) if normalized else empty


def verifier_findings(report: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(report, dict) or not report.get("blocked"):
        return []

    findings: List[str] = []
    reasons = [str(reason) for reason in report.get("blocked_reasons", [])]
    if reasons:
        findings.append(f"Git scope verifier blocked this attempt: {_format_inline_list(reasons)}.")
    if report.get("staged_paths"):
        findings.append(
            "Unexpected staged changes are present and require operator review: "
            f"{_format_inline_list(report.get('staged_paths', []))}."
        )
    if report.get("unexpected_commit_artifacts", {}).get("commits"):
        findings.append("Unexpected executor commit detected; do not rewrite history.")
    if report.get("branch_drift"):
        findings.append(
            "Branch drift detected: "
            f"expected {report.get('expected_branch')!r}, current {report.get('current_branch')!r}."
        )
    if report.get("head_drift"):
        findings.append(
            "HEAD drift detected between baseline and current worktree state."
        )
    if report.get("forbidden_changed_paths"):
        findings.append(
            "Forbidden paths changed: "
            f"{_format_inline_list(report.get('forbidden_changed_paths', []))}."
        )
    if report.get("new_unrelated_dirty_paths"):
        findings.append(
            "Dirty paths outside the current phase write scope appeared: "
            f"{_format_inline_list(report.get('new_unrelated_dirty_paths', []))}."
        )
    if report.get("preexisting_dirty_allowed_paths"):
        findings.append(
            "Pre-existing dirty files inside allowed write paths require operator review: "
            f"{_format_inline_list(report.get('preexisting_dirty_allowed_paths', []))}."
        )
    return findings or ["Git scope verifier blocked this attempt."]


def relay_findings(
    relay_result: Optional[Dict[str, Any]],
    attempt_record: Dict[str, Any],
) -> List[str]:
    relay_result = relay_result or {}
    details = [
        f"Executor relay did not complete successfully; exit code {relay_result.get('exit_code')!r}, status {relay_result.get('status')!r}."
    ]
    stderr = _single_line_excerpt(attempt_record.get("executor_relay_stderr_excerpt", ""))
    stdout = _single_line_excerpt(attempt_record.get("executor_relay_stdout_excerpt", ""))
    if stderr:
        details.append(f"Relay stderr excerpt: {stderr}")
    if stdout:
        details.append(f"Relay stdout excerpt: {stdout}")
    return details


def validation_findings(results: Sequence[Dict[str, Any]]) -> List[str]:
    findings: List[str] = []
    for result in results:
        if not result.get("blocking"):
            continue
        rendered = str(result.get("rendered") or result.get("id") or "validation")
        result_id = str(result.get("id") or "validation")
        status = str(result.get("status") or "failed")
        exit_code = result.get("exit_code")
        detail = f"Validation `{result_id}` (`{rendered}`) ended with status {status}"
        if exit_code is not None:
            detail += f" and exit code {exit_code}"
        if result.get("timed_out"):
            detail += f" after {result.get('timeout_seconds')} seconds"
        stdout = _single_line_excerpt(result.get("stdout_excerpt", ""))
        stderr = _single_line_excerpt(result.get("stderr_excerpt", ""))
        if stdout:
            detail += f"; stdout: {stdout}"
        if stderr:
            detail += f"; stderr: {stderr}"
        findings.append(detail + ".")
    return findings


def _validation_prompt_lines(phase: Dict[str, Any]) -> List[str]:
    return [
        render_validation_command(_validation_command_from_mapping(command))
        for command in phase.get("validation", [])
    ]


def render_followup_prompt(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    repo_root: Path,
    followup_number: int,
    max_followups: int,
    findings: Sequence[str],
) -> str:
    """Render a bounded same-phase follow-up prompt from verifier/validation findings."""

    final_report_json = {
        "phase_id": phase.get("id"),
        "status": "completed | failed | blocked",
        "files_changed": [],
        "validations_run": [
            {"command": "<command>", "result": "passed | failed | skipped"}
        ],
        "remaining_findings": [],
        "commit_made": False,
        "staged_files": [],
    }
    return "\n".join(
        [
            "Not approved yet.",
            "",
            "You are continuing the current PhaseGate phase.",
            "",
            f"Target repo: {repo_root.resolve()}",
            f"Working directory: {repo_root.resolve()}",
            f"Branch: {plan.get('branch')}",
            f"Run: {plan.get('run_name')}",
            f"Current phase: {phase.get('name')} ({phase.get('id')})",
            f"Follow-up attempt: {followup_number} of {max_followups}",
            "",
            "Do not commit.",
            "Do not stage changes unless explicitly asked.",
            "Do not touch unrelated repos or unrelated files.",
            "You are not alone in the codebase. Preserve existing changes and only modify what is needed for this follow-up.",
            "",
            "Allowed write paths:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "allowed_write_paths")),
            "",
            "Expected pre-existing dirty paths:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "expected_dirty_paths")),
            "",
            "Do not modify:",
            _format_prompt_list(_path_specs_as_prompt_items(phase, "forbidden_paths")),
            "",
            "Issues to fix:",
            _format_prompt_list(list(findings)),
            "",
            "Expected changes:",
            "- Make only the minimal changes needed to resolve the listed findings.",
            "- Keep all intentional code changes inside the allowed write paths for this phase.",
            "- If resolving a finding requires broader scope, stop and report the manual decision needed.",
            "",
            "Validation to run:",
            _format_prompt_list(_validation_prompt_lines(phase)),
            "",
            "Final report format:",
            "- Files changed.",
            "- What changed.",
            "- Validation commands run and exact results.",
            "- Smoke/live checks run, skipped, or blocked.",
            "- Remaining findings or manual decisions needed.",
            "- Confirmation that no commit was made and no files were staged.",
            "- Include this structured JSON report in a fenced json block:",
            "```json",
            json.dumps(final_report_json, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def _coerce_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _output_excerpt(value: Any, *, limit: int = DEFAULT_OUTPUT_EXCERPT_CHARS) -> Tuple[str, bool]:
    text = _coerce_output_text(value)
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _validation_command_from_mapping(raw: Any) -> ValidationCommand:
    if isinstance(raw, ValidationCommand):
        return raw
    if not isinstance(raw, dict):
        raise CommandContractError("validation command must be an object")

    argv_raw = raw.get("argv")
    argv = [str(item) for item in argv_raw] if isinstance(argv_raw, list) else None
    command_raw = raw.get("command")
    command = str(command_raw) if command_raw is not None else None
    return ValidationCommand(
        id=str(raw.get("id") or ""),
        cwd=str(raw.get("cwd") or "."),
        required=bool(raw.get("required", True)),
        timeout_seconds=float(raw.get("timeout_seconds", DEFAULT_VALIDATION_TIMEOUT_SECONDS)),
        shell=bool(raw.get("shell", False)),
        argv=argv,
        command=command,
        manual=bool(raw.get("manual", False)),
        live=bool(raw.get("live", False)),
    )


def _validation_cwd_path(repo_root: Path, command: ValidationCommand) -> Path:
    normalized = normalize_repo_relative_path(
        command.cwd,
        repo_root=repo_root,
        allow_dot=True,
        must_exist=True,
        require_dir=True,
    )
    return repo_root if normalized == "." else repo_root / normalized


def _validation_classification(command: ValidationCommand, status: str) -> str:
    prefix = "required" if command.required else "optional"
    return f"{prefix}_{status}"


def _build_validation_result(
    command: ValidationCommand,
    *,
    status: str,
    exit_code: Optional[int],
    timed_out: bool,
    stdout: Any = "",
    stderr: Any = "",
    skip_reason: str = "",
) -> Dict[str, Any]:
    stdout_excerpt, stdout_truncated = _output_excerpt(stdout)
    stderr_excerpt, stderr_truncated = _output_excerpt(stderr)
    blocking = command.required and status != "passed"
    return {
        "id": command.id,
        "status": status,
        "classification": _validation_classification(command, status),
        "required": command.required,
        "blocking": blocking,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_seconds": command.timeout_seconds,
        "rendered": render_validation_command(command),
        "cwd": command.cwd,
        "shell": command.shell,
        "manual": command.manual,
        "live": command.live,
        "stdout_excerpt": stdout_excerpt,
        "stdout_truncated": stdout_truncated,
        "stderr_excerpt": stderr_excerpt,
        "stderr_truncated": stderr_truncated,
        "skip_reason": skip_reason,
    }


def _validation_skip_reason(
    command: ValidationCommand,
    *,
    allow_manual_validation: bool,
    allow_live_validation: bool,
) -> str:
    reasons: List[str] = []
    if command.manual and not allow_manual_validation:
        reasons.append("manual validation requires --allow-manual-validation")
    if command.live and not allow_live_validation:
        reasons.append("live validation requires --allow-live-validation")
    return "; ".join(reasons)


def execute_validation_command(
    *,
    repo_root: Path,
    command: ValidationCommand,
) -> Dict[str, Any]:
    cwd = _validation_cwd_path(repo_root, command)
    try:
        if command.shell:
            if command.command is None:
                raise CommandContractError("shell validation command is missing command text")
            result = subprocess.run(
                command.command,
                cwd=cwd,
                shell=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=command.timeout_seconds,
                check=False,
            )
        else:
            if not command.argv:
                raise CommandContractError("argv validation command is missing argv")
            result = subprocess.run(
                command.argv,
                cwd=cwd,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=command.timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        return _build_validation_result(
            command,
            status="timed_out",
            exit_code=None,
            timed_out=True,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )

    status = "passed" if result.returncode == 0 else "failed"
    return _build_validation_result(
        command,
        status=status,
        exit_code=result.returncode,
        timed_out=False,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def summarize_validation_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    required_failed = [
        str(result["id"])
        for result in results
        if result.get("required") and result.get("status") == "failed"
    ]
    required_timed_out = [
        str(result["id"])
        for result in results
        if result.get("required") and result.get("status") == "timed_out"
    ]
    required_skipped = [
        str(result["id"])
        for result in results
        if result.get("required") and result.get("status") == "skipped"
    ]
    optional_failed = [
        str(result["id"])
        for result in results
        if not result.get("required") and result.get("status") == "failed"
    ]
    optional_timed_out = [
        str(result["id"])
        for result in results
        if not result.get("required") and result.get("status") == "timed_out"
    ]
    optional_skipped = [
        str(result["id"])
        for result in results
        if not result.get("required") and result.get("status") == "skipped"
    ]
    passed = [str(result["id"]) for result in results if result.get("status") == "passed"]
    blocking_validation_ids = [
        str(result["id"]) for result in results if bool(result.get("blocking"))
    ]
    return {
        "total": len(results),
        "passed": passed,
        "required_failed": required_failed,
        "required_timed_out": required_timed_out,
        "required_skipped": required_skipped,
        "optional_failed": optional_failed,
        "optional_timed_out": optional_timed_out,
        "optional_skipped": optional_skipped,
        "blocking_validation_ids": blocking_validation_ids,
        "has_required_failure": bool(required_failed),
        "has_required_timeout": bool(required_timed_out),
        "has_required_skip": bool(required_skipped),
        "has_optional_failure": bool(optional_failed or optional_timed_out),
    }


def run_validation_commands(
    *,
    repo_root: Path,
    commands: Sequence[Any],
    allow_manual_validation: bool = False,
    allow_live_validation: bool = False,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    for raw_command in commands:
        command = _validation_command_from_mapping(raw_command)
        skip_reason = _validation_skip_reason(
            command,
            allow_manual_validation=allow_manual_validation,
            allow_live_validation=allow_live_validation,
        )
        if skip_reason:
            results.append(
                _build_validation_result(
                    command,
                    status="skipped",
                    exit_code=None,
                    timed_out=False,
                    skip_reason=skip_reason,
                )
            )
            continue
        results.append(execute_validation_command(repo_root=repo_root, command=command))

    summary = summarize_validation_results(results)
    summary["results"] = results
    return summary


def record_manual_check_skips(manual_checks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    skipped: List[Dict[str, Any]] = []
    for check in manual_checks:
        blocking = bool(check.get("blocking", False))
        skipped.append(
            {
                "id": str(check.get("id") or ""),
                "description": str(check.get("description") or ""),
                "status": "skipped",
                "classification": (
                    "blocking_manual_check_skipped"
                    if blocking
                    else "optional_manual_check_skipped"
                ),
                "blocking": blocking,
                "skip_reason": (
                    "manual check requires operator review; automated execution is not "
                    "implemented in this phase"
                ),
            }
        )
    return skipped


def validate_repo_root(repo_root_value: Any) -> Path:
    if not isinstance(repo_root_value, (str, Path)):
        raise PhaseGateError("--repo-root must be a path")
    repo_root = Path(repo_root_value).expanduser().resolve()
    if not repo_root.is_dir():
        raise PhaseGateError(f"repo root does not exist or is not a directory: {repo_root}")
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PhaseGateError(f"repo root is not a git repository: {repo_root}: {detail}")
    git_root = Path(result.stdout.strip()).resolve()
    if git_root != repo_root:
        raise PhaseGateError(f"repo root must be the git top-level directory: {repo_root}")
    return repo_root


def _normalize_manual_checks(raw_items: Any) -> List[Dict[str, Any]]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise PhaseGateError("manual_checks must be an array")
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise PhaseGateError(f"manual_checks[{index}] must be an object")
        check_id = _clean_id(str(item.get("id") or ""), f"manual_check_{index:03d}")
        description = item.get("description", "")
        if not isinstance(description, str):
            raise PhaseGateError(f"manual_checks[{index}].description must be a string")
        normalized.append(
            {
                "id": check_id,
                "description": description,
                "blocking": bool(item.get("blocking", False)),
            }
        )
    return normalized


def normalize_plan(raw: Any, *, repo_root: Path) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise PhaseGateError("plan must be a JSON object")

    schema_version = raw.get("schema_version")
    if schema_version != PLAN_SCHEMA_VERSION:
        raise PhaseGateError(f"schema_version must be {PLAN_SCHEMA_VERSION}")

    plan_repo_root_raw = raw.get("repo_root")
    if not isinstance(plan_repo_root_raw, str) or not plan_repo_root_raw.strip():
        raise PhaseGateError("repo_root is required")
    plan_repo_root = Path(plan_repo_root_raw).expanduser().resolve()
    if plan_repo_root != repo_root.resolve():
        raise PhaseGateError(f"plan repo_root does not match --repo-root: {plan_repo_root}")

    branch = raw.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise PhaseGateError("branch is required")

    run_name = raw.get("run_name")
    if not isinstance(run_name, str) or not run_name.strip():
        raise PhaseGateError("run_name is required")
    run_name = _clean_id(run_name, "phase_gate_run")

    commit_mode = str(raw.get("commit_mode") or COMMIT_MODE_NO_COMMIT).strip()
    if commit_mode not in KNOWN_COMMIT_MODES:
        raise PhaseGateError(f"unsupported commit_mode: {commit_mode}")
    if commit_mode not in SUPPORTED_COMMIT_MODES:
        raise PhaseGateError(f"unsupported commit_mode: {commit_mode}")

    allow_dirty_approved_continuation = raw.get(
        "allow_dirty_approved_continuation",
        DEFAULT_ALLOW_DIRTY_APPROVED_CONTINUATION,
    )
    if not isinstance(allow_dirty_approved_continuation, bool):
        raise PhaseGateError("allow_dirty_approved_continuation must be a boolean")

    max_followups = raw.get("max_followups_per_phase", 0)
    if not isinstance(max_followups, int) or isinstance(max_followups, bool) or max_followups < 0:
        raise PhaseGateError("max_followups_per_phase must be a non-negative integer")

    phases_raw = raw.get("phases")
    if not isinstance(phases_raw, list):
        raise PhaseGateError("phases must be an array")
    if not phases_raw:
        raise PhaseGateError("phases must not be empty")

    phases: List[Dict[str, Any]] = []
    for index, phase_raw in enumerate(phases_raw, start=1):
        if not isinstance(phase_raw, dict):
            raise PhaseGateError(f"phases[{index}] must be an object")
        phase_id = _clean_id(str(phase_raw.get("id") or ""), f"phase_{index:03d}")
        name = phase_raw.get("name")
        objective = phase_raw.get("objective")
        if not isinstance(name, str) or not name.strip():
            raise PhaseGateError(f"phases[{index}].name is required")
        if not isinstance(objective, str) or not objective.strip():
            raise PhaseGateError(f"phases[{index}].objective is required")

        validations_raw = phase_raw.get("validation", [])
        if not isinstance(validations_raw, list):
            raise CommandContractError(f"phases[{index}].validation must be an array")
        validations = [
            asdict(
                _normalize_validation_command(
                    item,
                    index=validation_index,
                    repo_root=repo_root,
                )
            )
            for validation_index, item in enumerate(validations_raw, start=1)
        ]
        for validation in validations:
            validation["rendered"] = render_validation_command(ValidationCommand(**{
                key: validation.get(key)
                for key in (
                    "id",
                    "cwd",
                    "required",
                    "timeout_seconds",
                    "shell",
                    "argv",
                    "command",
                    "manual",
                    "live",
                )
            }))

        phases.append(
            {
                "index": index,
                "id": phase_id,
                "name": name.strip(),
                "objective": objective.strip(),
                "non_goals": _ensure_string_list(
                    phase_raw.get("non_goals", []),
                    field_name=f"phases[{index}].non_goals",
                ),
                "read_first": [
                    asdict(spec)
                    for spec in _normalize_path_list(
                        phase_raw.get("read_first", []),
                        field_name=f"phases[{index}].read_first",
                        repo_root=repo_root,
                    )
                ],
                "allowed_write_paths": [
                    asdict(spec)
                    for spec in _normalize_path_list(
                        phase_raw.get("allowed_write_paths", []),
                        field_name=f"phases[{index}].allowed_write_paths",
                        repo_root=repo_root,
                    )
                ],
                "expected_dirty_paths": [
                    asdict(spec)
                    for spec in _normalize_path_list(
                        phase_raw.get("expected_dirty_paths", []),
                        field_name=f"phases[{index}].expected_dirty_paths",
                        repo_root=repo_root,
                    )
                ],
                "forbidden_paths": [
                    asdict(spec)
                    for spec in _normalize_path_list(
                        phase_raw.get("forbidden_paths", []),
                        field_name=f"phases[{index}].forbidden_paths",
                        repo_root=repo_root,
                    )
                ],
                "validation": validations,
                "manual_checks": _normalize_manual_checks(phase_raw.get("manual_checks", [])),
                "commit_message": phase_raw.get("commit_message")
                if isinstance(phase_raw.get("commit_message"), str)
                else "",
                "status": "pending",
            }
        )

    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "repo_root": str(repo_root.resolve()),
        "branch": branch.strip(),
        "run_name": run_name,
        "commit_mode": commit_mode,
        "allow_dirty_approved_continuation": allow_dirty_approved_continuation,
        "max_followups_per_phase": max_followups,
        "global_unrelated_dirty_policy": str(
            raw.get("global_unrelated_dirty_policy") or "block_on_overlap"
        ),
        "phases": phases,
    }


def load_plan_file(plan_file: Path, *, repo_root: Path) -> Dict[str, Any]:
    return normalize_plan(_load_json_file(plan_file), repo_root=repo_root)


def _run_git(repo_root: Path, args: Sequence[str], *, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 and not allow_failure:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitCommandError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def parse_porcelain_v1_z(raw: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    tokens = raw.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# ") or token.startswith("## "):
            continue
        if len(token) < 4:
            continue
        xy = token[:2]
        path = token[3:]
        original_path: Optional[str] = None
        if "R" in xy or "C" in xy:
            if index < len(tokens):
                original_path = tokens[index] or None
                index += 1
        entries.append({"xy": xy, "path": path, "orig_path": original_path})
    return entries


def parse_name_status(raw: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        code = status[0]
        if code in {"R", "C"} and len(fields) >= 3:
            entries.append(
                {
                    "status": status,
                    "code": code,
                    "path": fields[2],
                    "orig_path": fields[1],
                    "paths": [fields[1], fields[2]],
                }
            )
        elif len(fields) >= 2:
            entries.append(
                {
                    "status": status,
                    "code": code,
                    "path": fields[1],
                    "orig_path": None,
                    "paths": [fields[1]],
                }
            )
    return entries


def _paths_from_name_status(entries: Sequence[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for entry in entries:
        for path in entry.get("paths", []):
            if isinstance(path, str) and path:
                paths.append(path)
    return paths


def _paths_from_status_entries(entries: Sequence[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for entry in entries:
        path = entry.get("path")
        orig_path = entry.get("orig_path")
        if isinstance(path, str) and path:
            paths.append(path)
        if isinstance(orig_path, str) and orig_path:
            paths.append(orig_path)
    return paths


def _dedupe_sorted(paths: Iterable[str]) -> List[str]:
    return sorted({str(PurePosixPath(path)) for path in paths if path})


def _is_tracked_path(repo_root: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _is_ignored_path(repo_root: Path, path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", path],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def classify_repo_path(repo_root: Path, path: str) -> Dict[str, Any]:
    normalized = normalize_repo_relative_path(path, repo_root=repo_root)
    tracked = _is_tracked_path(repo_root, normalized)
    ignored = _is_ignored_path(repo_root, normalized)
    return {
        "path": normalized,
        "tracked": tracked,
        "ignored": ignored,
    }


def capture_unexpected_commit_artifacts(
    repo_root: Path,
    *,
    baseline_head: str,
    current_head: str,
) -> Dict[str, Any]:
    if not baseline_head or baseline_head == current_head:
        return {"log_oneline": "", "commits": []}

    log_oneline = _run_git(repo_root, ["log", "--oneline", f"{baseline_head}..HEAD"], allow_failure=True)
    rev_list_raw = _run_git(repo_root, ["rev-list", "--reverse", f"{baseline_head}..HEAD"], allow_failure=True)
    commits: List[Dict[str, Any]] = []
    for commit in [line.strip() for line in rev_list_raw.splitlines() if line.strip()]:
        commits.append(
            {
                "commit": commit,
                "show_stat": _run_git(repo_root, ["show", "--stat", commit], allow_failure=True),
                "show_find_renames_copies": _run_git(
                    repo_root,
                    ["show", "--find-renames", "--find-copies", commit],
                    allow_failure=True,
                ),
            }
        )
    return {"log_oneline": log_oneline, "commits": commits}


def capture_git_state(repo_root: Path, *, baseline_head: Optional[str] = None) -> Dict[str, Any]:
    head = _run_git(repo_root, ["rev-parse", "HEAD"]).strip()
    branch = _run_git(repo_root, ["branch", "--show-current"]).strip()
    status_short_branch = _run_git(repo_root, ["status", "--short", "--branch"])
    status_porcelain_z = _run_git(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all"],
    )
    status_ignored_porcelain_z = _run_git(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--branch", "--ignored", "--untracked-files=all"],
    )
    diff_name_status = _run_git(repo_root, ["diff", "--name-status"])
    diff_cached_name_status = _run_git(repo_root, ["diff", "--cached", "--name-status"])
    diff_stat = _run_git(repo_root, ["diff", "--stat"])

    status_entries = parse_porcelain_v1_z(status_porcelain_z)
    ignored_status_entries = parse_porcelain_v1_z(status_ignored_porcelain_z)
    diff_entries = parse_name_status(diff_name_status)
    cached_diff_entries = parse_name_status(diff_cached_name_status)

    staged_paths: List[str] = []
    unstaged_paths: List[str] = []
    untracked_paths: List[str] = []
    deleted_paths: List[str] = []
    renamed_paths: List[Dict[str, Optional[str]]] = []
    copied_paths: List[Dict[str, Optional[str]]] = []

    for entry in status_entries:
        xy = str(entry.get("xy") or "  ")
        x_status = xy[0]
        y_status = xy[1]
        entry_paths = _paths_from_status_entries([entry])
        if xy == "??":
            untracked_paths.extend(entry_paths)
            continue
        if x_status not in {" ", "?", "!"}:
            staged_paths.extend(entry_paths)
        if y_status not in {" ", "?", "!"}:
            unstaged_paths.extend(entry_paths)
        if "D" in xy:
            deleted_paths.extend(entry_paths)
        if "R" in xy:
            renamed_paths.append(
                {"from": entry.get("orig_path"), "to": entry.get("path"), "status": xy}
            )
        if "C" in xy:
            copied_paths.append(
                {"from": entry.get("orig_path"), "to": entry.get("path"), "status": xy}
            )

    for entry in diff_entries + cached_diff_entries:
        code = str(entry.get("code") or "")
        if code == "D":
            deleted_paths.extend(_paths_from_name_status([entry]))
        elif code == "R":
            renamed_paths.append(
                {"from": entry.get("orig_path"), "to": entry.get("path"), "status": entry.get("status")}
            )
        elif code == "C":
            copied_paths.append(
                {"from": entry.get("orig_path"), "to": entry.get("path"), "status": entry.get("status")}
            )

    changed_paths = _dedupe_sorted(
        _paths_from_status_entries(status_entries)
        + _paths_from_name_status(diff_entries)
        + _paths_from_name_status(cached_diff_entries)
    )
    ignored_paths = _dedupe_sorted(
        _paths_from_status_entries(
            [entry for entry in ignored_status_entries if entry.get("xy") == "!!"]
        )
    )

    artifacts = (
        capture_unexpected_commit_artifacts(
            repo_root,
            baseline_head=baseline_head,
            current_head=head,
        )
        if baseline_head and baseline_head != head
        else {"log_oneline": "", "commits": []}
    )

    return {
        "head": head,
        "branch": branch,
        "status_short_branch": status_short_branch,
        "status_porcelain_z": status_porcelain_z,
        "status_ignored_porcelain_z": status_ignored_porcelain_z,
        "diff_name_status": diff_name_status,
        "diff_cached_name_status": diff_cached_name_status,
        "diff_stat": diff_stat,
        "status_entries": status_entries,
        "ignored_status_entries": ignored_status_entries,
        "diff_name_status_entries": diff_entries,
        "diff_cached_name_status_entries": cached_diff_entries,
        "changed_paths": changed_paths,
        "staged_paths": _dedupe_sorted(staged_paths + _paths_from_name_status(cached_diff_entries)),
        "unstaged_paths": _dedupe_sorted(unstaged_paths + _paths_from_name_status(diff_entries)),
        "untracked_paths": _dedupe_sorted(untracked_paths),
        "ignored_paths": ignored_paths,
        "deleted_paths": _dedupe_sorted(deleted_paths),
        "renamed_paths": renamed_paths,
        "copied_paths": copied_paths,
        "unexpected_commit_artifacts": artifacts,
    }


def _artifact_path_spec(repo_root: Path, path: Path, *, is_dir: bool) -> Optional[PathSpec]:
    candidate = path if path.is_absolute() else repo_root / path
    try:
        relative = candidate.resolve(strict=False).relative_to(repo_root.resolve())
    except ValueError:
        return None
    normalized = str(PurePosixPath(relative.as_posix()))
    if not normalized or normalized == ".":
        return None
    return PathSpec(raw=normalized + ("/" if is_dir else ""), path=normalized, is_dir=is_dir)


def runner_artifact_specs(
    *,
    repo_root: Path,
    output_dir: Path,
    state_file: Optional[Path],
) -> List[PathSpec]:
    specs: List[PathSpec] = []
    output_spec = _artifact_path_spec(repo_root, output_dir, is_dir=True)
    if output_spec:
        specs.append(output_spec)
    if state_file is not None:
        state_spec = _artifact_path_spec(repo_root, state_file, is_dir=False)
        if state_spec and not path_matches_any(state_spec.path, specs):
            specs.append(state_spec)
    return specs


def _filter_path_list_for_specs(paths: Any, specs: Sequence[PathSpec]) -> List[str]:
    if not isinstance(paths, list) or not specs:
        return list(paths) if isinstance(paths, list) else []
    return [
        path
        for path in paths
        if isinstance(path, str) and not path_matches_any(path, specs)
    ]


def _entry_matches_specs(entry: Dict[str, Any], specs: Sequence[PathSpec]) -> bool:
    for path in _paths_from_status_entries([entry]) + _paths_from_name_status([entry]):
        if path_matches_any(path, specs):
            return True
    return False


def _rename_or_copy_matches_specs(entry: Dict[str, Any], specs: Sequence[PathSpec]) -> bool:
    for key in ("from", "to"):
        value = entry.get(key)
        if isinstance(value, str) and path_matches_any(value, specs):
            return True
    return False


def filter_git_state_for_runner_artifacts(
    git_state: Dict[str, Any],
    artifact_specs: Sequence[PathSpec],
) -> Dict[str, Any]:
    if not artifact_specs:
        return git_state

    filtered = dict(git_state)
    for key in (
        "changed_paths",
        "staged_paths",
        "unstaged_paths",
        "untracked_paths",
        "ignored_paths",
        "deleted_paths",
    ):
        filtered[key] = _filter_path_list_for_specs(git_state.get(key, []), artifact_specs)

    for key in (
        "status_entries",
        "ignored_status_entries",
        "diff_name_status_entries",
        "diff_cached_name_status_entries",
    ):
        raw_entries = git_state.get(key, [])
        filtered[key] = [
            entry
            for entry in raw_entries
            if isinstance(entry, dict) and not _entry_matches_specs(entry, artifact_specs)
        ]

    for key in ("renamed_paths", "copied_paths"):
        raw_entries = git_state.get(key, [])
        filtered[key] = [
            entry
            for entry in raw_entries
            if isinstance(entry, dict) and not _rename_or_copy_matches_specs(entry, artifact_specs)
        ]

    filtered["runner_artifact_paths"] = [
        spec.path + ("/" if spec.is_dir else "") for spec in artifact_specs
    ]
    return filtered


def _specs_from_phase(phase: Dict[str, Any], field: str) -> List[PathSpec]:
    specs: List[PathSpec] = []
    for raw in phase.get(field, []):
        if isinstance(raw, PathSpec):
            specs.append(raw)
        elif isinstance(raw, dict):
            specs.append(PathSpec(raw=str(raw["raw"]), path=str(raw["path"]), is_dir=bool(raw["is_dir"])))
        else:
            raise PhaseGateError(f"phase {field} contains invalid path spec")
    return specs


def _phase_with_additional_expected_dirty(
    phase: Dict[str, Any],
    paths: Sequence[str],
) -> Dict[str, Any]:
    normalized_paths = _dedupe_sorted(paths)
    if not normalized_paths:
        return phase
    updated = dict(phase)
    existing = [
        asdict(spec) if isinstance(spec, PathSpec) else dict(spec)
        for spec in phase.get("expected_dirty_paths", [])
        if isinstance(spec, (dict, PathSpec))
    ]
    existing_paths = {str(item.get("path")) for item in existing if item.get("path")}
    for path in normalized_paths:
        if path in existing_paths:
            continue
        existing.append({"raw": path, "path": path, "is_dir": False})
    updated["expected_dirty_paths"] = existing
    return updated


def classify_git_scope(
    *,
    repo_root: Path,
    phase: Dict[str, Any],
    baseline_state: Dict[str, Any],
    current_state: Dict[str, Any],
    expected_branch: Optional[str] = None,
) -> Dict[str, Any]:
    allowed_specs = _specs_from_phase(phase, "allowed_write_paths")
    expected_specs = _specs_from_phase(phase, "expected_dirty_paths")
    forbidden_specs = _specs_from_phase(phase, "forbidden_paths")

    changed_paths = _dedupe_sorted(current_state.get("changed_paths", []))
    baseline_changed_paths = _dedupe_sorted(baseline_state.get("changed_paths", []))
    baseline_allowed_dirty = [
        path
        for path in baseline_changed_paths
        if path_matches_any(path, allowed_specs) and not path_matches_any(path, expected_specs)
    ]
    baseline_unrelated_dirty = [
        path
        for path in baseline_changed_paths
        if not path_matches_any(path, allowed_specs)
        and not path_matches_any(path, expected_specs)
        and not path_matches_any(path, forbidden_specs)
    ]

    allowed_changed_paths = [
        path
        for path in changed_paths
        if path_matches_any(path, allowed_specs) or path_matches_any(path, expected_specs)
    ]
    forbidden_changed_paths = [
        path for path in changed_paths if path_matches_any(path, forbidden_specs)
    ]
    unrelated_dirty_paths = [
        path
        for path in changed_paths
        if not path_matches_any(path, allowed_specs)
        and not path_matches_any(path, expected_specs)
        and not path_matches_any(path, forbidden_specs)
    ]
    new_unrelated_dirty_paths = [
        path
        for path in unrelated_dirty_paths
        if path not in set(baseline_unrelated_dirty)
    ]

    branch_drift = current_state.get("branch") != baseline_state.get("branch")
    if expected_branch is not None:
        branch_drift = branch_drift or current_state.get("branch") != expected_branch
    head_drift = current_state.get("head") != baseline_state.get("head")
    staged_paths = _dedupe_sorted(current_state.get("staged_paths", []))

    blocked_reasons: List[str] = []
    if branch_drift:
        blocked_reasons.append("branch_drift")
    if head_drift:
        blocked_reasons.append("head_drift")
    if current_state.get("unexpected_commit_artifacts", {}).get("commits"):
        blocked_reasons.append("unexpected_commit")
    if staged_paths:
        blocked_reasons.append("staged_changes_present")
    if forbidden_changed_paths:
        blocked_reasons.append("forbidden_paths_changed")
    if new_unrelated_dirty_paths:
        blocked_reasons.append("dirty_paths_outside_allowed_scope")
    if baseline_allowed_dirty:
        blocked_reasons.append("preexisting_dirty_allowed_paths")

    report = {
        "phase_id": phase.get("id"),
        "baseline_head": baseline_state.get("head"),
        "current_head": current_state.get("head"),
        "baseline_branch": baseline_state.get("branch"),
        "current_branch": current_state.get("branch"),
        "expected_branch": expected_branch,
        "branch_drift": branch_drift,
        "head_drift": head_drift,
        "changed_paths": changed_paths,
        "allowed_changed_paths": _dedupe_sorted(allowed_changed_paths),
        "expected_dirty_paths": [
            path for path in changed_paths if path_matches_any(path, expected_specs)
        ],
        "forbidden_changed_paths": _dedupe_sorted(forbidden_changed_paths),
        "unrelated_dirty_paths": _dedupe_sorted(unrelated_dirty_paths),
        "baseline_unrelated_dirty_paths": _dedupe_sorted(baseline_unrelated_dirty),
        "new_unrelated_dirty_paths": _dedupe_sorted(new_unrelated_dirty_paths),
        "preexisting_dirty_allowed_paths": _dedupe_sorted(baseline_allowed_dirty),
        "staged_paths": staged_paths,
        "unstaged_paths": _dedupe_sorted(current_state.get("unstaged_paths", [])),
        "untracked_paths": _dedupe_sorted(current_state.get("untracked_paths", [])),
        "deleted_paths": _dedupe_sorted(current_state.get("deleted_paths", [])),
        "renamed_paths": current_state.get("renamed_paths", []),
        "copied_paths": current_state.get("copied_paths", []),
        "diff_name_status": current_state.get("diff_name_status", ""),
        "diff_cached_name_status": current_state.get("diff_cached_name_status", ""),
        "diff_stat": current_state.get("diff_stat", ""),
        "unexpected_commit_artifacts": current_state.get("unexpected_commit_artifacts", {}),
        "blocked": bool(blocked_reasons),
        "blocked_reasons": blocked_reasons,
    }
    return report


def render_git_add_command(paths: Sequence[str]) -> str:
    normalized = _dedupe_sorted(paths)
    if not normalized:
        raise PhaseGateError("cannot render git add command without paths")
    return "git add -- " + " ".join(shlex.quote(path) for path in normalized)


def render_git_commit_command(message: str) -> str:
    commit_message = message.strip()
    if not commit_message:
        raise PhaseGateError("cannot render git commit command without a message")
    return "git commit -m " + shlex.quote(commit_message)


def render_targeted_commit_commands(paths: Sequence[str], message: str) -> List[str]:
    return [render_git_add_command(paths), render_git_commit_command(message)]


def _phase_commit_message(phase: Dict[str, Any]) -> str:
    return str(phase.get("commit_message") or "").strip()


def _reviewed_files_from_decision(
    decision: Optional[Dict[str, Any]],
    *,
    repo_root: Path,
) -> List[str]:
    if not isinstance(decision, dict) or not decision.get("approved"):
        return []
    reviewed_raw = decision.get("reviewed_files", [])
    if not isinstance(reviewed_raw, list):
        return []
    reviewed_files: List[str] = []
    for item in reviewed_raw:
        if not isinstance(item, str):
            continue
        try:
            reviewed_files.append(normalize_repo_relative_path(item, repo_root=repo_root))
        except PathContractError:
            continue
    return _dedupe_sorted(reviewed_files)


def _semantic_review_is_approved(decision: Optional[Dict[str, Any]]) -> bool:
    return isinstance(decision, dict) and bool(decision.get("approved"))


def _commit_gate_review_needed_files(
    *,
    packet: Optional[Dict[str, Any]],
    verifier_report: Dict[str, Any],
) -> List[str]:
    if isinstance(packet, dict):
        packet_files = packet.get("review_required_files")
        if isinstance(packet_files, list):
            return _dedupe_sorted([str(path) for path in packet_files if isinstance(path, str)])
    return _review_required_changed_files(verifier_report)


def _phase_has_later_phases(plan: Dict[str, Any], phase: Dict[str, Any]) -> bool:
    return int(phase.get("index") or 0) < len(plan.get("phases", []))


def _allow_dirty_approved_continuation(
    *,
    plan: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> bool:
    return bool(
        getattr(args, "allow_dirty_approved_continuation", False)
        or (plan or {}).get("allow_dirty_approved_continuation")
    )


def _render_commit_gate_markdown(record: Dict[str, Any]) -> str:
    approved_commands = record.get("approved_commands", [])
    candidate_commands = record.get("candidate_commands_not_approved", [])
    lines = [
        "# PhaseGate Commit Gate",
        "",
        f"Phase: {record.get('phase_name')} ({record.get('phase_id')})",
        f"Commit mode: {record.get('commit_mode')}",
        f"Status: {record.get('status')}",
        "",
        "## Reviewed Files",
        _format_prompt_list([str(path) for path in record.get("reviewed_files", [])]),
        "",
        "## Review Needed Files",
        _format_prompt_list([str(path) for path in record.get("review_needed_files", [])]),
        "",
    ]
    if record.get("approved"):
        lines.extend(["## Approved For Commit", ""])
        if approved_commands:
            lines.extend(["```sh", *[str(command) for command in approved_commands], "```", ""])
        else:
            lines.extend(["- No approved commit commands were emitted.", ""])
    else:
        lines.extend(
            [
                "## Not Approved For Commit",
                "",
                "The commands below are candidates only and are not approved to run.",
                "",
            ]
        )
        if candidate_commands:
            lines.extend(
                [
                    "```sh",
                    *[str(command) for command in candidate_commands],
                    "```",
                    "",
                ]
            )
        else:
            lines.extend(["- No candidate commit commands are available.", ""])
    if record.get("blocking_reasons"):
        lines.extend(
            [
                "## Blocking Reasons",
                _format_prompt_list([str(reason) for reason in record.get("blocking_reasons", [])]),
                "",
            ]
        )
    return "\n".join(lines)


def _persist_commit_gate_record(
    *,
    attempt_record: Dict[str, Any],
    phase_record: Dict[str, Any],
    ledger: Dict[str, Any],
    record: Dict[str, Any],
) -> None:
    _write_json_file(Path(attempt_record["commit_gate_path"]), record)
    command_path = Path(attempt_record["commit_command_path"])
    command_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.write_text(_render_commit_gate_markdown(record), encoding="utf-8")

    attempt_record["reviewed_files"] = record.get("reviewed_files", [])
    attempt_record["commit_gate"] = record
    attempt_record["commit_gate_status"] = record.get("status")
    attempt_record["commit_gate_blocking_reasons"] = record.get("blocking_reasons", [])
    attempt_record["approved_commit_commands"] = record.get("approved_commands", [])
    attempt_record["candidate_commit_commands_not_approved"] = record.get(
        "candidate_commands_not_approved",
        [],
    )
    attempt_record["approval_checkpoint"] = record.get("approval_checkpoint")
    attempt_record["auto_commit_result"] = record.get("auto_commit_result")
    attempt_record["commit_hash"] = record.get("commit_hash")
    _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
    _sync_ledger_from_phase(ledger, phase_record)


def _build_commit_gate_record(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    repo_root: Path,
    decision: Optional[Dict[str, Any]],
    packet: Optional[Dict[str, Any]],
    verifier_report: Dict[str, Any],
) -> Dict[str, Any]:
    commit_mode = str(plan.get("commit_mode") or COMMIT_MODE_NO_COMMIT)
    approved = _semantic_review_is_approved(decision)
    reviewed_files = _reviewed_files_from_decision(decision, repo_root=repo_root)
    review_needed_files = _commit_gate_review_needed_files(
        packet=packet,
        verifier_report=verifier_report,
    )
    commit_message = _phase_commit_message(phase)
    blocking_reasons: List[str] = []
    approved_commands: List[str] = []
    candidate_commands_not_approved: List[str] = []

    if not approved:
        blocking_reasons.append("semantic_review_not_approved")
        candidate_files = review_needed_files
        if candidate_files and commit_message:
            candidate_commands_not_approved = render_targeted_commit_commands(
                candidate_files,
                commit_message,
            )
    else:
        if not reviewed_files:
            blocking_reasons.append("reviewed_files_empty")
        if not commit_message:
            blocking_reasons.append("commit_message_missing")
        if not blocking_reasons:
            approved_commands = render_targeted_commit_commands(reviewed_files, commit_message)

    status = "approved" if approved and not blocking_reasons else "not_approved"
    return {
        "schema_version": 1,
        "created_at_utc": _utc_now_iso(),
        "phase_id": phase.get("id"),
        "phase_name": phase.get("name"),
        "commit_mode": commit_mode,
        "status": status,
        "approved": status == "approved",
        "semantic_review_approved": approved,
        "reviewed_files": reviewed_files,
        "review_needed_files": review_needed_files,
        "commit_message": commit_message,
        "approved_commands": approved_commands,
        "candidate_commands_not_approved": candidate_commands_not_approved,
        "blocking_reasons": _dedupe_sorted(blocking_reasons),
        "semantic_review_decision": decision,
    }


def _phase_artifact_paths(output_dir: Path, phase: Dict[str, Any]) -> Dict[str, str]:
    index = int(phase.get("index") or 0)
    phase_id = str(phase.get("id") or f"phase_{index:03d}")
    stem = f"{index:03d}_{phase_id}"
    return {
        "executor_prompt_path": str(output_dir / "prompts" / f"{stem}.md"),
        "executor_result_path": str(output_dir / "executor-results" / f"{stem}.json"),
        "executor_relay_output_json_path": str(output_dir / "executor-results" / f"{stem}.json"),
        "executor_artifacts_dir": str(output_dir / "executor-artifacts" / stem),
        "verifier_report_path": str(output_dir / "verifier" / f"{stem}.json"),
        "pre_executor_verifier_report_path": str(output_dir / "verifier" / f"{stem}_pre_executor.json"),
        "post_executor_verifier_report_path": str(output_dir / "verifier" / f"{stem}_post_executor.json"),
        "post_validation_verifier_report_path": str(output_dir / "verifier" / f"{stem}_post_validation.json"),
        "validation_result_path": str(output_dir / "validation" / f"{stem}.json"),
        "semantic_review_packet_path": str(output_dir / "review" / f"{stem}.md"),
        "semantic_review_prompt_path": str(output_dir / "review" / f"{stem}_reviewer_prompt.md"),
        "semantic_review_output_json_path": str(output_dir / "review" / f"{stem}_reviewer_output.json"),
        "semantic_review_verdict_path": str(output_dir / "review" / f"{stem}_verdict.json"),
        "semantic_review_decision_path": str(output_dir / "review" / f"{stem}_decision.json"),
        "semantic_review_artifacts_dir": str(output_dir / "review-artifacts" / stem),
        "manual_approval_artifact_path": str(output_dir / "manual" / f"{stem}.json"),
        "commit_gate_path": str(output_dir / "commit-gate" / f"{stem}.json"),
        "commit_command_path": str(output_dir / "commit-commands" / f"{stem}.md"),
    }


def _phase_attempt_artifact_paths(
    output_dir: Path,
    phase: Dict[str, Any],
    attempt_index: int,
) -> Dict[str, str]:
    if attempt_index == 0:
        return _phase_artifact_paths(output_dir, phase)

    index = int(phase.get("index") or 0)
    phase_id = str(phase.get("id") or f"phase_{index:03d}")
    stem = f"{index:03d}_{phase_id}_followup_{attempt_index:02d}"
    return {
        "executor_prompt_path": str(output_dir / "prompts" / f"{stem}.md"),
        "executor_result_path": str(output_dir / "executor-results" / f"{stem}.json"),
        "executor_relay_output_json_path": str(output_dir / "executor-results" / f"{stem}.json"),
        "executor_artifacts_dir": str(output_dir / "executor-artifacts" / stem),
        "verifier_report_path": str(output_dir / "verifier" / f"{stem}.json"),
        "pre_executor_verifier_report_path": str(output_dir / "verifier" / f"{stem}_pre_executor.json"),
        "post_executor_verifier_report_path": str(output_dir / "verifier" / f"{stem}_post_executor.json"),
        "post_validation_verifier_report_path": str(output_dir / "verifier" / f"{stem}_post_validation.json"),
        "validation_result_path": str(output_dir / "validation" / f"{stem}.json"),
        "semantic_review_packet_path": str(output_dir / "review" / f"{stem}.md"),
        "semantic_review_prompt_path": str(output_dir / "review" / f"{stem}_reviewer_prompt.md"),
        "semantic_review_output_json_path": str(output_dir / "review" / f"{stem}_reviewer_output.json"),
        "semantic_review_verdict_path": str(output_dir / "review" / f"{stem}_verdict.json"),
        "semantic_review_decision_path": str(output_dir / "review" / f"{stem}_decision.json"),
        "semantic_review_artifacts_dir": str(output_dir / "review-artifacts" / stem),
        "manual_approval_artifact_path": str(output_dir / "manual" / f"{stem}.json"),
        "commit_gate_path": str(output_dir / "commit-gate" / f"{stem}.json"),
        "commit_command_path": str(output_dir / "commit-commands" / f"{stem}.md"),
    }


def create_phase_attempt_record(
    *,
    output_dir: Path,
    phase: Dict[str, Any],
    attempt_index: int,
    findings: Sequence[str],
) -> Dict[str, Any]:
    attempt_record = {
        "attempt_index": attempt_index,
        "attempt_kind": "followup" if attempt_index else "initial",
        "followup_number": attempt_index,
        "started_at_utc": _utc_now_iso(),
        "completed_at_utc": None,
        "status": "pending",
        "decision": None,
        "followup_findings": list(findings),
        "executor_relay_mode": None,
        "executor_relay_command": [],
        "executor_relay_exit_code": None,
        "executor_relay_stdout_excerpt": "",
        "executor_relay_stderr_excerpt": "",
        "executor_relay_output": None,
        "executor_relay_output_error": None,
        "last_executor_message_path": None,
        "last_executor_message_excerpt": "",
        "last_executor_message_truncated": False,
        "validations": [],
        "validation_summary": None,
        "manual_check_results": [],
        "skipped_live_checks": [],
        "skipped_manual_checks": [],
        "semantic_review_mode": None,
        "semantic_review_packet": None,
        "semantic_review_packet_text_sha256": None,
        "semantic_review_packet_text_chars": 0,
        "semantic_review_packet_excerpt": "",
        "semantic_review_packet_excerpt_truncated": False,
        "semantic_review_reviewer_command": [],
        "semantic_review_reviewer_exit_code": None,
        "semantic_review_reviewer_stdout_excerpt": "",
        "semantic_review_reviewer_stderr_excerpt": "",
        "semantic_review_reviewer_output": None,
        "semantic_review_reviewer_output_error": None,
        "semantic_review_parsed_verdict": None,
        "semantic_review_decision": None,
        "semantic_review_fingerprint_check": None,
        "semantic_review_post_review_verifier_report": None,
        "reviewed_files": [],
        "commit_gate": None,
        "commit_gate_status": None,
        "commit_gate_blocking_reasons": [],
        "approved_commit_commands": [],
        "candidate_commit_commands_not_approved": [],
        "approval_checkpoint": None,
        "auto_commit_result": None,
        "commit_hash": None,
    }
    attempt_record.update(
        _phase_attempt_artifact_paths(
            output_dir=output_dir,
            phase=phase,
            attempt_index=attempt_index,
        )
    )
    if attempt_index:
        attempt_record["followup_prompt_path"] = attempt_record["executor_prompt_path"]
    return attempt_record


def create_initial_ledger(
    *,
    plan: Dict[str, Any],
    plan_file: Path,
    repo_root: Path,
    output_dir: Path,
    git_state: Dict[str, Any],
    verifier_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    run_id = f"{plan['run_name']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    phases: List[Dict[str, Any]] = []
    for phase in plan["phases"]:
        phase_record = {
            "index": phase["index"],
            "id": phase["id"],
            "name": phase["name"],
            "status": "pending",
            "decision": None,
            "commit_hash": None,
            "expected_dirty_paths": [item["path"] for item in phase.get("expected_dirty_paths", [])],
            "validations": [],
            "validation_commands": phase.get("validation", []),
            "manual_checks": phase.get("manual_checks", []),
            "known_caveats": [],
            "skipped_live_checks": [],
            "skipped_manual_checks": [],
            "manual_check_results": [],
            "attempts": [],
            "followup_prompts": [],
            "followups_used": 0,
            "max_followups": plan.get("max_followups_per_phase", 0),
            "dirty_files_before": git_state.get("changed_paths", []),
            "dirty_files_after": [],
            "executor_prompt_text_sha256": None,
            "executor_prompt_text_chars": 0,
            "executor_prompt_excerpt": "",
            "executor_relay_mode": None,
            "executor_relay_command": [],
            "executor_relay_exit_code": None,
            "executor_relay_stdout_excerpt": "",
            "executor_relay_stderr_excerpt": "",
            "executor_relay_output": None,
            "last_executor_message_path": None,
            "last_executor_message_excerpt": "",
            "last_executor_message_truncated": False,
            "semantic_review_mode": None,
            "semantic_review_packet": None,
            "semantic_review_decision": None,
            "semantic_review_parsed_verdict": None,
            "semantic_review_reviewer_output": None,
            "semantic_review_fingerprint_check": None,
            "reviewed_files": [],
            "commit_gate": None,
            "commit_gate_status": None,
            "commit_gate_blocking_reasons": [],
            "approved_commit_commands": [],
            "candidate_commit_commands_not_approved": [],
            "approval_checkpoint": None,
            "auto_commit_result": None,
            "next_output_state": "kickoff",
        }
        phase_record.update(_phase_artifact_paths(output_dir, phase))
        phases.append(phase_record)

    ledger = {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": run_id,
        "run_name": plan["run_name"],
        "commit_mode": plan["commit_mode"],
        "allow_dirty_approved_continuation": plan.get(
            "allow_dirty_approved_continuation",
            DEFAULT_ALLOW_DIRTY_APPROVED_CONTINUATION,
        ),
        "plan_file": str(plan_file.resolve()),
        "repo_root": str(repo_root.resolve()),
        "output_dir": str(output_dir),
        "started_at_utc": _utc_now_iso(),
        "updated_at_utc": _utc_now_iso(),
        "branch_at_start": git_state.get("branch"),
        "baseline_head": git_state.get("head"),
        "last_known_head": git_state.get("head"),
        "current_phase_index": 0,
        "current_phase_name": phases[0]["name"] if phases else None,
        "previous_approved_commits": [],
        "approved_dirty_continuation": False,
        "approved_dirty_continuation_paths": [],
        "approval_checkpoint": None,
        "max_followups_per_phase": plan.get("max_followups_per_phase", 0),
        "followups_used": 0,
        "expected_dirty_files_for_current_phase": phases[0]["expected_dirty_paths"] if phases else [],
        "unrelated_dirty_files_to_avoid": (
            verifier_report.get("baseline_unrelated_dirty_paths", []) if verifier_report else []
        ),
        "dirty_files_before": git_state.get("changed_paths", []),
        "dirty_files_after": [],
        "validations_run": [],
        "validation_commands": phases[0]["validation_commands"] if phases else [],
        "smoke_checks": phases[0]["manual_checks"] if phases else [],
        "known_caveats": [],
        "skipped_live_checks": [],
        "semantic_review_mode": None,
        "semantic_review_packet_path": phases[0]["semantic_review_packet_path"] if phases else None,
        "semantic_review_decision": None,
        "semantic_review_parsed_verdict": None,
        "semantic_review_reviewer_output": None,
        "semantic_review_fingerprint_check": None,
        "manual_approval_artifact_path": phases[0]["manual_approval_artifact_path"] if phases else None,
        "next_output_state": "kickoff",
        "decision": "checkpointed",
        "commit_hash": None,
        "commit_gate": None,
        "phases": phases,
        "git_baseline": {
            "head": git_state.get("head"),
            "branch": git_state.get("branch"),
            "status_short_branch": git_state.get("status_short_branch"),
            "diff_name_status": git_state.get("diff_name_status"),
            "diff_cached_name_status": git_state.get("diff_cached_name_status"),
            "diff_stat": git_state.get("diff_stat"),
        },
        "latest_verifier_report": verifier_report,
    }
    return ledger


def persist_ledger(ledger: Dict[str, Any], state_file: Path) -> None:
    ledger["updated_at_utc"] = _utc_now_iso()
    ledger["state_file"] = str(state_file)
    _write_json_file(state_file, ledger)


def load_resume_state(state_file: Path) -> Dict[str, Any]:
    state = _load_json_file(state_file)
    if not isinstance(state, dict):
        raise ResumeStateError("resume state must be a JSON object")
    return state


def validate_resume_state(
    state: Dict[str, Any],
    *,
    repo_root: Path,
    current_git_state: Dict[str, Any],
) -> None:
    required_fields = [
        "schema_version",
        "run_id",
        "plan_file",
        "repo_root",
        "branch_at_start",
        "baseline_head",
        "last_known_head",
        "current_phase_index",
        "phases",
    ]
    for field in required_fields:
        if field not in state:
            raise ResumeStateError(f"resume state missing required field: {field}")
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ResumeStateError(f"resume state schema_version must be {STATE_SCHEMA_VERSION}")
    state_repo_root = Path(str(state["repo_root"])).resolve()
    if state_repo_root != repo_root.resolve():
        raise ResumeStateError("resume state repo_root does not match --repo-root")
    if not isinstance(state.get("phases"), list) or not state["phases"]:
        raise ResumeStateError("resume state phases must be a non-empty array")
    expected_branch = str(state.get("branch_at_start") or "")
    if current_git_state.get("branch") != expected_branch:
        raise ResumeStateError(
            f"branch drift detected: state={expected_branch} current={current_git_state.get('branch')}"
        )
    expected_head = str(state.get("last_known_head") or state.get("baseline_head") or "")
    if current_git_state.get("head") != expected_head and state.get("decision") != "approval_checkpoint":
        raise ResumeStateError(
            f"HEAD drift detected: state={expected_head} current={current_git_state.get('head')}"
        )


def _resolve_under_repo(repo_root: Path, path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return repo_root / path


def persist_prompt_text(*, record: Dict[str, Any], prompt_text: str) -> None:
    prompt_path = Path(record["executor_prompt_path"])
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")
    excerpt, truncated = _output_excerpt(prompt_text, limit=1200)
    record["executor_prompt_text_sha256"] = hashlib.sha256(
        prompt_text.encode("utf-8")
    ).hexdigest()
    record["executor_prompt_text_chars"] = len(prompt_text)
    record["executor_prompt_excerpt"] = excerpt
    record["executor_prompt_excerpt_truncated"] = truncated


def persist_executor_prompt(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    phase_record: Dict[str, Any],
    repo_root: Path,
    relay_mode: str,
    local_sandbox: str,
) -> str:
    prompt_text = render_executor_prompt(
        plan=plan,
        phase=phase,
        repo_root=repo_root,
        relay_mode=relay_mode,
        local_sandbox=local_sandbox,
    )
    persist_prompt_text(record=phase_record, prompt_text=prompt_text)
    return prompt_text


def packaged_relay_script_path() -> Path:
    """Return the relay packaged beside this runner, independent of --repo-root."""

    return SCRIPT_DIR / "relay_headless_codex.py"


def build_executor_relay_command(
    *,
    repo_root: Path,
    prompt_file: Path,
    output_file: Path,
    local_artifacts_dir: Path,
    args: argparse.Namespace,
) -> List[str]:
    relay_script = packaged_relay_script_path()
    command = [
        sys.executable,
        str(relay_script),
        "--repo-root",
        str(repo_root),
        "--prompt-file",
        str(prompt_file),
        "--output-file",
        str(output_file),
        "--model",
        args.executor_model,
        "--local-sandbox",
        args.executor_local_sandbox,
        "--local-reasoning-effort",
        args.executor_local_reasoning_effort,
        "--local-service-tier",
        args.executor_local_service_tier,
        "--codex-bin",
        args.executor_codex_bin,
        "--local-timeout-seconds",
        str(args.executor_local_timeout_seconds),
        "--local-artifacts-dir",
        str(local_artifacts_dir),
        "--json",
    ]
    if not args.executor_no_local_skip_git_repo_check:
        command.append("--local-skip-git-repo-check")
    return command


def run_executor_relay_command(
    command: Sequence[str],
    *,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    relay_script = packaged_relay_script_path()
    if not relay_script.is_file():
        raise PhaseGateError(f"packaged relay script is missing: {relay_script}")
    return subprocess.run(
        list(command),
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def _load_json_file_if_present(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    loaded = _load_json_file(path)
    if not isinstance(loaded, dict):
        raise PhaseGateError(f"JSON file must contain an object: {path}")
    return loaded


def _last_message_metadata(relay_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    last_message_path = None
    if isinstance(relay_payload, dict):
        value = relay_payload.get("last_message_path")
        if isinstance(value, str) and value.strip():
            last_message_path = value.strip()
    excerpt = ""
    truncated = False
    if last_message_path:
        try:
            excerpt, truncated = _output_excerpt(Path(last_message_path).read_text(encoding="utf-8"))
        except OSError:
            result_summary = (
                relay_payload.get("result_summary")
                if isinstance(relay_payload, dict)
                else None
            )
            stdout = result_summary.get("stdout") if isinstance(result_summary, dict) else ""
            excerpt, truncated = _output_excerpt(stdout)
    elif isinstance(relay_payload, dict):
        result_summary = relay_payload.get("result_summary")
        stdout = result_summary.get("stdout") if isinstance(result_summary, dict) else ""
        excerpt, truncated = _output_excerpt(stdout)
    return {
        "last_executor_message_path": last_message_path,
        "last_executor_message_excerpt": excerpt,
        "last_executor_message_truncated": truncated,
    }


def run_executor_relay(
    *,
    repo_root: Path,
    phase_record: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    prompt_file = Path(phase_record["executor_prompt_path"])
    output_file = Path(phase_record["executor_relay_output_json_path"])
    output_file.parent.mkdir(parents=True, exist_ok=True)
    local_artifacts_dir = Path(phase_record["executor_artifacts_dir"])
    local_artifacts_dir.mkdir(parents=True, exist_ok=True)

    command = build_executor_relay_command(
        repo_root=repo_root,
        prompt_file=prompt_file,
        output_file=output_file,
        local_artifacts_dir=local_artifacts_dir,
        args=args,
    )
    phase_record["executor_relay_mode"] = args.executor_relay_mode
    phase_record["executor_relay_command"] = command
    result = run_executor_relay_command(command, repo_root=repo_root)
    phase_record["executor_relay_exit_code"] = result.returncode
    phase_record["executor_relay_stdout_excerpt"] = _output_excerpt(result.stdout)[0]
    phase_record["executor_relay_stderr_excerpt"] = _output_excerpt(result.stderr)[0]

    try:
        relay_payload = _load_json_file_if_present(output_file)
    except PhaseGateError as exc:
        phase_record["executor_relay_output_error"] = str(exc)
        relay_payload = None
    phase_record["executor_relay_output"] = relay_payload
    phase_record.update(_last_message_metadata(relay_payload))

    result_summary = relay_payload.get("result_summary") if isinstance(relay_payload, dict) else None
    result_ok = (
        result.returncode == 0
        and isinstance(result_summary, dict)
        and result_summary.get("ok") is True
    )
    relay_status = relay_payload.get("status") if isinstance(relay_payload, dict) else None
    return {
        "ok": result_ok,
        "exit_code": result.returncode,
        "status": relay_status,
        "payload": relay_payload,
        "output_file": str(output_file),
        "timed_out": result.returncode == RELAY_EXIT_TIMEOUT or relay_status == "timeout",
    }


LATEST_ATTEMPT_FIELDS = (
    "executor_prompt_path",
    "executor_result_path",
    "executor_relay_output_json_path",
    "executor_artifacts_dir",
    "verifier_report_path",
    "pre_executor_verifier_report_path",
    "post_executor_verifier_report_path",
    "post_validation_verifier_report_path",
    "validation_result_path",
    "semantic_review_packet_path",
    "semantic_review_prompt_path",
    "semantic_review_output_json_path",
    "semantic_review_verdict_path",
    "semantic_review_decision_path",
    "semantic_review_artifacts_dir",
    "manual_approval_artifact_path",
    "commit_gate_path",
    "commit_command_path",
    "executor_prompt_text_sha256",
    "executor_prompt_text_chars",
    "executor_prompt_excerpt",
    "executor_prompt_excerpt_truncated",
    "executor_relay_mode",
    "executor_relay_command",
    "executor_relay_exit_code",
    "executor_relay_stdout_excerpt",
    "executor_relay_stderr_excerpt",
    "executor_relay_output",
    "executor_relay_output_error",
    "last_executor_message_path",
    "last_executor_message_excerpt",
    "last_executor_message_truncated",
    "pre_executor_verifier_report",
    "post_executor_verifier_report",
    "post_validation_verifier_report",
    "validations",
    "validation_summary",
    "manual_check_results",
    "skipped_live_checks",
    "skipped_manual_checks",
    "semantic_review_mode",
    "semantic_review_packet",
    "semantic_review_packet_text_sha256",
    "semantic_review_packet_text_chars",
    "semantic_review_packet_excerpt",
    "semantic_review_packet_excerpt_truncated",
    "semantic_review_reviewer_command",
    "semantic_review_reviewer_exit_code",
    "semantic_review_reviewer_stdout_excerpt",
    "semantic_review_reviewer_stderr_excerpt",
    "semantic_review_reviewer_output",
    "semantic_review_reviewer_output_error",
    "semantic_review_parsed_verdict",
    "semantic_review_decision",
    "semantic_review_fingerprint_check",
    "semantic_review_post_review_verifier_report",
    "reviewed_files",
    "commit_gate",
    "commit_gate_status",
    "commit_gate_blocking_reasons",
    "approved_commit_commands",
    "candidate_commit_commands_not_approved",
    "approval_checkpoint",
    "auto_commit_result",
    "commit_hash",
    "dirty_files_after_executor",
    "dirty_files_after",
)


def _copy_latest_attempt_to_phase_record(
    phase_record: Dict[str, Any],
    attempt_record: Dict[str, Any],
) -> None:
    for key in LATEST_ATTEMPT_FIELDS:
        if key in attempt_record:
            phase_record[key] = attempt_record[key]
    phase_record["current_attempt_index"] = attempt_record.get("attempt_index", 0)
    phase_record["followups_used"] = attempt_record.get("followup_number", 0)
    if attempt_record.get("attempt_kind") == "followup":
        phase_record["latest_followup_prompt_path"] = attempt_record.get("executor_prompt_path")


def _sync_ledger_from_phase(
    ledger: Dict[str, Any],
    phase_record: Dict[str, Any],
) -> None:
    for key in (
        "executor_prompt_path",
        "executor_prompt_text_sha256",
        "executor_prompt_text_chars",
        "executor_relay_output_json_path",
        "executor_relay_exit_code",
        "last_executor_message_path",
        "last_executor_message_excerpt",
        "validation_result_path",
        "semantic_review_packet_path",
        "semantic_review_prompt_path",
        "semantic_review_output_json_path",
        "semantic_review_verdict_path",
        "semantic_review_decision_path",
        "manual_approval_artifact_path",
        "commit_gate_path",
        "commit_command_path",
    ):
        if key in phase_record:
            ledger[key] = phase_record[key]
    ledger["followups_used"] = phase_record.get("followups_used", 0)
    for key in (
        "semantic_review_mode",
        "semantic_review_packet",
        "semantic_review_reviewer_output",
        "semantic_review_parsed_verdict",
        "semantic_review_decision",
        "semantic_review_fingerprint_check",
        "reviewed_files",
        "commit_gate",
        "commit_gate_status",
        "commit_gate_blocking_reasons",
        "approved_commit_commands",
        "candidate_commit_commands_not_approved",
        "approval_checkpoint",
        "auto_commit_result",
        "commit_hash",
    ):
        if key in phase_record:
            ledger[key] = phase_record[key]


def _write_verifier_report(
    *,
    report: Dict[str, Any],
    step_path: str,
    latest_path: str,
) -> None:
    _write_json_file(Path(step_path), report)
    if step_path != latest_path:
        _write_json_file(Path(latest_path), report)


def _write_validation_result(path: str, summary: Dict[str, Any], results: Sequence[Dict[str, Any]]) -> None:
    _write_json_file(
        Path(path),
        {
            "summary": summary,
            "results": list(results),
        },
    )


def _verifier_has_hard_block(report: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(report, dict):
        return False
    reasons = {str(reason) for reason in report.get("blocked_reasons", [])}
    return bool(reasons & HARD_BLOCKED_REASONS)


def _can_retry_followup(
    *,
    attempt_index: int,
    max_followups: int,
    relay_mode: str,
) -> bool:
    return relay_mode != "disabled" and attempt_index < max_followups


def _mark_attempt_for_followup(
    *,
    attempt_record: Dict[str, Any],
    phase_record: Dict[str, Any],
    ledger: Dict[str, Any],
    findings: Sequence[str],
) -> None:
    attempt_record["status"] = "needs_followup"
    attempt_record["decision"] = "followup"
    attempt_record["completed_at_utc"] = _utc_now_iso()
    attempt_record["next_followup_findings"] = list(findings)
    phase_record["status"] = "needs_followup"
    phase_record["decision"] = "followup"
    ledger["next_output_state"] = "followup"
    ledger["decision"] = "followup"


def _finalize_phase(
    *,
    attempt_record: Dict[str, Any],
    phase_record: Dict[str, Any],
    ledger: Dict[str, Any],
    status: str,
    next_output_state: str,
    decision: str,
    exit_code: int,
    followups_exhausted: bool = False,
) -> int:
    attempt_record["status"] = status
    attempt_record["decision"] = decision
    attempt_record["completed_at_utc"] = _utc_now_iso()
    attempt_record["followups_exhausted"] = followups_exhausted
    phase_record["status"] = status
    phase_record["decision"] = decision
    phase_record["followups_exhausted"] = followups_exhausted
    ledger["next_output_state"] = next_output_state
    ledger["decision"] = decision
    ledger["followups_exhausted"] = followups_exhausted
    return exit_code


def _commit_gate_branch_head_drift_reasons(
    *,
    packet: Optional[Dict[str, Any]],
    current_git_state: Dict[str, Any],
) -> List[str]:
    if not isinstance(packet, dict):
        return []
    packet_current = packet.get("current")
    if not isinstance(packet_current, dict):
        return []
    reasons: List[str] = []
    packet_branch = packet_current.get("branch")
    packet_head = packet_current.get("head")
    if packet_branch and current_git_state.get("branch") != packet_branch:
        reasons.append("branch_drift_after_review")
    if packet_head and current_git_state.get("head") != packet_head:
        reasons.append("head_drift_after_review")
    return reasons


def _auto_commit_rejection_reasons(
    *,
    record: Dict[str, Any],
    packet: Optional[Dict[str, Any]],
    current_git_state: Dict[str, Any],
    verifier_report: Dict[str, Any],
) -> List[str]:
    reasons = list(record.get("blocking_reasons", []))
    if not record.get("semantic_review_approved"):
        reasons.append("semantic_review_not_approved")

    reviewed_files = _dedupe_sorted(record.get("reviewed_files", []))
    reviewed_set = set(reviewed_files)
    if not reviewed_files:
        reasons.append("reviewed_files_empty")
    if not str(record.get("commit_message") or "").strip():
        reasons.append("commit_message_missing")

    staged_outside_reviewed = [
        path
        for path in _dedupe_sorted(current_git_state.get("staged_paths", []))
        if path not in reviewed_set
    ]
    if staged_outside_reviewed:
        reasons.append("staged_files_outside_reviewed_set")
        record["staged_files_outside_reviewed_set"] = staged_outside_reviewed

    unrelated_or_forbidden = _dedupe_sorted(
        list(verifier_report.get("unrelated_dirty_paths", []))
        + list(verifier_report.get("new_unrelated_dirty_paths", []))
        + list(verifier_report.get("forbidden_changed_paths", []))
    )
    unrelated_overlap = [path for path in unrelated_or_forbidden if path in reviewed_set]
    if unrelated_overlap:
        reasons.append("reviewed_files_overlap_unrelated_dirty")
        record["reviewed_files_overlap_unrelated_dirty"] = unrelated_overlap
    if verifier_report.get("branch_drift"):
        reasons.append("branch_drift")
    if verifier_report.get("head_drift"):
        reasons.append("head_drift")
    reasons.extend(
        _commit_gate_branch_head_drift_reasons(
            packet=packet,
            current_git_state=current_git_state,
        )
    )
    return _dedupe_sorted(reasons)


def _run_auto_commit_after_gate(
    *,
    repo_root: Path,
    record: Dict[str, Any],
    packet: Optional[Dict[str, Any]],
    current_git_state: Dict[str, Any],
    verifier_report: Dict[str, Any],
) -> Dict[str, Any]:
    rejection_reasons = _auto_commit_rejection_reasons(
        record=record,
        packet=packet,
        current_git_state=current_git_state,
        verifier_report=verifier_report,
    )
    if rejection_reasons:
        updated = dict(record)
        updated["status"] = "auto_commit_rejected"
        updated["approved"] = False
        updated["blocking_reasons"] = rejection_reasons
        updated["auto_commit_result"] = {
            "committed": False,
            "commit_hash": None,
            "rejection_reasons": rejection_reasons,
        }
        return updated

    reviewed_files = _dedupe_sorted(record.get("reviewed_files", []))
    commit_message = str(record.get("commit_message") or "").strip()
    _run_git(repo_root, ["add", "--", *reviewed_files])
    commit_stdout = _run_git(repo_root, ["commit", "-m", commit_message])
    commit_hash = _run_git(repo_root, ["rev-parse", "HEAD"]).strip()
    updated = dict(record)
    updated["status"] = "auto_committed"
    updated["approved"] = True
    updated["commit_hash"] = commit_hash
    updated["auto_commit_result"] = {
        "committed": True,
        "commit_hash": commit_hash,
        "stdout": commit_stdout,
        "reviewed_files": reviewed_files,
    }
    return updated


def _build_approval_checkpoint(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    record: Dict[str, Any],
    packet: Optional[Dict[str, Any]],
    current_git_state: Dict[str, Any],
) -> Dict[str, Any]:
    packet_fingerprint = (
        packet.get("review_input_fingerprint") if isinstance(packet, dict) else None
    )
    return {
        "schema_version": 1,
        "created_at_utc": _utc_now_iso(),
        "phase_index": int(phase.get("index") or 1) - 1,
        "phase_id": phase.get("id"),
        "next_phase_index": int(phase.get("index") or 1),
        "commit_mode": plan.get("commit_mode"),
        "pre_commit_head": current_git_state.get("head"),
        "pre_commit_branch": current_git_state.get("branch"),
        "expected_commit_hash": None,
        "expected_commit_message": record.get("commit_message"),
        "reviewed_files": record.get("reviewed_files", []),
        "approved_commands": record.get("approved_commands", []),
        "dirty_fingerprint": packet_fingerprint,
        "semantic_review_decision": record.get("semantic_review_decision"),
    }


def apply_commit_gate_after_review(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    attempt_record: Dict[str, Any],
    phase_record: Dict[str, Any],
    ledger: Dict[str, Any],
    repo_root: Path,
    decision: Optional[Dict[str, Any]],
    packet: Optional[Dict[str, Any]],
    verifier_report: Dict[str, Any],
    current_git_state: Dict[str, Any],
    args: argparse.Namespace,
) -> Optional[int]:
    record = _build_commit_gate_record(
        plan=plan,
        phase=phase,
        repo_root=repo_root,
        decision=decision,
        packet=packet,
        verifier_report=verifier_report,
    )
    commit_mode = str(plan.get("commit_mode") or COMMIT_MODE_NO_COMMIT)
    allow_dirty_continuation = _allow_dirty_approved_continuation(plan=plan, args=args)

    if commit_mode == COMMIT_MODE_AUTO_AFTER_GATE:
        record = _run_auto_commit_after_gate(
            repo_root=repo_root,
            record=record,
            packet=packet,
            current_git_state=current_git_state,
            verifier_report=verifier_report,
        )
        _persist_commit_gate_record(
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            record=record,
        )
        if not record.get("semantic_review_approved"):
            return None
        commit_hash = record.get("commit_hash")
        if record.get("status") == "auto_committed" and commit_hash:
            ledger["last_known_head"] = commit_hash
            ledger["commit_hash"] = commit_hash
            phase_record["commit_hash"] = commit_hash
            ledger.setdefault("previous_approved_commits", []).append(
                {
                    "phase_id": phase.get("id"),
                    "commit_hash": commit_hash,
                    "reviewed_files": record.get("reviewed_files", []),
                }
            )
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="auto_committed",
                next_output_state="committed",
                decision="auto_committed",
                exit_code=EXIT_OK,
            )
        return _finalize_phase(
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            status="auto_commit_blocked",
            next_output_state="blocked",
            decision="auto_commit_blocked",
            exit_code=EXIT_BLOCKED,
        )

    if (
        commit_mode in {COMMIT_MODE_NO_COMMIT, COMMIT_MODE_PREPARE_COMMAND}
        and record.get("approved")
        and _phase_has_later_phases(plan, phase)
        and not allow_dirty_continuation
    ):
        checkpoint = _build_approval_checkpoint(
            plan=plan,
            phase=phase,
            record=record,
            packet=packet,
            current_git_state=current_git_state,
        )
        record["status"] = "approval_checkpoint"
        record["approval_checkpoint"] = checkpoint
        _persist_commit_gate_record(
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            record=record,
        )
        ledger["approval_checkpoint"] = checkpoint
        phase_record["approval_checkpoint"] = checkpoint
        return _finalize_phase(
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            status="approval_checkpoint",
            next_output_state="approval_checkpoint",
            decision="approval_checkpoint",
            exit_code=EXIT_CHECKPOINT,
        )

    if (
        commit_mode in {COMMIT_MODE_NO_COMMIT, COMMIT_MODE_PREPARE_COMMAND}
        and record.get("approved")
        and _phase_has_later_phases(plan, phase)
        and allow_dirty_continuation
    ):
        approved_paths = _dedupe_sorted(
            list(ledger.get("approved_dirty_continuation_paths", []))
            + list(record.get("reviewed_files", []))
        )
        ledger["approved_dirty_continuation"] = True
        ledger["approved_dirty_continuation_paths"] = approved_paths

    _persist_commit_gate_record(
        attempt_record=attempt_record,
        phase_record=phase_record,
        ledger=ledger,
        record=record,
    )
    return None


def _truncate_review_text(value: Any, *, limit: int) -> Tuple[str, bool]:
    text = _coerce_output_text(value)
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 18)].rstrip() + "\n... [truncated]\n", True


def _run_git_for_review_paths(
    repo_root: Path,
    args: Sequence[str],
    paths: Sequence[str],
    *,
    allow_failure: bool = True,
) -> str:
    normalized_paths = _dedupe_sorted(paths)
    if not normalized_paths:
        return ""
    return _run_git(repo_root, [*args, "--", *normalized_paths], allow_failure=allow_failure)


def _targeted_file_excerpt(repo_root: Path, path: str) -> Dict[str, Any]:
    normalized = normalize_repo_relative_path(path, repo_root=repo_root)
    target = repo_root / normalized
    record: Dict[str, Any] = {
        "path": normalized,
        "exists": target.exists(),
        "is_file": target.is_file(),
        "excerpt": "",
        "truncated": False,
        "error": "",
    }
    if not target.exists():
        record["error"] = "path does not exist in the worktree"
        return record
    if not target.is_file():
        record["error"] = "path is not a regular file"
        return record
    try:
        excerpt, truncated = _truncate_review_text(
            target.read_text(encoding="utf-8", errors="replace"),
            limit=DEFAULT_REVIEW_FILE_EXCERPT_CHARS,
        )
    except OSError as exc:
        record["error"] = str(exc)
        return record
    record["excerpt"] = excerpt
    record["truncated"] = truncated
    return record


def _review_required_changed_files(verifier_report: Dict[str, Any]) -> List[str]:
    return _dedupe_sorted(
        list(verifier_report.get("allowed_changed_paths", []))
        + list(verifier_report.get("expected_dirty_paths", []))
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


def _stable_json_hash(payload: Any) -> str:
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def _file_content_fingerprint(repo_root: Path, path: str) -> Dict[str, Any]:
    normalized = normalize_repo_relative_path(path, repo_root=repo_root)
    target = repo_root / normalized
    record: Dict[str, Any] = {
        "path": normalized,
        "exists": target.exists(),
        "is_file": target.is_file(),
        "size": None,
        "sha256": None,
        "error": "",
    }
    if not target.exists() or not target.is_file():
        return record
    try:
        content = target.read_bytes()
    except OSError as exc:
        record["error"] = str(exc)
        return record
    record["size"] = len(content)
    record["sha256"] = hashlib.sha256(content).hexdigest()
    return record


def compute_semantic_review_input_fingerprint(
    *,
    repo_root: Path,
    review_required_files: Sequence[str],
    current_git_state: Dict[str, Any],
    validation_summary: Dict[str, Any],
) -> Dict[str, Any]:
    normalized_files = _dedupe_sorted(review_required_files)
    unstaged_diff = _run_git_for_review_paths(
        repo_root,
        ["diff", "--find-renames", "--find-copies"],
        normalized_files,
    )
    cached_diff = _run_git_for_review_paths(
        repo_root,
        ["diff", "--cached", "--find-renames", "--find-copies"],
        normalized_files,
    )
    payload = {
        "schema_version": 1,
        "review_required_files": normalized_files,
        "file_contents": [
            _file_content_fingerprint(repo_root, path) for path in normalized_files
        ],
        "worktree_classification": {
            "changed_paths": _dedupe_sorted(current_git_state.get("changed_paths", [])),
            "staged_paths": _dedupe_sorted(current_git_state.get("staged_paths", [])),
            "unstaged_paths": _dedupe_sorted(current_git_state.get("unstaged_paths", [])),
            "untracked_paths": _dedupe_sorted(current_git_state.get("untracked_paths", [])),
            "deleted_paths": _dedupe_sorted(current_git_state.get("deleted_paths", [])),
            "renamed_paths": current_git_state.get("renamed_paths", []),
            "copied_paths": current_git_state.get("copied_paths", []),
        },
        "diff_hashes": {
            "unstaged_sha256": _sha256_text(unstaged_diff),
            "cached_sha256": _sha256_text(cached_diff),
        },
        "validation_summary_sha256": _stable_json_hash(validation_summary),
    }
    return {
        "schema_version": 1,
        "algorithm": "phasegate-semantic-review-input-v1",
        "sha256": _stable_json_hash(payload),
        "payload": payload,
    }


def check_semantic_review_input_fingerprint(
    *,
    repo_root: Path,
    packet: Dict[str, Any],
    current_git_state: Dict[str, Any],
) -> Dict[str, Any]:
    expected = packet.get("review_input_fingerprint")
    if not isinstance(expected, dict):
        return {
            "stale": True,
            "blocking_reason": "semantic_review_packet_stale",
            "detail": "review packet is missing review_input_fingerprint",
            "expected_sha256": None,
            "current_sha256": None,
        }
    current = compute_semantic_review_input_fingerprint(
        repo_root=repo_root,
        review_required_files=packet.get("review_required_files", []),
        current_git_state=current_git_state,
        validation_summary=packet.get("validation_summary", {}),
    )
    expected_sha = expected.get("sha256")
    current_sha = current.get("sha256")
    stale = expected_sha != current_sha
    return {
        "stale": stale,
        "blocking_reason": "semantic_review_packet_stale" if stale else "",
        "detail": (
            "review inputs changed after packet creation"
            if stale
            else "review inputs match packet fingerprint"
        ),
        "expected_sha256": expected_sha,
        "current_sha256": current_sha,
        "current_fingerprint": current,
    }


def build_semantic_review_packet(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    ledger: Dict[str, Any],
    repo_root: Path,
    post_validation_git: Dict[str, Any],
    verifier_report: Dict[str, Any],
    validation_summary: Dict[str, Any],
    validation_results: Sequence[Dict[str, Any]],
    manual_check_results: Sequence[Dict[str, Any]],
    skipped_live_checks: Sequence[Dict[str, Any]],
    skipped_manual_checks: Sequence[Dict[str, Any]],
    blocking_manual_checks: Sequence[str],
) -> Dict[str, Any]:
    changed_files = _dedupe_sorted(verifier_report.get("changed_paths", []))
    review_required_files = _review_required_changed_files(verifier_report)
    unstaged_diff, unstaged_truncated = _truncate_review_text(
        _run_git_for_review_paths(
            repo_root,
            ["diff", "--find-renames", "--find-copies"],
            review_required_files,
        ),
        limit=DEFAULT_REVIEW_DIFF_CHARS,
    )
    cached_diff, cached_truncated = _truncate_review_text(
        _run_git_for_review_paths(
            repo_root,
            ["diff", "--cached", "--find-renames", "--find-copies"],
            review_required_files,
        ),
        limit=DEFAULT_REVIEW_DIFF_CHARS,
    )
    diff_cached_stat = _run_git(repo_root, ["diff", "--cached", "--stat"], allow_failure=True)
    file_excerpts = [_targeted_file_excerpt(repo_root, path) for path in review_required_files]
    review_input_fingerprint = compute_semantic_review_input_fingerprint(
        repo_root=repo_root,
        review_required_files=review_required_files,
        current_git_state=post_validation_git,
        validation_summary=validation_summary,
    )
    unrelated_to_exclude = _dedupe_sorted(
        list(verifier_report.get("unrelated_dirty_paths", []))
        + list(verifier_report.get("baseline_unrelated_dirty_paths", []))
    )

    return {
        "schema_version": 1,
        "created_at_utc": _utc_now_iso(),
        "run_id": ledger.get("run_id"),
        "run_name": plan.get("run_name"),
        "phase_id": phase.get("id"),
        "phase_name": phase.get("name"),
        "objective": phase.get("objective"),
        "baseline": {
            "head": ledger.get("baseline_head"),
            "branch": ledger.get("branch_at_start"),
        },
        "current": {
            "head": post_validation_git.get("head"),
            "branch": post_validation_git.get("branch"),
            "status_short_branch": post_validation_git.get("status_short_branch"),
        },
        "changed_files": changed_files,
        "review_required_files": review_required_files,
        "review_input_fingerprint": review_input_fingerprint,
        "classification": {
            "staged_paths": _dedupe_sorted(verifier_report.get("staged_paths", [])),
            "unstaged_paths": _dedupe_sorted(verifier_report.get("unstaged_paths", [])),
            "untracked_paths": _dedupe_sorted(verifier_report.get("untracked_paths", [])),
            "deleted_paths": _dedupe_sorted(verifier_report.get("deleted_paths", [])),
            "renamed_paths": verifier_report.get("renamed_paths", []),
            "copied_paths": verifier_report.get("copied_paths", []),
            "forbidden_changed_paths": _dedupe_sorted(
                verifier_report.get("forbidden_changed_paths", [])
            ),
            "unrelated_dirty_paths": _dedupe_sorted(
                verifier_report.get("unrelated_dirty_paths", [])
            ),
            "new_unrelated_dirty_paths": _dedupe_sorted(
                verifier_report.get("new_unrelated_dirty_paths", [])
            ),
        },
        "diff_stat": post_validation_git.get("diff_stat", ""),
        "diff_cached_stat": diff_cached_stat,
        "diffs": {
            "unstaged": unstaged_diff,
            "unstaged_truncated": unstaged_truncated,
            "cached": cached_diff,
            "cached_truncated": cached_truncated,
        },
        "file_excerpts": file_excerpts,
        "validation_summary": validation_summary,
        "validation_results": list(validation_results),
        "manual_live_checks": {
            "manual_check_results": list(manual_check_results),
            "skipped_live_checks": list(skipped_live_checks),
            "skipped_manual_checks": list(skipped_manual_checks),
            "blocking_manual_checks": list(blocking_manual_checks),
        },
        "unrelated_dirty_files_to_exclude": unrelated_to_exclude,
        "verifier_report": verifier_report,
    }


def _json_block(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def render_semantic_review_packet(packet: Dict[str, Any]) -> str:
    changed_files = packet.get("changed_files", [])
    review_required_files = packet.get("review_required_files", [])
    file_sections: List[str] = []
    for item in packet.get("file_excerpts", []):
        path = str(item.get("path") or "")
        status = "present" if item.get("exists") else "missing"
        if item.get("error"):
            status += f"; {item.get('error')}"
        file_sections.extend(
            [
                f"### {path}",
                f"Status: {status}",
                "",
                "```text",
                str(item.get("excerpt") or ""),
                "```",
                "",
            ]
        )

    return "\n".join(
        [
            "# PhaseGate Semantic Review Packet",
            "",
            "## Baseline",
            f"- Branch: {packet.get('baseline', {}).get('branch')}",
            f"- HEAD: {packet.get('baseline', {}).get('head')}",
            "",
            "## Current Worktree",
            f"- Branch: {packet.get('current', {}).get('branch')}",
            f"- HEAD: {packet.get('current', {}).get('head')}",
            "",
            "```text",
            str(packet.get("current", {}).get("status_short_branch") or ""),
            "```",
            "",
            "## Changed Files",
            _format_prompt_list([str(path) for path in changed_files]),
            "",
            "## Files Requiring Semantic Review",
            _format_prompt_list([str(path) for path in review_required_files]),
            "",
            "## Review Input Fingerprint",
            "```json",
            _json_block(packet.get("review_input_fingerprint", {})),
            "```",
            "",
            "## Classification",
            "```json",
            _json_block(packet.get("classification", {})),
            "```",
            "",
            "## Diff Stat",
            "```text",
            str(packet.get("diff_stat") or ""),
            "```",
            "",
            "## Cached Diff Stat",
            "```text",
            str(packet.get("diff_cached_stat") or ""),
            "```",
            "",
            "## Diffs For Review Files",
            "### Unstaged Diff",
            "```diff",
            str(packet.get("diffs", {}).get("unstaged") or ""),
            "```",
            "",
            "### Cached Diff",
            "```diff",
            str(packet.get("diffs", {}).get("cached") or ""),
            "```",
            "",
            "## Targeted File Reads",
            *(file_sections or ["- None.", ""]),
            "## Validation Results",
            "```json",
            _json_block(
                {
                    "summary": packet.get("validation_summary", {}),
                    "results": packet.get("validation_results", []),
                }
            ),
            "```",
            "",
            "## Manual And Live Checks",
            "```json",
            _json_block(packet.get("manual_live_checks", {})),
            "```",
            "",
            "## Unrelated Dirty Files To Exclude",
            _format_prompt_list(
                [str(path) for path in packet.get("unrelated_dirty_files_to_exclude", [])]
            ),
            "",
            "## Verifier Report",
            "```json",
            _json_block(packet.get("verifier_report", {})),
            "```",
            "",
        ]
    )


def persist_semantic_review_packet(
    *,
    attempt_record: Dict[str, Any],
    packet: Dict[str, Any],
) -> None:
    packet_text = render_semantic_review_packet(packet)
    packet_path = Path(attempt_record["semantic_review_packet_path"])
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text(packet_text, encoding="utf-8")
    excerpt, truncated = _output_excerpt(packet_text, limit=1600)
    attempt_record["semantic_review_packet"] = packet
    attempt_record["semantic_review_packet_text_sha256"] = hashlib.sha256(
        packet_text.encode("utf-8")
    ).hexdigest()
    attempt_record["semantic_review_packet_text_chars"] = len(packet_text)
    attempt_record["semantic_review_packet_excerpt"] = excerpt
    attempt_record["semantic_review_packet_excerpt_truncated"] = truncated


def render_model_review_prompt(*, packet_path: str, packet_text: str) -> str:
    return "\n".join(
        [
            "You are the read-only semantic reviewer for a PhaseGate run.",
            "",
            "Hard rules:",
            "- Do not edit files.",
            "- Do not stage files.",
            "- Do not commit.",
            "- Do not run cleanup commands.",
            "- Do not alter generated artifacts or the worktree.",
            "- Use read-only inspection only.",
            "",
            f"Review packet path: {packet_path}",
            "",
            "Review task:",
            "- Check whether the changed files are fully reviewed and safe for a later approval gate.",
            "- Treat unresolved findings as approval blockers.",
            "- Return only one JSON object, with no prose before or after it.",
            "",
            "Required JSON fields:",
            "```json",
            _json_block(
                {
                    "approved": False,
                    "findings": [],
                    "reviewed_files": [],
                    "validation_summary": {},
                    "residual_risks": [],
                    "commit_eligibility": False,
                }
            ),
            "```",
            "",
            "Review packet:",
            "",
            packet_text,
            "",
        ]
    )


def build_reviewer_relay_command(
    *,
    repo_root: Path,
    prompt_file: Path,
    output_file: Path,
    local_artifacts_dir: Path,
    args: argparse.Namespace,
) -> List[str]:
    command = [
        sys.executable,
        str(packaged_relay_script_path()),
        "--repo-root",
        str(repo_root),
        "--prompt-file",
        str(prompt_file),
        "--output-file",
        str(output_file),
        "--model",
        args.reviewer_model,
        "--local-sandbox",
        "read-only",
        "--local-reasoning-effort",
        args.reviewer_local_reasoning_effort,
        "--local-service-tier",
        args.reviewer_local_service_tier,
        "--codex-bin",
        args.reviewer_codex_bin,
        "--local-timeout-seconds",
        str(args.reviewer_local_timeout_seconds),
        "--local-artifacts-dir",
        str(local_artifacts_dir),
        "--json",
        "--local-skip-git-repo-check",
    ]
    return command


def _relay_payload_output_text(payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return ""
    last_message_path = payload.get("last_message_path")
    if isinstance(last_message_path, str) and last_message_path.strip():
        try:
            return Path(last_message_path).read_text(encoding="utf-8")
        except OSError:
            pass
    result_summary = payload.get("result_summary")
    if isinstance(result_summary, dict):
        stdout = result_summary.get("stdout")
        if isinstance(stdout, str):
            return stdout
    return ""


def _extract_json_object_from_text(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    stripped = text.strip()
    if not stripped:
        return None, "reviewer output was empty"

    candidates = [stripped]
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL):
        candidates.append(match.group(1).strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    errors: List[str] = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(exc.msg)
            continue
        if isinstance(parsed, dict):
            return parsed, None
        errors.append("parsed JSON was not an object")
    return None, "could not parse reviewer JSON object: " + "; ".join(errors[:3])


def parse_review_verdict_from_relay_payload(
    payload: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if isinstance(payload, dict):
        result_summary = payload.get("result_summary")
        if isinstance(result_summary, dict) and isinstance(result_summary.get("parsed_json"), dict):
            return result_summary["parsed_json"], None
    return _extract_json_object_from_text(_relay_payload_output_text(payload))


def parse_review_verdict_from_artifact(
    artifact_path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        loaded = _load_json_file(artifact_path)
    except PhaseGateError as exc:
        return None, str(exc)
    if not isinstance(loaded, dict):
        return None, "manual approval artifact must contain a JSON object"
    return loaded, None


def _normalize_verdict_fields(
    verdict: Optional[Dict[str, Any]],
    *,
    repo_root: Path,
) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    if not isinstance(verdict, dict):
        return {
            "approved": False,
            "findings": [],
            "reviewed_files": [],
            "validation_summary": None,
            "residual_risks": [],
            "commit_eligibility": False,
        }, ["reviewer verdict is missing or is not a JSON object"]

    missing = sorted(REVIEW_VERDICT_REQUIRED_FIELDS - set(verdict))
    if missing:
        errors.append("reviewer verdict missing required fields: " + ", ".join(missing))

    approved_raw = verdict.get("approved")
    if not isinstance(approved_raw, bool):
        errors.append("reviewer verdict field approved must be a boolean")
    findings_raw = verdict.get("findings", [])
    if not isinstance(findings_raw, list) or not all(
        isinstance(item, str) for item in findings_raw
    ):
        errors.append("reviewer verdict field findings must be an array of strings")
        findings: List[str] = []
    else:
        findings = [item for item in findings_raw if item.strip()]

    reviewed_raw = verdict.get("reviewed_files", [])
    reviewed_files: List[str] = []
    if not isinstance(reviewed_raw, list) or not all(
        isinstance(item, str) for item in reviewed_raw
    ):
        errors.append("reviewer verdict field reviewed_files must be an array of strings")
    else:
        for item in reviewed_raw:
            try:
                reviewed_files.append(normalize_repo_relative_path(item, repo_root=repo_root))
            except PathContractError as exc:
                errors.append(f"reviewed_files contains invalid path {item!r}: {exc}")

    residual_raw = verdict.get("residual_risks", [])
    if not isinstance(residual_raw, list) or not all(
        isinstance(item, str) for item in residual_raw
    ):
        errors.append("reviewer verdict field residual_risks must be an array of strings")
        residual_risks: List[str] = []
    else:
        residual_risks = [item for item in residual_raw if item.strip()]

    commit_eligibility_raw = verdict.get("commit_eligibility")
    if not isinstance(commit_eligibility_raw, bool):
        errors.append("reviewer verdict field commit_eligibility must be a boolean")

    return {
        "approved": approved_raw if isinstance(approved_raw, bool) else False,
        "findings": findings,
        "reviewed_files": _dedupe_sorted(reviewed_files),
        "validation_summary": verdict.get("validation_summary"),
        "residual_risks": residual_risks,
        "commit_eligibility": (
            commit_eligibility_raw if isinstance(commit_eligibility_raw, bool) else False
        ),
    }, errors


def evaluate_semantic_review_decision(
    *,
    mode: str,
    repo_root: Path,
    changed_files: Sequence[str],
    verifier_report: Dict[str, Any],
    validation_summary: Dict[str, Any],
    manual_check_results: Sequence[Dict[str, Any]],
    verdict: Optional[Dict[str, Any]],
    parse_error: Optional[str] = None,
    fingerprint_check: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_verdict, verdict_errors = _normalize_verdict_fields(verdict, repo_root=repo_root)
    blocking_reasons: List[str] = []

    if mode == "disabled":
        blocking_reasons.append("semantic_review_disabled")
    if parse_error:
        blocking_reasons.append("reviewer_output_invalid")
        verdict_errors.append(parse_error)
    if verifier_report.get("blocked"):
        blocking_reasons.append("verifier_risk_remains")
    if validation_summary.get("has_required_failure"):
        blocking_reasons.append("required_validations_failed")
    if validation_summary.get("has_required_timeout"):
        blocking_reasons.append("required_validations_timed_out")
    if validation_summary.get("has_required_skip"):
        blocking_reasons.append("required_validations_skipped")
    blocking_manual_checks = [
        str(check.get("id") or "")
        for check in manual_check_results
        if check.get("blocking") and check.get("status") != "passed"
    ]
    if blocking_manual_checks:
        blocking_reasons.append("blocking_manual_checks_incomplete")
    if isinstance(fingerprint_check, dict) and fingerprint_check.get("stale"):
        blocking_reasons.append("semantic_review_packet_stale")

    normalized_changed = _dedupe_sorted(changed_files)
    missing_reviewed_files: List[str] = []
    if mode != "disabled":
        if verdict_errors:
            blocking_reasons.append("reviewer_verdict_schema_invalid")
        if not normalized_verdict["approved"]:
            blocking_reasons.append("reviewer_not_approved")
        if normalized_verdict["findings"]:
            blocking_reasons.append("reviewer_findings_unresolved")

        reviewed_set = set(normalized_verdict["reviewed_files"])
        missing_reviewed_files = [path for path in normalized_changed if path not in reviewed_set]
        if missing_reviewed_files:
            blocking_reasons.append("changed_files_not_fully_reviewed")
        if not normalized_verdict["commit_eligibility"]:
            blocking_reasons.append("reviewer_commit_eligibility_false")

    blocking_reasons = _dedupe_sorted(blocking_reasons)
    approved = not blocking_reasons
    return {
        "schema_version": 1,
        "created_at_utc": _utc_now_iso(),
        "semantic_review_mode": mode,
        "status": "approved" if approved else "blocked",
        "approved": approved,
        "commit_eligibility": approved,
        "blocking_reasons": blocking_reasons,
        "verdict_errors": verdict_errors,
        "reviewed_files": normalized_verdict["reviewed_files"],
        "missing_reviewed_files": missing_reviewed_files,
        "findings": normalized_verdict["findings"],
        "validation_summary": normalized_verdict["validation_summary"],
        "residual_risks": normalized_verdict["residual_risks"],
        "blocking_manual_checks": blocking_manual_checks,
        "fingerprint_check": fingerprint_check,
    }


def _persist_semantic_review_verdict_and_decision(
    *,
    attempt_record: Dict[str, Any],
    verdict: Optional[Dict[str, Any]],
    decision: Dict[str, Any],
) -> None:
    _write_json_file(
        Path(attempt_record["semantic_review_verdict_path"]),
        verdict if isinstance(verdict, dict) else {"verdict": None},
    )
    _write_json_file(Path(attempt_record["semantic_review_decision_path"]), decision)
    attempt_record["semantic_review_parsed_verdict"] = verdict
    attempt_record["semantic_review_decision"] = decision


def run_model_semantic_review(
    *,
    repo_root: Path,
    attempt_record: Dict[str, Any],
    packet_text: str,
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    prompt_path = Path(attempt_record["semantic_review_prompt_path"])
    output_file = Path(attempt_record["semantic_review_output_json_path"])
    artifacts_dir = Path(attempt_record["semantic_review_artifacts_dir"])
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        render_model_review_prompt(
            packet_path=attempt_record["semantic_review_packet_path"],
            packet_text=packet_text,
        ),
        encoding="utf-8",
    )
    command = build_reviewer_relay_command(
        repo_root=repo_root,
        prompt_file=prompt_path,
        output_file=output_file,
        local_artifacts_dir=artifacts_dir,
        args=args,
    )
    attempt_record["semantic_review_reviewer_command"] = command
    result = run_executor_relay_command(command, repo_root=repo_root)
    attempt_record["semantic_review_reviewer_exit_code"] = result.returncode
    attempt_record["semantic_review_reviewer_stdout_excerpt"] = _output_excerpt(result.stdout)[0]
    attempt_record["semantic_review_reviewer_stderr_excerpt"] = _output_excerpt(result.stderr)[0]

    try:
        relay_payload = _load_json_file_if_present(output_file)
    except PhaseGateError as exc:
        relay_payload = None
        attempt_record["semantic_review_reviewer_output_error"] = str(exc)
    attempt_record["semantic_review_reviewer_output"] = relay_payload

    result_summary = relay_payload.get("result_summary") if isinstance(relay_payload, dict) else None
    relay_ok = (
        result.returncode == 0
        and isinstance(result_summary, dict)
        and result_summary.get("ok") is True
    )
    timed_out = (
        result.returncode == RELAY_EXIT_TIMEOUT
        or (isinstance(relay_payload, dict) and relay_payload.get("status") == "timeout")
    )
    if not relay_ok:
        return None, "reviewer relay did not complete successfully", timed_out
    verdict, parse_error = parse_review_verdict_from_relay_payload(relay_payload)
    return verdict, parse_error, timed_out


def run_semantic_review_gate(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    attempt_record: Dict[str, Any],
    phase_record: Dict[str, Any],
    ledger: Dict[str, Any],
    repo_root: Path,
    state_file: Path,
    artifact_specs: Sequence[PathSpec],
    baseline_git_state: Dict[str, Any],
    post_validation_git: Dict[str, Any],
    post_validation_report: Dict[str, Any],
    validation_summary: Dict[str, Any],
    validation_results: Sequence[Dict[str, Any]],
    manual_check_results: Sequence[Dict[str, Any]],
    skipped_live_checks: Sequence[Dict[str, Any]],
    skipped_manual_checks: Sequence[Dict[str, Any]],
    blocking_manual_checks: Sequence[str],
    args: argparse.Namespace,
) -> Optional[int]:
    mode = args.semantic_review_mode
    attempt_record["semantic_review_mode"] = mode
    ledger["semantic_review_mode"] = mode

    packet = build_semantic_review_packet(
        plan=plan,
        phase=phase,
        ledger=ledger,
        repo_root=repo_root,
        post_validation_git=post_validation_git,
        verifier_report=post_validation_report,
        validation_summary=validation_summary,
        validation_results=validation_results,
        manual_check_results=manual_check_results,
        skipped_live_checks=skipped_live_checks,
        skipped_manual_checks=skipped_manual_checks,
        blocking_manual_checks=blocking_manual_checks,
    )
    persist_semantic_review_packet(attempt_record=attempt_record, packet=packet)
    packet_text = Path(attempt_record["semantic_review_packet_path"]).read_text(encoding="utf-8")
    _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
    _sync_ledger_from_phase(ledger, phase_record)
    persist_ledger(ledger, state_file)

    if mode == "disabled":
        decision = evaluate_semantic_review_decision(
            mode=mode,
            repo_root=repo_root,
            changed_files=packet.get("review_required_files", []),
            verifier_report=post_validation_report,
            validation_summary=validation_summary,
            manual_check_results=manual_check_results,
            verdict=None,
        )
        _persist_semantic_review_verdict_and_decision(
            attempt_record=attempt_record,
            verdict=None,
            decision=decision,
        )
        _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
        _sync_ledger_from_phase(ledger, phase_record)
        persist_ledger(ledger, state_file)
        commit_gate_exit = apply_commit_gate_after_review(
            plan=plan,
            phase=phase,
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            repo_root=repo_root,
            decision=decision,
            packet=packet,
            verifier_report=post_validation_report,
            current_git_state=post_validation_git,
            args=args,
        )
        if commit_gate_exit is not None:
            return commit_gate_exit
        return None

    verdict: Optional[Dict[str, Any]]
    parse_error: Optional[str]
    timed_out = False
    commit_gate_git = post_validation_git

    if mode == "manual_artifact":
        manual_artifact_path = (
            _resolve_under_repo(repo_root, args.manual_approval_artifact)
            if args.manual_approval_artifact
            else Path(attempt_record["manual_approval_artifact_path"])
        )
        attempt_record["manual_approval_artifact_path"] = str(manual_artifact_path)
        if not manual_artifact_path.exists():
            decision = {
                "schema_version": 1,
                "created_at_utc": _utc_now_iso(),
                "semantic_review_mode": mode,
                "status": "pending_manual_artifact",
                "approved": False,
                "commit_eligibility": False,
                "blocking_reasons": ["manual_approval_artifact_missing"],
                "manual_approval_artifact_path": str(manual_artifact_path),
                "review_packet_path": attempt_record["semantic_review_packet_path"],
            }
            _persist_semantic_review_verdict_and_decision(
                attempt_record=attempt_record,
                verdict=None,
                decision=decision,
            )
            _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
            _sync_ledger_from_phase(ledger, phase_record)
            persist_ledger(ledger, state_file)
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="semantic_review_pending",
                next_output_state="checkpoint",
                decision="semantic_review_pending",
                exit_code=EXIT_CHECKPOINT,
            )
        verdict, parse_error = parse_review_verdict_from_artifact(manual_artifact_path)
        attempt_record["semantic_review_reviewer_output"] = {
            "mode": "manual_artifact",
            "path": str(manual_artifact_path),
            "payload": verdict,
            "parse_error": parse_error,
        }
        fingerprint_check = check_semantic_review_input_fingerprint(
            repo_root=repo_root,
            packet=packet,
            current_git_state=post_validation_git,
        )
        attempt_record["semantic_review_fingerprint_check"] = fingerprint_check
        ledger["semantic_review_fingerprint_check"] = fingerprint_check
    else:
        verdict, parse_error, timed_out = run_model_semantic_review(
            repo_root=repo_root,
            attempt_record=attempt_record,
            packet_text=packet_text,
            args=args,
        )

        post_review_git = filter_git_state_for_runner_artifacts(
            capture_git_state(repo_root, baseline_head=baseline_git_state.get("head")),
            artifact_specs,
        )
        commit_gate_git = post_review_git
        post_review_report = classify_git_scope(
            repo_root=repo_root,
            phase=phase,
            baseline_state=baseline_git_state,
            current_state=post_review_git,
            expected_branch=plan["branch"],
        )
        attempt_record["semantic_review_post_review_verifier_report"] = post_review_report
        ledger["post_semantic_review_git"] = {
            "head": post_review_git.get("head"),
            "branch": post_review_git.get("branch"),
            "status_short_branch": post_review_git.get("status_short_branch"),
            "diff_name_status": post_review_git.get("diff_name_status"),
            "diff_cached_name_status": post_review_git.get("diff_cached_name_status"),
            "diff_stat": post_review_git.get("diff_stat"),
        }
        if post_review_report.get("blocked"):
            if parse_error:
                parse_error += "; post-review verifier risk remains"
            else:
                parse_error = "post-review verifier risk remains"
        fingerprint_check = check_semantic_review_input_fingerprint(
            repo_root=repo_root,
            packet=packet,
            current_git_state=post_review_git,
        )
        attempt_record["semantic_review_fingerprint_check"] = fingerprint_check
        ledger["semantic_review_fingerprint_check"] = fingerprint_check

    decision = evaluate_semantic_review_decision(
        mode=mode,
        repo_root=repo_root,
        changed_files=packet.get("review_required_files", []),
        verifier_report=post_validation_report,
        validation_summary=validation_summary,
        manual_check_results=manual_check_results,
        verdict=verdict,
        parse_error=parse_error,
        fingerprint_check=(
            attempt_record.get("semantic_review_fingerprint_check")
            if mode != "disabled"
            else None
        ),
    )
    _persist_semantic_review_verdict_and_decision(
        attempt_record=attempt_record,
        verdict=verdict,
        decision=decision,
    )
    _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
    _sync_ledger_from_phase(ledger, phase_record)
    persist_ledger(ledger, state_file)

    commit_gate_exit = apply_commit_gate_after_review(
        plan=plan,
        phase=phase,
        attempt_record=attempt_record,
        phase_record=phase_record,
        ledger=ledger,
        repo_root=repo_root,
        decision=decision,
        packet=packet,
        verifier_report=post_validation_report,
        current_git_state=commit_gate_git,
        args=args,
    )
    if commit_gate_exit is not None:
        return commit_gate_exit

    if decision["approved"]:
        return _finalize_phase(
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            status="semantic_review_approved",
            next_output_state="semantic_review_complete",
            decision="semantic_review_approved",
            exit_code=EXIT_OK,
        )

    return _finalize_phase(
        attempt_record=attempt_record,
        phase_record=phase_record,
        ledger=ledger,
        status="semantic_review_timeout" if timed_out else "semantic_review_blocked",
        next_output_state="blocked",
        decision="semantic_review_timeout" if timed_out else "semantic_review_blocked",
        exit_code=EXIT_TIMEOUT if timed_out else EXIT_BLOCKED,
    )


def resume_manual_semantic_review_checkpoint(
    *,
    ledger: Dict[str, Any],
    repo_root: Path,
    state_file: Path,
    args: argparse.Namespace,
) -> Optional[int]:
    if ledger.get("decision") != "semantic_review_pending":
        return None
    if ledger.get("semantic_review_mode") != "manual_artifact":
        return None

    phases = ledger.get("phases")
    if not isinstance(phases, list) or not phases:
        return None
    phase_index = int(ledger.get("current_phase_index") or 0)
    if phase_index >= len(phases):
        phase_index = 0
    phase_record = phases[phase_index]
    attempts = phase_record.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    attempt_record = attempts[-1]

    manual_artifact_path = (
        _resolve_under_repo(repo_root, args.manual_approval_artifact)
        if args.manual_approval_artifact
        else Path(
            str(
                attempt_record.get("manual_approval_artifact_path")
                or phase_record.get("manual_approval_artifact_path")
                or ledger.get("manual_approval_artifact_path")
                or ""
            )
        )
    )
    attempt_record["manual_approval_artifact_path"] = str(manual_artifact_path)
    phase_record["manual_approval_artifact_path"] = str(manual_artifact_path)

    if not manual_artifact_path.exists():
        decision = dict(attempt_record.get("semantic_review_decision") or {})
        if not decision:
            decision = {
                "schema_version": 1,
                "created_at_utc": _utc_now_iso(),
                "semantic_review_mode": "manual_artifact",
                "status": "pending_manual_artifact",
                "approved": False,
                "commit_eligibility": False,
                "blocking_reasons": ["manual_approval_artifact_missing"],
                "manual_approval_artifact_path": str(manual_artifact_path),
                "review_packet_path": attempt_record.get("semantic_review_packet_path"),
            }
        _persist_semantic_review_verdict_and_decision(
            attempt_record=attempt_record,
            verdict=None,
            decision=decision,
        )
        _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
        _sync_ledger_from_phase(ledger, phase_record)
        persist_ledger(ledger, state_file)
        return EXIT_CHECKPOINT

    verdict, parse_error = parse_review_verdict_from_artifact(manual_artifact_path)
    attempt_record["semantic_review_reviewer_output"] = {
        "mode": "manual_artifact",
        "path": str(manual_artifact_path),
        "payload": verdict,
        "parse_error": parse_error,
    }
    packet = attempt_record.get("semantic_review_packet")
    if not isinstance(packet, dict):
        packet = phase_record.get("semantic_review_packet") if isinstance(phase_record, dict) else {}
    if not isinstance(packet, dict):
        packet = {}
    verifier_report = attempt_record.get("post_validation_verifier_report")
    if not isinstance(verifier_report, dict):
        verifier_report = ledger.get("latest_verifier_report") or {}
    validation_summary = attempt_record.get("validation_summary")
    if not isinstance(validation_summary, dict):
        validation_summary = ledger.get("validation_summary") or {}
    manual_check_results = attempt_record.get("manual_check_results")
    if not isinstance(manual_check_results, list):
        manual_check_results = ledger.get("manual_check_results") or []
    output_dir = Path(str(ledger.get("output_dir") or "."))
    artifact_specs = runner_artifact_specs(
        repo_root=repo_root,
        output_dir=output_dir,
        state_file=state_file,
    )
    plan_file = _resolve_under_repo(repo_root, str(ledger.get("plan_file") or ""))
    plan = load_plan_file(plan_file, repo_root=repo_root)
    if phase_index >= len(plan.get("phases", [])):
        raise ResumeStateError("resume phase index is outside the plan phases")
    phase = plan["phases"][phase_index]
    current_git_state = filter_git_state_for_runner_artifacts(
        capture_git_state(repo_root, baseline_head=str(ledger.get("baseline_head") or "")),
        artifact_specs,
    )
    fingerprint_check = check_semantic_review_input_fingerprint(
        repo_root=repo_root,
        packet=packet,
        current_git_state=current_git_state,
    )
    attempt_record["semantic_review_fingerprint_check"] = fingerprint_check
    ledger["semantic_review_fingerprint_check"] = fingerprint_check

    decision = evaluate_semantic_review_decision(
        mode="manual_artifact",
        repo_root=repo_root,
        changed_files=packet.get("review_required_files", []),
        verifier_report=verifier_report,
        validation_summary=validation_summary,
        manual_check_results=manual_check_results,
        verdict=verdict,
        parse_error=parse_error,
        fingerprint_check=fingerprint_check,
    )
    _persist_semantic_review_verdict_and_decision(
        attempt_record=attempt_record,
        verdict=verdict,
        decision=decision,
    )
    _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
    _sync_ledger_from_phase(ledger, phase_record)
    commit_gate_exit = apply_commit_gate_after_review(
        plan=plan,
        phase=phase,
        attempt_record=attempt_record,
        phase_record=phase_record,
        ledger=ledger,
        repo_root=repo_root,
        decision=decision,
        packet=packet,
        verifier_report=verifier_report,
        current_git_state=current_git_state,
        args=args,
    )
    if commit_gate_exit is not None:
        persist_ledger(ledger, state_file)
        return commit_gate_exit

    exit_code = _finalize_phase(
        attempt_record=attempt_record,
        phase_record=phase_record,
        ledger=ledger,
        status="semantic_review_approved" if decision["approved"] else "semantic_review_blocked",
        next_output_state=(
            "semantic_review_complete" if decision["approved"] else "blocked"
        ),
        decision=(
            "semantic_review_approved" if decision["approved"] else "semantic_review_blocked"
        ),
        exit_code=EXIT_OK if decision["approved"] else EXIT_BLOCKED,
    )
    persist_ledger(ledger, state_file)
    return exit_code


def execute_phase_with_followups(
    *,
    plan: Dict[str, Any],
    phase: Dict[str, Any],
    phase_record: Dict[str, Any],
    ledger: Dict[str, Any],
    repo_root: Path,
    state_file: Path,
    artifact_specs: Sequence[PathSpec],
    baseline_git_state: Dict[str, Any],
    args: argparse.Namespace,
) -> int:
    max_followups = int(plan.get("max_followups_per_phase", 0))
    output_dir = Path(str(ledger["output_dir"]))
    followup_findings: List[str] = []
    attempt_index = 0

    while True:
        attempt_record = create_phase_attempt_record(
            output_dir=output_dir,
            phase=phase,
            attempt_index=attempt_index,
            findings=followup_findings,
        )
        phase_record.setdefault("attempts", []).append(attempt_record)
        attempt_record["executor_relay_mode"] = args.executor_relay_mode
        phase_record["current_attempt_index"] = attempt_index
        phase_record["followups_used"] = attempt_index
        ledger["followups_used"] = attempt_index

        if attempt_index == 0:
            persist_executor_prompt(
                plan=plan,
                phase=phase,
                phase_record=attempt_record,
                repo_root=repo_root,
                relay_mode=args.executor_relay_mode,
                local_sandbox=args.executor_local_sandbox,
            )
        else:
            prompt_text = render_followup_prompt(
                plan=plan,
                phase=phase,
                repo_root=repo_root,
                followup_number=attempt_index,
                max_followups=max_followups,
                findings=followup_findings,
            )
            persist_prompt_text(record=attempt_record, prompt_text=prompt_text)
            phase_record.setdefault("followup_prompts", []).append(
                {
                    "attempt_index": attempt_index,
                    "path": attempt_record["executor_prompt_path"],
                    "sha256": attempt_record["executor_prompt_text_sha256"],
                    "findings": list(followup_findings),
                }
            )

        _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
        _sync_ledger_from_phase(ledger, phase_record)
        ledger["next_output_state"] = (
            "executor_launch" if args.executor_relay_mode != "disabled" else "validation"
        )
        persist_ledger(ledger, state_file)

        pre_executor_git = filter_git_state_for_runner_artifacts(
            capture_git_state(repo_root, baseline_head=baseline_git_state.get("head")),
            artifact_specs,
        )
        pre_executor_report = classify_git_scope(
            repo_root=repo_root,
            phase=phase,
            baseline_state=baseline_git_state,
            current_state=pre_executor_git,
            expected_branch=plan["branch"],
        )
        attempt_record["pre_executor_verifier_report"] = pre_executor_report
        ledger["latest_verifier_report"] = pre_executor_report
        _write_verifier_report(
            report=pre_executor_report,
            step_path=attempt_record["pre_executor_verifier_report_path"],
            latest_path=attempt_record["verifier_report_path"],
        )
        _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
        persist_ledger(ledger, state_file)

        if pre_executor_report["blocked"] and (
            attempt_index == 0 or _verifier_has_hard_block(pre_executor_report)
        ):
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="blocked",
                next_output_state="blocked",
                decision="blocked",
                exit_code=EXIT_BLOCKED,
            )

        if args.executor_relay_mode != "disabled":
            relay_result = run_executor_relay(
                repo_root=repo_root,
                phase_record=attempt_record,
                args=args,
            )
            attempt_record["executor_relay_result"] = relay_result
            _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
            _sync_ledger_from_phase(ledger, phase_record)
            persist_ledger(ledger, state_file)

            post_executor_git = filter_git_state_for_runner_artifacts(
                capture_git_state(repo_root, baseline_head=baseline_git_state.get("head")),
                artifact_specs,
            )
            post_executor_report = classify_git_scope(
                repo_root=repo_root,
                phase=phase,
                baseline_state=baseline_git_state,
                current_state=post_executor_git,
                expected_branch=plan["branch"],
            )
            attempt_record["post_executor_verifier_report"] = post_executor_report
            attempt_record["dirty_files_after_executor"] = post_executor_git.get(
                "changed_paths", []
            )
            ledger["post_executor_git"] = {
                "head": post_executor_git.get("head"),
                "branch": post_executor_git.get("branch"),
                "status_short_branch": post_executor_git.get("status_short_branch"),
                "diff_name_status": post_executor_git.get("diff_name_status"),
                "diff_cached_name_status": post_executor_git.get(
                    "diff_cached_name_status"
                ),
                "diff_stat": post_executor_git.get("diff_stat"),
            }
            ledger["latest_verifier_report"] = post_executor_report
            _write_verifier_report(
                report=post_executor_report,
                step_path=attempt_record["post_executor_verifier_report_path"],
                latest_path=attempt_record["verifier_report_path"],
            )
            _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
            persist_ledger(ledger, state_file)

            if post_executor_report["blocked"] and _verifier_has_hard_block(post_executor_report):
                return _finalize_phase(
                    attempt_record=attempt_record,
                    phase_record=phase_record,
                    ledger=ledger,
                    status="blocked",
                    next_output_state="blocked",
                    decision="blocked",
                    exit_code=EXIT_BLOCKED,
                )

            if not relay_result["ok"]:
                findings = relay_findings(relay_result, attempt_record) + verifier_findings(
                    post_executor_report
                )
                if _can_retry_followup(
                    attempt_index=attempt_index,
                    max_followups=max_followups,
                    relay_mode=args.executor_relay_mode,
                ):
                    _mark_attempt_for_followup(
                        attempt_record=attempt_record,
                        phase_record=phase_record,
                        ledger=ledger,
                        findings=findings,
                    )
                    persist_ledger(ledger, state_file)
                    followup_findings = findings
                    attempt_index += 1
                    continue

                return _finalize_phase(
                    attempt_record=attempt_record,
                    phase_record=phase_record,
                    ledger=ledger,
                    status=(
                        "executor_timeout"
                        if relay_result.get("timed_out")
                        else "executor_failed"
                    ),
                    next_output_state="blocked",
                    decision=(
                        "executor_timeout"
                        if relay_result.get("timed_out")
                        else "executor_failed"
                    ),
                    exit_code=(
                        EXIT_TIMEOUT
                        if relay_result.get("timed_out")
                        else EXIT_PHASE_FAILED
                    ),
                    followups_exhausted=attempt_index >= max_followups and max_followups > 0,
                )

            if post_executor_report["blocked"]:
                findings = verifier_findings(post_executor_report)
                if (
                    _can_retry_followup(
                        attempt_index=attempt_index,
                        max_followups=max_followups,
                        relay_mode=args.executor_relay_mode,
                    )
                ):
                    _mark_attempt_for_followup(
                        attempt_record=attempt_record,
                        phase_record=phase_record,
                        ledger=ledger,
                        findings=findings,
                    )
                    persist_ledger(ledger, state_file)
                    followup_findings = findings
                    attempt_index += 1
                    continue

                return _finalize_phase(
                    attempt_record=attempt_record,
                    phase_record=phase_record,
                    ledger=ledger,
                    status="blocked",
                    next_output_state="blocked",
                    decision="blocked",
                    exit_code=EXIT_BLOCKED,
                    followups_exhausted=(
                        attempt_index >= max_followups
                        and max_followups > 0
                        and not _verifier_has_hard_block(post_executor_report)
                    ),
                )
        else:
            attempt_record["executor_relay_exit_code"] = None
            attempt_record["executor_relay_output"] = None

        validation_summary = run_validation_commands(
            repo_root=repo_root,
            commands=phase.get("validation", []),
            allow_manual_validation=args.allow_manual_validation,
            allow_live_validation=args.allow_live_validation,
        )
        validation_results = validation_summary["results"]
        validation_summary_for_ledger = {
            key: value for key, value in validation_summary.items() if key != "results"
        }
        manual_check_results = record_manual_check_skips(phase.get("manual_checks", []))
        skipped_live_checks = [
            result
            for result in validation_results
            if result.get("status") == "skipped" and result.get("live")
        ]
        skipped_manual_checks = [
            result
            for result in validation_results
            if result.get("status") == "skipped" and result.get("manual")
        ] + manual_check_results
        blocking_manual_checks = [
            str(check["id"]) for check in manual_check_results if check.get("blocking")
        ]

        attempt_record["validations"] = validation_results
        attempt_record["validation_summary"] = validation_summary_for_ledger
        attempt_record["manual_check_results"] = manual_check_results
        attempt_record["skipped_live_checks"] = skipped_live_checks
        attempt_record["skipped_manual_checks"] = skipped_manual_checks
        _write_validation_result(
            attempt_record["validation_result_path"],
            validation_summary_for_ledger,
            validation_results,
        )

        post_validation_git = filter_git_state_for_runner_artifacts(
            capture_git_state(repo_root, baseline_head=baseline_git_state.get("head")),
            artifact_specs,
        )
        post_validation_report = classify_git_scope(
            repo_root=repo_root,
            phase=phase,
            baseline_state=baseline_git_state,
            current_state=post_validation_git,
            expected_branch=plan["branch"],
        )

        attempt_record["post_validation_verifier_report"] = post_validation_report
        attempt_record["dirty_files_after"] = post_validation_git.get("changed_paths", [])
        ledger["validations_run"] = validation_results
        ledger["validation_summary"] = validation_summary_for_ledger
        ledger["manual_check_results"] = manual_check_results
        ledger["skipped_live_checks"] = skipped_live_checks
        ledger["skipped_manual_checks"] = skipped_manual_checks
        ledger["dirty_files_after"] = post_validation_git.get("changed_paths", [])
        ledger["latest_verifier_report"] = post_validation_report
        ledger["post_validation_git"] = {
            "head": post_validation_git.get("head"),
            "branch": post_validation_git.get("branch"),
            "status_short_branch": post_validation_git.get("status_short_branch"),
            "diff_name_status": post_validation_git.get("diff_name_status"),
            "diff_cached_name_status": post_validation_git.get(
                "diff_cached_name_status"
            ),
            "diff_stat": post_validation_git.get("diff_stat"),
        }
        _write_verifier_report(
            report=post_validation_report,
            step_path=attempt_record["post_validation_verifier_report_path"],
            latest_path=attempt_record["verifier_report_path"],
        )
        _copy_latest_attempt_to_phase_record(phase_record, attempt_record)
        _sync_ledger_from_phase(ledger, phase_record)
        persist_ledger(ledger, state_file)

        if _verifier_has_hard_block(post_validation_report):
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="blocked",
                next_output_state="blocked",
                decision="blocked",
                exit_code=EXIT_BLOCKED,
            )

        if validation_summary_for_ledger["has_required_timeout"]:
            findings = validation_findings(validation_results)
            if _can_retry_followup(
                attempt_index=attempt_index,
                max_followups=max_followups,
                relay_mode=args.executor_relay_mode,
            ):
                _mark_attempt_for_followup(
                    attempt_record=attempt_record,
                    phase_record=phase_record,
                    ledger=ledger,
                    findings=findings,
                )
                persist_ledger(ledger, state_file)
                followup_findings = findings
                attempt_index += 1
                continue
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="validation_timeout",
                next_output_state="blocked",
                decision="validation_timeout",
                exit_code=EXIT_TIMEOUT,
                followups_exhausted=attempt_index >= max_followups and max_followups > 0,
            )

        if validation_summary_for_ledger["has_required_failure"]:
            findings = validation_findings(validation_results)
            if _can_retry_followup(
                attempt_index=attempt_index,
                max_followups=max_followups,
                relay_mode=args.executor_relay_mode,
            ):
                _mark_attempt_for_followup(
                    attempt_record=attempt_record,
                    phase_record=phase_record,
                    ledger=ledger,
                    findings=findings,
                )
                persist_ledger(ledger, state_file)
                followup_findings = findings
                attempt_index += 1
                continue
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="phase_failed",
                next_output_state="blocked",
                decision="phase_failed",
                exit_code=EXIT_PHASE_FAILED,
                followups_exhausted=attempt_index >= max_followups and max_followups > 0,
            )

        if validation_summary_for_ledger["has_required_skip"] or blocking_manual_checks:
            ledger["blocking_manual_checks"] = blocking_manual_checks
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="checkpointed",
                next_output_state="checkpoint",
                decision="checkpointed",
                exit_code=EXIT_CHECKPOINT,
            )

        if post_validation_report["blocked"]:
            findings = verifier_findings(post_validation_report)
            if _can_retry_followup(
                attempt_index=attempt_index,
                max_followups=max_followups,
                relay_mode=args.executor_relay_mode,
            ):
                _mark_attempt_for_followup(
                    attempt_record=attempt_record,
                    phase_record=phase_record,
                    ledger=ledger,
                    findings=findings,
                )
                persist_ledger(ledger, state_file)
                followup_findings = findings
                attempt_index += 1
                continue
            return _finalize_phase(
                attempt_record=attempt_record,
                phase_record=phase_record,
                ledger=ledger,
                status="blocked",
                next_output_state="blocked",
                decision="blocked",
                exit_code=EXIT_BLOCKED,
                followups_exhausted=attempt_index >= max_followups and max_followups > 0,
            )

        semantic_review_exit = run_semantic_review_gate(
            plan=plan,
            phase=phase,
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            repo_root=repo_root,
            state_file=state_file,
            artifact_specs=artifact_specs,
            baseline_git_state=baseline_git_state,
            post_validation_git=post_validation_git,
            post_validation_report=post_validation_report,
            validation_summary=validation_summary_for_ledger,
            validation_results=validation_results,
            manual_check_results=manual_check_results,
            skipped_live_checks=skipped_live_checks,
            skipped_manual_checks=skipped_manual_checks,
            blocking_manual_checks=blocking_manual_checks,
            args=args,
        )
        if semantic_review_exit is not None:
            return semantic_review_exit

        return _finalize_phase(
            attempt_record=attempt_record,
            phase_record=phase_record,
            ledger=ledger,
            status=(
                "validation_completed_with_optional_failures"
                if validation_summary_for_ledger["has_optional_failure"]
                else "validation_passed"
            ),
            next_output_state="validation_complete",
            decision=(
                "validation_completed_with_optional_failures"
                if validation_summary_for_ledger["has_optional_failure"]
                else "validation_passed"
            ),
            exit_code=EXIT_OK,
        )


def execute_plan_phases(
    *,
    plan: Dict[str, Any],
    ledger: Dict[str, Any],
    repo_root: Path,
    state_file: Path,
    output_dir: Path,
    artifact_specs: Sequence[PathSpec],
    start_index: int,
    args: argparse.Namespace,
) -> int:
    exit_code = EXIT_OK
    for phase_index in range(start_index, len(plan.get("phases", []))):
        base_phase = plan["phases"][phase_index]
        phase = _phase_with_additional_expected_dirty(
            base_phase,
            ledger.get("approved_dirty_continuation_paths", []),
        )
        phase_record = ledger["phases"][phase_index]
        ledger["current_phase_index"] = phase_index
        ledger["current_phase_name"] = phase_record.get("name")
        ledger["validation_commands"] = phase.get("validation", [])
        ledger["smoke_checks"] = phase.get("manual_checks", [])
        ledger["expected_dirty_files_for_current_phase"] = [
            item["path"] for item in phase.get("expected_dirty_paths", [])
        ]

        phase_git_state = filter_git_state_for_runner_artifacts(
            capture_git_state(repo_root),
            artifact_specs,
        )
        verifier_report = classify_git_scope(
            repo_root=repo_root,
            phase=phase,
            baseline_state=phase_git_state,
            current_state=phase_git_state,
            expected_branch=plan["branch"],
        )
        ledger["latest_verifier_report"] = verifier_report
        ledger["dirty_files_before"] = phase_git_state.get("changed_paths", [])
        phase_record["dirty_files_before"] = phase_git_state.get("changed_paths", [])
        verifier_path = Path(phase_record["verifier_report_path"])
        if verifier_report["blocked"]:
            _write_json_file(verifier_path, verifier_report)
            ledger["next_output_state"] = "blocked"
            ledger["decision"] = "blocked"
            phase_record["status"] = "blocked"
            phase_record["decision"] = "blocked"
            persist_ledger(ledger, state_file)
            return EXIT_BLOCKED

        exit_code = execute_phase_with_followups(
            plan=plan,
            phase=phase,
            phase_record=phase_record,
            ledger=ledger,
            repo_root=repo_root,
            state_file=state_file,
            artifact_specs=artifact_specs,
            baseline_git_state=phase_git_state,
            args=args,
        )
        persist_ledger(ledger, state_file)
        if exit_code != EXIT_OK:
            return exit_code

        if (
            phase_index + 1 < len(plan.get("phases", []))
            and plan.get("commit_mode") in {COMMIT_MODE_NO_COMMIT, COMMIT_MODE_PREPARE_COMMAND}
            and not ledger.get("approved_dirty_continuation")
        ):
            current_git = filter_git_state_for_runner_artifacts(
                capture_git_state(repo_root),
                artifact_specs,
            )
            if current_git.get("changed_paths"):
                ledger["next_output_state"] = "checkpoint"
                ledger["decision"] = "review_needed_checkpoint"
                phase_record["status"] = "review_needed_checkpoint"
                phase_record["decision"] = "review_needed_checkpoint"
                persist_ledger(ledger, state_file)
                return EXIT_CHECKPOINT

    if len(plan.get("phases", [])) > 1 and exit_code == EXIT_OK:
        ledger["next_output_state"] = "complete"
        ledger["decision"] = "completed"
    return exit_code


def _commit_changed_files(repo_root: Path, commit_hash: str) -> List[str]:
    raw = _run_git(
        repo_root,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
    )
    return _dedupe_sorted([line.strip() for line in raw.splitlines() if line.strip()])


def _verify_checkpoint_commit(
    *,
    repo_root: Path,
    checkpoint: Dict[str, Any],
    current_head: str,
    expected_commit_hash: str,
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    pre_commit_head = str(checkpoint.get("pre_commit_head") or "")
    if not expected_commit_hash:
        reasons.append("expected_commit_hash_missing")
    elif current_head != expected_commit_hash:
        reasons.append("expected_commit_hash_mismatch")
    try:
        parent = _run_git(repo_root, ["rev-parse", f"{current_head}^"]).strip()
    except GitCommandError:
        parent = ""
    if parent != pre_commit_head:
        reasons.append("commit_parent_mismatch")

    expected_message = str(checkpoint.get("expected_commit_message") or "").strip()
    actual_message = _run_git(repo_root, ["log", "-1", "--pretty=%B", current_head]).strip()
    if expected_message and actual_message != expected_message:
        reasons.append("commit_message_mismatch")

    reviewed_set = set(_dedupe_sorted(checkpoint.get("reviewed_files", [])))
    changed_files = _commit_changed_files(repo_root, current_head)
    outside_reviewed = [path for path in changed_files if path not in reviewed_set]
    if outside_reviewed:
        reasons.append("commit_touched_files_outside_reviewed_set")
        checkpoint["commit_files_outside_reviewed_set"] = outside_reviewed

    return not reasons, _dedupe_sorted(reasons)


def _latest_phase_attempt(phase_record: Dict[str, Any]) -> Dict[str, Any]:
    attempts = phase_record.get("attempts")
    if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict):
        return attempts[-1]
    return phase_record


def resume_approval_checkpoint(
    *,
    plan: Dict[str, Any],
    ledger: Dict[str, Any],
    repo_root: Path,
    state_file: Path,
    artifact_specs: Sequence[PathSpec],
    args: argparse.Namespace,
) -> Tuple[Optional[int], Optional[int]]:
    if ledger.get("decision") != "approval_checkpoint":
        return None, None
    checkpoint = ledger.get("approval_checkpoint")
    if not isinstance(checkpoint, dict):
        phases = ledger.get("phases") if isinstance(ledger.get("phases"), list) else []
        phase_index = int(ledger.get("current_phase_index") or 0)
        if phase_index < len(phases) and isinstance(phases[phase_index], dict):
            checkpoint = phases[phase_index].get("approval_checkpoint")
    if not isinstance(checkpoint, dict):
        ledger["next_output_state"] = "blocked"
        ledger["decision"] = "approval_checkpoint_blocked"
        ledger["checkpoint_blocking_reasons"] = ["approval_checkpoint_missing"]
        persist_ledger(ledger, state_file)
        return EXIT_BLOCKED, None

    phase_index = int(checkpoint.get("phase_index") or 0)
    next_phase_index = int(checkpoint.get("next_phase_index") or (phase_index + 1))
    phases = ledger.get("phases") if isinstance(ledger.get("phases"), list) else []
    phase_record = phases[phase_index] if phase_index < len(phases) else {}
    attempt_record = _latest_phase_attempt(phase_record) if isinstance(phase_record, dict) else {}

    current_git = filter_git_state_for_runner_artifacts(
        capture_git_state(repo_root, baseline_head=str(checkpoint.get("pre_commit_head") or "")),
        artifact_specs,
    )
    if current_git.get("branch") != checkpoint.get("pre_commit_branch"):
        ledger["next_output_state"] = "blocked"
        ledger["decision"] = "approval_checkpoint_blocked"
        ledger["checkpoint_blocking_reasons"] = ["branch_drift"]
        persist_ledger(ledger, state_file)
        return EXIT_BLOCKED, None

    current_head = str(current_git.get("head") or "")
    pre_commit_head = str(checkpoint.get("pre_commit_head") or "")
    expected_commit_hash = (
        str(getattr(args, "expected_commit_hash", "") or "").strip()
        or str(checkpoint.get("expected_commit_hash") or "").strip()
    )
    if current_head != pre_commit_head:
        ok, reasons = _verify_checkpoint_commit(
            repo_root=repo_root,
            checkpoint=checkpoint,
            current_head=current_head,
            expected_commit_hash=expected_commit_hash,
        )
        if not ok:
            ledger["next_output_state"] = "blocked"
            ledger["decision"] = "approval_checkpoint_blocked"
            ledger["checkpoint_blocking_reasons"] = reasons
            persist_ledger(ledger, state_file)
            return EXIT_BLOCKED, None
        if isinstance(phase_record, dict):
            phase_record["commit_hash"] = current_head
            phase_record["status"] = "commit_verified"
            phase_record["decision"] = "commit_verified"
        ledger["last_known_head"] = current_head
        ledger["commit_hash"] = current_head
        ledger["current_phase_index"] = next_phase_index
        ledger["approved_dirty_continuation"] = False
        ledger["approved_dirty_continuation_paths"] = []
        ledger.setdefault("previous_approved_commits", []).append(
            {
                "phase_id": checkpoint.get("phase_id"),
                "commit_hash": current_head,
                "reviewed_files": checkpoint.get("reviewed_files", []),
            }
        )
        ledger["approval_checkpoint"] = None
        ledger["next_output_state"] = "resume"
        ledger["decision"] = "commit_verified"
        persist_ledger(ledger, state_file)
        return None, next_phase_index

    if _allow_dirty_approved_continuation(plan=plan, args=args):
        packet = attempt_record.get("semantic_review_packet") if isinstance(attempt_record, dict) else None
        if not isinstance(packet, dict) and isinstance(phase_record, dict):
            packet = phase_record.get("semantic_review_packet")
        fingerprint_check = (
            check_semantic_review_input_fingerprint(
                repo_root=repo_root,
                packet=packet if isinstance(packet, dict) else {},
                current_git_state=current_git,
            )
            if isinstance(packet, dict)
            else {"stale": True, "blocking_reason": "semantic_review_packet_stale"}
        )
        if fingerprint_check.get("stale"):
            ledger["next_output_state"] = "blocked"
            ledger["decision"] = "approval_checkpoint_blocked"
            ledger["checkpoint_blocking_reasons"] = ["semantic_review_packet_stale"]
            ledger["semantic_review_fingerprint_check"] = fingerprint_check
            persist_ledger(ledger, state_file)
            return EXIT_BLOCKED, None
        ledger["approved_dirty_continuation"] = True
        ledger["approved_dirty_continuation_paths"] = _dedupe_sorted(
            checkpoint.get("reviewed_files", [])
        )
        ledger["current_phase_index"] = next_phase_index
        ledger["approval_checkpoint"] = None
        ledger["next_output_state"] = "resume"
        ledger["decision"] = "dirty_continuation_verified"
        ledger["semantic_review_fingerprint_check"] = fingerprint_check
        persist_ledger(ledger, state_file)
        return None, next_phase_index

    ledger["next_output_state"] = "approval_checkpoint"
    ledger["decision"] = "approval_checkpoint"
    persist_ledger(ledger, state_file)
    return EXIT_CHECKPOINT, None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a PhaseGate headless plan, optionally launch one executor "
            "through the packaged relay_headless_codex.py, capture git verifier state, and "
            "write a resumable ledger. This phase can write a semantic review "
            "packet and run a read-only reviewer. Staging or committing requires "
            "an approved commit gate and an authorized commit mode."
        ),
        epilog=(
            "Exit codes: 0=completed, 1=phase failed/verifier rejected, "
            "2=invalid input, 3=checkpoint, 4=blocked drift/scope risk, 5=timeout."
        ),
    )
    parser.add_argument("--plan-file", help="Path to the PhaseGate JSON plan.")
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Explicit target repository. Must be the git top-level directory.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for PhaseGate state and reports (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument("--state-file", default="", help="Optional explicit state file path.")
    parser.add_argument("--resume", default="", help="Resume from a previous state file.")
    parser.add_argument("--json", action="store_true", help="Print summary JSON to stdout.")
    parser.add_argument(
        "--semantic-review-mode",
        choices=tuple(sorted(SEMANTIC_REVIEW_MODES)),
        default=None,
        help=(
            "Semantic review mode after deterministic verification and validation: "
            "disabled is report-only for no-commit runs, manual_artifact writes a "
            "packet and waits for a JSON approval artifact, model_review runs a "
            "separate local read-only reviewer. Default: disabled."
        ),
    )
    parser.add_argument(
        "--manual-approval-artifact",
        default="",
        help=(
            "Optional path to a JSON approval artifact for --semantic-review-mode "
            "manual_artifact. Defaults to the phase manual artifact path."
        ),
    )
    parser.add_argument(
        "--allow-dirty-approved-continuation",
        action="store_true",
        help=(
            "Allow a multi-phase no-commit or prepare-command run to continue with "
            "unchanged dirty files that were approved by the semantic review gate."
        ),
    )
    parser.add_argument(
        "--expected-commit-hash",
        default="",
        help=(
            "Expected commit hash when resuming from an approval checkpoint after "
            "running the emitted commit commands."
        ),
    )
    parser.add_argument(
        "--allow-manual-validation",
        action="store_true",
        help="Run validation commands marked manual=true instead of recording a skip.",
    )
    parser.add_argument(
        "--allow-live-validation",
        action="store_true",
        help="Run validation commands marked live=true instead of recording a skip.",
    )
    parser.add_argument(
        "--executor-relay-mode",
        choices=("disabled", "local_exec"),
        default=DEFAULT_EXECUTOR_RELAY_MODE,
        help=(
            "Executor launch mode. 'disabled' persists the kickoff prompt only; "
            "'local_exec' runs Codex CLI with --executor-local-sandbox. "
            f"Default: {DEFAULT_EXECUTOR_RELAY_MODE}."
        ),
    )
    parser.add_argument(
        "--executor-model",
        choices=SUPPORTED_EXECUTOR_MODELS,
        default=None,
        help=(
            "Required for local_exec. Select explicitly by difficulty: gpt-6-sol "
            "for moderate through the most difficult work or unclear difficulty, "
            "or gpt-6-luna for straightforward work."
        ),
    )
    parser.add_argument(
        "--executor-local-sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="workspace-write",
        help="Codex sandbox for executor local_exec mode (default: workspace-write).",
    )
    parser.add_argument(
        "--executor-local-reasoning-effort",
        choices=(DEFAULT_EXECUTOR_LOCAL_REASONING_EFFORT,),
        default=DEFAULT_EXECUTOR_LOCAL_REASONING_EFFORT,
        help=(
            "model_reasoning_effort for executor local_exec mode "
            f"(default: {DEFAULT_EXECUTOR_LOCAL_REASONING_EFFORT})."
        ),
    )
    parser.add_argument(
        "--executor-local-service-tier",
        choices=(DEFAULT_EXECUTOR_LOCAL_SERVICE_TIER,),
        default=DEFAULT_EXECUTOR_LOCAL_SERVICE_TIER,
        help=(
            "service_tier for executor local_exec mode "
            f"(default: {DEFAULT_EXECUTOR_LOCAL_SERVICE_TIER})."
        ),
    )
    parser.add_argument(
        "--executor-local-timeout-seconds",
        type=float,
        default=DEFAULT_EXECUTOR_LOCAL_TIMEOUT_SECONDS,
        help=(
            "Max seconds to wait for executor local_exec completion "
            f"(default: {DEFAULT_EXECUTOR_LOCAL_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--executor-no-local-skip-git-repo-check",
        action="store_true",
        help="Disable --skip-git-repo-check forwarding for executor local_exec mode.",
    )
    parser.add_argument(
        "--executor-codex-bin",
        default="codex",
        help="Codex executable for executor local_exec mode (default: codex).",
    )
    parser.add_argument(
        "--reviewer-model",
        choices=SUPPORTED_EXECUTOR_MODELS,
        default=None,
        help="Explicit GPT-6 reviewer tier required for model_review.",
    )
    parser.add_argument(
        "--reviewer-local-reasoning-effort",
        choices=(DEFAULT_REVIEWER_LOCAL_REASONING_EFFORT,),
        default=DEFAULT_REVIEWER_LOCAL_REASONING_EFFORT,
        help=(
            "model_reasoning_effort for reviewer local_exec mode "
            f"(default: {DEFAULT_REVIEWER_LOCAL_REASONING_EFFORT})."
        ),
    )
    parser.add_argument(
        "--reviewer-local-service-tier",
        choices=(DEFAULT_REVIEWER_LOCAL_SERVICE_TIER,),
        default=DEFAULT_REVIEWER_LOCAL_SERVICE_TIER,
        help=(
            "service_tier for reviewer local_exec mode "
            f"(default: {DEFAULT_REVIEWER_LOCAL_SERVICE_TIER})."
        ),
    )
    parser.add_argument(
        "--reviewer-local-timeout-seconds",
        type=float,
        default=DEFAULT_REVIEWER_LOCAL_TIMEOUT_SECONDS,
        help=(
            "Max seconds to wait for reviewer local_exec completion "
            f"(default: {DEFAULT_REVIEWER_LOCAL_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--reviewer-codex-bin",
        default="codex",
        help="Codex executable for reviewer local_exec mode (default: codex).",
    )
    return parser


def _ledger_semantic_review_mode(ledger: Dict[str, Any]) -> str:
    mode = ledger.get("semantic_review_mode")
    if isinstance(mode, str) and mode in SEMANTIC_REVIEW_MODES:
        return mode
    phases = ledger.get("phases")
    if isinstance(phases, list):
        phase_index = int(ledger.get("current_phase_index") or 0)
        candidates: List[Dict[str, Any]] = []
        if 0 <= phase_index < len(phases) and isinstance(phases[phase_index], dict):
            candidates.append(phases[phase_index])
        candidates.extend([phase for phase in reversed(phases) if isinstance(phase, dict)])
        for phase in candidates:
            phase_mode = phase.get("semantic_review_mode")
            if isinstance(phase_mode, str) and phase_mode in SEMANTIC_REVIEW_MODES:
                return phase_mode
    return DEFAULT_SEMANTIC_REVIEW_MODE


def _ensure_fresh_semantic_review_mode(args: argparse.Namespace) -> str:
    if args.semantic_review_mode is None:
        args.semantic_review_mode = DEFAULT_SEMANTIC_REVIEW_MODE
    return str(args.semantic_review_mode)


def _ensure_resume_semantic_review_mode(
    args: argparse.Namespace,
    ledger: Dict[str, Any],
) -> str:
    if args.semantic_review_mode is None:
        args.semantic_review_mode = _ledger_semantic_review_mode(ledger)
    return str(args.semantic_review_mode)


def _validate_semantic_review_runtime_args(args: argparse.Namespace) -> None:
    if args.executor_relay_mode == "local_exec" and not args.executor_model:
        raise PhaseGateError(
            "--executor-model is required for local_exec; select gpt-6-sol "
            "or gpt-6-luna by phase difficulty"
        )
    if args.semantic_review_mode == "model_review" and not args.reviewer_model:
        raise PhaseGateError("--reviewer-model is required for model_review")


def _summary(exit_code: int, ledger: Optional[Dict[str, Any]], state_file: Optional[Path]) -> Dict[str, Any]:
    return {
        "exit_code": exit_code,
        "exit_code_name": exit_code_name(exit_code),
        "run_id": ledger.get("run_id") if ledger else None,
        "state_file": str(state_file) if state_file else None,
        "next_output_state": ledger.get("next_output_state") if ledger else None,
        "decision": ledger.get("decision") if ledger else None,
        "validation_summary": ledger.get("validation_summary") if ledger else None,
        "semantic_review_mode": ledger.get("semantic_review_mode") if ledger else None,
        "semantic_review_decision": ledger.get("semantic_review_decision") if ledger else None,
        "commit_mode": ledger.get("commit_mode") if ledger else None,
        "commit_hash": ledger.get("commit_hash") if ledger else None,
        "commit_gate_status": ledger.get("commit_gate_status") if ledger else None,
        "approved_commit_commands": ledger.get("approved_commit_commands") if ledger else None,
        "followups_used": ledger.get("followups_used") if ledger else None,
        "followups_exhausted": ledger.get("followups_exhausted") if ledger else None,
        "executor_relay_mode": (
            (ledger.get("phases") or [{}])[0].get("executor_relay_mode")
            if ledger and ledger.get("phases")
            else None
        ),
        "executor_relay_exit_code": (
            (ledger.get("phases") or [{}])[0].get("executor_relay_exit_code")
            if ledger and ledger.get("phases")
            else None
        ),
        "blocked_reasons": (
            (ledger.get("latest_verifier_report") or {}).get("blocked_reasons", [])
            if ledger
            else []
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_INVALID_INPUT

    ledger: Optional[Dict[str, Any]] = None
    state_file: Optional[Path] = None
    exit_code = EXIT_OK

    try:
        repo_root = validate_repo_root(args.repo_root)
        output_dir = _resolve_under_repo(repo_root, args.output_dir)
        if float(args.executor_local_timeout_seconds) <= 0:
            raise PhaseGateError("--executor-local-timeout-seconds must be greater than zero")
        if float(args.reviewer_local_timeout_seconds) <= 0:
            raise PhaseGateError("--reviewer-local-timeout-seconds must be greater than zero")

        if args.resume:
            state_file = _resolve_under_repo(repo_root, args.resume)
            ledger = load_resume_state(state_file)
            _ensure_resume_semantic_review_mode(args, ledger)
            _validate_semantic_review_runtime_args(args)
            current_git = capture_git_state(repo_root)
            validate_resume_state(ledger, repo_root=repo_root, current_git_state=current_git)
            if (
                args.semantic_review_mode == "disabled"
                and (ledger.get("commit_mode") or COMMIT_MODE_NO_COMMIT)
                != COMMIT_MODE_NO_COMMIT
            ):
                raise PhaseGateError(
                    "semantic review disabled is allowed only for report-only no-commit runs"
                )
            resume_review_exit = resume_manual_semantic_review_checkpoint(
                ledger=ledger,
                repo_root=repo_root,
                state_file=state_file,
                args=args,
            )
            if resume_review_exit is not None:
                exit_code = resume_review_exit
            elif ledger.get("decision") == "approval_checkpoint":
                plan_file = _resolve_under_repo(repo_root, str(ledger.get("plan_file") or ""))
                plan = load_plan_file(plan_file, repo_root=repo_root)
                output_dir = Path(str(ledger.get("output_dir") or output_dir))
                artifact_specs = runner_artifact_specs(
                    repo_root=repo_root,
                    output_dir=output_dir,
                    state_file=state_file,
                )
                checkpoint_exit, next_phase_index = resume_approval_checkpoint(
                    plan=plan,
                    ledger=ledger,
                    repo_root=repo_root,
                    state_file=state_file,
                    artifact_specs=artifact_specs,
                    args=args,
                )
                if checkpoint_exit is not None:
                    exit_code = checkpoint_exit
                elif next_phase_index is not None and next_phase_index < len(plan.get("phases", [])):
                    exit_code = execute_plan_phases(
                        plan=plan,
                        ledger=ledger,
                        repo_root=repo_root,
                        state_file=state_file,
                        output_dir=output_dir,
                        artifact_specs=artifact_specs,
                        start_index=next_phase_index,
                        args=args,
                    )
                else:
                    ledger["next_output_state"] = "complete"
                    ledger["decision"] = "completed"
                    persist_ledger(ledger, state_file)
                    exit_code = EXIT_OK
            else:
                ledger["dirty_files_after"] = current_git.get("changed_paths", [])
                ledger["next_output_state"] = "checkpoint"
                ledger["decision"] = "checkpointed"
                persist_ledger(ledger, state_file)
        else:
            if not args.plan_file:
                raise PhaseGateError("--plan-file is required unless --resume is used")
            plan_file = _resolve_under_repo(repo_root, args.plan_file)
            plan = load_plan_file(plan_file, repo_root=repo_root)
            _ensure_fresh_semantic_review_mode(args)
            _validate_semantic_review_runtime_args(args)
            if (
                args.semantic_review_mode == "disabled"
                and plan.get("commit_mode") != COMMIT_MODE_NO_COMMIT
            ):
                raise PhaseGateError(
                    "semantic review disabled is allowed only for report-only no-commit runs"
                )
            state_file = (
                _resolve_under_repo(repo_root, args.state_file)
                if args.state_file
                else output_dir / "phase_gate_state.json"
            )
            artifact_specs = runner_artifact_specs(
                repo_root=repo_root,
                output_dir=output_dir,
                state_file=state_file,
            )
            git_state = filter_git_state_for_runner_artifacts(
                capture_git_state(repo_root),
                artifact_specs,
            )
            first_phase = plan["phases"][0]
            verifier_report = classify_git_scope(
                repo_root=repo_root,
                phase=first_phase,
                baseline_state=git_state,
                current_state=git_state,
                expected_branch=plan["branch"],
            )
            ledger = create_initial_ledger(
                plan=plan,
                plan_file=plan_file,
                repo_root=repo_root,
                output_dir=output_dir,
                git_state=git_state,
                verifier_report=verifier_report,
            )
            verifier_path = Path(ledger["phases"][0]["verifier_report_path"])
            if verifier_report["blocked"]:
                _write_json_file(verifier_path, verifier_report)
                ledger["next_output_state"] = "blocked"
                ledger["decision"] = "blocked"
                exit_code = EXIT_BLOCKED
                persist_ledger(ledger, state_file)
            else:
                exit_code = execute_plan_phases(
                    plan=plan,
                    ledger=ledger,
                    repo_root=repo_root,
                    state_file=state_file,
                    output_dir=output_dir,
                    artifact_specs=artifact_specs,
                    start_index=0,
                    args=args,
                )
                persist_ledger(ledger, state_file)
    except PhaseGateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.json:
            print(json.dumps(_summary(EXIT_INVALID_INPUT, ledger, state_file), sort_keys=True))
        return EXIT_INVALID_INPUT
    except GitCommandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.json:
            print(json.dumps(_summary(EXIT_BLOCKED, ledger, state_file), sort_keys=True))
        return EXIT_BLOCKED

    if args.json:
        print(json.dumps(_summary(exit_code, ledger, state_file), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
