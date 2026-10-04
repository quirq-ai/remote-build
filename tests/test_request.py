import dataclasses

import pytest

from qqrbe import selftest
from qqrbe.errors import BadRequest, InputRootMismatch, PlatformMismatch
from qqrbe.request import ExecRequest, Source, check_platform, input_files, verify_input_root


def test_round_trip_keeps_the_action_digest(repo):
    req = selftest.request(repo, Source("quirq-ai/remote-build", "a" * 40))
    back = ExecRequest.from_json(req.to_json())
    assert back == req
    assert back.action.digest() == req.action.digest()


def test_tampered_action_is_rejected(repo):
    data = selftest.request(repo).to_json()
    data["action"]["argv"] = ["sh", "-c", "true"]
    with pytest.raises(BadRequest, match="does not match"):
        ExecRequest.from_json(data)


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd"])
def test_inputs_must_stay_inside_the_repo(repo, bad):
    data = selftest.request(repo).to_json()
    data["inputs"] = [bad]
    with pytest.raises(BadRequest, match="inside the repo"):
        ExecRequest.from_json(data)


def test_wrong_schema_and_missing_fields_are_bad_requests(repo):
    with pytest.raises(BadRequest, match="not a qq-exec-request/1"):
        ExecRequest.from_json({"schema": "other"})
    data = selftest.request(repo).to_json()
    del data["inputs"]
    with pytest.raises(BadRequest, match="inputs"):
        ExecRequest.from_json(data)


@pytest.mark.parametrize("repository,commit", [("noslash", "a" * 40), ("o/r", "abc")])
def test_source_is_validated(repository, commit):
    with pytest.raises(BadRequest):
        Source(repository, commit)


def test_changed_input_is_an_input_root_mismatch(repo):
    req = selftest.request(repo)
    verify_input_root(repo, req)
    (repo / selftest.FIXTURE).write_text("changed\n")
    with pytest.raises(InputRootMismatch, match="uncommitted"):
        verify_input_root(repo, req)
    (repo / selftest.FIXTURE).unlink()
    with pytest.raises(InputRootMismatch, match="missing"):
        verify_input_root(repo, req)


def test_input_files_lists_what_the_globs_match(repo):
    assert input_files(repo, ["selftest/**"]) == (selftest.FIXTURE,)


def test_platform_mismatch(repo):
    action = dataclasses.replace(selftest.request(repo).action, platform=(("os", "plan9"),))
    with pytest.raises(PlatformMismatch):
        check_platform(action)
