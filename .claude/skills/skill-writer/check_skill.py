#!/usr/bin/env python3
"""Check a skill against the rules that can be checked mechanically.

    python3 check_skill.py <skill-dir>      one skill
    python3 check_skill.py <skills-dir>     every skill under it, plus the
                                            total L1 cost of the library
    python3 check_skill.py --self-test      prove each rule still fires

Exit 0 when nothing failed, 1 otherwise. Warnings do not fail: the token bands
are what the survey observed, not limits anybody enforces, so exceeding one is a
thing to know rather than a thing to forbid.

Standard library only, on purpose. A checker that needs installing is a checker
that gets skipped, and this file is itself a bundled script — the thing the
survey measured at 2.12x the vulnerability rate — so it had better be readable
in one sitting and reach for nothing.

Token counts are chars/4. That is an estimate and is labelled as one everywhere
it is printed; the decisions it informs are order-of-magnitude.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Level 1 — frontmatter, pre-loaded into the system prompt for *every*
#: installed skill whether it triggers or not. The survey reports ~30 tokens per
#: skill as typical. Past the warn line you are paying for prose nobody routes
#: on; past the fail line one skill is costing more than most whole libraries.
L1_WARN, L1_FAIL = 60, 200

#: Level 2 — the SKILL.md body, loaded on trigger. The survey's observed band is
#: 300-3k tokens. Below the floor is usually fine (a skill can be a few dozen
#: lines); above the ceiling means Level 3 is being inlined.
L2_FLOOR, L2_CEILING, L2_FAIL = 300, 3000, 6000

#: Phrases that ask a person to stop being asked. The survey's prompt-injection
#: finding is that a benign task-specific approval with "Don't ask again"
#: carries over to closely related but harmful actions, so a skill must never
#: request one.
BLANKET_APPROVAL = (
    "don't ask again",
    "dont ask again",
    "do not ask again",
    "always approve",
    "auto-approve",
    "without asking",
    "skip confirmation",
    "no confirmation",
)

SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".ts", ".rb", ".pl", ".ps1", ".bash")


class Report:
    """What one skill's check found, and whether it failed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: list[tuple[str, str]] = []
        self.failed = False

    def ok(self, text: str) -> None:
        self.lines.append(("ok", text))

    def warn(self, text: str) -> None:
        self.lines.append(("warn", text))

    def fail(self, text: str) -> None:
        self.lines.append(("FAIL", text))
        self.failed = True

    def show(self) -> None:
        print(f"\n{self.name}")
        for level, text in self.lines:
            print(f"  {level:>4}  {text}")


def tokens(text: str) -> int:
    """A rough token count. chars/4, which is close enough to budget with."""
    return len(text) // 4


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """``(frontmatter, body)``, or ``None`` when there is no frontmatter."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def parse_frontmatter(raw: str) -> dict[str, str]:
    """Flat ``key: value`` pairs, with continuation lines folded in.

    Hand-parsed rather than imported: skill frontmatter is flat by
    specification, and a YAML dependency would make this script something you
    have to install before you can check anything.
    """
    found: dict[str, str] = {}
    key = ""
    for line in raw.splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", line)
        if match:
            key = match.group(1)
            found[key] = match.group(2).strip()
        elif key and line.strip():
            found[key] = (found[key] + " " + line.strip()).strip()
    return found


def linked(body: str) -> set[str]:
    """Paths the body says to *load* — markdown links, and only those.

    A backticked filename is prose. ``config/schema.py`` in a sentence about
    where ground truth lives is not a bundled resource, and treating it as one
    reports a missing file for every repository path a skill mentions.
    """
    found = set(re.findall(r"\]\(([^)#:]+)\)", body))
    return {p.strip() for p in found if not p.startswith(("http", "/", "#"))}


def mentioned(body: str) -> set[str]:
    """Paths the body names at all, links or backticks.

    Used only to decide whether a bundled file is reachable: a script invoked as
    ``python3 check_skill.py`` is named without being linked, and is not an
    orphan.
    """
    found = linked(body)
    found |= set(re.findall(r"`([\w./-]+\.(?:md|py|sh|json|ya?ml|txt))`", body))
    found |= set(re.findall(r"([\w./-]+\.(?:py|sh|js|ts|rb|pl|ps1|bash))", body))
    return {p.strip() for p in found if not p.startswith(("http", "/", "#"))}


def check(skill: Path) -> Report:
    report = Report(skill.name)
    doc = skill / "SKILL.md"
    if not doc.is_file():
        report.fail("no SKILL.md")
        return report

    text = doc.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        report.fail("no YAML frontmatter — Level 1 is what the router reads")
        return report
    raw, body = split
    meta = parse_frontmatter(raw)

    # --- Level 1: paid on every request, for every installed skill ---
    for field in ("name", "description"):
        if not meta.get(field):
            report.fail(f"frontmatter has no {field}")
    name = meta.get("name", "")
    if name and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        report.fail(f"name {name!r} is not kebab-case")
    if name and name != skill.name:
        report.fail(f"name {name!r} does not match directory {skill.name!r}")

    l1 = tokens(raw)
    if l1 > L1_FAIL:
        report.fail(f"Level 1 is ~{l1} tokens, paid on every request (limit {L1_FAIL})")
    elif l1 > L1_WARN:
        report.warn(f"Level 1 is ~{l1} tokens, paid on every request (~30 is typical)")
    else:
        report.ok(f"Level 1 ~{l1} tokens")

    # --- Level 2: paid on trigger ---
    l2 = tokens(body)
    if l2 > L2_FAIL:
        report.fail(f"Level 2 is ~{l2} tokens — Level 3 is being inlined (limit {L2_FAIL})")
    elif l2 > L2_CEILING:
        report.warn(f"Level 2 is ~{l2} tokens, above the observed {L2_CEILING} band")
    elif l2 < L2_FLOOR:
        report.ok(f"Level 2 ~{l2} tokens (under the {L2_FLOOR} band, which is fine)")
    else:
        report.ok(f"Level 2 ~{l2} tokens")

    # --- Level 3: loaded only when Level 2 names it ---
    points_at = linked(body)
    bundled = {
        p.relative_to(skill).as_posix()
        for p in skill.rglob("*")
        if p.is_file() and p.name != "SKILL.md"
    }
    reachable = {m.rsplit("/", 1)[-1] for m in mentioned(body)} | mentioned(body)
    missing = sorted(p for p in points_at if not (skill / p).exists())
    orphans = sorted(
        p for p in bundled if p not in reachable and p.rsplit("/", 1)[-1] not in reachable
    )
    if missing:
        report.fail(f"Level 2 points at files that do not exist: {missing}")
    for orphan in orphans:
        report.warn(f"{orphan} is bundled but nothing in SKILL.md names it, so it never loads")
    if points_at and not missing:
        report.ok(f"{len(points_at)} Level 3 file(s), all present")

    # --- scripts: the measured risk multiplier ---
    scripts = sorted(p for p in bundled if p.endswith(SCRIPT_SUFFIXES))
    if scripts:
        report.warn(
            f"bundles {len(scripts)} script(s) — 2.12x the measured vulnerability rate, "
            f"and needs trust tier T3+: {scripts}"
        )

    # --- approval ---
    lowered = text.lower()
    asked = [phrase for phrase in BLANKET_APPROVAL if phrase in lowered]
    if asked:
        report.fail(f"asks for blanket approval {asked} — it carries over to harmful actions")

    return report


# --- negative tests ---------------------------------------------------------
#
# The rule this satisfies is its own: write negative tests for the failure modes
# you claim to catch. A checker nobody has watched fail is a checker that
# silently stopped checking.

#: (directory, SKILL.md, extra files, substring the failure must contain)
BROKEN: tuple[tuple[str, str, dict[str, str], str], ...] = (
    ("no-frontmatter", "# nothing above this", {}, "no YAML frontmatter"),
    ("wrong-name", "---\nname: other\ndescription: d\n---\nbody", {}, "does not match directory"),
    ("not-kebab", "---\nname: Not_Kebab\ndescription: d\n---\nbody", {}, "kebab-case"),
    ("no-description", "---\nname: no-description\n---\nbody", {}, "no description"),
    (
        "fat-level-one",
        "---\nname: fat-level-one\ndescription: " + ("padding " * 250) + "\n---\nbody",
        {},
        "paid on every request",
    ),
    (
        "fat-level-two",
        "---\nname: fat-level-two\ndescription: d\n---\n" + ("body " * 7000),
        {},
        "Level 3 is being inlined",
    ),
    (
        "broken-link",
        "---\nname: broken-link\ndescription: d\n---\nSee [x](reference/gone.md).",
        {},
        "do not exist",
    ),
    (
        "blanket-approval",
        "---\nname: blanket-approval\ndescription: d\n---\nChoose Don't ask again.",
        {},
        "blanket approval",
    ),
)

#: (directory, SKILL.md, extra files, substring a *warning* must contain)
SMELLY: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "orphan",
        "---\nname: orphan\ndescription: d\n---\nbody",
        {"reference/stray.md": "nothing names me"},
        "never loads",
    ),
    (
        "bundles-script",
        "---\nname: bundles-script\ndescription: d\n---\nRun helper.py.",
        {"helper.py": "print(1)"},
        "2.12x",
    ),
)


def self_test() -> int:
    """Build each broken skill in a temp directory and confirm the rule fires."""
    import tempfile

    passed = failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = [(c, True) for c in BROKEN] + [(c, False) for c in SMELLY]
        for (name, doc, extras, wanted), must_fail in cases:
            skill = root / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(doc, encoding="utf-8")
            for rel, content in extras.items():
                target = skill / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            report = check(skill)
            said = [text for level, text in report.lines if wanted in text]
            level_ok = report.failed if must_fail else not report.failed
            if said and level_ok:
                passed += 1
                print(f"  ok    {name:18} {'fails' if must_fail else 'warns'} on {wanted!r}")
            else:
                failed += 1
                why = (
                    "did not fail" if must_fail and not report.failed else f"never said {wanted!r}"
                )
                print(f"  FAIL  {name:18} {why}")
    print(f"\n{passed}/{passed + failed} rules still fire")
    return 1 if failed else 0


def skills_under(path: Path) -> list[Path]:
    if (path / "SKILL.md").is_file():
        return [path]
    return sorted(p.parent for p in path.glob("*/SKILL.md"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    if argv[1] == "--self-test":
        return self_test()
    root = Path(argv[1]).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    found = skills_under(root)
    if not found:
        print(f"no SKILL.md under {root}")
        return 2

    reports = [check(skill) for skill in found]
    for report in reports:
        report.show()

    if len(found) > 1:
        total = sum(
            tokens(split[0])
            for skill in found
            if (split := split_frontmatter((skill / "SKILL.md").read_text(encoding="utf-8")))
        )
        print(f"\nlibrary: {len(found)} skills, ~{total} tokens of Level 1 on every request")

    bad = [r.name for r in reports if r.failed]
    print(f"\n{len(reports) - len(bad)}/{len(reports)} pass" + (f"; failed: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
