import json

from qqrbe import selftest, worker
from qqrbe.request import Source


def write_request(tmp_path, req):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(req.to_json()))
    return path


def test_worker_packs_result_outputs_and_log(repo, tmp_path):
    req = selftest.request(repo, Source("quirq-ai/remote-build", "b" * 40))
    out = tmp_path / "result"
    assert worker.run(write_request(tmp_path, req), repo, out) == 0
    packed = json.loads((out / "result.json").read_text())
    assert packed["schema"] == worker.RESULT_SCHEMA and packed["error"] is None
    result = packed["result"]
    assert result["exit_code"] == 0 and result["log"] == "log.txt" and result["junit"] == "junit.xml"
    stored = out / packed["outputs"][selftest.OUTPUT]
    assert stored.read_text() == (repo / selftest.OUTPUT).read_text()


def test_worker_reports_a_typed_error(repo, tmp_path):
    req = selftest.request(repo)
    (repo / selftest.FIXTURE).write_text("tampered\n")
    out = tmp_path / "result"
    assert worker.run(write_request(tmp_path, req), repo, out) == 2
    packed = json.loads((out / "result.json").read_text())
    assert packed["error"]["reason"] == "input-root-mismatch"


def test_worker_source_outputs(repo, tmp_path):
    req = selftest.request(repo, Source("quirq-ai/remote-build", "c" * 40))
    assert worker.source_outputs(write_request(tmp_path, req)) == (
        f"repository=quirq-ai/remote-build\ncommit={'c' * 40}\n")
