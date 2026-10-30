"""Pin the README header layout and its CI status badges to the workflow files.

The header is the title, a one-sentence subtitle, a `---` rule, the CI badges, and a
second `---` rule, then the rest of the README. A workflow that only runs when another
workflow calls it gets no badge. Every other workflow needs exactly one linked badge
between the two rules, so a new workflow cannot ship without one. lint.yml and
auto-lint-pr.yml must select the same files.
"""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/trycopilotai/gitchat"
WORKFLOWS = ROOT / ".github" / "workflows"


def clean_scalar(value):
    value = value.strip()
    if len(value) > 1 and value[0] in ("'", '"') and value[-1] == value[0]:
        return value[1:-1]
    return value.split(" #")[0].strip()


def read_workflow(path):
    """Return the workflow's name and event names; stop on a form it cannot read."""
    name = None
    events = []
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            continue
        key, _, rest = line.partition(":")
        key = key.strip().strip("\"'")
        rest = rest.strip()
        if key == "name":
            name = clean_scalar(rest)
        elif key == "on":
            if rest.startswith("["):
                items = rest.strip("[]").split(",")
                events = [item.strip().strip("\"'") for item in items if item.strip()]
            elif rest and not rest.startswith("#"):
                events = [clean_scalar(rest)]
            else:
                child_indent = None
                while index < len(lines):
                    child = lines[index]
                    if not child.strip() or child.lstrip().startswith("#"):
                        index += 1
                        continue
                    indent = len(child) - len(child.lstrip(" "))
                    if indent == 0:
                        break
                    if child_indent is None:
                        child_indent = indent
                    if indent == child_indent:
                        event = child.strip()
                        if event.startswith("- "):
                            event = event[2:]
                        events.append(event.split(":")[0].strip().strip("\"'"))
                    index += 1
    if not name:
        raise AssertionError("%s: no top-level name" % path.name)
    if not events:
        raise AssertionError("%s: no events under on:" % path.name)
    return name, events


def scan_readme(text):
    """Return the lines and the indexes of the title and of the first two --- rules."""
    lines = text.split("\n")
    title = None
    rules = []
    fence = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            continue
        if title is None:
            if line.startswith("# ") or "<h1" in line:
                title = index
        elif line.strip() == "---" and len(rules) < 2:
            rules.append(index)
    if title is None:
        raise AssertionError("README has no title")
    if len(rules) < 2:
        raise AssertionError("README needs two --- rules under the title and subtitle")
    return lines, title, rules[0], rules[1]


def workflow_inputs(path):
    """Return the paths: and languages: values of a workflow, as token lists."""
    lines = path.read_text(encoding="utf-8").splitlines()
    result = {"paths": [], "languages": []}
    index = 0
    while index < len(lines):
        match = re.match(r"^\s*(paths|languages):\s*(.*)$", lines[index])
        index += 1
        if match is None:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in ("'", '"') and not (len(value) > 1 and value[-1] == value[0]):
            while index < len(lines):
                value += " " + lines[index].strip()
                index += 1
                if value.rstrip()[-1:] == value[0]:
                    break
        elif not value:
            value = lines[index].strip()
            index += 1
            while value[:1] in ("'", '"') and value.rstrip()[-1:] != value[0]:
                value += " " + lines[index].strip()
                index += 1
        value = clean_scalar(value)
        if key == "paths":
            result[key] = shlex.split(value)
        else:
            result[key] = [item.strip() for item in value.split(",") if item.strip()]
    return result


class ReadmeBadgeTest(unittest.TestCase):
    def test_every_runnable_workflow_has_one_badge_below_the_title(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        lines, title, rule_one, rule_two = scan_readme(readme)
        paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        runnable = 0
        for path in paths:
            name, events = read_workflow(path)
            link = REPO_URL + "/actions/workflows/" + path.name
            image = link + "/badge.svg"
            if events == ["workflow_call"]:
                self.assertNotIn(image, readme, path.name + " is reusable-only")
                continue
            runnable += 1
            badge = "[![" + name + "](" + image + ")](" + link + ")"
            self.assertEqual(readme.count(badge), 1, path.name + ": " + badge)
            self.assertEqual(readme.count(image), 1, path.name + " badge image")
            at = [i for i, line in enumerate(lines) if badge in line]
            self.assertTrue(
                rule_one < at[0] < rule_two,
                "%s: badge on line %d is not between the two --- rules"
                % (path.name, at[0] + 1),
            )
        self.assertGreater(runnable, 0, "no runnable workflow found")

    def test_header_is_title_subtitle_rule_badges_rule(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        lines, title, rule_one, rule_two = scan_readme(readme)
        for rule in (rule_one, rule_two):
            self.assertEqual(
                lines[rule - 1].strip(), "", "a --- rule needs a blank line above it"
            )
            self.assertEqual(
                lines[rule + 1].strip(), "", "a --- rule needs a blank line below it"
            )
        subtitle = [line for line in lines[title + 1 : rule_one] if line.strip()]
        self.assertTrue(
            subtitle, "no subtitle between the title and the first --- rule"
        )
        blocks = 0
        previous_blank = True
        for line in lines[title + 1 : rule_one]:
            if line.strip() and previous_blank:
                blocks += 1
            previous_blank = not line.strip()
        self.assertEqual(blocks, 1, "the subtitle must be one paragraph")
        for line in lines[rule_one + 1 : rule_two]:
            if not line.strip():
                continue
            self.assertTrue(
                "badge.svg" in line
                or line.strip() in ('<div align="center">', "</div>"),
                "only badges may sit between the --- rules: " + line[:60],
            )

    def test_lint_and_auto_lint_pr_select_the_same_files(self):
        lint = WORKFLOWS / "lint.yml"
        auto = WORKFLOWS / "auto-lint-pr.yml"
        self.assertTrue(lint.is_file(), "lint.yml is missing")
        self.assertTrue(auto.is_file(), "auto-lint-pr.yml is missing")
        lint_inputs = workflow_inputs(lint)
        auto_inputs = workflow_inputs(auto)
        for key in ("paths", "languages"):
            self.assertEqual(lint_inputs[key], auto_inputs[key], key)
        for relative in lint_inputs["paths"]:
            self.assertTrue((ROOT / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
