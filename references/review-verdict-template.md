# Review Verdict Template

Use this template after local verification shows the phase is ready to commit.

For a headless `manual_artifact` gate, write the approval to the `manual_approval_artifact_path` shown in `phase_gate_state.json` instead of only replying in chat:

```json
{
  "approved": true,
  "findings": [],
  "reviewed_files": ["path/one", "path/two"],
  "validation_summary": {
    "commands_reviewed": ["COMMAND_1", "COMMAND_2"]
  },
  "residual_risks": [],
  "commit_eligibility": true
}
```

````text
Approved for commit.

Reviewed scope:
- [FILE_OR_AREA]
- [FILE_OR_AREA]

Validation:
- `[COMMAND]` -> [RESULT]
- `[COMMAND]` -> [RESULT]
- [SMOKE_OR_CLEANUP_CHECK] -> [RESULT]

Commit command:

```bash
git add [REVIEWED_FILE_1] [REVIEWED_FILE_2]
git commit -m "[COMMIT_MESSAGE]"
```

[WARN_ABOUT_UNRELATED_DIRTY_FILES_IF_PRESENT]

Next kickoff prompt:

```text
[COPY_READY_NEXT_PHASE_PROMPT]
```
````

If there is no next phase, replace `Next kickoff prompt` with:

````text
No next implementation kickoff is needed. Remaining optional checks:
- [OPTIONAL_CHECK]
- [OPTIONAL_CHECK]
````
