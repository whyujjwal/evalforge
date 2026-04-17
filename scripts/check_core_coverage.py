"""Fail CI if coverage on core modules drops below 90%.

CLI/server are exempt per spec. This script is called from CI after
`pytest --cov-report=xml` has written `coverage.xml`.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CORE_FILES = {
    "evalforge/types.py",
    "evalforge/errors.py",
    "evalforge/pipeline.py",
    "evalforge/engine.py",
    "evalforge/events.py",
    "evalforge/agents.py",
    "evalforge/storage/__init__.py",
    "evalforge/storage/sqlite.py",
}

FLOOR_PERCENT = 90.0


def main() -> int:
    path = Path("coverage.xml")
    if not path.exists():
        print("coverage.xml not found; run pytest --cov-report=xml first", file=sys.stderr)
        return 2

    root = ET.parse(path).getroot()
    total_lines = 0
    covered_lines = 0
    seen: set[str] = set()
    for cls in root.iter("class"):
        fn = cls.get("filename", "")
        if fn in CORE_FILES:
            seen.add(fn)
            for line in cls.iter("line"):
                total_lines += 1
                if int(line.get("hits", "0")) > 0:
                    covered_lines += 1

    missing = CORE_FILES - seen
    if missing:
        print(f"WARN: core files missing from coverage report: {sorted(missing)}")

    if total_lines == 0:
        print("No core lines counted; treating as failure", file=sys.stderr)
        return 2

    pct = covered_lines / total_lines * 100.0
    print(f"core coverage: {covered_lines}/{total_lines} = {pct:.2f}%")
    if pct < FLOOR_PERCENT:
        print(f"FAIL: core coverage {pct:.2f}% < floor {FLOOR_PERCENT}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
