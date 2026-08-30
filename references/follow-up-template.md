# Follow-Up Template

Use this template when the phase is not ready for commit.

For a headless `manual_artifact` gate, first write a rejection artifact to the `manual_approval_artifact_path`, resume once so the runner records `semantic_review_blocked`, then create a fresh follow-up run with the rejected in-scope dirty files listed in `expected_dirty_paths`.

```json
{
  "approved": false,
  "findings": ["Concrete blocking finding."],
  "reviewed_files": [],
  "validation_summary": {
    "commands_reviewed": ["COMMAND_1", "COMMAND_2"]
  },
  "residual_risks": ["Rejected work remains dirty for the follow-up run."],
  "commit_eligibility": false
}
```

````text
Not approved yet.

Findings:
1. [Severity]: [Concrete issue with file/line when possible].
2. [Severity]: [Concrete issue with file/line when possible].

Validation:
- `[COMMAND]` -> [RESULT]
- `[COMMAND]` -> [RESULT]

Follow-up prompt for executor:

```text
You are continuing [PHASE_NAME_OR_NUMBER] in:

[ABSOLUTE_REPO_PATH]

Do not commit.
Do not stage changes unless explicitly asked.
Do not touch unrelated repos or unrelated files.
You are not alone in the codebase. Preserve existing changes and only modify what is needed for this follow-up.

Issues to fix:
- [ISSUE_1]
- [ISSUE_2]

Expected changes:
- [EXPECTED_FILE_OR_BEHAVIOR_CHANGE]
- [EXPECTED_TEST_OR_DOC_CHANGE]

Validation to run:
- [COMMAND_1]
- [COMMAND_2]

Final report format:
- Files changed.
- What changed.
- Validation commands run and exact results.
- Confirmation that no commit was made.
```
````
