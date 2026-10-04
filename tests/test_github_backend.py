"""The github backend against a fake GitHub API whose "runner" is the real worker, run locally."""
import io
import json
import shutil
import subprocess
import zipfile
from urllib.parse import urlparse

import pytest
from qqrecipes import runner

from qqrbe import executor, selftest, worker
from qqrbe.backends.github import GitHubExecutor, Response
from qqrbe.errors import BackendUnavailable, InputRootMismatch, RemoteExecutionFailed


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(repo):
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", "https://github.com/quirq-ai/remote-build.git")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture")
    return repo


class FakeGitHub:
    """Enough of the Actions API for one dispatch: the run completes on the first poll."""

    def __init__(self, source_repo, tmp_path, tamper=None, dispatch_status=204, hidden_polls=0,
                 never_completes=False, artifact_url="https://api.github.com/zip", tamper_result=None):
        self.source_repo, self.tmp, self.tamper = source_repo, tmp_path, tamper
        self.dispatch_status = dispatch_status
        self.hidden_polls, self.never_completes = hidden_polls, never_completes
        self.artifact_url, self.tamper_result = artifact_url, tamper_result
        self.calls, self.tokens, self.zip = [], set(), None
        self.title = None

    def __call__(self, method, url, token, body):
        self.calls.append((method, url))
        self.tokens.add(token)
        path = urlparse(url).path
        if method == "POST" and path.endswith("/dispatches"):
            if self.dispatch_status != 204:
                return Response(self.dispatch_status, b'{"message":"Resource not accessible"}')
            self.run_worker(body)
            return Response(204, b"")
        if path.endswith("/execute.yml/runs"):
            if self.hidden_polls:  # the run takes a few polls to show up
                self.hidden_polls -= 1
                return Response(200, b'{"workflow_runs": []}')
            return Response(200, json.dumps({"workflow_runs": [
                {"id": 7, "display_title": "qq-exec other", "status": "completed"},
                {"id": 8, "display_title": self.title, "status": "in_progress",
                 "html_url": "https://github.com/run/8"}]}).encode())
        if path.endswith("/runs/8/cancel"):
            return Response(202, b"{}")
        if path.endswith("/runs/8"):
            return Response(200, json.dumps({"id": 8, "status": "queued" if self.never_completes else "completed", "conclusion": "success",
                                             "html_url": "https://github.com/run/8"}).encode())
        if path.endswith("/runs/8/artifacts"):
            return Response(200, json.dumps({"artifacts": [
                {"name": f"qq-result-{self.request_id}", "archive_download_url": self.artifact_url}]}).encode())
        if url == "https://api.github.com/zip":
            return Response(200, self.zip)
        return Response(404, b"{}")

    def run_worker(self, body):
        self.request_id = body["inputs"]["request_id"]
        self.title = f"qq-exec {self.request_id}"
        request = json.loads(body["inputs"]["request"])
        assert request["source"]["repository"] == "quirq-ai/remote-build"
        remote = self.tmp / "runner-src"  # a separate checkout, as on the runner
        shutil.copytree(self.source_repo, remote, ignore=shutil.ignore_patterns(".qq"))
        (self.tmp / "req.json").write_text(json.dumps(request))
        out = self.tmp / "runner-result"
        worker.run(self.tmp / "req.json", remote, out)
        if self.tamper:
            self.tamper(out)
        if self.tamper_result:
            packed = json.loads((out / "result.json").read_text())
            self.tamper_result(packed)
            (out / "result.json").write_text(json.dumps(packed))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for f in out.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(out).as_posix())
        self.zip = buf.getvalue()


def github(fake, **kw):
    return GitHubExecutor(token="t0ken", transport=fake, sleep=lambda s: None, **kw)


def test_same_output_digest_as_local(checkout, tmp_path):
    req = selftest.request(checkout)
    local = executor.load("local").execute(req, runner.Env(repo=checkout, out=tmp_path / "l"))
    (checkout / selftest.OUTPUT).unlink()
    fake = FakeGitHub(checkout, tmp_path)
    remote = github(fake).execute(req, runner.Env(repo=checkout, out=tmp_path / "g"))
    assert remote.backend == "github" and remote.ok
    assert remote.action_digest == local.action_digest
    assert remote.output_digests == local.output_digests
    assert (checkout / selftest.OUTPUT).is_file()  # outputs land where a local run puts them
    assert remote.log.is_file() and remote.details["run_url"] == "https://github.com/run/8"
    assert fake.tokens == {"t0ken"}


def test_no_token_is_backend_unavailable(checkout, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("QQ_GITHUB_TOKEN", raising=False)
    ex = GitHubExecutor(transport=FakeGitHub(checkout, tmp_path))
    with pytest.raises(BackendUnavailable, match="no GitHub token"):
        ex.execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path))


def test_refused_dispatch_is_backend_unavailable(checkout, tmp_path):
    fake = FakeGitHub(checkout, tmp_path, dispatch_status=403)
    with pytest.raises(BackendUnavailable, match="HTTP 403"):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path))


def test_uncommitted_input_is_refused_before_dispatch(checkout, tmp_path):
    (checkout / selftest.FIXTURE).write_text("zebra\n")
    req = selftest.request(checkout)  # matches the work tree, not the commit
    fake = FakeGitHub(checkout, tmp_path)
    with pytest.raises(InputRootMismatch, match="commit them"):
        github(fake).execute(req, runner.Env(repo=checkout, out=tmp_path))
    assert fake.calls == []


def test_tampered_output_is_caught(checkout, tmp_path):
    def tamper(out):
        (out / "outputs" / "0").write_text("not sorted\n")
    fake = FakeGitHub(checkout, tmp_path, tamper=tamper)
    with pytest.raises(RemoteExecutionFailed, match="does not match"):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))


def test_worker_error_comes_back_typed(checkout, tmp_path):
    def tamper(out):
        (out / "result.json").write_text(json.dumps(
            {"schema": worker.RESULT_SCHEMA, "error": {"reason": "input-root-mismatch", "message": "x"}}))
    fake = FakeGitHub(checkout, tmp_path, tamper=tamper)
    with pytest.raises(InputRootMismatch, match="on the GitHub runner"):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))


def test_options_are_checked():
    assert executor.load("github", ref="feature", poll_s="1").ref == "feature"
    with pytest.raises(TypeError):
        executor.load("github", nope="1")


def test_run_that_is_slow_to_appear_is_found(checkout, tmp_path):
    fake = FakeGitHub(checkout, tmp_path, hidden_polls=3)
    assert github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g")).ok
    listed = [u for m, u in fake.calls if "/execute.yml/runs" in u]
    assert len(listed) == 4 and all("created=%3E%3D" in u for u in listed)


def test_timeout_cancels_the_run(checkout, tmp_path):
    fake = FakeGitHub(checkout, tmp_path, never_completes=True)
    ex = github(fake, timeout_s=0)
    with pytest.raises(RemoteExecutionFailed, match="still in_progress"):
        ex.execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))
    assert ("POST", "https://api.github.com/repos/quirq-ai/remote-build/actions/runs/8/cancel") in fake.calls


@pytest.mark.parametrize("tamper_result,match", [
    (lambda p: p["result"].update(action_digest="sha256:" + "0" * 64), "not the requested"),
    (lambda p: p["outputs"].update({"/etc/evil": "outputs/0"}), "outside what was asked"),
    (lambda p: p["result"]["output_digests"].update({"../evil": None}), "not the declared"),
    (lambda p: p["result"].update(log="../../../../etc/passwd"), "outside the result"),
])
def test_result_cannot_point_outside_the_request(checkout, tmp_path, tamper_result, match):
    fake = FakeGitHub(checkout, tmp_path, tamper_result=tamper_result)
    with pytest.raises(RemoteExecutionFailed, match=match):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))


def test_token_is_only_sent_to_the_api_host(checkout, tmp_path):
    fake = FakeGitHub(checkout, tmp_path, artifact_url="https://evil.example/zip")
    with pytest.raises(RemoteExecutionFailed, match="refusing to send the token"):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))
    assert not any("evil" in u for _, u in fake.calls)


def test_urllib_transport_keeps_the_token_off_redirects():
    """The artifact download redirects to storage; the token must not follow it."""
    import http.server
    import threading

    from qqrbe.backends.github import _urllib_transport

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen[self.path] = self.headers.get("Authorization")
            if self.path == "/zip":
                self.send_response(302)
                self.send_header("Location", "/blob")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"data")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = _urllib_transport("GET", f"http://127.0.0.1:{server.server_port}/zip", "t0ken", None)
    finally:
        server.shutdown()
    assert resp.status == 200 and resp.body == b"data"
    assert seen == {"/zip": "Bearer t0ken", "/blob": None}


def test_digest_without_its_file_is_rejected(checkout, tmp_path):
    def drop_file(packed):
        packed["outputs"] = {}  # claims the digest, sends nothing
    fake = FakeGitHub(checkout, tmp_path, tamper_result=drop_file)
    (checkout / selftest.OUTPUT).unlink(missing_ok=True)
    with pytest.raises(RemoteExecutionFailed, match="did not send the files"):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))


def test_malformed_result_is_a_typed_failure(checkout, tmp_path):
    fake = FakeGitHub(checkout, tmp_path, tamper_result=lambda p: p["result"].pop("exit_code"))
    with pytest.raises(RemoteExecutionFailed, match="malformed"):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))


def test_rejected_result_leaves_the_work_tree_alone(checkout, tmp_path):
    (checkout / selftest.OUTPUT).parent.mkdir(parents=True, exist_ok=True)
    (checkout / selftest.OUTPUT).write_text("previous\n")
    fake = FakeGitHub(checkout, tmp_path, tamper_result=lambda p: p.update(outputs={}))
    with pytest.raises(RemoteExecutionFailed):
        github(fake).execute(selftest.request(checkout), runner.Env(repo=checkout, out=tmp_path / "g"))
    assert (checkout / selftest.OUTPUT).read_text() == "previous\n"
