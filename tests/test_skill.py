from __future__ import annotations

from pathlib import Path

import pytest

from apple_docs_mcp.skill import (
    SkillMissingError,
    find_skill_root,
    load_playbook,
    read_reference,
    reference_names,
)

SKILL_TEXT = """---
name: swift
description: Use when building or reviewing Apple-platform Swift code.
---

# Swift playbook

Never invent an API.

## Reference map

- `swiftui.md` — state and data flow.
"""


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "claude-skills" / "swift"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    (root / "references" / "swiftui.md").write_text("state and data flow", encoding="utf-8")
    (root / "references" / "concurrency.md").write_text("actors", encoding="utf-8")
    return root


def test_find_skill_root_accepts_a_directory_with_a_playbook(skill_root: Path) -> None:
    assert find_skill_root(skill_root) == skill_root


def test_find_skill_root_rejects_a_directory_without_one(tmp_path: Path) -> None:
    assert find_skill_root(tmp_path) is None
    assert find_skill_root(None) is None


def test_load_playbook_parses_frontmatter_and_lists_references(skill_root: Path) -> None:
    playbook = load_playbook(skill_root)

    assert playbook.name == "swift"
    assert playbook.description.startswith("Use when building")
    assert playbook.body.startswith("# Swift playbook")
    assert "---" not in playbook.body
    assert playbook.references == ("concurrency", "swiftui")


def test_load_playbook_without_a_root_raises_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(SkillMissingError):
        load_playbook(tmp_path / "absent")


def test_reference_names_is_empty_without_a_playbook(tmp_path: Path) -> None:
    assert reference_names(tmp_path / "absent") == []


def test_read_reference_returns_the_file(skill_root: Path) -> None:
    assert read_reference("swiftui", skill_root) == "state and data flow"


def test_read_reference_refuses_a_path_traversal(skill_root: Path) -> None:
    for topic in ("../SKILL", "references/swiftui", "/etc/passwd", ".hidden"):
        with pytest.raises(ValueError):
            read_reference(topic, skill_root)


def test_read_reference_reports_what_is_available(skill_root: Path) -> None:
    with pytest.raises(SkillMissingError) as error:
        read_reference("absent", skill_root)

    assert "swiftui" in str(error.value)
