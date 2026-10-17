"""The advisor's findings, read out of one portfolio-report.md revision.

Moved here from `g15-corpus.py` on 2026-09-12 so that the G15 corpus, the G15
replay and the proposer all read a report through one parser. Three copies of
these regexes would drift apart, and the gate would then be measuring a
different set of findings from the one the proposer acts on.

The parser only understands one layout: numbered, bold-titled items under
"## Cross-project priorities", and "- **Issues:**" alone on its line followed
by indented bullets in each repo section. The advisor's prompt pins that layout
because the model does not keep it on its own -- 14 of the first 21 committed
revisions used some other shape and yield nothing here.
"""

from __future__ import annotations

import re

# A finding is a bullet under a repo section's "**Issues:**" line, plus the
# numbered entries under "## Cross-project priorities". Those two carry the
# advisor's actual judgements; everything else in the report is state
# description (last activity, branch, recent work) that nothing should act on.
ISSUES_RE = re.compile(r"^\s*-\s+\*\*Issues:\*\*\s*$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^\s{2,}-\s+(.+?)\s*$")
PRIORITY_RE = re.compile(r"^\d+\.\s+\*\*(.+?)\*\*", re.S)


def normalise(text: str) -> str:
    """A finding restated with different numbers is the same finding.

    The advisor rewrites its prose nightly and its counts drift ("21 days" ->
    "23 days"), so a raw-text key would report one finding per night and make
    the corpus look far richer than it is. Digits are dropped and the first
    words kept.
    """
    flat = re.sub(r"[*`_]", "", text.lower())
    flat = re.sub(r"\d+", "#", flat)
    flat = re.sub(r"\s+", " ", flat).strip()
    return " ".join(flat.split()[:12])


def key(repo: str, text: str) -> str:
    """The identity a label is stored under in g15-corpus.json."""
    return f"{repo}::{normalise(text)}"


def is_cross_project(repo: str) -> bool:
    return repo.lower().startswith("cross-project")


def extract(text: str) -> list[tuple[str, str]]:
    """(repo, finding) pairs from one report revision."""
    findings: list[tuple[str, str]] = []
    section = "(cross-project)"
    in_issues = False

    for line in text.splitlines():
        header = SECTION_RE.match(line)
        if header:
            section = header.group(1).strip()
            in_issues = False
            continue

        if is_cross_project(section):
            hit = PRIORITY_RE.match(line.strip())
            if hit:
                findings.append((section, hit.group(1).strip()))
            continue

        if ISSUES_RE.match(line):
            in_issues = True
            continue
        # Any non-indented line ends the Issues block.
        if in_issues and line.strip() and not line.startswith("  "):
            in_issues = False

        if in_issues:
            bullet = BULLET_RE.match(line)
            if bullet:
                findings.append((section, bullet.group(1).strip()))

    return findings
