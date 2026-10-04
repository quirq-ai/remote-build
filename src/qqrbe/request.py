"""What an executor is asked to run, and how it travels between machines.

An ExecRequest is a recipes Action (REAPI `Action`: command, input root digest, platform) plus the
repo-relative paths that make up its input root, and where those files can be fetched from.

v0 has no content-addressed store (CAS). A remote worker gets the input root by checking out
`source` (a git repository and commit) and proves it has the right files by recomputing the input
root digest over `inputs` before it runs anything.
TODO(expert): upload inputs to a REAPI CAS instead of relying on git (v1 shared cache).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from qqrecipes import digest
from qqrecipes.contract import Action, Service, host_platform

from qqrbe.errors import BadRequest, InputRootMismatch, PlatformMismatch

SCHEMA = "qq-exec-request/1"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Source:
    """Where a remote worker fetches the input root: a repository on the backend's forge."""

    repository: str  # owner/name
    commit: str  # full commit id

    def __post_init__(self):
        if not REPOSITORY_RE.match(self.repository):
            raise BadRequest(f"source repository {self.repository!r} is not owner/name")
        if not COMMIT_RE.match(self.commit):
            raise BadRequest(f"source commit {self.commit!r} is not a full 40-character commit id")

    @classmethod
    def from_git(cls, repo: Path, repository: str | None = None) -> Source:
        """The checkout's HEAD commit, and its repository from `repository` or the origin remote."""
        commit = _git(repo, "rev-parse", "HEAD")
        if repository is None:
            url = _git(repo, "remote", "get-url", "origin")
            m = re.search(r"[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
            if not m:
                raise BadRequest(f"cannot tell the repository from origin {url!r}; pass it explicitly")
            repository = m.group(1)
        return cls(repository, commit)


@dataclass(frozen=True)
class ExecRequest:
    action: Action
    inputs: tuple[str, ...]  # repo-relative files whose digest is action.input_root_digest
    source: Source | None = None  # needed by remote backends only

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "action": self.action.to_json(),
            "inputs": list(self.inputs),
            "source": None if self.source is None else {
                "repository": self.source.repository, "commit": self.source.commit},
        }

    @classmethod
    def from_json(cls, data: dict) -> ExecRequest:
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise BadRequest(f"not a {SCHEMA} request (schema is {_get(data, 'schema')!r})")
        try:
            action = action_from_json(data["action"])
            inputs = data["inputs"]
            src = data.get("source")
            source = None if src is None else Source(src["repository"], src["commit"])
        except (KeyError, TypeError) as e:
            raise BadRequest(f"malformed {SCHEMA} request: missing or wrong field {e}") from None
        if not isinstance(inputs, list):
            raise BadRequest("inputs must be a list of repo-relative paths")
        for rel in inputs:
            if not isinstance(rel, str) or not _inside(rel):
                raise BadRequest(f"input {rel!r} must be a repo-relative path inside the repo")
        return cls(action, tuple(inputs), source)


def action_from_json(data: dict) -> Action:
    """Rebuild a recipes Action from `Action.to_json()`, checking it still has the same digest."""
    if not isinstance(data, dict):
        raise BadRequest("action must be an object")
    try:
        service = data.get("service")
        action = Action(
            target=_typed(data["target"], str, "target"),
            capability=_typed(data["capability"], str, "capability"),
            name=_typed(data["name"], str, "name"),
            argv=_strs(data["argv"], "argv"),
            input_root_digest=_typed(data["input_root_digest"], str, "input_root_digest"),
            env=_pairs(data.get("env", []), "env"),
            workdir=_typed(data.get("workdir", "."), str, "workdir"),
            outputs=_strs(data.get("outputs", []), "outputs"),
            junit=None if data.get("junit") is None else _typed(data["junit"], str, "junit"),
            toolchains=_pairs(data.get("toolchains", []), "toolchains"),
            adapter=_typed(data.get("adapter", ""), str, "adapter"),
            platform=_pairs(data.get("platform", []), "platform"),
            cacheable=_typed(data.get("cacheable", True), bool, "cacheable"),
            timeout_s=_typed(data.get("timeout_s", 1800), int, "timeout_s"),
            service=None if service is None else Service(
                ready_path=_typed(service["ready_path"], str, "service.ready_path"),
                ready_timeout_s=_typed(service["ready_timeout_s"], int, "service.ready_timeout_s"),
                probes=_strs(service["probes"], "service.probes")),
        )
        claimed = data["digest"]
    except (KeyError, TypeError, AttributeError) as e:
        raise BadRequest(f"malformed action: missing or wrong field {e}") from None
    if not action.argv:
        raise BadRequest("action argv is empty")
    for rel in (action.workdir, *action.outputs):
        if not _inside(rel):
            raise BadRequest(f"workdir or output {rel!r} must be a relative path inside the repo")
    if claimed != action.digest():
        raise BadRequest(f"action digest {claimed} does not match its fields ({action.digest()})")
    return action


def _inside(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in Path(rel).parts


def _typed(value, kind, name):
    # bool is an int in Python; a timeout of True is still wrong.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise BadRequest(f"action field {name} must be {kind.__name__}, got {value!r}")
    return value


def _strs(value, name) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest(f"action field {name} must be a list of strings")
    return tuple(value)


def _pairs(value, name) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not all(
            isinstance(kv, list) and len(kv) == 2 and all(isinstance(x, str) for x in kv) for kv in value):
        raise BadRequest(f"action field {name} must be a list of [key, value] string pairs")
    return tuple(tuple(kv) for kv in value)


def input_files(repo: Path, globs) -> tuple[str, ...]:
    """The repo files matched by `globs`: the file list behind recipes' `digest.input_root`."""
    globs = tuple(globs)
    files = digest.list_files(repo)
    return tuple(f for f in files if any(digest.matches(g, f) for g in globs))


def verify_input_root(repo: Path, request: ExecRequest) -> None:
    """Raise InputRootMismatch unless the files under `repo` hash to the action's input root."""
    missing = [rel for rel in request.inputs if not (Path(repo) / rel).is_file()]
    if missing:
        raise InputRootMismatch(f"input root files missing under {repo}: {', '.join(missing[:5])}")
    got = digest.files_digest(repo, request.inputs)
    if got != request.action.input_root_digest:
        raise InputRootMismatch(
            f"inputs under {repo} hash to {got}, but the action's input root is"
            f" {request.action.input_root_digest}: a file changed or is uncommitted")


def check_platform(action: Action) -> None:
    """Raise PlatformMismatch if the action names platform properties this machine lacks."""
    host = dict(host_platform())
    wrong = {k: v for k, v in action.platform if host.get(k) != v}
    if wrong:
        raise PlatformMismatch(f"action wants {dict(action.platform)}, this executor is {host}")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise BadRequest(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout.strip()


def _get(data, key):
    return data.get(key) if isinstance(data, dict) else None
