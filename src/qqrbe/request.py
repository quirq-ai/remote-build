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
            inputs = tuple(data["inputs"])
            src = data.get("source")
            source = None if src is None else Source(src["repository"], src["commit"])
        except (KeyError, TypeError) as e:
            raise BadRequest(f"malformed {SCHEMA} request: missing or wrong field {e}") from None
        for rel in inputs:
            if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts:
                raise BadRequest(f"input {rel!r} must be a repo-relative path inside the repo")
        return cls(action, inputs, source)


def action_from_json(data: dict) -> Action:
    """Rebuild a recipes Action from `Action.to_json()`, checking it still has the same digest."""
    service = data.get("service")
    action = Action(
        target=data["target"],
        capability=data["capability"],
        name=data["name"],
        argv=tuple(data["argv"]),
        input_root_digest=data["input_root_digest"],
        env=tuple(tuple(kv) for kv in data.get("env", ())),
        workdir=data.get("workdir", "."),
        outputs=tuple(data.get("outputs", ())),
        junit=data.get("junit"),
        toolchains=tuple(tuple(kv) for kv in data.get("toolchains", ())),
        adapter=data.get("adapter", ""),
        platform=tuple(tuple(kv) for kv in data.get("platform", ())),
        cacheable=data.get("cacheable", True),
        timeout_s=data.get("timeout_s", 1800),
        service=None if service is None else Service(
            ready_path=service["ready_path"], ready_timeout_s=service["ready_timeout_s"],
            probes=tuple(service["probes"])),
    )
    if "digest" in data and data["digest"] != action.digest():
        raise BadRequest(f"action digest {data['digest']} does not match its fields ({action.digest()})")
    return action


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
