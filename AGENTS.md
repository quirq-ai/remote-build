# Agent guide

How an agent changes this repo safely. Read `README.md` first.

- Every change is a pull request against `main`, titled with its work item id (for example
  `V0-RBE-01: ...`). It lands only with the `presubmit` check green.
- The executor core names no language, build tool or test runner: it runs whatever command an
  action carries. Backend-specific code (GitHub now, Launchpad later) stays behind the `backend`
  field, in its own module.
- Actions come from `quirq-ai/recipes` (`qqrecipes`), used by pinned commit, never copied.
- Leave `.github/CODEOWNERS` and any `owners` list empty; suraj assigns people.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- This repo is public: no secrets, tokens or internal hostnames.
