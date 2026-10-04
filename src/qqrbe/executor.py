"""The executor interface: run one REAPI-shaped action on a backend, get an ActionResult.

Core file: names no language, tool or cloud. A backend is a module `qqrbe.backends.<name>` with a
`create(**options) -> Executor`. Backends are discovered, not registered: adding one (`launchpad`,
`buildbarn`) adds a module and edits no core file. A backend with no module is BackendNotFound; a
backend module that fails to import raises, so a broken install never looks like a missing backend.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from pathlib import Path

from qqrecipes import runner

from qqrbe.errors import BackendNotFound
from qqrbe.request import ExecRequest

PACKAGE = "qqrbe.backends"
BACKEND_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class ActionResult:
    """REAPI `ActionResult`, v0 subset: output digests and an exit code, plus where it ran."""

    action_digest: str
    backend: str
    exit_code: int
    duration_s: float
    output_digests: dict[str, str | None]  # declared output path -> digest, None if not produced
    log: Path | None = None
    junit: Path | None = None
    details: dict = field(default_factory=dict)  # backend facts, such as the remote run's URL

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_json(self) -> dict:
        return {
            "action_digest": self.action_digest,
            "backend": self.backend,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "output_digests": dict(self.output_digests),
            "log": None if self.log is None else str(self.log),
            "junit": None if self.junit is None else str(self.junit),
            "details": dict(self.details),
        }

    @classmethod
    def from_json(cls, data: dict) -> ActionResult:
        return cls(
            action_digest=data["action_digest"],
            backend=data["backend"],
            exit_code=data["exit_code"],
            duration_s=data["duration_s"],
            output_digests=dict(data["output_digests"]),
            log=None if data.get("log") is None else Path(data["log"]),
            junit=None if data.get("junit") is None else Path(data["junit"]),
            details=dict(data.get("details", {})),
        )


class Executor:
    """Base class for backends. Subclasses set `backend` and implement `execute`."""

    backend: str = ""

    def execute(self, request: ExecRequest, env: runner.Env) -> ActionResult:
        """Run the request's action. `env` says where the repo is and where results go.

        A non-zero exit code is a result. Anything that stops the action from running as asked
        raises a typed qqrbe.errors.ExecutorError.
        """
        raise NotImplementedError


def load(backend: str, **options) -> Executor:
    if not BACKEND_RE.match(backend):
        raise BackendNotFound(f"{backend!r} is not a backend name (lowercase letters, digits and '-')")
    modname = f"{PACKAGE}.{backend.replace('-', '_')}"
    try:
        module = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name == modname:
            raise BackendNotFound(
                f"no backend {backend!r}: add {modname} with a create(), or pick one of:"
                f" {', '.join(available()) or 'none'}") from None
        raise  # the backend exists but something it imports is missing: fail loudly
    executor = module.create(**options)
    if not isinstance(executor, Executor) or executor.backend != backend:
        raise TypeError(f"{modname}.create() must return an Executor whose backend is {backend!r}")
    return executor


def available() -> list[str]:
    pkg = importlib.import_module(PACKAGE)
    paths = [str(Path(p)) for p in getattr(pkg, "__path__", [])]
    return sorted(m.name.replace("_", "-") for m in pkgutil.iter_modules(paths)
                  if not m.name.startswith("_"))
