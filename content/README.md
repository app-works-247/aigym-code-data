# content

Free, public content shared by the pipeline and the AIGym Code app. Filled in during P3.

| Path | What | How it changes |
|---|---|---|
| `reference/<group>.yaml` | Claude Code reference: slash commands, CLI flags, settings, hooks, environment variables | `docs_watch` opens a PR when the official docs change; the owner reviews and merges |
| `templates/` | Config templates: CLAUDE.md, SKILL.md, subagent, hook, `.mcp.json`, `plugin.json` | Edited by hand, each one checked against a Claude Code version |
| `models.yaml` | Claude model table | `docs_watch` opens a PR when release notes, models or pricing pages change |

Descriptions are written in our own words (vi/en). Only names that cannot change (commands, flags, keys, variables) are copied verbatim, always with a link to the official docs.
