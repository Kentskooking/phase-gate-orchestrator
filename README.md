# Phase Gate Orchestrator

Give your main agent a task. By default, it manages the work from planning through commits:

1. **Plan:** split the task into manageable phases.
2. **Delegate:** launch a background coding agent for one phase.
3. **Review:** inspect the actual code and check that it works.
4. **Fix:** send any problems back to a coding agent and review the repairs.
5. **Commit and continue:** commit the approved changes, then repeat until the plan is complete.

Your main agent is the **orchestrator** and reviewer. The background agent is the **executor**: it writes code but does not stage or commit. The orchestrator can adjust the plan when it hits a roadblock.

## Start a session

With the skill installed, use:

```text
Use $phase-gate-orchestrator with its default workflow.

Repository: [absolute repo path]
Objective: [what you want built or changed]

Review and commit each completed phase, and continue until the plan is finished.
```


## Requirements

- **Python 3.8+** for the packaged scripts. No Python packages need installing.
- **Git** and an authenticated **Codex CLI** available to the agent.
- The target project's tools for builds and tests.

The agent checks for Python before launching scripts and tells you if it is missing. It selects an executor model based on the task: `gpt-5.6-luna`, `gpt-5.6-terra`, or `gpt-5.6-sol`.

## Optional workflows

- **Manual handoff:** the orchestrator prepares prompts for you to pass to an executor, then reviews the returned work.
- **Scripted orchestration:** a Python runner manages the phase sequence, checks, saved results, and checkpoints for resuming later.

The scripted runner supports orchestrator review (`manual_artifact`), a separate model reviewer (`model_review`), or disabled review for report-only runs.

Its commit settings are separate from the workflow choice:

| Setting | What happens |
| --- | --- |
| `prepare-commit-command-only` | Produces targeted commit commands after review. |
| `no-commit` | Leaves changes uncommitted. |
| `auto-commit-after-gate` | The runner commits after review passes; requires your authorization. |

## Scripts

| Script | Purpose |
| --- | --- |
| [relay_headless_codex.py](scripts/relay_headless_codex.py) | Launches one executor and captures its response. Used by the default workflow. |
| [phase_gate_headless.py](scripts/phase_gate_headless.py) | Runs the optional scripted orchestration workflow. |

See [SKILL.md](SKILL.md) for agent instructions and command examples, or [references/](references/) for plan and prompt templates.
