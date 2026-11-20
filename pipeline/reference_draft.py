"""Draft vi/en descriptions for reference items the YAML does not have yet.

The docs' own text is given to the model only as a brief. Anthropic's docs are copyrighted, so
a draft that repeats 6 or more consecutive words of it is rejected and retried alone. Drafts
are saved with reviewed: false; the owner reads them before the app ships (PLAN.md 12.2).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

from pipeline import reference
from pipeline.docs import DocItem
from pipeline.llm import LLMError, Provider

BATCH = 25
MAX_LEN = 160
COPY_NGRAM = 6

SYSTEM_PROMPT = """\
You write one-line descriptions for a Claude Code reference inside AIGym Code, an app for
developers. For each item you get its exact name, its syntax and a brief taken from the
official docs.

- Write a new description in your own words. Never copy phrases from the brief.
- Use only facts in the brief. Do not add defaults, versions or behaviour it does not state.
- One plain sentence per language, at most 150 characters. No markdown, no links, no emoji.
- Keep names of commands, flags, keys, variables, files and values exactly as written.
- English starts with a verb in third person ("Adds…", "Sets…", "Runs when…").
- Vietnamese is natural and concise for Vietnamese developers. Keep the English terms they
  already use (Claude Code, prompt, token, hook, skill, subagent, plugin, MCP server, session,
  context, model, repo, commit, terminal) and start with a verb ("Thêm…", "Đặt…", "Chạy khi…").
- Return every item you were given, with its name unchanged.
"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "en": {"type": "STRING"},
                    "vi": {"type": "STRING"},
                },
                "required": ["name", "en", "vi"],
                "propertyOrdering": ["name", "en", "vi"],
            },
        }
    },
    "required": ["items"],
}

_WORD = re.compile(r"[a-z0-9_./-]+")


def copied_phrase(ours: str, docs_text: str, n: int = COPY_NGRAM) -> str | None:
    """The first run of n words our text shares with the docs, or None."""
    ours_words = _WORD.findall(ours.lower())
    docs_words = _WORD.findall(docs_text.lower())
    docs_grams = {tuple(docs_words[i : i + n]) for i in range(len(docs_words) - n + 1)}
    for i in range(len(ours_words) - n + 1):
        gram = tuple(ours_words[i : i + n])
        if gram in docs_grams:
            return " ".join(gram)
    return None


def problems(item: DocItem, en: str, vi: str) -> list[str]:
    found = []
    for lang, text in (("en", en), ("vi", vi)):
        if not 10 <= len(text) <= MAX_LEN:
            found.append(f"{lang} length {len(text)}")
        if "http" in text or "**" in text or "](" in text:
            found.append(f"{lang} has markdown or a link")
    phrase = copied_phrase(en, item.docs_text)
    if phrase:
        found.append(f"copies the docs: '{phrase}'")
    if vi.strip() == en.strip():
        found.append("vi equals en")
    return found


def _prompt(batch: list[DocItem]) -> str:
    lines = []
    for item in batch:
        lines.append(f"- name: {item.name}\n  syntax: {item.syntax}\n  brief: {item.docs_text}")
    return "Items:\n" + "\n".join(lines)


def draft(
    provider: Provider,
    doc_items: list[DocItem],
    today: date,
    log: Callable[[str], None] = print,
) -> tuple[list[reference.Entry], list[str]]:
    """Returns (new entries, names that could not be drafted). Failed items are tried once
    more on their own; whatever still fails is left for the next run."""
    done: list[reference.Entry] = []
    retry: list[DocItem] = []
    failed: list[str] = []

    def attempt(batch: list[DocItem], last_chance: bool) -> None:
        try:
            answer = provider.generate_json(SYSTEM_PROMPT, _prompt(batch), SCHEMA)
        except LLMError as err:
            log(f"draft: batch of {len(batch)} failed: {err}")
            if last_chance:
                failed.extend(i.name for i in batch)
            else:
                retry.extend(batch)
            return
        by_name = {str(a.get("name")): a for a in answer.get("items") or []}
        for item in batch:
            a = by_name.get(item.name)
            en = str((a or {}).get("en", "")).strip()
            vi = str((a or {}).get("vi", "")).strip()
            issues = ["missing from the answer"] if a is None else problems(item, en, vi)
            if issues:
                log(f"draft: {item.name}: {'; '.join(issues)}")
                if last_chance:
                    failed.append(item.name)
                else:
                    retry.append(item)
                continue
            done.append(reference.entry_from_doc(item, {"vi": vi, "en": en}, today))

    for start in range(0, len(doc_items), BATCH):
        attempt(doc_items[start : start + BATCH], last_chance=False)
        log(f"draft: {min(start + BATCH, len(doc_items))}/{len(doc_items)}")
    for item in list(retry):
        attempt([item], last_chance=True)
    return done, failed
