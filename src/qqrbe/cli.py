"""qqrbe: run REAPI-shaped actions on an executor backend.

    qqrbe backends                                       the backends this install has
    qqrbe exec --request FILE --backend NAME [--option KEY=VALUE ...]
    qqrbe selftest --backend NAME [--option KEY=VALUE ...] [--json]
    qqrbe compare A.json B.json                          same action, same output digests?
    qqrbe worker --request FILE --repo DIR --out DIR     the remote side of a backend
    qqrbe worker-source --request FILE                   the request's source, as CI step outputs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qqrecipes import runner

from qqrbe import executor, selftest, worker
from qqrbe.errors import ExecutorError


def parse_options(values: list[str]) -> dict[str, str]:
    options = {}
    for v in values:
        key, sep, value = v.partition("=")
        if not sep or not key:
            raise SystemExit(f"qqrbe: --option takes KEY=VALUE, got {v!r}")
        options[key.replace("-", "_")] = value
    return options


def _execute(args, request) -> int:
    repo = Path(args.repo).resolve()
    env = runner.Env(repo=repo, out=Path(args.out).resolve())
    try:
        result = executor.load(args.backend, **parse_options(args.option)).execute(request, env)
    except ExecutorError as e:
        print(f"qqrbe: {args.backend}: {e.reason}: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        print(f"{result.backend}: exit {result.exit_code} in {result.duration_s:.1f}s"
              f"  action {result.action_digest}")
        for path, dg in result.output_digests.items():
            print(f"  {path}  {dg or 'not produced'}")
        for key, value in result.details.items():
            print(f"  {key}: {value}")
    return 0 if result.ok else 1


def cmd_exec(args) -> int:
    try:
        request = worker.read_request(Path(args.request))
    except ExecutorError as e:
        print(f"qqrbe: {e.reason}: {e}", file=sys.stderr)
        return 2
    return _execute(args, request)


def cmd_selftest(args) -> int:
    return _execute(args, selftest.request(Path(args.repo).resolve()))


def cmd_compare(args) -> int:
    a, b = (json.loads(Path(p).read_text()) for p in (args.a, args.b))
    problems = []
    for r in (a, b):
        if r["exit_code"] != 0:
            problems.append(f"{r['backend']} exited {r['exit_code']}")
        if not r["output_digests"] or None in r["output_digests"].values():
            problems.append(f"{r['backend']} did not produce every output: {r['output_digests']}")
    if a["action_digest"] != b["action_digest"]:
        problems.append(f"different actions: {a['action_digest']} vs {b['action_digest']}")
    if a["output_digests"] != b["output_digests"]:
        problems.append(f"different outputs: {a['backend']} {a['output_digests']}"
                        f" vs {b['backend']} {b['output_digests']}")
    if problems:
        print("qqrbe compare: FAIL\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    for path, dg in a["output_digests"].items():
        print(f"same output on {a['backend']} and {b['backend']}: {path} {dg}")
    print(f"action {a['action_digest']}")
    return 0


def cmd_worker(args) -> int:
    return worker.run(Path(args.request), Path(args.repo), Path(args.out))


def cmd_worker_source(args) -> int:
    try:
        sys.stdout.write(worker.source_outputs(Path(args.request), args.out and Path(args.out)))
    except ExecutorError as e:
        print(f"qqrbe: {e.reason}: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_backends(args) -> int:
    print("\n".join(executor.available()))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qqrbe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backends").set_defaults(fn=cmd_backends)
    for name, fn in (("exec", cmd_exec), ("selftest", cmd_selftest)):
        s = sub.add_parser(name)
        if name == "exec":
            s.add_argument("--request", required=True)
        s.add_argument("--backend", default="local")
        s.add_argument("--option", action="append", default=[], metavar="KEY=VALUE")
        s.add_argument("--repo", default=".")
        s.add_argument("--out", default=".qq/out")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("compare")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("worker")
    s.add_argument("--request", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_worker)
    s = sub.add_parser("worker-source")
    s.add_argument("--request", required=True)
    s.add_argument("--out", help="on a bad request, write its typed error here as result.json")
    s.set_defaults(fn=cmd_worker_source)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
