"""The local action cache and the fallback counters (V0-RBE-02).

The cache is keyed by the action digest (recipes' `Action.digest()`: command, input root, env,
outputs, toolchain pins, platform, recipes fingerprint). A hit restores the outputs, log and JUnit
of the earlier run and is reported as a hit; it is never silent. Only successful runs of
`cacheable` actions are stored: an action that is not hermetic (`cacheable = false`, or run on an
ambient toolchain) always runs.

Layout under the cache directory (default `.qq/cache`):

    ac/<hex>.json     action digest -> the ActionResult and where its files are in cas/
    cas/<hex>         content-addressed blobs: outputs (a file, or a directory tree), logs, JUnit
    stats.json        counters: cache hits, misses and uncacheable runs; fallbacks by reason

A remote backend that cannot run an action can fall back to another one (`local`). Each fallback is
a typed event, counted by (from, to, reason) and kept in stats.json, and printed when it happens.
Only BackendUnavailable and RemoteExecutionFailed fall back: a bad request, wrong inputs or wrong
platform would fail the same way anywhere.
TODO(expert): v1 shares this through bazel-remote; add eviction and a lock for parallel writers.
TODO(expert): a remote result is keyed by the client's toolchain pins (such as the selftest's runner
image); workers must refuse pins that are not theirs before results are shared.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from qqrecipes import digest, runner

from qqrbe.errors import BackendUnavailable, RemoteExecutionFailed
from qqrbe.executor import ActionResult, Executor
from qqrbe.request import ExecRequest, verify_input_root

DEFAULT_DIR = Path(".qq/cache")
FALLBACK_ON = (BackendUnavailable, RemoteExecutionFailed)
KEEP_EVENTS = 100


def _hex(dg: str) -> str:
    algo, _, hexpart = dg.partition(":")
    if algo != "sha256" or len(hexpart) != 64 or not all(c in "0123456789abcdef" for c in hexpart):
        raise ValueError(f"not a sha256 digest: {dg!r}")
    return hexpart


@dataclass(frozen=True)
class Fallback:
    """One remote-to-local fallback: which backends, why (a stable reason), for which action."""

    from_backend: str
    to_backend: str
    reason: str
    action_digest: str
    message: str
    at: float

    @property
    def key(self) -> str:
        return f"{self.from_backend}->{self.to_backend}"


class Stats:
    """Counters kept in `<cache dir>/stats.json`, so they add up across runs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {}
        self.data.setdefault("cache", {"hit": 0, "miss": 0, "uncacheable": 0})
        self.data.setdefault("fallbacks", {})
        self.data.setdefault("events", [])

    def count_cache(self, outcome: str) -> None:
        self.data["cache"][outcome] = self.data["cache"].get(outcome, 0) + 1
        self._save()

    def count_fallback(self, fb: Fallback) -> None:
        by_reason = self.data["fallbacks"].setdefault(fb.key, {})
        by_reason[fb.reason] = by_reason.get(fb.reason, 0) + 1
        self.data["events"] = (self.data["events"] + [dataclasses.asdict(fb)])[-KEEP_EVENTS:]
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")


class ActionCache:
    def __init__(self, root: Path = DEFAULT_DIR):
        self.root = Path(root)
        self.stats = Stats(self.root / "stats.json")

    def _ac(self, action_digest: str) -> Path:
        return self.root / "ac" / f"{_hex(action_digest)}.json"

    def _cas(self, dg: str) -> Path:
        return self.root / "cas" / _hex(dg)

    def lookup(self, action_digest: str) -> dict | None:
        try:
            return json.loads(self._ac(action_digest).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _put(self, src: Path) -> str | None:
        """Store a file or tree by content; None if it does not exist."""
        dg = digest.path_digest(src)
        if dg is None:
            return None
        dst = self._cas(dg)
        if not dst.exists():
            tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, tmp)
            try:
                os.replace(tmp, dst)
            except OSError:  # another writer stored the same content first
                (shutil.rmtree if tmp.is_dir() else os.unlink)(tmp)
        return dg

    def store(self, request: ExecRequest, result: ActionResult, env: runner.Env) -> None:
        cwd = (Path(env.repo) / request.action.workdir).resolve()
        for path, dg in result.output_digests.items():
            if dg is None or self._put(cwd / path) != dg:
                return  # an output is missing or changed since the run: do not cache it
        files = {k: self._put(p) for k in ("log", "junit")
                 if (p := getattr(result, k)) is not None and Path(p).is_file()}
        stored = result.to_json()
        # A cached run reports how it ran then, not how later hits ran.
        stored["details"] = {k: v for k, v in stored["details"].items() if k not in ("cache", "fallback")}
        entry = {"result": stored, "files": files, "stored_at": time.time()}
        path = self._ac(result.action_digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(entry, indent=2, sort_keys=True) + "\n")

    def restore(self, request: ExecRequest, entry: dict, env: runner.Env) -> ActionResult | None:
        """Put a cached run's outputs, log and JUnit in place. None if the entry is unusable.

        The entry is trusted only as far as it matches the request: same action digest, exactly
        the declared outputs, and every blob re-hashed to its digest before it is copied out. A
        corrupt blob is deleted and the lookup is a miss.
        """
        action = request.action
        try:
            res = entry["result"]
            digests = res["output_digests"]
            if (res["action_digest"] != action.digest() or set(digests) != set(action.outputs)
                    or res["exit_code"] != 0 or not isinstance(res.get("details", {}), dict)):
                return None  # only successful runs are stored: anything else is not ours
            blobs = {}
            for path in action.outputs:
                dg = digests[path]
                blob = self._cas(dg) if dg else None
                if blob is None or not blob.exists():
                    return None
                if digest.path_digest(blob) != dg:
                    (shutil.rmtree if blob.is_dir() else os.unlink)(blob)
                    return None
                blobs[path] = blob
            files = {}
            for key in ("log", "junit"):
                if key in entry.get("files", {}):
                    blob = self._cas(entry["files"][key])
                    if digest.path_digest(blob) != entry["files"][key]:
                        return None
                    files[key] = blob
            cwd = (Path(env.repo) / action.workdir).resolve()
            for path, blob in blobs.items():
                dst = cwd / path
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()  # never write through a symlink to somewhere else
                elif dst.is_dir():
                    shutil.rmtree(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree if blob.is_dir() else shutil.copyfile)(blob, dst)
            out, slug = Path(env.out).resolve(), runner.slug(action)
            placed = {}
            for key, sub, ext in (("log", "logs", "log"), ("junit", "junit", "xml")):
                if key in files:
                    dst = out / sub / f"{slug}.{ext}"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(files[key], dst)
                    placed[key] = dst
            return ActionResult(
                action_digest=action.digest(),
                backend=res["backend"],
                exit_code=res["exit_code"],
                duration_s=0.0,
                output_digests={p: digests[p] for p in action.outputs},
                log=placed.get("log"),
                junit=placed.get("junit"),
                details={**res.get("details", {}), "cache": "hit",
                         "cached_duration_s": res["duration_s"]},
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None


class CachingExecutor(Executor):
    """Looks an action up before running it on `inner`, and stores successful cacheable runs."""

    def __init__(self, inner: Executor, cache: ActionCache):
        self.inner, self.cache = inner, cache
        self.backend = inner.backend

    def execute(self, request: ExecRequest, env: runner.Env) -> ActionResult:
        action = request.action
        if not action.cacheable:
            self.cache.stats.count_cache("uncacheable")
            return _note(self.inner.execute(request, env), cache="uncacheable")
        # A hit must be for the inputs a run would have checked. The platform is part of the
        # digest, so a hit is a run on that platform, wherever it happened.
        verify_input_root(env.repo, request)
        entry = self.cache.lookup(action.digest())
        if entry is not None:
            hit = self.cache.restore(request, entry, env)
            if hit is not None:
                self.cache.stats.count_cache("hit")
                return hit
        result = self.inner.execute(request, env)
        self.cache.stats.count_cache("miss")
        if result.ok:
            self.cache.store(request, result, env)
        return _note(result, cache="miss")


class FallbackExecutor(Executor):
    """Runs on `primary`; if that backend cannot run the action, runs on `fallback`, counted."""

    def __init__(self, primary: Executor, fallback: Executor, stats: Stats, warn=None):
        self.primary, self.fallback, self.stats = primary, fallback, stats
        self.backend = primary.backend
        self.warn = warn or (lambda msg: print(msg, file=sys.stderr))

    def execute(self, request: ExecRequest, env: runner.Env) -> ActionResult:
        try:
            return self.primary.execute(request, env)
        except FALLBACK_ON as e:
            fb = Fallback(self.primary.backend, self.fallback.backend, e.reason,
                          request.action.digest(), str(e), time.time())
            self.stats.count_fallback(fb)
            self.warn(f"qqrbe: FALLBACK {fb.key} ({fb.reason}): {fb.message}")
        result = self.fallback.execute(request, env)
        return _note(result, fallback={"from": fb.from_backend, "reason": fb.reason})


def _note(result: ActionResult, **details) -> ActionResult:
    return dataclasses.replace(result, details={**result.details, **details})


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
