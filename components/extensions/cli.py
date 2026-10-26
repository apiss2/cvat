#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Route maintenance commands without changing the deployment command parser."""
import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    router = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    router.add_argument("--env-file")
    router.add_argument("--state-dir")
    router.add_argument("--model-id")
    router.add_argument("command", nargs="?")
    route, _ = router.parse_known_args(argv)
    target = "backup.py" if route.command == "backup" else "entrypoint.py"
    if target == "entrypoint.py" and ("--help" in argv or "-h" in argv):
        print("Backup commands: cvatctl backup --help\n", flush=True)
    os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name(target)), *argv])


if __name__ == "__main__":
    main()
