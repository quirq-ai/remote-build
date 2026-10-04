"""A small deterministic action, to show every backend gives the same output digest for it.

It sorts `selftest/words.txt` byte-wise into `.qq/rbe-selftest/sorted.txt` with POSIX `sh` and
`sort` under `LC_ALL=C`. Those come from the host, not from a toolchain. On a GitHub-hosted runner
the action pins the runner image (its `ImageOS` and `ImageVersion`), so its digest changes when the
image does and it may be cached. Anywhere else the host is ambient and the action is not cacheable.
TODO(expert): run it on a pinned toolchain from quirq-ai/toolchains once one is published, and
have workers refuse an image pin that is not theirs.
"""
from __future__ import annotations

import os
from pathlib import Path

from qqrecipes import digest
from qqrecipes.contract import Action, host_platform

from qqrbe.request import ExecRequest, Source

FIXTURE = "selftest/words.txt"
OUTPUT = ".qq/rbe-selftest/sorted.txt"


def host_pin() -> str:
    image, version = os.environ.get("ImageOS"), os.environ.get("ImageVersion")
    return f"{image}@{version}" if image and version else "ambient"


def request(repo: Path, source: Source | None = None) -> ExecRequest:
    pin = host_pin()
    action = Action(
        target="rbe-selftest",
        capability="build",
        name="sort",
        argv=("sh", "-c", f"mkdir -p {Path(OUTPUT).parent} && sort {FIXTURE} > {OUTPUT}"),
        input_root_digest=digest.input_root(repo, [FIXTURE]),
        env=(("LC_ALL", "C"),),
        outputs=(OUTPUT,),
        toolchains=(("host", pin),),
        platform=host_platform(),
        adapter="qqrbe-selftest",
        cacheable=pin != "ambient",
        timeout_s=60,
    )
    return ExecRequest(action, (FIXTURE,), source)
