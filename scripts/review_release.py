"""Review a local evidence manifest; exits 1 for blocked and 2 for invalid input."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.evaluation.release import review  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = review(json.loads(args.manifest.read_text(encoding="utf-8")), ROOT)
    except (OSError, ValueError, TypeError, AttributeError):
        print("Invalid release manifest", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "release_gate": report["release_gate"],
                "blocker_count": len(report["blockers"]),
            }
        )
    )
    return int(bool(report["blockers"]))


if __name__ == "__main__":
    sys.exit(main())
