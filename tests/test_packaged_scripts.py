from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"


def load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


phase_gate = load_script_module("phase_gate_headless_test_module", "phase_gate_headless.py")
relay = load_script_module("relay_headless_codex_test_module", "relay_headless_codex.py")


class PackagedScriptResolutionTests(unittest.TestCase):
    def executor_args(self, model: str = "gpt-6-sol") -> argparse.Namespace:
        return argparse.Namespace(
            executor_model=model,
            executor_local_sandbox="workspace-write",
            executor_local_reasoning_effort="xhigh",
            executor_local_service_tier="fast",
            executor_codex_bin="codex",
            executor_local_timeout_seconds=120.0,
            executor_no_local_skip_git_repo_check=False,
        )

    def test_relay_path_is_packaged_sibling_not_target_repo_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory) / "target-repository"
            misleading_script = repo_root.joinpath("scripts", "relay_headless_codex.py")
            misleading_script.parent.mkdir(parents=True)
            misleading_script.write_text("raise RuntimeError('wrong relay')\n", encoding="utf-8")

            command = phase_gate.build_executor_relay_command(
                repo_root=repo_root,
                prompt_file=repo_root / "prompt.md",
                output_file=repo_root / "result.json",
                local_artifacts_dir=repo_root / "artifacts",
                args=self.executor_args(),
            )

            packaged_relay = phase_gate.packaged_relay_script_path()
            self.assertEqual(packaged_relay, SCRIPTS_DIR / "relay_headless_codex.py")
            self.assertEqual(Path(command[1]), packaged_relay)
            self.assertNotEqual(Path(command[1]), misleading_script)
            self.assertTrue(packaged_relay.is_file())

    def test_executor_command_forwards_explicit_model_and_tier_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory)
            command = phase_gate.build_executor_relay_command(
                repo_root=repo_root,
                prompt_file=repo_root / "prompt.md",
                output_file=repo_root / "result.json",
                local_artifacts_dir=repo_root / "artifacts",
                args=self.executor_args("gpt-6-sol"),
            )

            self.assertEqual(command[command.index("--model") + 1], "gpt-6-sol")
            self.assertEqual(command[command.index("--repo-root") + 1], str(repo_root))
            self.assertEqual(
                command[command.index("--local-reasoning-effort") + 1], "xhigh"
            )
            self.assertEqual(command[command.index("--local-service-tier") + 1], "fast")

    def test_reviewer_command_forwards_explicit_model_and_tier_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory)
            args = argparse.Namespace(
                reviewer_model="gpt-6-luna",
                reviewer_local_reasoning_effort="xhigh",
                reviewer_local_service_tier="fast",
                reviewer_codex_bin="codex",
                reviewer_local_timeout_seconds=120.0,
            )
            command = phase_gate.build_reviewer_relay_command(
                repo_root=repo_root,
                prompt_file=repo_root / "review.md",
                output_file=repo_root / "review.json",
                local_artifacts_dir=repo_root / "review-artifacts",
                args=args,
            )

            self.assertEqual(Path(command[1]), phase_gate.packaged_relay_script_path())
            self.assertEqual(command[command.index("--model") + 1], "gpt-6-luna")
            self.assertEqual(
                command[command.index("--local-reasoning-effort") + 1], "xhigh"
            )
            self.assertEqual(command[command.index("--local-service-tier") + 1], "fast")


class RelayCommandTests(unittest.TestCase):
    def test_default_relay_artifacts_stay_beside_requested_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_file = Path(temporary_directory) / "requested-output" / "result.json"
            self.assertEqual(
                relay._default_local_artifacts_dir(output_file),
                output_file.parent.resolve() / "local-exec-artifacts",
            )

    def test_codex_executable_resolution_supports_windows_command_wrappers(self) -> None:
        with mock.patch.object(
            relay.shutil,
            "which",
            return_value=r"C:\tools\codex.cmd",
        ):
            self.assertEqual(
                relay._resolve_codex_executable("codex"),
                r"C:\tools\codex.cmd",
            )

    def test_codex_command_forwards_explicit_model_reasoning_and_service_tier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory)
            command = relay._build_local_exec_command(
                codex_bin="codex",
                model="gpt-6-sol",
                sandbox="workspace-write",
                reasoning_effort="xhigh",
                service_tier="fast",
                repo_root=repo_root,
                output_last_message_path=repo_root / "last-message.txt",
                skip_git_repo_check=True,
            )

            self.assertEqual(command[command.index("--model") + 1], "gpt-6-sol")
            self.assertIn("model_reasoning_effort=xhigh", command)
            self.assertIn("service_tier=fast", command)
            self.assertEqual(command[command.index("--cd") + 1], str(repo_root.resolve()))


if __name__ == "__main__":
    unittest.main()
