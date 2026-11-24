#!/usr/bin/env python3
"""Fail closed: no enforced OpenCode implementation sandbox is installed.

The former cooperative canary probe could not verify containment. This legacy
entry point remains an explicit failure and never starts a model or edits config.
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int)
    parser.parse_args()
    print("opencode_implementation_sandbox_unconfigured: implementation remains disabled; "
          "use runtime=codex for factory work submitted from OpenCode", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
