import json

from qqrbe import cli


def result(backend, digest="sha256:1", out="sha256:2", code=0):
    return {"backend": backend, "action_digest": digest, "exit_code": code,
            "output_digests": {"o": out}}


def compare(tmp_path, a, b):
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    return cli.main(["compare", str(pa), str(pb)])


def test_compare_passes_on_same_digests(tmp_path, capsys):
    assert compare(tmp_path, result("local"), result("github")) == 0
    assert "same output on local and github" in capsys.readouterr().out


def test_compare_fails_on_different_outputs_or_failures(tmp_path):
    assert compare(tmp_path, result("local"), result("github", out="sha256:3")) == 1
    assert compare(tmp_path, result("local"), result("github", code=1)) == 1
    assert compare(tmp_path, result("local"), result("github", out=None)) == 1


def test_selftest_local(repo, tmp_path, capsys):
    assert cli.main(["selftest", "--repo", str(repo), "--out", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "local"


def test_unknown_backend_is_exit_2(repo, tmp_path):
    assert cli.main(["selftest", "--backend", "nope", "--repo", str(repo), "--out", str(tmp_path)]) == 2
