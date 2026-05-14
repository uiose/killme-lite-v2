#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import AppConfig, load_config
from router import Router


def build_parser(config: AppConfig) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="killme-lite CLI")
    parser.add_argument(
        "--db",
        default=str(config.db_path),
        help="SQLite database path. Defaults to KILLME_DB_PATH or ./data/killme.sqlite.",
    )
    parser.add_argument(
        "--script",
        help="Run commands from a text file instead of interactive mode.",
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialize the SQLite database and exit.",
    )
    return parser


def run_script(router: Router, script_path: str) -> int:
    path = Path(script_path)
    if not path.exists():
        print(f"Script not found: {path}", file=sys.stderr)
        return 2

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        print(f"\n> {line}")
        try:
            output = router.handle_line(line)
        except Exception as exc:  # keep batch smoke tests alive and diagnosable
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        if output:
            print(output)
    return 0


def repl(router: Router) -> int:
    router.progress = lambda message: print(message, flush=True)
    print("killme-lite CLI")
    print("Type /start <idea> for decision mode, or /explore <question> for exploration mode. Type Ctrl-D or /quit to exit.")
    while True:
        try:
            line = input("killme> ").strip()
        except EOFError:
            print()
            return 0

        if not line:
            continue
        if line in {"/quit", "quit", "exit"}:
            return 0

        try:
            output = router.handle_line(line)
        except Exception as exc:  # keep CLI alive during prototyping
            output = f"[error] {exc}"
        if output:
            print(output)


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    config = load_config(base_dir)
    args = build_parser(config).parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    router = Router(base_dir=base_dir, db_path=db_path, config=config)

    if args.init_db:
        router.storage.init_db()
        print(f"Initialized database: {db_path}")
        return 0

    if args.script:
        return run_script(router, args.script)

    return repl(router)


if __name__ == "__main__":
    raise SystemExit(main())
