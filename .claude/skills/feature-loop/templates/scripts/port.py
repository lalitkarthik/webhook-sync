"""Prints this worktree's own port (3000-3999) and records it in .verify/port."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = fl.repo_root(argv[0] if argv else ".")
    p = fl.port_for(root)
    (root / ".verify").mkdir(exist_ok=True)
    (root / ".verify" / "port").write_text(str(p), encoding="utf-8")
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
