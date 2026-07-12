---
description: "Use when modifying Python code, adding a new Python module, changing runtime flows, or editing flow_configs.json. Requires a brief plan before edits, focused static validation after edits, and proactive cleanup of new diagnostics."
name: "Python Change Workflow"
applyTo:
  - "**/*.py"
  - "flow_configs.json"
---

# Python Change Workflow

- Before editing, state a short plan that names the target file or symbol, the expected behavior change, and the first validation you will run.
- Keep the first edit small and local. If the code path is uncertain, prefer a reversible probe over a broad rewrite.
- After the first substantive edit, immediately run the narrowest useful validation for the touched slice.
- Minimum validation for this repo:
  - Run diagnostics on each touched Python file.
  - If you changed imports, module boundaries, or added a new module, run a focused import or execution snippet.
  - If you changed flow_configs.json, validate the file structure after editing.
- Before completing the task, proactively check for and fix new errors in the touched files rather than only responding to user-reported failures.
- When adding Python dependencies, update both requirements.txt and dependency_manager.py in the same task.