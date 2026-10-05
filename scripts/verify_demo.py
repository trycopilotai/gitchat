#!/usr/bin/env python3
"""Check the demo images against the session they claim to show.

The generator could be changed to draw anything. This script
does not trust it: it reads the committed SVG files and the
transcript and checks the properties a reader depends on.

    python3 scripts/verify_demo.py

It checks that:

- both images equal a fresh render of the transcript;
- every line of terminal text is a line of the transcript;
- the animation loops, and each step stays visible until the
  loop restarts, so later frames contain earlier ones;
- the canvas is tall and wide enough for the whole session;
- every text colour has a contrast ratio of at least 4.5 to 1
  against the background;
- the poster carries no animation;
- both images say which transcript they were built from.
"""

from __future__ import annotations

import html
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_demo.py"
MINIMUM_CONTRAST = 4.5

TEXT_ELEMENT = re.compile(
    r'<text x="(?P<x>\d+)" y="(?P<y>\d+)" fill="(?P<fill>#[0-9a-f]{6})"'
    r'(?P<rest>[^>]*)>(?P<body>[^<]*)</text>'
)
KEYFRAMES = re.compile(
    r"@keyframes reveal-(?P<step>\d+) \{\s*"
    r"0%, (?P<hidden>\d+)% \{ opacity: 0; \}\s*"
    r"(?P<shown>\d+)%, (?P<hold>\d+)% \{ opacity: 1; \}\s*"
    r"100% \{ opacity: 0; \}\s*\}"
)
STEP_RULE = re.compile(
    r"\.step-(?P<step>\d+) \{\s*opacity: 0;\s*"
    r"animation: reveal-(?P<name>\d+) (?P<seconds>\d+)s infinite;\s*\}"
)


def load_generator():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("generate_demo", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def channel(value: int) -> float:
    scaled = value / 255
    if scaled <= 0.03928:
        return scaled / 12.92
    return ((scaled + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    red, green, blue = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def problems_in(generator, transcript: str) -> list[str]:
    problems: list[str] = []
    demo = generator.DEMO.read_text(encoding="utf-8")
    poster = generator.POSTER.read_text(encoding="utf-8")

    if demo != generator.render(transcript, animated=True):
        problems.append("demo.svg differs from a fresh render")
    if poster != generator.render(transcript, animated=False):
        problems.append("poster.svg differs from a fresh render")

    transcript_lines = set(transcript.splitlines())
    for name, image in (("demo.svg", demo), ("poster.svg", poster)):
        texts = list(TEXT_ELEMENT.finditer(image))
        if not texts:
            problems.append("%s has no text" % name)
            continue
        label = html.unescape(texts[-1].group("body"))
        if label != generator.SOURCE_LABEL:
            problems.append("%s does not name its transcript" % name)
        session = texts[:-1]
        for match in session:
            line = html.unescape(match.group("body"))
            if line not in transcript_lines:
                problems.append("%s shows a line the transcript lacks: %r" % (name, line))
            width = int(match.group("x")) + len(line) * generator.GLYPH_WIDTH
            if width > generator.WIDTH - generator.MARGIN_X:
                problems.append("%s: a line overruns the canvas: %r" % (name, line))
        lowest = max(int(match.group("y")) for match in texts)
        if lowest + generator.LABEL_FONT_SIZE > generator.HEIGHT:
            problems.append("%s: text runs below the canvas" % name)
        baselines = [int(match.group("y")) for match in session]
        if baselines != sorted(baselines) or len(set(baselines)) != len(baselines):
            problems.append("%s: session lines are not in order" % name)
        for match in texts:
            ratio = contrast(match.group("fill"), generator.BACKGROUND)
            if ratio < MINIMUM_CONTRAST:
                problems.append(
                    "%s: %s on %s is %.2f to 1"
                    % (name, match.group("fill"), generator.BACKGROUND, ratio)
                )

    _header, steps = generator.steps_from_transcript(transcript)
    expected = len(steps)
    rules = {int(m.group("step")): m for m in STEP_RULE.finditer(demo)}
    frames = {int(m.group("step")): m for m in KEYFRAMES.finditer(demo)}
    if sorted(rules) != list(range(1, expected + 1)):
        problems.append("demo.svg does not animate every step, or not forever")
    if sorted(frames) != list(range(1, expected + 1)):
        problems.append("demo.svg lacks a keyframe set for every step")
    else:
        hidden = [int(frames[i].group("hidden")) for i in sorted(frames)]
        shown = [int(frames[i].group("shown")) for i in sorted(frames)]
        holds = {int(frames[i].group("hold")) for i in sorted(frames)}
        if hidden != sorted(hidden) or len(set(hidden)) != len(hidden):
            problems.append("steps do not appear one after another")
        if any(s <= h for s, h in zip(shown, hidden)):
            problems.append("a step is shown before it stops being hidden")
        if len(holds) != 1 or max(holds) <= max(shown):
            problems.append("steps do not all hold until the loop restarts")
    for step, rule in rules.items():
        if int(rule.group("name")) != step:
            problems.append("step %d uses another step's keyframes" % step)
    if "prefers-reduced-motion: reduce" not in demo:
        problems.append("demo.svg ignores a reduced-motion preference")
    for marker in ("animation", "@keyframes", "<style"):
        if marker in poster:
            problems.append("poster.svg carries %s" % marker)
    for index in range(1, expected + 1):
        if 'class="step-%d"' % index not in demo:
            problems.append("demo.svg has no step-%d group" % index)
    return problems


def main() -> int:
    generator = load_generator()
    transcript = generator.TRANSCRIPT.read_text(encoding="utf-8")
    problems = problems_in(generator, transcript)
    if problems:
        for problem in problems:
            print("FAIL  %s" % problem)
        return 1
    print("demo.svg and poster.svg agree with the transcript")
    return 0


if __name__ == "__main__":
    sys.exit(main())
