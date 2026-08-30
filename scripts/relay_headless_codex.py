#!/usr/bin/env python3
"""Run one headless Codex executor in an explicitly selected repository."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


SUPPORTED_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
DEFAULT_LOCAL_TIMEOUT_SECONDS = 900
DEFAULT_LOCAL_REASONING_EFFORT = "xhigh"
DEFAULT_LOCAL_SERVICE_TIER = "fast"
LOCAL_SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")

EXIT_OK = 0
EXIT_NOT_OK = 1
EXIT_TIMEOUT = 2
EXIT_INVALID = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one local Codex executor in --repo-root and write a structured "
            "output JSON file."
        )
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Explicit target repository for the Codex executor.",
    )
    parser.add_argument(
        "--prompt-file",
        required=True,
        help="Path to a UTF-8 text file containing the executor prompt.",
    )
    parser.add_argument(
        "--output-file",
        required=True,
        help="Path to write the structured relay output JSON.",
    )
    parser.add_argument(
        "--model",
        required=True,
        choices=SUPPORTED_MODELS,
        help=(
            "Explicit GPT-5.6 executor tier selected for the task: sol for the most "
            "difficult work, terra for moderate or unclear difficulty, or luna for "
            "straightforward work."
        ),
    )
    parser.add_argument(
        "--local-sandbox",
        choices=LOCAL_SANDBOX_CHOICES,
        default="workspace-write",
        help="Codex sandbox for the executor (default: workspace-write).",
    )
    parser.add_argument(
        "--local-reasoning-effort",
        choices=(DEFAULT_LOCAL_REASONING_EFFORT,),
        default=DEFAULT_LOCAL_REASONING_EFFORT,
        help="Codex reasoning effort; Phase Gate requires xhigh.",
    )
    parser.add_argument(
        "--local-service-tier",
        choices=(DEFAULT_LOCAL_SERVICE_TIER,),
        default=DEFAULT_LOCAL_SERVICE_TIER,
        help="Codex service tier; Phase Gate requires fast.",
    )
    parser.add_argument(
        "--local-skip-git-repo-check",
        dest="local_skip_git_repo_check",
        action="store_true",
        help="Pass --skip-git-repo-check to codex exec (default: enabled).",
    )
    parser.add_argument(
        "--no-local-skip-git-repo-check",
        dest="local_skip_git_repo_check",
        action="store_false",
        help="Do not pass --skip-git-repo-check to codex exec.",
    )
    parser.set_defaults(local_skip_git_repo_check=True)
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable (default: codex).",
    )
    parser.add_argument(
        "--local-timeout-seconds",
        type=float,
        default=DEFAULT_LOCAL_TIMEOUT_SECONDS,
        help=(
            "Maximum seconds to wait for codex exec "
            f"(default: {DEFAULT_LOCAL_TIMEOUT_SECONDS})."
        ),
    )
    parser.add_argument(
        "--local-artifacts-dir",
        default="",
        help=(
            "Directory for local executor artifacts. Relative paths are resolved "
            "under --repo-root. Default: local-exec-artifacts beside --output-file."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the structured summary JSON to stdout.",
    )
    return parser


def _write_json_file(path_value: str, payload: Dict[str, Any]) -> None:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_local_exec_command(
    *,
    codex_bin: str,
    model: str,
    sandbox: str,
    reasoning_effort: str,
    service_tier: str,
    repo_root: Path,
    output_last_message_path: Path,
    skip_git_repo_check: bool,
) -> List[str]:
    command = [
        codex_bin,
        "exec",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "--model",
        model,
        "-c",
        f"model_reasoning_effort={reasoning_effort}",
        "-c",
        f"service_tier={service_tier}",
    ]
    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    command.extend(
        [
            "--cd",
            str(repo_root.resolve()),
            "--output-last-message",
            str(output_last_message_path.resolve()),
            "-",
        ]
    )
    return command


def _resolve_codex_executable(codex_bin: str) -> str:
    """Resolve command wrappers such as codex.cmd on native Windows."""

    return shutil.which(codex_bin) or codex_bin


def _default_local_artifacts_dir(output_file: Path) -> Path:
    """Keep standalone relay artifacts beside its explicitly requested output."""

    return output_file.expanduser().resolve().parent / "local-exec-artifacts"


def _render_payload(
    *,
    status: str,
    model: str,
    result_summary: Optional[Dict[str, Any]],
    event_timeline: Optional[List[Dict[str, Any]]],
    error: Optional[str] = None,
    last_message_path: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": status,
        "model": model,
        "request_metadata": {
            "relay_mode": "local_exec",
            "reasoning_effort": DEFAULT_LOCAL_REASONING_EFFORT,
            "service_tier": DEFAULT_LOCAL_SERVICE_TIER,
        },
        "result_summary": result_summary
        if result_summary is not None
        else {
            "ok": None,
            "exit_code": None,
            "stderr": None,
            "parsed_json": None,
            "stdout": None,
        },
        "event_timeline": event_timeline or [],
    }
    if last_message_path:
        payload["last_message_path"] = last_message_path
    if error:
        payload["error"] = error
    return payload


def _run_local_exec(
    *,
    prompt_text: str,
    model: str,
    repo_root: Path,
    codex_bin: str,
    sandbox: str,
    reasoning_effort: str,
    service_tier: str,
    skip_git_repo_check: bool,
    timeout_seconds: float,
    artifacts_dir: Path,
) -> tuple[Dict[str, Any], int]:
    if not repo_root.exists() or not repo_root.is_dir():
        raise ValueError(f"--repo-root does not exist or is not a directory: {repo_root}")

    if not artifacts_dir.is_absolute():
        artifacts_dir = repo_root / artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    last_message_path = artifacts_dir / f"last_message_{timestamp}.txt"
    last_message_path_text = str(last_message_path.resolve())

    resolved_codex_bin = _resolve_codex_executable(codex_bin)
    command = _build_local_exec_command(
        codex_bin=resolved_codex_bin,
        model=model,
        sandbox=sandbox,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        repo_root=repo_root,
        output_last_message_path=last_message_path,
        skip_git_repo_check=skip_git_repo_check,
    )
    timeline: List[Dict[str, Any]] = [
        {"event": "status", "message": "Preparing local Codex execution."},
        {"event": "status", "message": "Running codex exec in the target repository."},
    ]

    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            input=prompt_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        error_message = f"Codex executable not found: {codex_bin}"
        timeline.append({"event": "failed", "ok": False, "message": error_message})
        return (
            _render_payload(
                status="error",
                model=model,
                result_summary={
                    "ok": False,
                    "exit_code": None,
                    "stderr": error_message,
                    "parsed_json": None,
                    "stdout": "",
                },
                event_timeline=timeline,
                error=error_message,
                last_message_path=last_message_path_text,
            ),
            EXIT_INVALID,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        last_message = ""
        if last_message_path.exists():
            last_message = last_message_path.read_text(encoding="utf-8", errors="replace")
        error_message = f"Codex local_exec timed out after {timeout_seconds:g} seconds"
        timeline.append({"event": "failed", "ok": False, "message": error_message})
        return (
            _render_payload(
                status="timeout",
                model=model,
                result_summary={
                    "ok": False,
                    "exit_code": None,
                    "stderr": stderr.strip() or error_message,
                    "parsed_json": None,
                    "stdout": last_message.strip() or stdout.strip(),
                },
                event_timeline=timeline,
                error=error_message,
                last_message_path=last_message_path_text,
            ),
            EXIT_TIMEOUT,
        )

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    last_message = ""
    if last_message_path.exists():
        last_message = last_message_path.read_text(encoding="utf-8", errors="replace")
    ok = result.returncode == 0
    timeline.append(
        {"event": "completed" if ok else "failed", "ok": ok, "exit_code": result.returncode}
    )
    return (
        _render_payload(
            status="completed" if ok else "failed",
            model=model,
            result_summary={
                "ok": ok,
                "exit_code": result.returncode,
                "stderr": stderr,
                "parsed_json": None,
                "stdout": last_message.strip() or stdout.strip(),
            },
            event_timeline=timeline,
            last_message_path=last_message_path_text,
        ),
        EXIT_OK if ok else EXIT_NOT_OK,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_INVALID

    try:
        if args.local_timeout_seconds <= 0:
            raise ValueError("--local-timeout-seconds must be greater than zero")
        prompt_text = Path(args.prompt_file).read_text(encoding="utf-8")
        if not prompt_text.strip():
            raise ValueError(f"Prompt file is empty: {args.prompt_file}")
        payload, exit_code = _run_local_exec(
            prompt_text=prompt_text,
            model=args.model,
            repo_root=Path(args.repo_root).expanduser().resolve(),
            codex_bin=args.codex_bin,
            sandbox=args.local_sandbox,
            reasoning_effort=args.local_reasoning_effort,
            service_tier=args.local_service_tier,
            skip_git_repo_check=bool(args.local_skip_git_repo_check),
            timeout_seconds=float(args.local_timeout_seconds),
            artifacts_dir=(
                Path(args.local_artifacts_dir)
                if args.local_artifacts_dir
                else _default_local_artifacts_dir(Path(args.output_file))
            ),
        )
    except Exception as exc:
        payload = _render_payload(
            status="error",
            model=str(getattr(args, "model", "")),
            result_summary=None,
            event_timeline=[],
            error=str(exc),
        )
        exit_code = EXIT_INVALID

    try:
        _write_json_file(args.output_file, payload)
    except Exception as exc:
        print(f"ERROR: failed to write output file: {exc}", file=sys.stderr)
        return EXIT_INVALID

    if args.json:
        print(json.dumps({"exit_code": exit_code, **payload}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
