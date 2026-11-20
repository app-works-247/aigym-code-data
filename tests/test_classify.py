import json
import unittest
from datetime import UTC, datetime

from pipeline import classify
from pipeline.github import GitHubClient, Response

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def repo(topics=(), name="tool", description="", matched_by=None, **extra):
    data = {
        "owner": "someone",
        "name": name,
        "description": description,
        "topics": list(topics),
        "matched_by": matched_by or [f"topic:{t}" for t in topics],
        "license_spdx": "MIT",
        "archived": False,
        "created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-11-01T00:00:00Z",
        "default_branch": "main",
    }
    data.update(extra)
    return data


def run(r, paths=(), deps=None):
    return classify.classify(r, list(paths), deps or {}, now=NOW)


def slugs(result, min_conf=0.0):
    return {m.slug for m in result.categories if m.confidence >= min_conf}


class TreeRulesTest(unittest.TestCase):
    def test_skill_collection_is_active_even_with_generic_topic(self):
        r = run(
            repo(["agent-skills"], description="Agent skills for PDF and Word files"),
            ["skills/pdf/SKILL.md", "skills/docx/SKILL.md"],
        )
        self.assertEqual(r.status, "active")
        self.assertIn("skills", slugs(r, classify.ACTIVE_CONFIDENCE))

    def test_library_that_ships_a_skill_is_not_a_skill_pack(self):
        # Hand check 2026-11-20: ngx-stripe, an Angular wrapper with one SKILL.md.
        r = run(
            repo(["agent-skills"], name="ngx-stripe", description="Angular wrapper for StripeJS"),
            ["skills/ngx-stripe/SKILL.md", "src/index.ts"],
        )
        self.assertEqual(r.status, "hidden")

    def test_plugin_marketplace_with_agents_commands_hooks(self):
        paths = [
            ".claude-plugin/marketplace.json",
            "plugins/a/.claude-plugin/plugin.json",
            "plugins/a/agents/reviewer.md",
            "plugins/a/commands/review.md",
            "plugins/a/hooks/hooks.json",
        ]
        r = run(repo(["claude-code-plugin"]), paths)
        self.assertEqual(r.status, "active")
        self.assertEqual(
            slugs(r, classify.ACTIVE_CONFIDENCE), {"plugins", "subagents", "commands", "hooks"}
        )
        self.assertIn("has_shell_hooks", r.flags)

    def test_dot_claude_setup_in_unrelated_project_is_weak(self):
        # A big app that keeps its own Claude Code setup in .claude/ is not a Radar entry.
        paths = [".claude/commands/release.md", ".claude/agents/dev.md", "src/main.ts"]
        r = run(repo(["mcp-server"]), paths)
        self.assertEqual(r.status, "hidden")
        self.assertTrue(all(m.confidence < classify.ACTIVE_CONFIDENCE for m in r.categories))

    def test_dot_claude_content_goes_to_the_llm_even_for_claude_tagged_repos(self):
        # Hand check 2026-11-20: tools such as rtk keep skills and agents in .claude/ for their
        # own development; that alone does not make them a skill pack.
        paths = [".claude/agents/a.md", ".claude/agents/b.md", ".claude/skills/x/SKILL.md"]
        r = run(repo(["claude-code"]), paths)
        self.assertEqual(r.status, "pending")
        self.assertIn("subagents", slugs(r))

    def test_skill_files_deep_in_product_code_are_weak(self):
        for path in [
            "docs/skills/runtime/SKILL.md",
            "packages/design-system/SKILL.md",
            "apps/landing/public/SKILL.md",
        ]:
            with self.subTest(path=path):
                self.assertEqual(run(repo(["claude-code"]), [path]).status, "pending")
        for path in [
            "SKILL.md",
            "writer/SKILL.md",
            "skills/a/SKILL.md",
            "plugins/p/skills/a/SKILL.md",
        ]:
            with self.subTest(path=path):
                self.assertEqual(run(repo(["claude-code"]), [path]).status, "active")

    def test_hooks_file_deep_in_app_resources_is_weak(self):
        # Hand check 2026-11-20: a terminal app that bundles prompt suggestions with a hooks.json.
        deep = "app/res/prompt-suggestions/claude/hooks/hooks.json"
        self.assertEqual(run(repo(["claude-code"]), [deep]).status, "pending")
        self.assertEqual(run(repo(["claude-code"]), ["hooks/hooks.json"]).status, "active")

    def test_plugin_manifest_deep_in_app_resources_is_weak(self):
        deep = "app/res/prompts/claude/.claude-plugin/plugin.json"
        self.assertEqual(run(repo(["claude-code"]), [deep]).status, "pending")
        self.assertEqual(run(repo([]), ["plugins/x/.claude-plugin/plugin.json"]).status, "active")

    def test_other_tools_config_folders_are_weak_too(self):
        # Found live: a CLI keeps its own skills in .gemini/skills/; that is not a skill pack.
        paths = [".gemini/skills/a/SKILL.md", ".github/agents/x.md", ".cursor/commands/y.md"]
        r = run(repo(["mcp-server"]), paths)
        self.assertEqual(r.status, "hidden")
        self.assertTrue(any("SKILL.md under .gemini/" in m.evidence for m in r.categories))
        self.assertTrue(all(m.confidence < classify.ACTIVE_CONFIDENCE for m in r.categories))

    def test_mcp_server_with_a_bundled_skill_stays_an_mcp_server(self):
        # Hand check 2026-11-20: mongodb-mcp-server, keboola/mcp-server, mcp-tts.
        deps = {"package.json": json.dumps({"dependencies": {"@modelcontextprotocol/sdk": "1"}})}
        r = run(
            repo(["mcp-server"], name="mongodb-mcp-server", description="MCP server for MongoDB"),
            ["package.json", "skills/migrate/SKILL.md"],
            deps,
        )
        self.assertEqual(r.categories[0].slug, "mcp")

    def test_mcp_name_and_topic_without_a_known_sdk(self):
        # Hand check 2026-11-20: pubmed-mcp-server uses its own framework, not the SDK.
        r = run(repo(["mcp-server"], name="pubmed-mcp-server"), ["src/index.ts"])
        self.assertEqual(r.status, "active")
        self.assertEqual(r.categories[0].slug, "mcp")

    def test_course_about_claude_is_learning_material(self):
        r = run(
            repo(
                ["claude-code"],
                name="claude-code-rag-masterclass",
                description="An 8-module course",
            ),
            [".claude/commands/a.md", ".claude/commands/b.md"],
        )
        self.assertEqual((r.status, r.categories[0].slug), ("active", "learn"))

    def test_tie_goes_to_the_more_specific_category(self):
        # Found live: anthropics/skills ships SKILL.md files inside a plugin marketplace.
        paths = [".claude-plugin/marketplace.json", "skills/pdf/SKILL.md"]
        r = run(repo(["claude-code"]), paths)
        self.assertEqual([m.slug for m in r.categories][:2], ["skills", "plugins"])

    def test_plugin_manifest_folder_is_not_a_config_folder(self):
        r = run(repo([]), ["plugins/x/.claude-plugin/plugin.json"])
        self.assertEqual(r.status, "active")

    def test_vendored_example_and_test_files_are_ignored(self):
        for path in [
            "node_modules/x/skills/a/SKILL.md",
            "packages/cli/src/examples/skills/greeter/SKILL.md",  # found live in a CLI repo
            "tests/fixtures/SKILL.md",
        ]:
            with self.subTest(path=path):
                self.assertEqual(run(repo(["agent-skills"]), [path]).status, "hidden")

    def test_loose_agents_folder_alone_is_not_enough(self):
        r = run(repo(["claude-code"]), ["agents/a.md", "agents/b.md"])
        self.assertEqual(r.status, "pending")
        self.assertIn("subagents", slugs(r))


class MetadataRulesTest(unittest.TestCase):
    def test_claude_topic_without_files_goes_to_llm(self):
        r = run(repo(["claude-code"]), ["src/index.ts"])
        self.assertEqual(r.status, "pending")

    def test_keyword_match_counts_as_claude_related(self):
        r = run(repo([], matched_by=['keyword:"claude code"']), ["main.py"])
        self.assertEqual(r.status, "pending")

    def test_generic_mcp_topic_without_sdk_is_hidden(self):
        r = run(repo(["mcp-server"]), ["src/index.ts", "package.json"])
        self.assertEqual(r.status, "hidden")
        self.assertEqual(slugs(r), {"mcp"})  # kept for the record, below the bar

    def test_awesome_list_about_claude_is_active_docs(self):
        r = run(repo(["claude-code"], name="awesome-claude-code"), ["README.md"])
        self.assertEqual(r.status, "active")
        self.assertTrue(r.is_docs)
        self.assertIn("learn", slugs(r, classify.ACTIVE_CONFIDENCE))

    def test_general_awesome_list_that_mentions_claude_goes_to_llm(self):
        # Found live: a general AI list matched only because its description names Claude Code.
        r = run(
            repo([], name="Awesome-AI-Pedia", matched_by=['keyword:"claude code"']), ["README.md"]
        )
        self.assertEqual(r.status, "pending")

    def test_awesome_mcp_list_with_generic_topic_is_active(self):
        r = run(repo(["mcp-server"], name="awesome-mcp-servers"), ["README.md"])
        self.assertEqual(r.status, "active")

    def test_awesome_list_about_something_else_is_ignored(self):
        r = run(repo(["awesome"], name="awesome-rust"), ["README.md"])
        self.assertEqual(r.status, "hidden")
        self.assertNotIn("learn", slugs(r))

    def test_claude_md_templates(self):
        r = run(
            repo(["claude-code"], description="Curated CLAUDE.md templates for every stack"),
            ["README.md"],
        )
        self.assertIn("templates", slugs(r, classify.ACTIVE_CONFIDENCE))


class McpDependencyTest(unittest.TestCase):
    def test_detects_each_ecosystem(self):
        cases = {
            "package.json": json.dumps({"dependencies": {"@modelcontextprotocol/sdk": "^1"}}),
            "pyproject.toml": '[project]\ndependencies = ["mcp[cli]>=1.2", "httpx"]\n',
            "requirements.txt": "# deps\nFastMCP==2.0\nrequests\n",
            "go.mod": "module x\nrequire github.com/mark3labs/mcp-go v0.30.0\n",
            "Cargo.toml": '[dependencies]\nrmcp = { version = "0.8" }\n',
            "pom.xml": "<groupId>io.modelcontextprotocol.sdk</groupId>",
            "Server.csproj": '<PackageReference Include="ModelContextProtocol" Version="0.4" />',
        }
        for path, text in cases.items():
            with self.subTest(path=path):
                self.assertIsNotNone(classify.mcp_dependency(path, text))

    def test_no_false_positives(self):
        cases = {
            "package.json": json.dumps({"dependencies": {"mcp-client-ui": "1", "react": "18"}}),
            "pyproject.toml": '[project]\ndependencies = ["mcpx", "my-mcp-tools"]\n',
            "requirements.txt": "-e .\n# mcp\nmcp-proxy-ish\n",
            "package.json ": "not json",
            "Cargo.toml": "this is [not toml",
        }
        for path, text in cases.items():
            with self.subTest(path=path):
                self.assertIsNone(classify.mcp_dependency(path.strip(), text))

    def test_poetry_dependencies(self):
        text = '[tool.poetry.dependencies]\npython = "^3.11"\nmcp = "^1.0"\n'
        self.assertEqual(classify.mcp_dependency("pyproject.toml", text), "mcp")

    def test_mcp_sdk_makes_generic_repo_active(self):
        deps = {"package.json": json.dumps({"dependencies": {"@modelcontextprotocol/sdk": "1"}})}
        r = run(repo(["mcp-server"]), ["package.json", "src/index.ts"], deps)
        self.assertEqual(r.status, "active")
        self.assertEqual(r.categories[0].source, "deps")

    def test_dependency_file_selection(self):
        paths = [
            "package.json",
            "packages/server/package.json",
            "packages/a/b/c/package.json",  # too deep
            "node_modules/x/package.json",
            "examples/demo/pyproject.toml",
            "server/Server.csproj",
            "README.md",
        ]
        self.assertEqual(
            classify.dependency_files(paths),
            ["package.json", "server/Server.csproj", "packages/server/package.json"],
        )

    def test_mcp_plausible(self):
        self.assertTrue(classify.mcp_plausible(repo(["model-context-protocol"])))
        self.assertTrue(classify.mcp_plausible(repo([], description="An MCP server for Jira")))
        self.assertFalse(classify.mcp_plausible(repo(["claude-code"], description="Skills pack")))


class FlagsTest(unittest.TestCase):
    def test_flags(self):
        r = run(
            repo(
                ["claude-code"],
                license_spdx=None,
                pushed_at="2025-06-01T00:00:00Z",
                created_at="2025-01-01T00:00:00Z",
            )
        )
        self.assertEqual(r.flags, ["no_license", "inactive"])
        r = run(repo(["claude-code"], created_at="2026-11-10T00:00:00Z", archived=True))
        self.assertEqual(r.flags, ["inactive", "very_new"])

    def test_missing_tree_never_hides(self):
        r = classify.classify(repo(["mcp-server"]), None, now=NOW)
        self.assertEqual(r.status, "pending")


class GatherSignalsTest(unittest.TestCase):
    def test_fetches_tree_then_stops_at_first_mcp_manifest(self):
        tree = {
            "truncated": False,
            "tree": [
                {"path": "package.json", "type": "blob"},
                {"path": "server/package.json", "type": "blob"},
                {"path": "src", "type": "tree"},
            ],
        }
        manifest = json.dumps({"dependencies": {"@modelcontextprotocol/sdk": "1"}})
        urls = []

        def transport(url, headers):
            urls.append((url, headers["Accept"]))
            if "/git/trees/" in url:
                return Response(200, {}, json.dumps(tree).encode())
            return Response(200, {}, manifest.encode())

        client = GitHubClient(transport=transport, sleep=lambda s: None)
        paths, deps, truncated = classify.gather_signals(client, repo(["mcp-server"]))
        self.assertEqual(paths, ["package.json", "server/package.json"])
        self.assertEqual(list(deps), ["package.json"])  # stopped after the first hit
        self.assertFalse(truncated)
        self.assertEqual(len(urls), 2)
        self.assertIn("recursive=1", urls[0][0])
        self.assertEqual(urls[1][1], "application/vnd.github.raw+json")

    def test_empty_or_missing_repo_gives_no_tree(self):
        def transport(url, headers):
            return Response(409, {}, b'{"message":"Git Repository is empty."}')

        client = GitHubClient(transport=transport, sleep=lambda s: None)
        self.assertEqual(classify.gather_signals(client, repo(["mcp-server"])), (None, {}, False))

    def test_non_mcp_repo_costs_one_request(self):
        calls = []

        def transport(url, headers):
            calls.append(url)
            body = {"tree": [{"path": "package.json", "type": "blob"}]}
            return Response(200, {}, json.dumps(body).encode())

        client = GitHubClient(transport=transport, sleep=lambda s: None)
        classify.gather_signals(client, repo(["claude-code"], description="slash commands"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
