"""Execute untrusted context policies as bounded, sandboxed JSON programs."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import time


MAX_OUTPUT_BYTES = 131072
MAX_CONTEXT_CHARS = 64000
MAX_POLICY_BYTES = 262144


class PolicyError(RuntimeError):
    def __init__(self, message: str, *, category: str = "candidate"):
        super().__init__(message)
        self.category = category


# This wrapper is supervisor-owned. Candidate code executes only in this child.
# The start marker distinguishes a broken sandbox/interpreter from policy errors.
WRAPPER = r'''
import contextlib, io, json, pathlib, runpy, sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path("policy").resolve()))
print(json.dumps({"rsi_policy_event": "started"}), flush=True)
class BoundedOutput(io.StringIO):
    def write(self, value):
        if self.tell() + len(value) > 65536:
            raise RuntimeError("Policy output exceeded its limit")
        return super().write(value)
out = BoundedOutput()
try:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            runpy.run_path("policy/policy.py", run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
    print(json.dumps({"rsi_policy_event": "result", "output": out.getvalue()}), flush=True)
except BaseException as exc:
    print(json.dumps({"rsi_policy_event": "error", "error": type(exc).__name__ + ": " + str(exc)[:2000]}), flush=True)
    sys.exit(1)
'''


def policy_digest(directory: Path) -> str:
    """Hash every file; links and non-policy assets are never executable inputs."""
    digest = hashlib.sha256()
    count = 0
    total = 0
    if directory.is_symlink() or not directory.is_dir():
        raise PolicyError("Policy must be a real directory.")
    for path in sorted(directory.rglob("*")):
        if "__pycache__" in path.relative_to(directory).parts:
            continue
        if path.is_symlink():
            raise PolicyError("Policy symlinks are not supported.")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix not in {".py", ".md", ".json"}:
            raise PolicyError(f"Ineligible policy file: {path.name}")
        count += 1
        total += path.stat().st_size
        if count > 32 or total > MAX_POLICY_BYTES:
            raise PolicyError("Policy exceeds the file or byte limit.")
        name = path.relative_to(directory).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    if not (directory / "policy.py").is_file():
        raise PolicyError("Policy requires policy.py.")
    return digest.hexdigest()


def validate_request(request: dict) -> dict:
    if not isinstance(request, dict) or set(request) - {
        "query", "documents", "experience", "max_context_chars"
    }:
        raise PolicyError("Policy request contains unsupported fields.", category="infrastructure")
    query = request.get("query")
    documents = request.get("documents")
    experience = request.get("experience", [])
    limit = request.get("max_context_chars", 8000)
    if not isinstance(query, str) or not query or len(query) > 32000:
        raise PolicyError("query must be a nonempty bounded string.", category="infrastructure")
    if not isinstance(documents, list) or len(documents) > 1000:
        raise PolicyError("documents must be a bounded list.", category="infrastructure")
    ids = set()
    total = 0
    for doc in documents:
        if not isinstance(doc, dict) or set(doc) != {"id", "text"}:
            raise PolicyError("Documents require exactly id and text.", category="infrastructure")
        if not isinstance(doc["id"], str) or not doc["id"] or len(doc["id"]) > 256 or doc["id"] in ids:
            raise PolicyError("Document IDs must be unique bounded strings.", category="infrastructure")
        if not isinstance(doc["text"], str) or len(doc["text"]) > 100000:
            raise PolicyError("Document text must be a bounded string.", category="infrastructure")
        ids.add(doc["id"])
        total += len(doc["text"])
    if total > 2000000:
        raise PolicyError("Document corpus is too large.", category="infrastructure")
    if type(limit) is not int or not 1 <= limit <= MAX_CONTEXT_CHARS:
        raise PolicyError("Invalid max_context_chars.", category="infrastructure")
    if not isinstance(experience, list) or len(experience) > 16 or any(not isinstance(item, dict) for item in experience):
        raise PolicyError("experience must contain at most 16 records.", category="infrastructure")
    try:
        serialized = json.dumps(experience, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PolicyError("Experience must be JSON serializable.", category="infrastructure") from exc
    if len(serialized) > 64000:
        raise PolicyError("Experience records exceed their size limit.", category="infrastructure")
    # Round-trip to detach all inputs from later caller mutation.
    return json.loads(json.dumps({"query": query, "documents": documents, "experience": experience,
                                 "max_context_chars": limit}, ensure_ascii=True, allow_nan=False))


def validate_response(response: object, request: dict) -> dict:
    if not isinstance(response, dict) or set(response) != {"selected_ids", "instructions"}:
        raise PolicyError("Policy response requires exactly selected_ids and instructions.")
    selected = response["selected_ids"]
    instructions = response["instructions"]
    documents = {doc["id"]: doc["text"] for doc in request["documents"]}
    if not isinstance(selected, list) or len(selected) > len(documents) or any(not isinstance(value, str) for value in selected):
        raise PolicyError("selected_ids must be a list of document IDs.")
    if len(set(selected)) != len(selected) or any(value not in documents for value in selected):
        raise PolicyError("Policy selected unknown or duplicate document IDs.")
    if not isinstance(instructions, str) or len(instructions) > 8192:
        raise PolicyError("Policy instructions exceed their limit.")
    parts = []
    included = []
    size = 0
    for doc_id in selected:
        block = f"[{doc_id}]\n{documents[doc_id]}\n"
        if size + len(block) <= request["max_context_chars"]:
            parts.append(block)
            included.append(doc_id)
            size += len(block)
    return {"selected_ids": included, "instructions": instructions, "context": "".join(parts),
            "context_chars": size, "policy_prompt_chars": size + len(instructions) + 1}


class PolicyRunner:
    def __init__(self, codex: str, sandbox_home: Path, timeout: int = 60, *, executor=None, command_builder=None):
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("Policy timeout must be a positive integer.")
        self.codex = codex
        self.sandbox_home = Path(sandbox_home)
        self.timeout = timeout
        self.executor = executor
        self.command_builder = command_builder

    def run(self, policy_dir: Path, request: dict, *, artifacts: Path) -> dict:
        from rsi.core import sandbox_command
        from rsi.process import ProcessTimeout, check_environment, execute

        request = validate_request(request)
        policy_dir = Path(policy_dir)
        original_digest = policy_digest(policy_dir)
        artifacts = Path(artifacts)
        if artifacts.is_symlink():
            raise PolicyError("Artifact directory must not be a symlink.", category="infrastructure")
        artifacts.mkdir(parents=True, exist_ok=True)
        workspace = artifacts / "workspace"
        if workspace.exists():
            raise PolicyError("Use a fresh artifact directory for each policy execution.", category="infrastructure")
        workspace.mkdir(mode=0o755)
        shutil.copytree(policy_dir, workspace / "policy", ignore=shutil.ignore_patterns("__pycache__"))
        if policy_digest(workspace / "policy") != original_digest:
            raise PolicyError("Policy changed while creating its execution snapshot.")
        wrapper = workspace / "_run_policy.py"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        (workspace / ".rsi" / "check-tmp").mkdir(parents=True)
        self.sandbox_home.mkdir(parents=True, exist_ok=True)
        input_text = json.dumps(request, ensure_ascii=True, allow_nan=False)
        (artifacts / "request.json").write_text(input_text, encoding="utf-8")
        log = artifacts / "policy.log"
        command = (self.command_builder or sandbox_command)(
            self.codex, workspace, [sys.executable, "-I", str(wrapper)], readonly=True, restricted=True
        )
        started_at = time.monotonic()
        code = None
        failure = None
        try:
            code = (self.executor or execute)(command, cwd=workspace, log=log, timeout=self.timeout,
                                             input_text=input_text, env=check_environment(workspace, self.sandbox_home))
        except (OSError, ProcessTimeout) as exc:
            failure = exc
        try:
            intact = (policy_digest(workspace / "policy") == original_digest
                      and wrapper.read_text(encoding="utf-8") == WRAPPER)
        except OSError as exc:
            raise PolicyError("Policy removed an immutable execution input.") from exc
        if not intact:
            raise PolicyError("Policy mutated its execution snapshot.")
        if not log.exists():
            raise PolicyError(f"Policy process produced no log: {failure}", category="infrastructure")
        if log.stat().st_size > MAX_OUTPUT_BYTES:
            raise PolicyError("Policy process output exceeded its byte limit.")
        log_text = log.read_text(encoding="utf-8", errors="replace")
        records = []
        for line in log_text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue  # Codex may emit its own startup diagnostic lines.
            if isinstance(value, dict) and "rsi_policy_event" in value:
                records.append(value)
        started = any(record.get("rsi_policy_event") == "started" for record in records)
        if failure is not None or code != 0:
            message = str(failure) if failure else next(
                (record.get("error", "Policy failed") for record in records if record.get("rsi_policy_event") == "error"),
                f"Policy process exited {code}: {log_text[-2000:].strip()}; see {log}",
            )
            raise PolicyError(message, category="candidate" if started else "infrastructure")
        results = [record for record in records if record.get("rsi_policy_event") == "result"]
        if not started or len(results) != 1:
            raise PolicyError("Policy worker did not return one complete result.", category="candidate" if started else "infrastructure")
        try:
            response = json.loads(results[0]["output"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError("Policy output was not valid JSON.") from exc
        result = validate_response(response, request)
        result.update(status="completed", elapsed_seconds=round(time.monotonic() - started_at, 6),
                      policy_digest=original_digest)
        (artifacts / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
