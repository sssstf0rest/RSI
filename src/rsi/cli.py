import argparse
import json
from pathlib import Path
import subprocess
import sys

from rsi.core import RSIError, Seed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI: a supervised seed agent")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--codex", default="codex", help="Official Codex executable")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check login and sandbox without calling a model")
    commands.add_parser("runs", help="List saved runs")
    run = commands.add_parser("run", help="Develop a candidate in a separate worktree")
    task = run.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    for name in ("show", "diff", "verify", "apply"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        seed = Seed(args.repo, codex=args.codex)
        if args.command == "doctor":
            result = seed.doctor()
        elif args.command == "runs":
            result = seed.runs()
        elif args.command == "run":
            prompt = args.task_file.read_text(encoding="utf-8") if args.task_file else args.task
            result = seed.run_task(prompt)
        elif args.command == "show":
            result = seed.get(args.run_id)
        elif args.command == "diff":
            with seed.operation():
                patch = seed.snapshot(args.run_id)
            sys.stdout.buffer.write(patch)
            return 0
        elif args.command == "verify":
            result = seed.verify(args.run_id)
        else:
            result = seed.apply(args.run_id)
        print(json.dumps(result, indent=2))
        return 0 if args.command != "doctor" or result["ready"] else 1
    except KeyboardInterrupt:
        print("Interrupted. Candidate files and logs have been retained.", file=sys.stderr)
        return 130
    except (RSIError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"rsi: {exc}", file=sys.stderr)
        return 1
