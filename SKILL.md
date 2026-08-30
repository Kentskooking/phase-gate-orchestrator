---
name: phase-gate-orchestrator
description: Orchestrate phased repository work through a separate executor agent with dirty-worktree protection, semantic review, resumable checkpoints, explicit commit authorization, and targeted commit gates. Use when Codex should plan or launch a bounded implementation phase, verify executor changes locally, withhold commits until review passes, issue a focused same-phase follow-up, or resume an approved phase without implementing the executor work itself.
---

# Phase Gate Orchestrator

## Resolve Packaged Resources

Treat the directory containing this `SKILL.md` as the skill directory. Invoke the Python files under that directory's `scripts/` folder; never look for these scripts in the target repository.

Always pass `--repo-root` with the absolute Git top-level directory being operated on. The runner passes that target repository to the packaged relay while keeping script lookup anchored to the installed skill.

## Choose The Workflow

- Use the manual workflow to create copy-ready executor prompts and review returned work in chat.
- Use the headless workflow to persist prompts, verifier reports, validation results, semantic review packets, commit gates, and resume state.
- Keep the orchestrator and executor roles separate. The executor implements; the orchestrator plans, verifies, reviews, and gates commits.
- Tell the executor not to stage or commit unless the user has already provided explicit authorization for that exact action.

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

- `gpt-5.6-sol` for the most difficult, ambiguous, high-risk, or cross-cutting work
- `gpt-5.6-terra` for moderately difficult work and when difficulty is unclear
- `gpt-5.6-luna` for straightforward, narrow, low-risk, or mechanical work

For every local executor run, pass the selected model explicitly with:

```text
--executor-model <selected-gpt-5.6-tier>
--executor-local-reasoning-effort xhigh
--executor-local-service-tier fast
```

Do not rely on a default model. If the installed Codex CLI rejects `xhigh` or `fast`, stop before substantial executor work and report the incompatibility; do not silently downgrade.

For `model_review`, also select and pass an explicit GPT-5.6 reviewer tier with `--reviewer-model`, `--reviewer-local-reasoning-effort xhigh`, and `--reviewer-local-service-tier fast`. Prefer `manual_artifact` unless the user requests model review.

## Prepare A Headless Run

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
  --executor-model gpt-5.6-terra \
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
  --executor-model gpt-5.6-terra \
  --executor-local-reasoning-effort xhigh \
  --executor-local-service-tier fast \
  --json
```

## Review Headless Results

Treat exit `3` with `semantic_review_pending` as the normal manual review checkpoint. Inspect:

- the semantic review packet
- pre-executor, post-executor, and post-validation verifier reports
- validation results
- `git diff --stat`
- the full diff and targeted file reads for every changed in-scope file

Write the approval or rejection JSON to the `manual_approval_artifact_path` recorded in state, then resume the runner.

On approval, let the runner emit targeted commit commands. Ask for explicit user authorization before running `git add` or `git commit`, stage only reviewed files, and record the resulting hash.

On rejection, resume once to record the blocked decision. Create a fresh bounded follow-up plan with the rejected dirty files in `expected_dirty_paths`, narrow `allowed_write_paths` to the repair scope, and keep unrelated changes excluded.

## Preserve Safety Gates

- Block unexpected branch or HEAD drift, unexpected commits, staged changes, forbidden paths, new out-of-scope dirty paths, and unapproved pre-existing changes inside the write scope.
- Recompute semantic-review fingerprints on resume and reject stale review inputs.
- Preserve unrelated user work; never revert or include it in commit commands.
- Use `allow_dirty_approved_continuation` only when the user explicitly wants reviewed dirty files carried across phases.
- Use `auto-commit-after-gate` only with explicit user authorization for automated commits.
- Treat executor and reviewer reports as summaries, not proof; verify locally.

Commit modes:

- `prepare-commit-command-only`: emit reviewed, targeted commands after semantic approval.
- `no-commit`: finish without commit authorization; useful for reports and disabled probes.
- `auto-commit-after-gate`: stage only reviewed files and commit after approval; require explicit user authorization.

Exit codes:

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
