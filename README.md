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

## How it works (v0)

- An **action** is a recipes `Action` (`qqrecipes.contract`, pinned by commit): a command, an
  input root digest, environment, declared outputs and platform properties. `Action.digest()` is
  its key. A request (`qq-exec-request/1`) adds the repo-relative files of the input root and,
  for remote backends, the git repository and commit to fetch them from.
- An **executor** runs one request and returns an `ActionResult`: exit code and the digest of
  every declared output. `qqrbe.executor.load(backend)` imports `qqrbe.backends.<backend>`;
  nothing registers backends, so `launchpad` or `buildbarn` is one new module.
- Before running, every executor checks that the files it sees hash to the input root digest and
  that it is the action's platform. A failure there is a typed error (`qqrbe.errors`), never a
  silent run on the wrong inputs. A non-zero exit code is a result, not an error.
- Backends: `local` runs the action here through recipes' runner. `github` dispatches
  `.github/workflows/execute.yml`, a worker that checks out the commit, verifies the input root,
  runs the action with the local executor and sends back the result and outputs as an artifact.
  v0 uses git as the input store. TODO(expert): a REAPI CAS (v1 shared cache).
  The `github` backend needs a token with `actions: write` (QQ_GITHUB_TOKEN or GITHUB_TOKEN).
- CI's `cross-backend` job runs `qqrbe selftest` on both backends and `qqrbe compare` fails it
  unless both give the same action digest and the same output digests (V0-RBE-01 done-when).

- **Action cache** (`qqrbe.cache`, V0-RBE-02): `exec` and `selftest` look the action digest up in
  `<repo>/.qq/cache` first. A hit restores the outputs, log and JUnit and says `cache: hit`; a miss
  runs and stores the result if it succeeded. Actions marked `cacheable = false` (or run on an
  ambient toolchain) always run and say `cache: uncacheable`. `--no-cache` turns it off.
- **Fallback** (`--fallback local`): if the backend cannot run the action (`backend-unavailable`,
  `remote-execution-failed`), it runs locally instead. Every fallback prints a `FALLBACK` line,
  is marked in the result, and is counted by backend pair and reason in `.qq/cache/stats.json`.
  Wrong inputs, a wrong platform or a bad request never fall back: they would fail anywhere.
- CI shows both: a second run of the selftest must be a cache hit, and a run on `github` with no
  token must fall back and be counted (`qqrbe stats`).

```sh
qqrbe backends                                   # local, github, ...
qqrbe selftest --backend local                   # a small deterministic action
qqrbe exec --request req.json --backend github   # run a qq-exec-request/1
qqrbe compare local.json github.json             # same action, same output digests?
qqrbe selftest --backend github --fallback local # fall back if GitHub cannot run it, counted
qqrbe stats                                      # cache hits/misses, fallbacks by reason
```

Actions are planned by the adapters in [quirq-ai/recipes](https://github.com/quirq-ai/recipes).
Plan and every v0 item: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-RBE-01 | Executor interface (`local`, `github`) | #2, #3 | merged |
| V0-RBE-02 | Local action cache and fallback counters | #4 | merged |

## Working here

Read `AGENTS.md`. Run the tests with `pip install -e ".[test]" && pytest`.

## Licence

Apache License 2.0; see `LICENSE`.
