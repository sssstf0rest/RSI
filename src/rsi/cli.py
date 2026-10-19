"""Local control commands; installation does not start services or inference."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from rsi.archive import Archive
from rsi.core import RSIError, Seed
from rsi.evolution import Evolution
from rsi.queue import QueueStore
from rsi.runtime import Worker, prune, worker_lock


def _stop_signals(event):
    def stop(signum, frame):
        event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _diff(seed, identifier):
    try:
        version = Archive(seed).get(identifier)
        identifier = version["source_run"]
        if not identifier:
            return "Initial policy has no candidate patch."
    except RSIError:
        pass
    run = seed.get(identifier)
    if Path(run["workspace"]).exists():
        with seed.operation():
            return seed.snapshot(identifier).decode("utf-8", errors="replace")
    return (seed.artifacts(identifier) / "candidate.patch").read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI: evaluated policy evolution and durable personal tasks")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--codex", default="codex", help="Official Codex executable")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "runs", "jobs", "pause", "resume"):
        commands.add_parser(name)
    run = commands.add_parser("run", help="Develop a capability candidate")
    task = run.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    run.add_argument("--no-memory", action="store_true")
    for name in ("show", "diff", "verify", "apply", "job", "cancel", "retry"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "retry":
            command.add_argument("--max-attempts", type=int, default=3)
    experiment = commands.add_parser("experiment", help="Propose and compare one policy; no automatic activation")
    experiment.add_argument("--task", default="")
    experiment.add_argument("--candidate", type=Path, help="Evaluate a supplied local policy without a model proposal")
    experiment.add_argument("--parent")
    experiment.add_argument("--no-memory", action="store_true")
    evaluate = commands.add_parser("evaluate", help="Score a recorded policy on public development/selection tasks")
    evaluate.add_argument("--version")
    evaluate.add_argument("--no-memory", action="store_true")
    audit = commands.add_parser("audit", help="Score a separately provisioned private audit dataset")
    audit.add_argument("--dataset", type=Path, required=True)
    audit.add_argument("--version", required=True)
    audit.add_argument("--baseline", required=True)
    strategy = commands.add_parser("strategy", help="Bounded matched trials for mutation instructions")
    strategies = strategy.add_subparsers(dest="action", required=True)
    for action in ("list", "best"):
        strategies.add_parser(action)
    propose = strategies.add_parser("propose")
    propose.add_argument("--task", default="")
    meta = strategies.add_parser("experiment")
    meta.add_argument("--candidate", type=Path, required=True)
    meta.add_argument("--trials", type=int, default=1)
    meta.add_argument("--parent")
    meta.add_argument("--task", default="")
    for action in ("promote", "rollback"):
        sub = strategies.add_parser(action)
        sub.add_argument("id")
    policy = commands.add_parser("policy")
    policy_commands = policy.add_subparsers(dest="action", required=True)
    for action in ("init", "list", "best"):
        policy_commands.add_parser(action)
    for action in ("promote", "rollback"):
        sub = policy_commands.add_parser(action)
        sub.add_argument("version")
    enqueue = commands.add_parser("enqueue")
    kind = enqueue.add_mutually_exclusive_group(required=True)
    for flag in ("task", "experiment", "promote", "rollback"):
        kind.add_argument("--" + flag)
    enqueue.add_argument("--job-id")
    worker = commands.add_parser("worker", help="Consume queued jobs; one finite job by default")
    worker.add_argument("--max-jobs", type=int, default=1)
    worker.add_argument("--serve", action="store_true", help="Wait for queued work until stopped")
    worker.add_argument("--poll-seconds", type=float, default=5)
    retention = commands.add_parser("prune")
    retention.add_argument("--keep", type=int, default=8)
    commands.add_parser("telegram", help="Start the configured Telegram interface")
    args = parser.parse_args(argv)
    try:
        seed = Seed(args.repo, codex=args.codex)
        queue = QueueStore(seed.state / "state.sqlite3")
        if args.command == "doctor":
            result = seed.doctor()
        elif args.command == "runs":
            result = seed.runs()
        elif args.command == "run":
            prompt = args.task_file.read_text(encoding="utf-8") if args.task_file else args.task
            with worker_lock(seed):
                result = seed.run_task(prompt, memory_enabled=not args.no_memory)
        elif args.command == "show":
            result = seed.get(args.id)
        elif args.command == "diff":
            print(_diff(seed, args.id))
            return 0
        elif args.command == "verify":
            with worker_lock(seed):
                result = seed.verify(args.id)
        elif args.command == "apply":
            with worker_lock(seed):
                result = seed.apply(args.id)
        elif args.command == "experiment":
            with worker_lock(seed):
                result = Evolution(seed).experiment(args.task, candidate=args.candidate,
                    parent_id=args.parent, memory_enabled=not args.no_memory)
        elif args.command == "evaluate":
            with worker_lock(seed):
                result = Evolution(seed).evaluate(args.version, memory_enabled=not args.no_memory)
        elif args.command == "audit":
            from rsi.evaluation import ContextEvaluator
            from rsi.policy_runner import PolicyRunner
            if args.dataset.resolve().is_relative_to(seed.repo):
                raise RSIError("Audit answers must be outside the checkout readable by proposing workers.")
            for runtime_root in (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()):
                if args.dataset.resolve().is_relative_to(runtime_root):
                    raise RSIError("Audit answers must be outside readable interpreter/runtime roots.")
            with worker_lock(seed), seed.operation():
                archive = Archive(seed)
                evaluator = ContextEvaluator(args.dataset, external_audit=True)
                runner = PolicyRunner(seed.codex, seed.sandbox_home, seed.config.get("policy_timeout_seconds", 60))
                result = {label: evaluator.evaluate_audit(archive.directory(archive.get(identifier)), runner,
                    artifacts=seed.state / "audits" / identifier)
                    for label, identifier in (("baseline", args.baseline), ("candidate", args.version))}
        elif args.command == "strategy":
            from rsi.meta import StrategyStore, MetaEvolution
            store = StrategyStore(seed)
            if args.action == "list":
                result = store.versions()
            elif args.action == "best":
                with seed.operation():
                    result = store.active()
            else:
                with worker_lock(seed):
                    if args.action == "experiment":
                        result = MetaEvolution(seed).experiment(args.candidate, trials=args.trials,
                            parent_id=args.parent, task=args.task)
                    elif args.action == "propose":
                        result = MetaEvolution(seed).propose(args.task)
                    else:
                        result = store.activate(args.id, rollback=args.action == "rollback")
        elif args.command == "policy":
            archive = Archive(seed)
            if args.action == "list":
                result = archive.versions()
            elif args.action == "best":
                result = archive.active()
            else:
                with worker_lock(seed):
                    evolution = Evolution(seed)
                    result = evolution.initialize() if args.action == "init" else evolution.promote(
                        args.version, rollback=args.action == "rollback")
        elif args.command == "enqueue":
            name = next(name for name in ("task", "experiment", "promote", "rollback") if getattr(args, name) is not None)
            value = getattr(args, name)
            payload = {"version_id": value} if name in ("promote", "rollback") else {"task": value}
            result = queue.submit(name, payload, args.job_id)
        elif args.command == "jobs":
            result = {"scheduling": queue.status(), "jobs": queue.list()}
        elif args.command == "job":
            result = queue.get(args.id)
        elif args.command == "cancel":
            queue.cancel(args.id)
            result = queue.get(args.id)
        elif args.command == "retry":
            queue.retry(args.id, max_attempts=args.max_attempts)
            result = queue.get(args.id)
        elif args.command in ("pause", "resume"):
            getattr(queue, args.command)()
            result = queue.status()
        elif args.command == "worker":
            stop = threading.Event()
            _stop_signals(stop)
            result = Worker(seed).run(max_jobs=args.max_jobs, serve=args.serve,
                                      poll_seconds=args.poll_seconds, stop_event=stop)
        elif args.command == "prune":
            result = prune(seed, keep=args.keep)
        else:
            return telegram_main(repo=seed.repo, codex=seed.codex)
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.command in ("evaluate", "audit"):
            return 0 if all(report["status"] == "completed" for report in result.values()) else 1
        return 0 if args.command != "doctor" or result["ready"] else 1
    except KeyboardInterrupt:
        print("Interrupted. Candidate files and logs have been retained.", file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"rsi: {exc}", file=sys.stderr)
        return 1


def telegram_main(*, repo=None, codex="codex") -> int:
    from rsi_telegram.bot import Bot
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
    try:
        if not token or not user_id.isdecimal() or int(user_id) <= 0:
            print("rsi telegram: set TELEGRAM_BOT_TOKEN and a positive TELEGRAM_ALLOWED_USER_ID in a private environment file.", file=sys.stderr)
            return 1
        seed = Seed(Path(repo or os.environ.get("RSI_REPO", Path.cwd())), codex=codex)
        queue = QueueStore(seed.state / "state.sqlite3")
        def status(argument):
            state = queue.status()
            jobs = queue.list(limit=5)
            return json.dumps({"paused": state["paused"], "reason": state["reason"],
                "queued": state["queued"], "observed_tokens": state["observed_tokens"],
                "usage_unknown": state["usage_unknown"], "jobs": [{"id": job["id"], "status": job["status"],
                "result_id": (job["result"] or {}).get("id")} for job in jobs]}, indent=2)
        def best(argument):
            value = Archive(seed).active()
            return json.dumps({key: value[key] for key in ("id", "decision", "parent_id")} if value else None, indent=2)
        bot = Bot(queue, {"status": status, "best": best, "diff": lambda identifier: _diff(seed, identifier)}, token, int(user_id))
        stop = threading.Event()
        _stop_signals(stop)
        bot.run(stop)
        return 0
    except (RuntimeError, OSError, ValueError):
        # Do not interpolate arbitrary HTTP/configuration exceptions into logs.
        print("rsi telegram: configuration or service error; check private configuration.", file=sys.stderr)
        return 1
