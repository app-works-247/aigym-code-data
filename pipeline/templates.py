"""Config templates in content/templates/<id>.yaml (PLAN.md 8.4).

Each template holds one or more files, a vi/en explanation and the Claude Code version it was
checked against. Spots the user must fill are written {{key}} and every key is declared with a
vi/en hint. Validation fills each placeholder with a sample and checks that the result parses
(JSON for settings and .mcp.json, YAML frontmatter for SKILL.md and subagents).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content" / "templates"
KINDS = ("claude_md", "skill", "subagent", "hook", "mcp", "plugin")
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class TemplateError(ValueError):
    pass


@dataclass
class Placeholder:
    key: str
    vi: str
    en: str
    sample: str  # used by validation and as the example shown in the app


@dataclass
class TemplateFile:
    path: str  # where it goes, relative to the project root (or ~ for user files)
    content: str


@dataclass
class Template:
    id: str
    kind: str
    title: dict[str, str]
    summary: dict[str, str]
    docs: str
    checked_with: str  # Claude Code version the template was checked against
    files: list[TemplateFile]
    placeholders: list[Placeholder] = field(default_factory=list)
    notes: dict[str, list[str]] = field(default_factory=dict)  # extra steps, vi/en
    lessons: list[str] = field(default_factory=list)


def load_all(directory: Path = CONTENT_DIR) -> list[Template]:
    templates = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["files"] = [TemplateFile(**f) for f in data["files"]]
        data["placeholders"] = [Placeholder(**p) for p in data.get("placeholders") or []]
        template = Template(**data)
        if template.id != path.stem:
            raise TemplateError(f"{path.name}: id {template.id!r} must match the file name")
        validate(template)
        templates.append(template)
    return templates


def fill(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def validate(t: Template) -> None:
    where = f"template {t.id}"
    problems = []
    if t.kind not in KINDS:
        problems.append(f"kind {t.kind!r} not in {KINDS}")
    for part in ("title", "summary"):
        if set(getattr(t, part)) != {"vi", "en"}:
            problems.append(f"{part} needs vi and en")
    if not t.docs.startswith("https://code.claude.com/docs/"):
        problems.append("docs must link to code.claude.com/docs")
    if not re.fullmatch(r"\d+\.\d+\.\d+", t.checked_with):
        problems.append("checked_with must be a version like 2.1.329")
    if not t.files:
        problems.append("no files")
    declared = {p.key for p in t.placeholders}
    used = {key for f in t.files for key in _PLACEHOLDER.findall(f.content + f.path)}
    if used - declared:
        problems.append(f"placeholders used but not declared: {sorted(used - declared)}")
    if declared - used:
        problems.append(f"placeholders declared but unused: {sorted(declared - used)}")
    if problems:
        raise TemplateError(f"{where}: " + "; ".join(problems))

    samples = {p.key: p.sample for p in t.placeholders}
    for f in t.files:
        text = fill(f.content, samples)
        try:
            _check_file(t.kind, fill(f.path, samples), text)
        except (ValueError, KeyError) as err:
            raise TemplateError(f"{where}: {f.path}: {err}") from err


def _frontmatter(text: str) -> dict:
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError("missing --- frontmatter ---")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data


def _check_file(kind: str, path: str, text: str) -> None:
    if path.endswith(".json"):
        data = json.loads(text)
        if path.endswith("settings.json") and kind == "hook":
            for event, groups in data["hooks"].items():
                for group in groups:
                    for handler in group["hooks"]:
                        if handler["type"] != "command" or not handler["command"]:
                            raise ValueError(f"{event}: each handler needs type and command")
        if path.endswith(".mcp.json"):
            for name, server in data["mcpServers"].items():
                if "url" in server and server.get("type") not in ("http", "sse", "ws"):
                    raise ValueError(f"{name}: a url server needs type http, sse or ws")
                if "url" not in server and "command" not in server:
                    raise ValueError(f"{name}: needs command (stdio) or url")
        if path.endswith("plugin.json"):
            if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", data["name"]):
                raise ValueError("plugin name must be kebab-case")
    elif kind == "skill" and path.endswith("SKILL.md"):
        meta = _frontmatter(text)
        if not meta.get("description"):
            raise ValueError("SKILL.md needs a description")
    elif kind == "subagent" and path.endswith(".md"):
        meta = _frontmatter(text)
        if not meta.get("name") or not meta.get("description"):
            raise ValueError("a subagent needs name and description")
    elif path.endswith(".sh"):
        if not text.startswith("#!"):
            raise ValueError("a hook script needs a #! line")
    elif kind == "claude_md":
        lines = text.count("\n")
        if lines > 200:
            raise ValueError(f"{lines} lines; the docs advise under 200")


def to_json(templates: list[Template], generated_at: str) -> dict:
    """/v1/templates.json."""
    return {
        "schema": 1,
        "updated_at": generated_at,
        "items": [asdict(t) for t in templates],
    }
