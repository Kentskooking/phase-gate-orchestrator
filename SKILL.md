---
name: phase-gate-orchestrator
description: Orchestrate phased repository work with headless executors while the orchestrator plans, reviews actual code, requests repairs, commits approved work, and continues through the plan. Use for agent-led phase execution, manual handoffs, or scripted orchestration with dirty-worktree protection and resumable review gates.
---

# Phase Gate Orchestrator

## Prerequisites

The packaged execution workflows require **Python 3.8+**. Both scripts use only the Python standard library; no `pip` packages or `requirements.txt` are needed.

Before launching either script, check for a working interpreter with `python3 --version`, `python --version`, or, on Windows, `py -3 --version`. Use the command or interpreter path that succeeds and reports Python 3.8 or newer in place of `python` in the examples below.

If no compatible interpreter is detected, tell the user that Python is required and ask them to install it or provide the path to an existing installation. Planning and manual handoffs can continue, but verify the interpreter before launching either packaged script.

## Choose The Workflow

- **Default: agent-led orchestration.** The orchestrator creates the plan, launches headless executors, reviews and approves their work, handles commits directly, and continues through every phase. The orchestrator controls the loop and can adapt the plan within the user's scope.
- **Manual handoff:** use when the user wants copy-ready executor prompts and review verdicts exchanged in chat.
- **Scripted headless orchestration:** use when the user requests the Python runner to manage execution, persisted validation/review artifacts, and resume checkpoints. All existing runner modes remain available.

A headless executor is part of the default workflow; using it does not require handing the orchestration loop to `phase_gate_headless.py`.

## Run The Default Workflow

1. Inspect live Git status, branch, HEAD, and staged changes. Create and persist a new phase-gate plan covering the requested work in bounded phases, with write scope and validation for each phase. Maintain the phase ledger below.
2. Select an executor tier for the current phase. Write its prompt using [references/kickoff-template.md](references/kickoff-template.md), then launch it through the packaged `scripts/relay_headless_codex.py`. The executor implements and must not stage or commit; the orchestrator owns review and commits.
3. Review the actual work locally: inspect Git status, the full in-scope diff and changed files, and run appropriate validation. Executor summaries alone do not establish correctness. The orchestrator is also the reviewer and approver.
4. If issues remain, send a focused follow-up using [references/follow-up-template.md](references/follow-up-template.md) and launch another executor run with the phase context and existing dirty work. Repeat review after repairs. The relay starts a new execution each time, so include the relevant prior findings in its prompt.
5. Once approved, stage only reviewed files, run the Git commit directly, and record the commit hash and validation results in the ledger. A user request for this plan/review/commit workflow authorizes its reviewed phase commits; carry that authorization across the plan without asking again per phase. Respect any narrower user instruction, such as no commits or approval before each commit.
6. Launch the next phase's executor and continue until the whole plan is implemented. When a roadblock persists, revise the approach or remaining phases within the authorized scope and record why. Pause only for missing authorization, a decision outside that scope, or a blocker that needs user input.

Use the active Python interpreter to launch each executor (replace paths and the selected tier):

```bash
python "<installed-skill-directory>/scripts/relay_headless_codex.py" \
  --repo-root "/absolute/path/to/target-repository" \
  --prompt-file "/absolute/path/to/phase-prompt.md" \
  --output-file "/absolute/path/to/phase-result.json" \
  --model <selected-gpt-6-tier> \
  --local-sandbox workspace-write \
  --local-reasoning-effort xhigh \
  --local-service-tier fast \
  --json
```

The manual templates also serve as prompt/review formats for this loop. Their copy-ready output and the scripted runner's artifact/checkpoint steps apply only when using those workflows.

## Resolve Packaged Resources

Treat the directory containing this `SKILL.md` as the skill directory. Invoke the Python files under that directory's `scripts/` folder; never look for these scripts in the target repository.

Always pass `--repo-root` with the absolute Git top-level directory being operated on. The runner passes that target repository to the packaged relay while keeping script lookup anchored to the installed skill.

## Maintain The Phase Ledger

Track:

- absolute target repository and branch
- current phase name or number
- previous approved commits
- expected in-scope dirty paths
- unrelated dirty paths to preserve
- validation commands and manual or live checks
- caveats and environmental limits
- next output: kickoff, approval, follow-up, checkpoint, or wrap-up

Infer missing details from conversation history and live Git state. Ask only when missing information would make a commit gate unsafe.

## Select The Executor Tier

Honor an executor model or tier explicitly requested by the user until the user changes or withdraws it. Otherwise choose per phase:

- `gpt-6-sol` for moderately difficult through the most difficult work, ambiguous, high-risk, or cross-cutting work, and when difficulty is unclear
- `gpt-6-luna` for straightforward, narrow, low-risk, or mechanical work

For direct relay runs, pass the selected model explicitly with `--model`, `--local-reasoning-effort xhigh`, and `--local-service-tier fast` as above. When using the scripted runner, the equivalent flags are:

```text
--executor-model <selected-gpt-6-tier>
--executor-local-reasoning-effort xhigh
--executor-local-service-tier fast
```

Do not rely on a default model. If the installed Codex CLI rejects `xhigh` or `fast`, stop before substantial executor work and report the incompatibility; do not silently downgrade.

For the scripted runner's `model_review`, also select and pass an explicit GPT-6 reviewer tier with `--reviewer-model`, `--reviewer-local-reasoning-effort xhigh`, and `--reviewer-local-service-tier fast`. Prefer `manual_artifact` unless the user requests model review; in that mode the orchestrator reviews and writes the verdict artifact.

## Prepare A Scripted Headless Run (Optional)

1. Inspect `git status --short --branch --untracked-files=all` and `git diff --cached --name-only` in the target repository.
2. Copy [references/headless-plan-template.json](references/headless-plan-template.json) and replace every placeholder.
3. Set plan `repo_root` to the same absolute path passed with `--repo-root`.
4. Default to one bounded phase, `commit_mode` `prepare-commit-command-only`, `allow_dirty_approved_continuation` `false`, and `max_followups_per_phase` `0`.
5. Keep `allowed_write_paths` narrow. Put only intentional in-scope pre-existing changes in `expected_dirty_paths`; leave unrelated dirty work out of both lists.
6. Use `--semantic-review-mode manual_artifact` for an implementation run.

Use the active Python interpreter and the packaged runner. Replace `<installed-skill-directory>` with the absolute directory containing this file:

```bash
python "<installed-skill-directory>/scripts/phase_gate_headless.py" \
  --plan-file "/absolute/path/to/phase-gate-plan.json" \
  --repo-root "/absolute/path/to/target-repository" \
  --output-dir ".phase-gate/my-run" \
  --executor-relay-mode local_exec \
  --executor-local-sandbox workspace-write \
  --executor-model gpt-6-sol \
  --executor-local-reasoning-effort xhigh \
  --executor-local-service-tier fast \
  --semantic-review-mode manual_artifact \
  --json
```

For a prompt-rendering or validation-only probe, explicitly disable execution and use a `no-commit` plan:

```bash
python "<installed-skill-directory>/scripts/phase_gate_headless.py" \
  --plan-file "/absolute/path/to/no-op-plan.json" \
  --repo-root "/absolute/path/to/target-repository" \
  --output-dir ".phase-gate/no-op" \
  --executor-relay-mode disabled \
  --semantic-review-mode disabled \
  --json
```

Resume from a checkpoint with the same packaged runner and explicit target repository:

```bash
python "<installed-skill-directory>/scripts/phase_gate_headless.py" \
  --resume "/absolute/path/to/phase_gate_state.json" \
  --repo-root "/absolute/path/to/target-repository" \
  --expected-commit-hash "<approved-commit-hash>" \
  --executor-model gpt-6-sol \
  --executor-local-reasoning-effort xhigh \
  --executor-local-service-tier fast \
  --json
```

## Review Scripted Headless Results

Treat exit `3` with `semantic_review_pending` as the normal manual review checkpoint. Inspect:

- the semantic review packet
- pre-executor, post-executor, and post-validation verifier reports
- validation results
- `git diff --stat`
- the full diff and targeted file reads for every changed in-scope file

Write the approval or rejection JSON to the `manual_approval_artifact_path` recorded in state, then resume the runner.

On approval, let the runner emit targeted commit commands. With user authorization for phase commits, run those commands directly, stage only reviewed files, and record the resulting hash. Ask only if commit authorization is missing; do not re-request authorization already given for the plan.

On rejection, resume once to record the blocked decision. Create a fresh bounded follow-up plan with the rejected dirty files in `expected_dirty_paths`, narrow `allowed_write_paths` to the repair scope, and keep unrelated changes excluded.

## Preserve Safety Gates

- Block unexpected branch or HEAD drift, unexpected commits, staged changes, forbidden paths, new out-of-scope dirty paths, and unapproved pre-existing changes inside the write scope.
- When resuming a scripted review, recompute semantic-review fingerprints and reject stale review inputs.
- Preserve unrelated user work; never revert or include it in commit commands.
- Use `allow_dirty_approved_continuation` only when the user explicitly wants reviewed dirty files carried across phases.
- Use `auto-commit-after-gate` only with explicit user authorization for automated commits.
- Treat executor and reviewer reports as summaries, not proof; verify locally.

Scripted runner commit modes:

- `prepare-commit-command-only`: emit reviewed, targeted commands after semantic approval.
- `no-commit`: finish without commit authorization; useful for reports and disabled probes.
- `auto-commit-after-gate`: stage only reviewed files and commit after approval; require explicit user authorization.

Scripted runner exit codes:

- `0`: completed
- `1`: executor or required validation failed
- `2`: invalid input or plan
- `3`: operator checkpoint
- `4`: blocked by drift, scope, staging, stale review, or commit-gate risk
- `5`: executor, validation, or reviewer timeout

## Create Manual Kickoffs And Verdicts

Read [references/kickoff-template.md](references/kickoff-template.md) for a kickoff. Include the absolute target repository, objective, non-goals, write scope, validation, no-stage/no-commit rules, shared-worktree warning, and final report format.

When reviewing executor work, inspect live Git status, recent commits when relevant, diff stat, full in-scope diff, changed source/tests/docs/config, and appropriate validation or smoke checks.

Use [references/review-verdict-template.md](references/review-verdict-template.md) after approval. Clearly list reviewed files, exact validation results, and targeted `git add -- ...` and `git commit -m ...` commands.

Use [references/follow-up-template.md](references/follow-up-template.md) after rejection. Lead with findings ordered by severity, withhold commit approval, and provide one copy-ready same-phase repair prompt.

## References

- [references/headless-plan-template.json](references/headless-plan-template.json): generic one-phase plan
- [references/kickoff-template.md](references/kickoff-template.md): manual executor kickoff
- [references/review-verdict-template.md](references/review-verdict-template.md): approval and commit gate
- [references/follow-up-template.md](references/follow-up-template.md): rejected-phase follow-up
