"""Prepare or explicitly activate a reviewed runtime for an existing install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory.service import repair_service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--name", default="buchos")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--ready-timeout", type=float, default=30)
    args = parser.parse_args()
    os.umask(0o077)
    result = repair_service(args.source, args.config, expected_revision=args.expected_revision,
                            python=sys.executable, name=args.name, activate=args.activate,
                            ready_timeout=args.ready_timeout)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
