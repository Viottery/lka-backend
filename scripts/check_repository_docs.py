"""Validate release documentation without a model, network or personal data."""

from __future__ import annotations

import argparse
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

PUBLIC_DOCS = frozenset({
    "README.md", "getting_started.md", "configuration.md", "usage.md",
    "architecture.md", "maintenance.md", "api_contract.md",
})
LOCAL_PREFIXES = ("docs/_engineering/", "evals/baselines/", "tmp_")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets: list[str] = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.targets.append(value)


def validate(root: Path, *, check_index: bool = False) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    found = {path.name for path in (root / "docs").glob("*.md")}
    if found != PUBLIC_DOCS:
        errors.append(f"Public docs differ: missing={sorted(PUBLIC_DOCS - found)}, "
                      f"extra={sorted(found - PUBLIC_DOCS)}")
    files = [root / name for name in ("README.md", "AGENTS.md", "evals/README.md")]
    files += [root / "docs" / name for name in sorted(PUBLIC_DOCS)]
    for path in files:
        if not path.is_file():
            errors.append(f"Missing documentation: {path.relative_to(root)}")
            continue
        text = path.read_text(encoding="utf-8")
        fence_count = len(re.findall(r"^```", text, re.MULTILINE))
        if fence_count % 2:
            errors.append(f"Unbalanced code fences: {path.relative_to(root)}")
        # Code samples can intentionally contain fictitious links and file paths.
        prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", text,
                       flags=re.MULTILINE | re.DOTALL)
        targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", prose)
        parser = _Links()
        parser.feed(prose)
        targets.extend(parser.targets)
        for target in targets:
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(root):
                errors.append(f"Outside-repository link: {path.relative_to(root)} -> {target}")
            elif resolved.is_relative_to(root / "docs/_engineering"):
                errors.append(f"Local-only link: {path.relative_to(root)} -> {target}")
            elif not resolved.exists():
                errors.append(f"Missing link: {path.relative_to(root)} -> {target}")
    for path in (root / "docs/assets").glob("*.svg"):
        try:
            svg = ElementTree.parse(path).getroot()
            if any(node.tag.rsplit("}", 1)[-1] in {"script", "foreignObject"}
                   for node in svg.iter()):
                errors.append(f"Active SVG content: {path.relative_to(root)}")
        except ElementTree.ParseError:
            errors.append(f"Invalid SVG: {path.relative_to(root)}")
    if check_index:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                                capture_output=True, text=True)
        for name in filter(None, result.stdout.split("\0")):
            if name.startswith(LOCAL_PREFIXES) or name == "temp.md":
                errors.append(f"Local-only file tracked: {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check-index", action="store_true")
    args = parser.parse_args()
    errors = validate(args.root, check_index=args.check_index)
    for error in errors:
        print(error)
    if not errors:
        print("Documentation checked: 7 public guides, local links and SVG assets.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
