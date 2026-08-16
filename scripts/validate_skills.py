#!/usr/bin/env python3
"""Validate the reusable Skill assets shipped with ProofMesh."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    try:
        raw = text.split("---\n", 2)[1]
    except IndexError as exc:
        raise ValueError("unterminated YAML frontmatter") from exc
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def main() -> None:
    errors: list[str] = []
    paths = sorted(SKILLS.glob("*/SKILL.md"))
    if len(paths) != 7:
        errors.append(f"expected 7 Skills, found {len(paths)}")

    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            meta = frontmatter(text)
        except ValueError as exc:
            errors.append(f"{path.parent.name}: {exc}")
            continue
        name = meta.get("name", "")
        description = meta.get("description", "")
        if name != path.parent.name or not NAME.fullmatch(name):
            errors.append(f"{path.parent.name}: invalid or mismatched name {name!r}")
        if len(description) < 80 or not re.search(
            r"\bUse (?:when|after|on|only)\b",
            description,
            flags=re.IGNORECASE,
        ):
            errors.append(f"{name}: description must include a concrete usage trigger")
        body = text.split("---\n", 2)[-1]
        if not re.search(
            r"\bnever\b|\bdo not\b|\bstop\b|\btreat\b|\bfail closed\b",
            body,
            flags=re.IGNORECASE,
        ):
            errors.append(f"{name}: missing explicit safety/failure boundary")
        if len(re.findall(r"^\d+\. ", body, flags=re.MULTILINE)) < 4:
            errors.append(f"{name}: workflow is not sufficiently specified")

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Skill assets valid: {len(paths)}/7")


if __name__ == "__main__":
    main()
