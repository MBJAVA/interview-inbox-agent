#!/usr/bin/env python
"""Regenerate the README's Mermaid diagram from graph.get_graph(xray=1).draw_mermaid()."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from graph.parent_graph import graph  # noqa: E402


def mermaid() -> str:
    return graph.get_graph(xray=1).draw_mermaid()


def main() -> int:
    readme = ROOT / "README.md"
    text = readme.read_text()
    block = "<!-- mermaid:start -->\n```mermaid\n" + mermaid().strip() + "\n```\n<!-- mermaid:end -->"
    text = re.sub(r"<!-- mermaid:start -->.*?<!-- mermaid:end -->", lambda _: block, text, flags=re.S)
    readme.write_text(text)
    print("README Mermaid diagram updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
