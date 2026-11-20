"""Rule-based classification of Radar candidates (PLAN.md 6.2 and 6.5, decision 24).

Input per repo: what discover found (topics, name, description) plus the repo's file list
(Git Trees API) and, only when MCP is plausible, a few dependency files.

Output: categories with source and confidence (the `repo_categories` table), warning flags
(`repo_flags`) and a status:
- active : real evidence in files or dependencies, or an awesome list about Claude. Shown on Radar.
- pending: Claude-specific topic or keyword but no file evidence. Left for the LLM step.
- hidden : only a generic topic (MCP, agent-skills) and no evidence. Not shown, no LLM cost.

Confidence: evidence in the repo's own content scores >= ACTIVE_CONFIDENCE; topics alone score
below it. Files under a root `.claude/` folder are usually a project's own developer setup,
so they only count as strong evidence when the repo is also tagged as Claude-related.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from pipeline.categories import BY_SLUG

ACTIVE_CONFIDENCE = 0.8
INACTIVE_AFTER = timedelta(days=365)  # PLAN.md 6.4: no push for over a year
VERY_NEW_WITHIN = timedelta(days=30)
MAX_DEP_FILES = 4

CLAUDE_TOPICS = frozenset(
    {
        "claude-code",
        "claude-code-plugin",
        "claude-code-plugins",
        "claude-code-hooks",
        "claude-code-skills",
        "claude-skills",
        "claude-agents",
        "claude-subagents",
        "claude-mcp",
    }
)
CLAUDE_KEYWORD_LABEL = 'keyword:"claude code"'

TOPIC_CATEGORIES = {
    "claude-skills": "skills",
    "claude-code-skills": "skills",
    "agent-skills": "skills",
    "claude-agents": "subagents",
    "claude-subagents": "subagents",
    "mcp-server": "mcp",
    "mcp-servers": "mcp",
    "model-context-protocol": "mcp",
    "claude-mcp": "mcp",
    "claude-code-hooks": "hooks",
    "claude-code-plugin": "plugins",
    "claude-code-plugins": "plugins",
}
TOPIC_CONFIDENCE = 0.6

# Directories that hold vendored, sample or test code, never what the repo itself ships.
_IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "third_party",
        ".venv",
        "dist",
        "build",
        "examples",
        "example",
        "test",
        "tests",
        "__tests__",
        "fixtures",
        "testdata",
    }
)

_DEP_BASENAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }
)
_MCP_NPM_PACKAGES = frozenset({"fastmcp", "mcp-framework"})
_MCP_PY_PACKAGES = frozenset({"mcp", "fastmcp"})
_MCP_GO_MODULES = ("github.com/mark3labs/mcp-go", "github.com/modelcontextprotocol/go-sdk")
_MCP_RUST_CRATES = frozenset({"rmcp"})
_MCP_JVM_MARKER = "io.modelcontextprotocol"
_MCP_DOTNET_RE = re.compile(r'Include="ModelContextProtocol(\.[A-Za-z]+)?"')
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

_DOT_CLAUDE_HOOK_SCRIPT_RE = re.compile(r"^\.claude/hooks/.+\.(sh|py|js|ts)$")
_AGENT_FILE_RE = re.compile(r"(^|/)agents/[^/]+\.md$")
_COMMAND_FILE_RE = re.compile(r"(^|/)commands/.+\.md$")
_AWESOME_ON_TOPIC = ("claude", "mcp", "skill", "subagent", "plugin")
_TEMPLATE_WORDS_RE = re.compile(r"\b(templates?|rules|examples?|starter|boilerplate)\b")


@dataclass
class Match:
    slug: str
    source: str  # tree | deps | topic (repo_categories.source)
    confidence: float
    evidence: str


@dataclass
class Result:
    full_name: str
    status: str  # active | pending | hidden
    reason: str
    categories: list[Match] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    is_docs: bool = False
    tree_truncated: bool = False


def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _ignored(path: str) -> bool:
    return any(p in _IGNORED_DIRS for p in _parts(path))


def is_claude_related(repo: dict) -> bool:
    return bool(CLAUDE_TOPICS & set(repo.get("topics") or [])) or (
        CLAUDE_KEYWORD_LABEL in (repo.get("matched_by") or [])
    )


def mcp_plausible(repo: dict) -> bool:
    """Only then are dependency files worth the extra requests."""
    topics = set(repo.get("topics") or [])
    if topics & {t for t, slug in TOPIC_CATEGORIES.items() if slug == "mcp"} or "mcp" in topics:
        return True
    text = f"{repo.get('name') or ''} {repo.get('description') or ''}".lower()
    return bool(re.search(r"\bmcp\b|model context protocol", text))


def dependency_files(paths: list[str]) -> list[str]:
    """The dependency manifests worth fetching: shallow ones first, at most MAX_DEP_FILES."""
    picked = [
        p
        for p in paths
        if (PurePosixPath(p).name in _DEP_BASENAMES or p.endswith(".csproj"))
        and p.count("/") <= 2
        and not _ignored(p)
    ]
    return sorted(picked, key=lambda p: (p.count("/"), p))[:MAX_DEP_FILES]


# ----- dependency parsing -------------------------------------------------------------


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _py_requirement_names(lines: list[str]) -> set[str]:
    names = set()
    for line in lines:
        m = _REQ_NAME_RE.match(line)
        if m and not line.lstrip().startswith(("#", "-")):
            names.add(_norm(m.group(1)))
    return names


def mcp_dependency(path: str, text: str) -> str | None:
    """Return the MCP SDK this manifest depends on, or None."""
    base = PurePosixPath(path).name
    try:
        if base == "package.json":
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                for dep in data.get(key) or {}:
                    if dep.startswith("@modelcontextprotocol/") or dep in _MCP_NPM_PACKAGES:
                        return dep
        elif base == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project") or {}
            reqs = list(project.get("dependencies") or [])
            for extra in (project.get("optional-dependencies") or {}).values():
                reqs += list(extra)
            names = _py_requirement_names(reqs)
            poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
            names |= {_norm(n) for n in poetry}
            hit = sorted(names & _MCP_PY_PACKAGES)
            return hit[0] if hit else None
        elif base == "requirements.txt":
            hit = sorted(_py_requirement_names(text.splitlines()) & _MCP_PY_PACKAGES)
            return hit[0] if hit else None
        elif base == "go.mod":
            return next((m for m in _MCP_GO_MODULES if m in text), None)
        elif base == "Cargo.toml":
            data = tomllib.loads(text)
            deps = set(data.get("dependencies") or {}) | set(data.get("dev-dependencies") or {})
            hit = sorted(deps & _MCP_RUST_CRATES)
            return hit[0] if hit else None
        elif base in {"pom.xml", "build.gradle", "build.gradle.kts"}:
            return _MCP_JVM_MARKER if _MCP_JVM_MARKER in text else None
        elif base.endswith(".csproj"):
            m = _MCP_DOTNET_RE.search(text)
            return m.group(0)[len('Include="') : -1] if m else None
    except (ValueError, AttributeError, TypeError):
        return None  # a broken manifest is simply no evidence
    return None


# ----- classification -----------------------------------------------------------------


def _add(found: dict[str, Match], match: Match) -> None:
    current = found.get(match.slug)
    if current is None or match.confidence > current.confidence:
        found[match.slug] = match


def _in_tool_config(path: str) -> bool:
    """True for files inside a hidden folder such as .claude/, .gemini/ or .github/.

    Those hold a project's own setup for some AI tool, not something it ships to users.
    `.claude-plugin/` is the exception: it is the plugin manifest folder.
    """
    return any(p.startswith(".") and p != ".claude-plugin" for p in _parts(path)[:-1])


def _tree_matches(paths: list[str], claude_related: bool) -> list[Match]:
    paths = [p for p in paths if not _ignored(p)]
    out: list[Match] = []
    is_plugin_repo = any(
        p.endswith((".claude-plugin/plugin.json", ".claude-plugin/marketplace.json")) for p in paths
    )
    config = 0.85 if claude_related else 0.4  # a project's own dev setup is weak evidence

    def split(found: list[str]) -> tuple[list[str], list[str]]:
        return [p for p in found if not _in_tool_config(p)], [
            p for p in found if _in_tool_config(p)
        ]

    def where(found: list[str]) -> str:
        return _parts(found[0])[0] + "/"

    own, conf = split([p for p in paths if PurePosixPath(p).name == "SKILL.md"])
    if own:
        out.append(Match("skills", "tree", 0.95, f"{len(own)} SKILL.md, e.g. {own[0]}"))
    elif conf:
        out.append(Match("skills", "tree", config, f"{len(conf)} SKILL.md under {where(conf)}"))

    manifests = [p for p in paths if p.endswith(".claude-plugin/plugin.json")]
    markets = [p for p in paths if p.endswith(".claude-plugin/marketplace.json")]
    if manifests or markets:
        out.append(Match("plugins", "tree", 0.95, (markets or manifests)[0]))

    own, conf = split([p for p in paths if _AGENT_FILE_RE.search(p)])
    if is_plugin_repo and own:
        out.append(Match("subagents", "tree", 0.9, f"{len(own)} agents/*.md in a plugin"))
    elif conf:
        out.append(Match("subagents", "tree", config, f"{len(conf)} agents under {where(conf)}"))
    elif len(own) >= 2:
        out.append(Match("subagents", "tree", 0.5, f"{len(own)} agents/*.md"))

    own, conf = split([p for p in paths if _COMMAND_FILE_RE.search(p)])
    if is_plugin_repo and own:
        out.append(Match("commands", "tree", 0.85, f"{len(own)} commands/*.md in a plugin"))
    elif conf:
        out.append(Match("commands", "tree", config, f"{len(conf)} commands under {where(conf)}"))

    hook_files = [
        p
        for p in paths
        if PurePosixPath(p).name == "hooks.json" or _DOT_CLAUDE_HOOK_SCRIPT_RE.match(p)
    ]
    own, conf = split(hook_files)
    if own and (is_plugin_repo or claude_related):
        out.append(Match("hooks", "tree", 0.85, own[0]))
    elif conf:
        out.append(Match("hooks", "tree", config, f"{len(conf)} hook files under {where(conf)}"))

    return out


def _metadata_matches(repo: dict) -> tuple[list[Match], bool]:
    """Matches from topics, name and description. Returns (matches, is_docs)."""
    out: list[Match] = []
    topics = set(repo.get("topics") or [])
    for topic in sorted(topics):
        slug = TOPIC_CATEGORIES.get(topic)
        if slug:
            out.append(Match(slug, "topic", TOPIC_CONFIDENCE, f"topic {topic}"))

    name = (repo.get("name") or "").lower()
    text = f"{name} {(repo.get('description') or '').lower()}"
    is_awesome = name.startswith("awesome") or bool(topics & {"awesome", "awesome-list"})
    if is_awesome:
        # Strong only when the list itself is about our ecosystem; a general AI list that
        # merely mentions Claude Code goes to the LLM step instead.
        on_topic = bool(CLAUDE_TOPICS & topics) or any(w in name for w in _AWESOME_ON_TOPIC)
        out.append(Match("learn", "topic", 0.9 if on_topic else 0.6, "awesome list"))
    if "claude.md" in text and _TEMPLATE_WORDS_RE.search(text):
        out.append(Match("templates", "topic", 0.8, "CLAUDE.md templates in name/description"))
    return out, is_awesome


def flags_for(repo: dict, has_hooks: bool, now: datetime) -> list[str]:
    flags = []
    if has_hooks:
        flags.append("has_shell_hooks")  # hooks run shell commands on the user's machine
    if not repo.get("license_spdx"):
        flags.append("no_license")
    pushed = repo.get("pushed_at")
    if repo.get("archived") or (pushed and now - _ts(pushed) > INACTIVE_AFTER):
        flags.append("inactive")
    if now - _ts(repo["created_at"]) < VERY_NEW_WITHIN:
        flags.append("very_new")
    return flags  # needs_token comes later, from the README (enrich)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify(
    repo: dict,
    paths: list[str] | None,
    dep_texts: dict[str, str] | None = None,
    tree_truncated: bool = False,
    now: datetime | None = None,
) -> Result:
    """Classify one repo. `paths` is None when the file list could not be read."""
    now = now or datetime.now(UTC)
    full_name = f"{repo['owner']}/{repo['name']}"
    claude_related = is_claude_related(repo)

    found: dict[str, Match] = {}
    tree = _tree_matches(paths or [], claude_related)
    for match in tree:
        _add(found, match)
    for path, text in sorted((dep_texts or {}).items()):
        sdk = mcp_dependency(path, text)
        if sdk:
            _add(found, Match("mcp", "deps", 0.9, f"{path} uses {sdk}"))
            break
    meta, is_docs = _metadata_matches(repo)
    for match in meta:
        if match.slug == "learn" and not (claude_related or found):
            continue  # an awesome list about something else
        _add(found, match)

    # Ties go to the more specific category, in the app's chip order (skills before plugins).
    categories = sorted(found.values(), key=lambda m: (-m.confidence, BY_SLUG[m.slug].sort_order))
    strong = [m for m in categories if m.confidence >= ACTIVE_CONFIDENCE]
    if strong:
        status, reason = "active", f"{strong[0].slug}: {strong[0].evidence}"
    elif claude_related:
        status, reason = "pending", "Claude topic or keyword but no file evidence: needs LLM"
    else:
        status, reason = "hidden", "generic topic only, no evidence in files or dependencies"
    if paths is None and status == "hidden":
        status, reason = "pending", "file list unavailable: retry next run"

    has_hooks = any(m.slug == "hooks" and m.source == "tree" for m in categories)
    return Result(
        full_name=full_name,
        status=status,
        reason=reason,
        categories=categories,
        flags=flags_for(repo, has_hooks, now),
        is_docs=is_docs,
        tree_truncated=tree_truncated,
    )


def to_dict(result: Result) -> dict:
    return asdict(result)


# ----- fetching from GitHub -------------------------------------------------------------


def gather_signals(client, repo: dict) -> tuple[list[str] | None, dict[str, str], bool]:
    """Fetch the file list and, when MCP is plausible, dependency files.

    Cost: 1 request per repo, plus up to MAX_DEP_FILES for MCP-looking repos (it stops at the
    first manifest that names an MCP SDK). Returns (paths or None, dep_texts, truncated).
    """
    from pipeline.github import GitHubError

    owner, name, ref = repo["owner"], repo["name"], repo.get("default_branch")
    if not ref:
        return None, {}, False
    try:
        tree = client.tree(owner, name, ref)
    except GitHubError as err:
        if err.status in (404, 409, 451):  # gone, empty, or blocked: no evidence this run
            return None, {}, False
        raise
    paths = [entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"]
    dep_texts: dict[str, str] = {}
    if mcp_plausible(repo):
        for path in dependency_files(paths):
            try:
                dep_texts[path] = client.file_text(owner, name, path, ref)
            except GitHubError as err:
                if err.status == 404:
                    continue
                raise
            if mcp_dependency(path, dep_texts[path]):
                break
    return paths, dep_texts, bool(tree.get("truncated"))
