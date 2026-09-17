"""Serve the Swift engineering playbook beside the documentation corpus.

The playbook is an authored skill, not generated documentation, so it is read
from where it is maintained instead of being copied here: one source of truth,
no drift between two repositories. Only the paths are configured.

Serving it from this server is what makes the pair useful in one connection —
the playbook says which rule applies, the corpus proves the signature — and it
reaches clients that have no skills mechanism of their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "SKILL_FILE",
    "Playbook",
    "SkillMissingError",
    "find_skill_root",
    "load_playbook",
    "read_reference",
    "reference_names",
]

SKILL_FILE: Final = "SKILL.md"
REFERENCE_DIR: Final = "references"

_FRONTMATTER: Final = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME: Final = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESCRIPTION: Final = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


class SkillMissingError(RuntimeError):
    """The Swift playbook is not present at the configured location."""


@dataclass(frozen=True, slots=True)
class Playbook:
    """The authored skill: its rules file and the judgement files it references."""

    name: str
    description: str
    body: str
    references: tuple[str, ...]
    root: Path


def find_skill_root(candidate: Path | None = None) -> Path | None:
    """Return the skill root when it holds a playbook, else ``None``."""
    if candidate is None:
        return None
    if (candidate / SKILL_FILE).is_file():
        return candidate
    return None


def load_playbook(root: Path | None) -> Playbook:
    """Read the playbook's front matter, rules text, and reference names."""
    resolved = find_skill_root(root)
    if resolved is None:
        raise SkillMissingError(
            f"No Swift playbook found at {root}. Point APPLE_DOCS_SWIFT_SKILL at the "
            "skill directory that holds SKILL.md."
        )

    body = (resolved / SKILL_FILE).read_text(encoding="utf-8")
    frontmatter = _FRONTMATTER.match(body)
    header = frontmatter.group(1) if frontmatter else ""
    name = _NAME.search(header)
    description = _DESCRIPTION.search(header)
    rules = body[frontmatter.end() :].strip() if frontmatter else body.strip()

    return Playbook(
        name=name.group(1).strip() if name else resolved.name,
        description=description.group(1).strip() if description else "",
        body=rules,
        references=tuple(reference_names(resolved)),
        root=resolved,
    )


def reference_names(root: Path | None) -> list[str]:
    """Return the available reference topics, sorted."""
    resolved = find_skill_root(root)
    if resolved is None:
        return []
    directory = resolved / REFERENCE_DIR
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.md") if path.is_file())


def read_reference(topic: str, root: Path | None) -> str:
    """Return one reference file's text.

    ``topic`` arrives from a client, so anything that could escape the
    reference directory is refused rather than resolved.
    """
    resolved = find_skill_root(root)
    if resolved is None:
        raise SkillMissingError(
            f"No Swift playbook found at {root}. Point APPLE_DOCS_SWIFT_SKILL at the "
            "skill directory that holds SKILL.md."
        )
    if topic != Path(topic).name or topic.startswith("."):
        raise ValueError(f"{topic!r} is not a reference topic name")

    directory = resolved / REFERENCE_DIR
    path = directory / f"{topic}.md"
    if not path.is_file() or path.parent != directory:
        available = ", ".join(reference_names(resolved)) or "none"
        raise SkillMissingError(f"No reference named {topic!r}. Available: {available}.")
    return path.read_text(encoding="utf-8")
