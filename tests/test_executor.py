import pytest
from qqrecipes import runner

from qqrbe import executor, selftest
from qqrbe.backends.local import LocalExecutor
from qqrbe.errors import BackendNotFound


def test_backends_are_discovered():
    assert "local" in executor.available()
    assert isinstance(executor.load("local"), LocalExecutor)


def test_unknown_backend_names_the_known_ones():
    with pytest.raises(BackendNotFound, match="local"):
        executor.load("nope")
    with pytest.raises(BackendNotFound):
        executor.load("Bad_Name")


def test_local_runs_the_selftest_deterministically(repo, tmp_path_factory):
    req = selftest.request(repo)
    results = [executor.load("local").execute(req, runner.Env(repo=repo, out=tmp_path_factory.mktemp("o")))
               for _ in range(2)]
    a, b = results
    assert a.ok and a.backend == "local"
    assert a.action_digest == req.action.digest()
    assert a.output_digests == b.output_digests
    assert None not in a.output_digests.values()
    out = (repo / selftest.OUTPUT).read_text().splitlines()
    assert out == sorted(out)  # byte order under LC_ALL=C


def test_result_round_trips(repo, tmp_path):
    r = executor.load("local").execute(selftest.request(repo), runner.Env(repo=repo, out=tmp_path))
    assert executor.ActionResult.from_json(r.to_json()).to_json() == r.to_json()
