import dataclasses
import json

import pytest
from qqrecipes import runner

from qqrbe import cache as qqcache, cli, executor, selftest
from qqrbe.errors import BackendUnavailable, BadRequest, InputRootMismatch, RemoteExecutionFailed
from qqrbe.executor import Executor


@pytest.fixture(autouse=True)
def pinned_image(monkeypatch):
    """Make the selftest cacheable, as on a GitHub-hosted runner."""
    monkeypatch.setenv("ImageOS", "ubuntu24")
    monkeypatch.setenv("ImageVersion", "20260928.1")


class Counting(Executor):
    def __init__(self, inner, backend=None):
        self.inner, self.runs = inner, 0
        self.backend = backend or inner.backend

    def execute(self, request, env):
        self.runs += 1
        return self.inner.execute(request, env)


class Down(Executor):
    backend = "github"

    def __init__(self, error):
        self.error = error

    def execute(self, request, env):
        raise self.error


def env(repo, tmp_path):
    return runner.Env(repo=repo, out=tmp_path / "out")


def test_rerun_is_a_reported_hit_that_restores_outputs(repo, tmp_path):
    inner = Counting(executor.load("local"))
    ex = qqcache.CachingExecutor(inner, qqcache.ActionCache(tmp_path / "cache"))
    req = selftest.request(repo)
    first = ex.execute(req, env(repo, tmp_path))
    (repo / selftest.OUTPUT).unlink()
    second = ex.execute(req, env(repo, tmp_path))
    assert first.details["cache"] == "miss" and second.details["cache"] == "hit"
    assert inner.runs == 1
    assert second.output_digests == first.output_digests and second.backend == "local"
    assert (repo / selftest.OUTPUT).is_file() and second.log.is_file() and second.junit.is_file()
    stats = json.loads((tmp_path / "cache" / "stats.json").read_text())
    assert stats["cache"] == {"hit": 1, "miss": 1, "uncacheable": 0}


def test_changed_input_misses(repo, tmp_path):
    ex = qqcache.CachingExecutor(executor.load("local"), qqcache.ActionCache(tmp_path / "cache"))
    ex.execute(selftest.request(repo), env(repo, tmp_path))
    (repo / selftest.FIXTURE).write_text("zebra\nant\n")
    assert ex.execute(selftest.request(repo), env(repo, tmp_path)).details["cache"] == "miss"


def test_stale_request_is_refused_not_served_from_cache(repo, tmp_path):
    ex = qqcache.CachingExecutor(executor.load("local"), qqcache.ActionCache(tmp_path / "cache"))
    req = selftest.request(repo)
    ex.execute(req, env(repo, tmp_path))
    (repo / selftest.FIXTURE).write_text("zebra\n")
    with pytest.raises(InputRootMismatch):
        ex.execute(req, env(repo, tmp_path))


def test_uncacheable_and_failed_actions_always_run(repo, tmp_path, monkeypatch):
    inner = Counting(executor.load("local"))
    ex = qqcache.CachingExecutor(inner, qqcache.ActionCache(tmp_path / "cache"))
    monkeypatch.delenv("ImageOS")
    ambient = selftest.request(repo)
    for _ in range(2):
        assert ex.execute(ambient, env(repo, tmp_path)).details["cache"] == "uncacheable"
    monkeypatch.setenv("ImageOS", "ubuntu24")
    req = selftest.request(repo)
    failing = dataclasses.replace(req, action=dataclasses.replace(req.action, argv=("sh", "-c", "exit 3")))
    for _ in range(2):
        assert ex.execute(failing, env(repo, tmp_path)).exit_code == 3
    assert inner.runs == 4


def test_missing_blobs_are_a_miss(repo, tmp_path):
    cache = qqcache.ActionCache(tmp_path / "cache")
    ex = qqcache.CachingExecutor(executor.load("local"), cache)
    req = selftest.request(repo)
    first = ex.execute(req, env(repo, tmp_path))
    for blob in (tmp_path / "cache" / "cas").iterdir():
        blob.unlink()
    assert ex.execute(req, env(repo, tmp_path)).details["cache"] == "miss"
    assert first.ok


@pytest.mark.parametrize("error", [BackendUnavailable("no token"), RemoteExecutionFailed("timed out")])
def test_fallback_is_typed_counted_and_reported(repo, tmp_path, error):
    stats = qqcache.Stats(tmp_path / "stats.json")
    warnings = []
    ex = qqcache.FallbackExecutor(Down(error), executor.load("local"), stats, warn=warnings.append)
    result = ex.execute(selftest.request(repo), env(repo, tmp_path))
    assert result.ok and result.backend == "local"
    assert result.details["fallback"] == {"from": "github", "reason": error.reason}
    assert "FALLBACK github->local" in warnings[0]
    saved = json.loads((tmp_path / "stats.json").read_text())
    assert saved["fallbacks"] == {"github->local": {error.reason: 1}}
    assert saved["events"][0]["action_digest"] == selftest.request(repo).action.digest()


def test_requests_that_would_fail_anywhere_do_not_fall_back(repo, tmp_path):
    stats = qqcache.Stats(tmp_path / "stats.json")
    ex = qqcache.FallbackExecutor(Down(BadRequest("bad")), executor.load("local"), stats)
    with pytest.raises(BadRequest):
        ex.execute(selftest.request(repo), env(repo, tmp_path))
    assert stats.data["fallbacks"] == {}


def test_cli_reports_hit_and_fallback_counts(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("QQ_GITHUB_TOKEN", raising=False)
    base = ["--repo", str(repo), "--out", str(tmp_path / "out"), "--json"]
    assert cli.main(["selftest", *base]) == 0
    assert cli.main(["selftest", *base]) == 0
    second = json.loads(capsys.readouterr().out.split("\n}\n")[1] + "\n}")
    assert second["details"]["cache"] == "hit"
    assert cli.main(["selftest", "--backend", "github", "--fallback", "local", "--no-cache", *base]) == 0
    assert "FALLBACK github->local (backend-unavailable)" in capsys.readouterr().err
    assert cli.main(["stats", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "cache: 1 hit, 1 miss" in out and "fallback github->local (backend-unavailable): 1" in out


def _cached(repo, tmp_path):
    cache = qqcache.ActionCache(tmp_path / "cache")
    ex = qqcache.CachingExecutor(executor.load("local"), cache)
    req = selftest.request(repo)
    first = ex.execute(req, env(repo, tmp_path))
    return cache, ex, req, first


def test_corrupt_blob_is_a_miss_not_a_poisoned_hit(repo, tmp_path):
    cache, ex, req, first = _cached(repo, tmp_path)
    blob = cache._cas(first.output_digests[selftest.OUTPUT])
    blob.write_text("POISON\n")
    again = ex.execute(req, env(repo, tmp_path))
    assert again.details["cache"] == "miss"
    assert (repo / selftest.OUTPUT).read_text() != "POISON\n"


def test_tampered_entry_cannot_write_outside_the_repo(repo, tmp_path):
    cache, ex, req, first = _cached(repo, tmp_path)
    ac = cache._ac(req.action.digest())
    entry = json.loads(ac.read_text())
    dg = entry["result"]["output_digests"].pop(selftest.OUTPUT)
    entry["result"]["output_digests"]["../../escaped.txt"] = dg
    ac.write_text(json.dumps(entry))
    assert ex.execute(req, env(repo, tmp_path)).details["cache"] == "miss"
    assert not (repo.parent.parent / "escaped.txt").exists()
    entry["result"]["output_digests"] = {selftest.OUTPUT: dg}
    entry["result"]["action_digest"] = "sha256:" + "0" * 64
    ac.write_text(json.dumps(entry))
    assert ex.execute(req, env(repo, tmp_path)).details["cache"] == "miss"


def test_hit_does_not_repeat_an_old_fallback(repo, tmp_path):
    cache = qqcache.ActionCache(tmp_path / "cache")
    fb = qqcache.FallbackExecutor(Down(BackendUnavailable("down")), executor.load("local"), cache.stats,
                                  warn=lambda m: None)
    ex = qqcache.CachingExecutor(fb, cache)
    req = selftest.request(repo)
    assert ex.execute(req, env(repo, tmp_path)).details["fallback"]["reason"] == "backend-unavailable"
    hit = ex.execute(req, env(repo, tmp_path))
    assert hit.details["cache"] == "hit" and "fallback" not in hit.details
    assert cache.stats.data["fallbacks"] == {"github->local": {"backend-unavailable": 1}}


def test_restore_does_not_write_through_a_symlink(repo, tmp_path_factory, tmp_path):
    cache, ex, req, first = _cached(repo, tmp_path)
    target = tmp_path_factory.mktemp("outside") / "target.txt"
    target.write_text("keep\n")
    out = repo / selftest.OUTPUT
    out.unlink()
    out.symlink_to(target)
    assert ex.execute(req, env(repo, tmp_path)).details["cache"] == "hit"
    assert target.read_text() == "keep\n" and not out.is_symlink()


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("backend"),
    lambda r: r.update(details=[]),
    lambda r: r.update(exit_code=3),
])
def test_malformed_or_failed_entry_is_a_miss(repo, tmp_path, mutate):
    cache, ex, req, first = _cached(repo, tmp_path)
    ac = cache._ac(req.action.digest())
    entry = json.loads(ac.read_text())
    mutate(entry["result"])
    ac.write_text(json.dumps(entry))
    again = ex.execute(req, env(repo, tmp_path))
    assert again.details["cache"] == "miss" and again.ok
