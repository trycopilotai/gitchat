"""Pin the README's CI status badges to the workflow files.

A workflow that only runs when another workflow calls it gets no badge. Every other
workflow needs exactly one linked badge between the README title and its first
second-level heading, so a new workflow cannot ship without one. lint.yml and
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
    """Return the lines, the title's index, and the first ## heading's index."""
    lines = text.split("\n")
    title = None
    first_section = len(lines)
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
        elif line.startswith("## ") and first_section == len(lines):
            first_section = index
    if title is None:
        raise AssertionError("README has no title")
    return lines, title, first_section


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
        lines, title, first_section = scan_readme(readme)
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
                title < at[0] < first_section,
                "%s: badge on line %d is outside the title and the first section"
                % (path.name, at[0] + 1),
            )
        self.assertGreater(runnable, 0, "no runnable workflow found")

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
