"""A small deterministic action, to show every backend gives the same output digest for it.

It sorts `selftest/words.txt` byte-wise into `.qq/rbe-selftest/sorted.txt` with POSIX `sh` and
`sort` under `LC_ALL=C`. Those come from the host, not a pinned toolchain.
TODO(expert): run it on a pinned toolchain from quirq-ai/toolchains once one is published.
"""
from __future__ import annotations

from pathlib import Path

from qqrecipes import digest
from qqrecipes.contract import Action, host_platform

from qqrbe.request import ExecRequest, Source

FIXTURE = "selftest/words.txt"
OUTPUT = ".qq/rbe-selftest/sorted.txt"


def request(repo: Path, source: Source | None = None) -> ExecRequest:
    action = Action(
        target="rbe-selftest",
        capability="build",
        name="sort",
        argv=("sh", "-c", f"mkdir -p {Path(OUTPUT).parent} && sort {FIXTURE} > {OUTPUT}"),
        input_root_digest=digest.input_root(repo, [FIXTURE]),
        env=(("LC_ALL", "C"),),
        outputs=(OUTPUT,),
        platform=host_platform(),
        adapter="qqrbe-selftest",
        timeout_s=60,
    )
    return ExecRequest(action, (FIXTURE,), source)
