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

    def __init__(self, source_repo, tmp_path, tamper=None, dispatch_status=204):
        self.source_repo, self.tmp, self.tamper = source_repo, tmp_path, tamper
        self.dispatch_status = dispatch_status
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
            return Response(200, json.dumps({"workflow_runs": [
                {"id": 7, "display_title": "qq-exec other", "status": "completed"},
                {"id": 8, "display_title": self.title, "status": "in_progress",
                 "html_url": "https://github.com/run/8"}]}).encode())
        if path.endswith("/runs/8"):
            return Response(200, json.dumps({"id": 8, "status": "completed", "conclusion": "success",
                                             "html_url": "https://github.com/run/8"}).encode())
        if path.endswith("/runs/8/artifacts"):
            return Response(200, json.dumps({"artifacts": [
                {"name": f"qq-result-{self.request_id}", "archive_download_url": "https://dl/zip"}]}).encode())
        if url == "https://dl/zip":
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
