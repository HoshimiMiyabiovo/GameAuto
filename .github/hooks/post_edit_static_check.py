import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


EDIT_TOOL_NAMES = {
    "apply_patch",
    "create_file",
    "edit_notebook_file",
    "vscode_renameSymbol",
    "mcp_pylance_mcp_s_pylanceInvokeRefactoring",
}

VALIDATION_RELEVANT_EXTENSIONS = {
    ".py",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ps1",
    ".psm1",
    ".bat",
    ".cmd",
    ".ipynb",
}


def emit_continue(message: str = "") -> None:
    payload: dict[str, Any] = {"continue": True}
    if message:
        payload["systemMessage"] = message
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def emit_block(files: list[str]) -> None:
    joined = ", ".join(files)
    payload = {
        "continue": True,
        "systemMessage": f"检测到编辑工具已触达文件: {joined}。下一步请先对这些文件运行聚焦静态检查，再继续修改或结束任务。",
        "decision": "block",
        "reason": "Edited files require focused static validation before continuing.",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "You just edited these files: "
                f"{joined}. Before more edits or completion, run focused static validation on the touched files. "
                "At minimum, run diagnostics/errors for each touched Python or config file. "
                "If Python imports, module boundaries, or flow_configs.json changed, also run a focused import or structure validation."
            ),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def resolve_input_path(value: str | None, cwd: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None

    if text.lower().startswith("file://"):
        try:
            return str(Path(text.removeprefix("file://")).resolve())
        except OSError:
            return None

    path = Path(text)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def add_file(target: list[str], value: str | None, cwd: str) -> None:
    resolved = resolve_input_path(value, cwd)
    if resolved and resolved not in target:
        target.append(resolved)


def patch_files(patch_text: str, cwd: str) -> list[str]:
    files: list[str] = []
    for match in re.finditer(r"(?m)^\*\*\* (?:Update|Add|Delete) File: (.+?)(?:\s*->\s*.+)?$", patch_text or ""):
        add_file(files, match.group(1), cwd)
    return files


def iter_values(container: Any, key: str) -> Iterable[str]:
    if not isinstance(container, dict) or key not in container:
        return ()
    value = container[key]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def touched_files(hook_input: dict[str, Any]) -> list[str]:
    cwd = str(hook_input.get("cwd") or "")
    tool_name = str(hook_input.get("tool_name") or "")
    tool_input = hook_input.get("tool_input") or {}

    files: list[str] = []
    if tool_name == "apply_patch":
        return patch_files(str(tool_input.get("input") or ""), cwd)

    for key in ("filePath", "path", "fileUri", "uri"):
        for value in iter_values(tool_input, key):
            add_file(files, value, cwd)

    for key in ("filePaths", "paths"):
        for value in iter_values(tool_input, key):
            add_file(files, value, cwd)

    return files


def needs_validation(files: list[str]) -> bool:
    for file_name in files:
        path = Path(file_name)
        if path.name.lower() == "flow_configs.json":
            return True
        if path.suffix.lower() in VALIDATION_RELEVANT_EXTENSIONS:
            return True
    return False


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            emit_continue()
            return 0

        hook_input = json.loads(raw)
        tool_name = str(hook_input.get("tool_name") or "")
        if tool_name not in EDIT_TOOL_NAMES:
            emit_continue()
            return 0

        files = touched_files(hook_input)
        if not files or not needs_validation(files):
            emit_continue()
            return 0

        emit_block(files)
        return 0
    except Exception as exc:
        emit_continue(f"post-edit-static-check hook failed: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())