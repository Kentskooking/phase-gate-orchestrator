# Kickoff Prompt Template

Use this template when creating a new phase kickoff for an executor agent. Replace bracketed fields and delete irrelevant lines.

````text
You are the executor agent for [PROJECT_OR_FEATURE]. Work only in:

[ABSOLUTE_REPO_PATH]

Current phase:
[PHASE_NAME_OR_NUMBER]

Objective:
[ONE_OR_TWO_SENTENCES_DESCRIBING_THE_PHASE_GOAL]

Context:
- [IMPORTANT_PRIOR_COMMIT_OR_PHASE_CONTEXT]
- [IMPORTANT_ARCHITECTURE_OR_POLICY_CONTEXT]
- [KNOWN_ENVIRONMENT_LIMITS]

Scope:
- Own these files/modules if changes are needed: [FILES_OR_DIRECTORIES]
- Add or update tests in: [TEST_FILES_OR_DIRECTORIES]
- Do not modify: [OUT_OF_SCOPE_FILES_OR_REPOS]

Hard rules:
- Do not commit.
- Do not stage changes unless explicitly asked.
- Do not touch unrelated repos or unrelated files.
- You are not alone in the codebase. Preserve user/orchestrator changes and work with any dirty files you encounter.
- Keep changes narrowly scoped to this phase.
- Do not add temporary planning or orchestration artifacts to git history unless explicitly instructed.

Implementation notes:
- [SPECIFIC TECHNICAL REQUIREMENT]
- [SPECIFIC COMPATIBILITY OR API REQUIREMENT]
- [SPECIFIC TESTING OR TELEMETRY REQUIREMENT]

Validation to run:
- [COMMAND_1]
- [COMMAND_2]
- [OPTIONAL_SMOKE_COMMAND_OR_REASON_TO_SKIP]

Final report format:
- Current branch and git status summary.
- Files changed.
- What changed.
- Validation commands run and exact pass/fail results.
- Smoke/live checks run, skipped, or blocked.
- Deviations from the phase plan.
- Confirmation that no commit was made.
````
