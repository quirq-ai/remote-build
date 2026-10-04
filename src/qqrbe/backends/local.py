"""The `local` backend: run the action as a subprocess on this machine.

It runs through recipes' runner (placeholders, logs, JUnit), after checking that this machine is
the action's platform and that the repo's files hash to the action's input root.
"""
from __future__ import annotations

from qqrecipes import runner

from qqrbe.executor import ActionResult, Executor
from qqrbe.request import ExecRequest, check_platform, verify_input_root


class LocalExecutor(Executor):
    backend = "local"

    def execute(self, request: ExecRequest, env: runner.Env) -> ActionResult:
        check_platform(request.action)
        verify_input_root(env.repo, request)
        r = runner.run(request.action, env)
        return ActionResult(
            action_digest=r.action_digest,
            backend=self.backend,
            exit_code=r.exit_code,
            duration_s=r.duration_s,
            output_digests=r.output_digests,
            log=r.log,
            junit=r.junit,
        )


def create(**options) -> LocalExecutor:
    if options:
        raise TypeError(f"the local backend takes no options, got {', '.join(sorted(options))}")
    return LocalExecutor()
