"""The remote side of a backend: run one request on this machine and pack the result.

A remote backend (the `github` one in v0) starts a worker, which checks out the request's source,
runs the action with the local executor, and leaves a directory to send back:

    result.json     qq-exec-result/1: the ActionResult, or the typed error that stopped it
    outputs/<i>     each declared output that was produced, named in result.json
    log.txt         the action's combined stdout and stderr
    junit.xml       the action's JUnit report
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from qqrecipes import runner

from qqrbe.backends.local import LocalExecutor
from qqrbe.errors import BadRequest, ExecutorError
from qqrbe.request import ExecRequest

RESULT_SCHEMA = "qq-exec-result/1"


def read_request(path: Path) -> ExecRequest:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BadRequest(f"cannot read request {path}: {e}") from None
    return ExecRequest.from_json(data)


def run(request_path: Path, repo: Path, out: Path) -> int:
    """Run the request in `repo`, write the result directory `out`. 0 unless the run errored."""
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        request = read_request(request_path)
        result = LocalExecutor().execute(request, runner.Env(repo=Path(repo), out=out / "run"))
    except ExecutorError as e:
        _write(out, {"schema": RESULT_SCHEMA, "error": {"reason": e.reason, "message": str(e)}})
        return 2
    cwd = (Path(repo) / request.action.workdir).resolve()
    outputs = {}
    for i, (path, dg) in enumerate(result.output_digests.items()):
        if dg is None:
            continue
        src, dst = cwd / path, out / "outputs" / str(i)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copyfile(src, dst)
        outputs[path] = f"outputs/{i}"
    packed = result.to_json()
    for key, name in (("log", "log.txt"), ("junit", "junit.xml")):
        src = getattr(result, key)
        if src is not None and Path(src).is_file():
            shutil.copyfile(src, out / name)
            packed[key] = name
        else:
            packed[key] = None
    _write(out, {"schema": RESULT_SCHEMA, "result": packed, "outputs": outputs, "error": None})
    return 0


def source_outputs(request_path: Path) -> str:
    """`key=value` lines naming the request's source, for a CI step's outputs."""
    request = read_request(request_path)
    if request.source is None:
        raise BadRequest("the request has no source; a remote worker cannot fetch its inputs")
    return f"repository={request.source.repository}\ncommit={request.source.commit}\n"


def _write(out: Path, payload: dict) -> None:
    (out / "result.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
