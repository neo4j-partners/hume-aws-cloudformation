"""Check that every relative link in the docs reaches a file and a heading.

Run from the repository root:
  uv run --with boto3 --with python-dotenv --with pyyaml \
    python -m unittest tests.docs.test_docs_links
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} +(.+?) *$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
EXTERNAL = ("http://", "https://", "mailto:")


def slug(heading: str) -> str:
    """Return the anchor GitHub gives a heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Return every heading anchor in a Markdown file."""
    text = FENCE.sub("", path.read_text())
    return {slug(heading) for heading in HEADING.findall(text)}


def broken_links(path: Path) -> list[str]:
    """Return a line for each relative link in path that does not resolve."""
    problems = []
    for target in LINK.findall(FENCE.sub("", path.read_text())):
        if target.startswith(EXTERNAL):
            continue
        name, _, anchor = target.partition("#")
        dest = (path.parent / name).resolve() if name else path
        where = f"{path.relative_to(ROOT)}: {target}"
        if not dest.exists():
            problems.append(f"missing file, {where}")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            problems.append(f"missing heading, {where}")
    return problems


class DocsLinksTest(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        pages = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        problems = [line for page in pages for line in broken_links(page)]
        self.assertEqual(problems, [])

    def test_slug_matches_github_anchors(self) -> None:
        self.assertEqual(slug("Use `neo4j+s://`"), "use-neo4js")
        self.assertEqual(slug("What `up` does"), "what-up-does")


if __name__ == "__main__":
    unittest.main()
