"""Typed executor failures. Each has a stable `reason`, the key fallbacks are counted by.

A failing *action* (a non-zero exit code) is a result, not one of these: these mean the executor
could not run the action as asked.
"""
from __future__ import annotations


class ExecutorError(Exception):
    """Base of every typed executor failure."""

    reason = "executor-error"


class BadRequest(ExecutorError):
    """The request is malformed: a missing field, or an action whose digest does not match it."""

    reason = "bad-request"


class BackendNotFound(ExecutorError, LookupError):
    reason = "backend-not-found"


class BackendUnavailable(ExecutorError):
    """The backend could not be reached or used: no credentials, an API error, no worker."""

    reason = "backend-unavailable"


class RemoteExecutionFailed(ExecutorError):
    """The remote worker itself failed or timed out (not the action it was running)."""

    reason = "remote-execution-failed"


class InputRootMismatch(ExecutorError):
    """The files the executor sees do not hash to the action's input root digest."""

    reason = "input-root-mismatch"


class PlatformMismatch(ExecutorError):
    """The action asks for a platform this executor is not."""

    reason = "platform-mismatch"


BY_REASON = {cls.reason: cls for cls in (
    ExecutorError, BadRequest, BackendNotFound, BackendUnavailable, RemoteExecutionFailed,
    InputRootMismatch, PlatformMismatch)}
