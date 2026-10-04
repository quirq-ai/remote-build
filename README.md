# remote-build

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo is
where actions run and how their results are reused: the executor interface and the action cache.

**Chromium counterpart:** goma, then reclient, then Siso with RBE over REAPI, which works with any
REAPI backend. As there, an action is a command, an input root digest and a platform, and its
result is a set of output digests and an exit code. Actions are shaped like REAPI's `Action` and
`ActionResult` ([bazelbuild/remote-apis](https://github.com/bazelbuild/remote-apis)); v0 needs no
gRPC.

## v0 scope

- One executor interface with a `backend` field: `local` and `github` (a GitHub Actions runner)
  now, so `launchpad` (quirq's own cloud) and Buildbarn slot in later.
- A local action cache keyed by the action digest. Actions that are not hermetic are marked
  `cacheable = false` and never cached. Any remote-to-local fallback is typed and counted, never
  silent.

Out of scope for v0: a shared bazel-remote cache (v1), Buildbarn remote execution and autoscaled
workers (v2).

Actions are planned by the adapters in [quirq-ai/recipes](https://github.com/quirq-ai/recipes).
Plan and every v0 item: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-RBE-01 | Executor interface (`local`, `github`) | | not started |
| V0-RBE-02 | Local action cache and fallback counters | | not started |
