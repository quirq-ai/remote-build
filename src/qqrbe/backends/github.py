"""The `github` backend: run the action on a GitHub Actions runner.

It dispatches the worker workflow (`.github/workflows/execute.yml` in `repository`, default
quirq-ai/remote-build) with the request, finds the run by its run-name, waits for it, downloads
the `qq-result-<request_id>` artifact, and puts the outputs where a local run would have. Every
output is checked against the digest the worker reported.

The token comes from QQ_GITHUB_TOKEN or GITHUB_TOKEN and needs `actions: write` on `repository`.
It is sent only to the GitHub API, never to the artifact storage the API redirects downloads to.
Options (`qqrbe ... --option KEY=VALUE`): repository, workflow, ref (the branch whose worker runs),
api, timeout_s, poll_s. The worker checks the action's platform; v0 workers are ubuntu-24.04 x86_64.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib import error, parse, request as urlrequest

from qqrecipes import digest, runner

from qqrbe.errors import BY_REASON, BackendUnavailable, InputRootMismatch, RemoteExecutionFailed
from qqrbe.executor import ActionResult, Executor
from qqrbe.request import ExecRequest, Source, verify_input_root

API = "https://api.github.com"
DISPATCH_LIMIT = 65_000  # GitHub caps a workflow_dispatch payload at 65,535 characters


@dataclass
class Response:
    status: int
    body: bytes

    def json(self):
        return json.loads(self.body or b"null")


class GitHubExecutor(Executor):
    backend = "github"

    def __init__(self, repository: str = "quirq-ai/remote-build", workflow: str = "execute.yml",
                 ref: str = "main", api: str = API, token: str | None = None,
                 timeout_s: float = 1080, poll_s: float = 5, transport=None, sleep=time.sleep):
        self.repository, self.workflow, self.ref, self.api = repository, workflow, ref, api.rstrip("/")
        self.token = token
        self.timeout_s, self.poll_s = float(timeout_s), float(poll_s)
        self.transport = transport or _urllib_transport
        self.sleep = sleep

    def execute(self, request: ExecRequest, env: runner.Env) -> ActionResult:
        token = self.token or os.environ.get("QQ_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise BackendUnavailable("no GitHub token: set QQ_GITHUB_TOKEN or GITHUB_TOKEN"
                                     " (it needs actions: write on " + self.repository + ")")
        verify_input_root(env.repo, request)
        if request.source is None:
            request = ExecRequest(request.action, request.inputs, _source_for(env.repo, request))
        _check_committed(env.repo, request.inputs)

        request_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
        payload = json.dumps(request.to_json(), separators=(",", ":"))
        if len(payload) > DISPATCH_LIMIT:
            raise BackendUnavailable(f"request is {len(payload)} characters; a GitHub dispatch takes"
                                     f" at most {DISPATCH_LIMIT}. TODO(expert): send it through a CAS")
        started = time.monotonic()
        # Runs created before the dispatch cannot be ours; a minute of slack for clock skew.
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        self._call(token, "POST", f"/repos/{self.repository}/actions/workflows/{self.workflow}/dispatches",
                   {"ref": self.ref, "inputs": {"request_id": request_id, "request": payload}},
                   expect=(200, 204), what="dispatch the worker")
        run = self._find_run(token, request_id, started, since)
        run = self._wait(token, run, started)
        packed, root = self._fetch_result(token, run, request_id, env)
        if packed.get("error"):
            err = packed["error"]
            cls = BY_REASON.get(err.get("reason"), RemoteExecutionFailed)
            raise cls(f"on the GitHub runner ({run['html_url']}): {err.get('message')}")
        return self._materialize(request, env, packed, root, run, time.monotonic() - started)

    # The API calls.

    def _call(self, token, method, path, body=None, expect=(200,), what="call the GitHub API") -> Response:
        url = path if path.startswith("https://") else self.api + path
        if parse.urlsplit(url).netloc != parse.urlsplit(self.api).netloc:
            raise RemoteExecutionFailed(f"refusing to send the token to {url}: not the API host {self.api}")
        try:
            resp = self.transport(method, url, token, body)
        except OSError as e:
            raise BackendUnavailable(f"could not {what}: {e}") from None
        if resp.status not in expect:
            detail = resp.body[:300].decode(errors="replace")
            raise BackendUnavailable(f"could not {what}: HTTP {resp.status} from {method} {path}: {detail}")
        return resp

    def _find_run(self, token, request_id: str, started: float, since: str) -> dict:
        title = f"qq-exec {request_id}"
        query = parse.urlencode({"event": "workflow_dispatch", "branch": self.ref, "per_page": 100,
                                 "created": f">={since}"})
        path = f"/repos/{self.repository}/actions/workflows/{self.workflow}/runs?{query}"
        deadline = started + min(self.timeout_s, 300)
        while True:
            runs = self._call(token, "GET", path, what="list worker runs").json()["workflow_runs"]
            for run in runs:
                if run.get("display_title") == title:
                    return run
            if time.monotonic() > deadline:
                raise BackendUnavailable(f"the worker run {title!r} never appeared on {self.repository}"
                                         f" (branch {self.ref}); is {self.workflow} on that branch?")
            self.sleep(self.poll_s)

    def _wait(self, token, run: dict, started: float) -> dict:
        while run.get("status") != "completed":
            if time.monotonic() > started + self.timeout_s:
                try:  # best effort: do not leave the run holding a runner
                    self._call(token, "POST", f"/repos/{self.repository}/actions/runs/{run['id']}/cancel",
                               expect=(202,), what="cancel the worker run")
                except (BackendUnavailable, RemoteExecutionFailed):
                    pass
                raise RemoteExecutionFailed(f"worker run {run['html_url']} still {run.get('status')}"
                                            f" after {self.timeout_s:.0f}s")
            self.sleep(self.poll_s)
            run = self._call(token, "GET", f"/repos/{self.repository}/actions/runs/{run['id']}",
                             what="poll the worker run").json()
        return run

    def _fetch_result(self, token, run: dict, request_id: str, env: runner.Env) -> tuple[dict, Path]:
        name = f"qq-result-{request_id}"
        listing = self._call(token, "GET", f"/repos/{self.repository}/actions/runs/{run['id']}/artifacts"
                             f"?name={name}", what="list the worker's artifacts").json()
        arts = [a for a in listing.get("artifacts", []) if a.get("name") == name]
        if not arts:
            raise RemoteExecutionFailed(f"worker run {run['html_url']} ended {run.get('conclusion')}"
                                        " without a result; see its log")
        blob = self._call(token, "GET", arts[0]["archive_download_url"], what="download the result").body
        root = Path(env.out).resolve() / "remote" / request_id
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(root)  # zipfile drops absolute paths and '..' components
        try:
            return json.loads((root / "result.json").read_text()), root
        except (OSError, json.JSONDecodeError) as e:
            raise RemoteExecutionFailed(f"worker run {run['html_url']} sent no readable result: {e}") from None

    def _materialize(self, request, env, packed, root: Path, run: dict, duration: float) -> ActionResult:
        """Put outputs where a local run leaves them, after checking each one's digest.

        Only what the request asked for is trusted: the action digest must be the request's, and
        output paths must be the action's declared outputs.
        """
        res = packed["result"]
        url = run["html_url"]
        if res.get("action_digest") != request.action.digest():
            raise RemoteExecutionFailed(f"worker run {url} reports action {res.get('action_digest')},"
                                        f" not the requested {request.action.digest()}")
        if set(res.get("output_digests", {})) != set(request.action.outputs):
            raise RemoteExecutionFailed(f"worker run {url} reports outputs {sorted(res.get('output_digests', {}))},"
                                        f" not the declared {sorted(request.action.outputs)}")
        root = root.resolve()
        cwd = (Path(env.repo) / request.action.workdir).resolve()
        for path, stored in packed.get("outputs", {}).items():
            src = (root / stored).resolve()
            if path not in request.action.outputs or not src.is_relative_to(root):
                raise RemoteExecutionFailed(f"worker run {url} names output {path!r} at {stored!r},"
                                            " outside what was asked for")
            if digest.path_digest(src) != res["output_digests"].get(path):
                raise RemoteExecutionFailed(f"output {path} does not match its reported digest")
            dst = cwd / path
            if dst.is_dir():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, dst)
        out = Path(env.out).resolve()
        slug = runner.slug(request.action)
        files = {}
        for key, sub, ext in (("log", "logs", "log"), ("junit", "junit", "xml")):
            if res.get(key):
                src = (root / res[key]).resolve()
                if not src.is_relative_to(root) or not src.is_file():
                    raise RemoteExecutionFailed(f"worker run {url} names {key} file {res[key]!r} outside the result")
                dst = out / sub / f"{slug}.{ext}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                files[key] = dst
        return ActionResult(
            action_digest=request.action.digest(),
            backend=self.backend,
            exit_code=res["exit_code"],
            duration_s=duration,
            output_digests=dict(res["output_digests"]),
            log=files.get("log"),
            junit=files.get("junit"),
            details={"run_url": run["html_url"], "remote_duration_s": res["duration_s"]},
        )


def _source_for(repo: Path, request: ExecRequest) -> Source:
    return Source.from_git(repo, os.environ.get("GITHUB_REPOSITORY") or None)


def _check_committed(repo: Path, inputs) -> None:
    """The worker sees the commit, not this work tree: refuse inputs with uncommitted changes."""
    if not inputs:
        return
    r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *inputs],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0 or r.stdout.strip():
        raise InputRootMismatch("inputs differ from the commit the GitHub worker will check out:"
                                f" {r.stdout.strip() or r.stderr.strip()}; commit them first")


def _urllib_transport(method: str, url: str, token: str, body) -> Response:
    data = None if body is None else json.dumps(body).encode()
    req = urlrequest.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "qqrbe")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # Unredirected: urllib follows the artifact download's redirect to storage without the token.
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            return Response(resp.status, resp.read())
    except error.HTTPError as e:
        return Response(e.code, e.read())


def create(**options) -> GitHubExecutor:
    for key in ("timeout_s", "poll_s"):
        if key in options:
            options[key] = float(options[key])
    allowed = {"repository", "workflow", "ref", "api", "timeout_s", "poll_s"}
    unknown = set(options) - allowed
    if unknown:
        raise TypeError(f"the github backend takes {', '.join(sorted(allowed))}; got {', '.join(sorted(unknown))}")
    return GitHubExecutor(**options)

