#!/usr/bin/env python3
"""The packaging contract.

Facts this repository states in more than one place are
pinned here where a script can compare them: the name and
version, the claim and the transcript behind it, the demo
images, the install blocks, and the evidence hashes.

Runs offline with the standard library and `git`:

    python3 tests/test_integrations.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "gitchat"
PACKAGE = ROOT / "skills" / NAME
SCRIPTS = PACKAGE / "scripts"
SEND = SCRIPTS / "gitchat_send.py"
POLL = SCRIPTS / "gitchat_poll.py"
CHANNEL = SCRIPTS / "gitchat_channel.py"
SKILL = PACKAGE / "SKILL.md"
README = ROOT / "README.md"
SECURITY = ROOT / "SECURITY.md"
TRANSCRIPT = ROOT / "evidence" / "transcripts" / "send-poll-session.txt"
MANIFEST = ROOT / "evidence" / "demo-manifest.json"
CLAIM = "A poll stops returning a prompt once it is answered."
REPOSITORY = "https://github.com/trycopilotai/" + NAME
SECURITY_STATEMENTS = (
    "Push access to the remote equals prompt execution with full agent "
    "permissions on the serving host.",
    "The default workers disable approvals and sandboxing and fetch "
    "packages at run time.",
    "The orchestration guard is pattern matching, not a security boundary.",
    "Use only a private remote with trusted writers.",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def plain(path: Path) -> str:
    """The file's text with markdown emphasis removed and spaces collapsed."""
    return " ".join(read(path).replace("**", "").split())


def session_blocks(transcript: str) -> list:
    """(command, output lines, exit status) for each recorded command."""
    result = []
    lines = transcript.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.startswith("$ ") or line.endswith(" }"):
            continue
        output = []
        while lines[index] != '$ echo "exit status: $?"':
            output.append(lines[index])
            index += 1
        status = lines[index + 1]
        index += 2
        result.append((line[2:], output, status))
    return result


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(product: str) -> dict:
    return json.loads(read(ROOT / product / "plugin.json"))


def frontmatter(text: str) -> dict:
    """The `key: value` pairs between the two `---` lines."""
    lines = text.splitlines()
    if lines[0] != "---":
        raise AssertionError("SKILL.md does not open with frontmatter")
    end = lines.index("---", 1)
    fields: dict = {}
    key = None
    for line in lines[1:end]:
        match = re.match(r"^([a-z_-]+):\s*(.*)$", line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip()
            continue
        if key is None or not line.startswith(" "):
            raise AssertionError("unexpected frontmatter line: " + line)
        fields[key] = (fields[key] + " " + line.strip()).strip()
    for name, value in fields.items():
        if value.startswith(">-"):
            fields[name] = value[2:].strip()
    return fields


def interface_yaml(text: str) -> dict:
    """The quoted scalars under `interface:` in agents/openai.yaml."""
    lines = text.splitlines()
    if lines[0] != "interface:":
        raise AssertionError("openai.yaml does not start with interface:")
    fields: dict = {}
    key = None
    for line in lines[1:]:
        match = re.match(r"^  ([a-z_]+):\s*(.*)$", line)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip()
            continue
        fields[key] = (fields[key] + " " + line.strip()).strip()
    for name, value in fields.items():
        if not (value.startswith('"') and value.endswith('"')):
            raise AssertionError(name + " is not a double-quoted scalar")
        fields[name] = value[1:-1]
    return fields


def install_blocks() -> list:
    return re.findall(r"```sh\nset -eu\n(.*?)```", read(README), flags=re.S)


class LayoutTest(unittest.TestCase):
    def test_skill_is_a_symlink_into_the_canonical_package(self) -> None:
        link = ROOT / "skill"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)), "skills/" + NAME)
        self.assertFalse(PACKAGE.is_symlink())

    def test_package_holds_what_the_readme_says_it_installs(self) -> None:
        for relative in (
            "SKILL.md",
            "agents/openai.yaml",
            "references/log-streaming.md",
            "references/running-indefinitely.md",
            "references/tiered-dispatch.md",
            "references/transport-scripts.md",
            "scripts/gitchat_channel.py",
            "scripts/gitchat_pick_worker.py",
            "scripts/gitchat_poll.py",
            "scripts/gitchat_send.py",
            "scripts/gitchat_serve.py",
            "scripts/gitchat_serve_forever.sh",
            "scripts/gitchat_stream_log.py",
            "scripts/gitchat_tail.py",
            "systemd/gitchat-serve@.service",
        ):
            self.assertTrue((PACKAGE / relative).is_file(), relative)

    def test_scripts_find_their_siblings(self) -> None:
        names = {path.name for path in SCRIPTS.iterdir() if path.is_file()}
        for path in sorted(SCRIPTS.iterdir()):
            if not path.is_file():
                continue
            for sibling in re.findall(r"gitchat_[a-z_]+\.(?:py|sh)", read(path)):
                self.assertIn(sibling, names, path.name)

    def test_history_has_no_co_author_trailer(self) -> None:
        messages = git("log", "--all", "--format=%B")
        self.assertNotIn("co-authored-by", messages.lower())


class SkillTest(unittest.TestCase):
    def test_frontmatter_is_name_and_description_only(self) -> None:
        fields = frontmatter(read(SKILL))
        self.assertEqual(sorted(fields), ["description", "name"])
        self.assertEqual(fields["name"], NAME)
        self.assertRegex(NAME, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(NAME), 64)
        self.assertTrue(fields["description"])
        self.assertLessEqual(len(fields["description"]), 1024)

    def test_skill_stays_under_five_hundred_lines(self) -> None:
        self.assertLess(len(read(SKILL).splitlines()), 500)

    def test_files_the_skill_points_at_exist(self) -> None:
        text = read(SKILL)
        relatives = set(
            re.findall(r"(?:scripts|references|systemd)/[A-Za-z0-9_@.-]+[a-z]", text)
        )
        self.assertIn("references/transport-scripts.md", relatives)
        self.assertIn("references/log-streaming.md", relatives)
        self.assertIn("references/tiered-dispatch.md", relatives)
        self.assertIn("scripts/gitchat_serve.py", relatives)
        for relative in sorted(relatives):
            self.assertTrue((PACKAGE / relative).is_file(), relative)


class ManifestTest(unittest.TestCase):
    def test_both_manifests_agree(self) -> None:
        claude = manifest(".claude-plugin")
        codex = manifest(".codex-plugin")
        for field in (
            "name",
            "version",
            "description",
            "license",
            "homepage",
            "repository",
            "skills",
        ):
            self.assertEqual(claude[field], codex[field], field)
        self.assertEqual(claude["name"], NAME)
        self.assertEqual(claude["skills"], "./skills/")
        self.assertEqual(claude["repository"], REPOSITORY)
        self.assertEqual(claude["license"], "MIT")
        self.assertRegex(claude["version"], r"^\d+\.\d+\.\d+$")

    def test_a_release_tag_on_head_is_the_manifest_version(self) -> None:
        tags = git("tag", "--points-at", "HEAD").split()
        releases = [tag for tag in tags if tag.startswith("v")]
        if not releases:
            self.skipTest("HEAD carries no release tag")
        self.assertEqual(releases, ["v" + manifest(".claude-plugin")["version"]])

    def test_codex_interface_matches_the_agent_file(self) -> None:
        interface = manifest(".codex-plugin")["interface"]
        for field in (
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "websiteURL",
        ):
            self.assertTrue(interface.get(field), field)
        prompts = interface["defaultPrompt"]
        self.assertEqual(len(prompts), 1)
        self.assertIn("$" + NAME, prompts[0])
        agent = interface_yaml(read(PACKAGE / "agents" / "openai.yaml"))
        self.assertEqual(agent["default_prompt"], prompts[0])
        self.assertEqual(agent["display_name"], interface["displayName"])
        self.assertEqual(agent["short_description"], interface["shortDescription"])


class ReadmeTest(unittest.TestCase):
    def test_claim_is_on_its_own_line(self) -> None:
        self.assertIn(CLAIM, read(README).splitlines())

    def test_transcript_shows_the_prompt_until_it_is_answered(self) -> None:
        session = session_blocks(read(TRANSCRIPT))
        self.assertEqual(len(session), 5)
        sent, first_poll, answered, second_poll, all_poll = session
        for _command, _output, status in session:
            self.assertEqual(status, "exit status: 0")
        prompt_id = json.loads(sent[1][0])["id"]
        self.assertEqual(first_poll[0], "poll --repo beta --slug beta")
        polled = json.loads("\n".join(first_poll[1]))
        self.assertEqual(polled["id"], prompt_id)
        self.assertEqual(polled["kind"], "prompt")
        self.assertEqual(polled["to"], "beta")
        self.assertIn("--kind response", answered[0])
        reply = json.loads(answered[1][0])
        self.assertEqual(reply["conversation_id"], prompt_id)
        self.assertIs(reply["published"], True)
        self.assertEqual(second_poll[0], first_poll[0])
        self.assertEqual(second_poll[1], [])
        self.assertEqual(all_poll[0], first_poll[0] + " --all")
        self.assertEqual(all_poll[1], ["[]"])

    def test_each_install_block_pins_the_manifest_version(self) -> None:
        version = manifest(".claude-plugin")["version"]
        blocks = install_blocks()
        self.assertEqual(len(blocks), 2)
        roots = []
        for block in blocks:
            self.assertEqual(
                re.findall(r"^release=(\S+)$", block, flags=re.M),
                ["v" + version],
            )
            self.assertIn(REPOSITORY + " \\\n", block)
            self.assertIn('--branch "$release"', block)
            target = re.findall(r'^install_target="\$HOME/(\S+)"$', block, flags=re.M)
            self.assertEqual(len(target), 1)
            roots.append(target[0])
        self.assertEqual(
            sorted(roots),
            [".agents/skills/" + NAME, ".claude/skills/" + NAME],
        )

    def test_relative_links_resolve(self) -> None:
        targets = re.findall(r"\]\(([^)#]+)\)", read(README))
        self.assertTrue(targets)
        for target in targets:
            if target.startswith("http"):
                continue
            self.assertTrue((ROOT / target).exists(), target)

    def test_readme_says_what_was_not_measured(self) -> None:
        text = " ".join(read(README).split())
        self.assertIn("Not measured, stated up front.", plain(README))
        self.assertIn("No agent used the skill", text)
        self.assertIn("has not been measured", text)

    def test_security_statement_is_in_readme_security_and_skill(self) -> None:
        for path in (README, SECURITY, SKILL):
            text = plain(path)
            for statement in SECURITY_STATEMENTS:
                self.assertIn(statement, text, path.name)

    def test_demo_is_offered_with_a_reduced_motion_poster(self) -> None:
        text = read(README)
        picture = re.search(r"<picture>(.*?)</picture>", text, flags=re.S)
        self.assertIsNotNone(picture)
        body = picture.group(1)
        self.assertIn('media="(prefers-reduced-motion: reduce)"', body)
        self.assertIn('srcset="assets/poster.svg"', body)
        self.assertIn('src="assets/demo.svg"', body)


class EvidenceTest(unittest.TestCase):
    def test_manifest_hashes_match_the_files(self) -> None:
        record = json.loads(read(MANIFEST))
        self.assertEqual(record["skill"]["sha256"], sha256(SKILL))
        programs = {item["path"]: item["sha256"] for item in record["programs"]}
        self.assertEqual(
            programs,
            {
                path.relative_to(ROOT).as_posix(): sha256(path)
                for path in (CHANNEL, POLL, SEND)
            },
        )
        self.assertEqual(record["output"]["sha256"], sha256(TRANSCRIPT))
        self.assertIs(record["output"]["edited"], False)
        self.assertEqual(record["output"]["transforms"], [])
        self.assertIs(record["agent"]["invoked_the_skill"], False)

    def test_manifest_commands_are_the_ones_in_the_transcript(self) -> None:
        record = json.loads(read(MANIFEST))
        commands = [
            line[2:]
            for line in read(TRANSCRIPT).splitlines()
            if line.startswith("$ ") and not line.startswith("$ echo")
        ]
        invocation = record["invocation"]
        self.assertEqual(
            invocation["shell_functions"] + invocation["commands"], commands
        )

    def test_a_fresh_session_behaves_as_the_transcript_shows(self) -> None:
        environment = dict(os.environ)
        environment.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            PYTHONDONTWRITEBYTECODE="1",
        )
        environment.pop("GITHUB_TOKEN", None)

        def run(*command: str) -> str:
            done = subprocess.run(
                command, capture_output=True, text=True, env=environment
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            remote = str(base / "remote.git")
            run("git", "init", "-q", "--bare", "-b", "main", remote)
            for name in ("alpha", "beta"):
                run("git", "init", "-q", "-b", "main", str(base / name))
                run("git", "-C", str(base / name), "remote", "add", "origin", remote)
            send = (sys.executable, str(SEND), "--message-dir", "messages/")
            poll = (
                sys.executable,
                str(POLL),
                "--message-dir",
                "messages/",
                "--state-dir",
                "state/",
                "--repo",
                str(base / "beta"),
                "--slug",
                "beta",
            )
            sent = json.loads(
                run(
                    *send,
                    "--repo",
                    str(base / "alpha"),
                    "--from",
                    "alpha",
                    "--to",
                    "beta",
                    "--msg",
                    "What is 2 + 2?",
                )
            )
            before = json.loads(run(*poll))
            self.assertEqual(before["id"], sent["id"])
            self.assertEqual(json.loads(run(*poll))["id"], sent["id"])
            run(
                *send,
                "--repo",
                str(base / "beta"),
                "--from",
                "beta",
                "--to",
                "alpha",
                "--kind",
                "response",
                "--reply-to",
                sent["id"],
                "--conversation-id",
                sent["id"],
                "--msg",
                "4",
            )
            self.assertEqual(run(*poll), "")
            self.assertEqual(json.loads(run(*poll, "--all")), [])

    def test_transcript_carries_no_capture_path(self) -> None:
        text = read(TRANSCRIPT)
        for marker in ("/var/folders", "/tmp", "/Users", "/home"):
            self.assertNotIn(marker, text)


class DemoTest(unittest.TestCase):
    def test_images_agree_with_the_transcript(self) -> None:
        verifier = load(ROOT / "scripts" / "verify_demo.py", "verify_demo")
        generator = verifier.load_generator()
        self.assertEqual(verifier.problems_in(generator, read(TRANSCRIPT)), [])


class SocialPreviewTest(unittest.TestCase):
    def test_preview_is_the_size_github_expects(self) -> None:
        header = (ROOT / "assets" / "social-preview.png").read_bytes()[:24]
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", header[16:24]), (1280, 640))

    def test_stamp_binds_the_source_and_the_render(self) -> None:
        recorded = {}
        for line in read(ROOT / "assets" / "social-preview.sha256").splitlines():
            value, name = line.split()
            recorded[name] = value
        for name in ("social-preview.html", "social-preview.png"):
            self.assertEqual(recorded[name], sha256(ROOT / "assets" / name), name)

    def test_preview_source_carries_the_claim(self) -> None:
        text = " ".join(read(ROOT / "assets" / "social-preview.html").split())
        self.assertIn(CLAIM, text)


class SupportFilesTest(unittest.TestCase):
    def test_license_is_mit(self) -> None:
        self.assertTrue(read(ROOT / "LICENSE").startswith("MIT License\n"))

    def test_security_names_this_repository_for_reports(self) -> None:
        self.assertIn(
            REPOSITORY + "/security/advisories/new",
            read(ROOT / "SECURITY.md"),
        )

    def test_contributing_names_the_check_command(self) -> None:
        self.assertIn("make check", read(ROOT / "CONTRIBUTING.md"))


if __name__ == "__main__":
    unittest.main()
