"""Local command line entry point; no network services or API keys required."""
import argparse
import json
from pathlib import Path
import sys

from .engine import run_review

ROOT = Path(__file__).resolve().parent.parent


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def no_constant(value):
        raise ValueError("Non-finite JSON number: " + value)

    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle, parse_float=str, parse_constant=no_constant,
                         object_pairs_hook=unique)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Financial Statements Tie-Out")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Open the local review application")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--audit-dir", type=Path, default=Path("audit-runs"))
    review = sub.add_parser("review", help="Run a structured engagement")
    review.add_argument("input", type=Path)
    review.add_argument("--rules", type=Path, default=ROOT / "rules/core.json")
    review.add_argument("--settings", type=Path)
    review.add_argument("--tolerance", default="0")
    review.add_argument("--reviewer", default="")
    review.add_argument("--reason", default="")
    review.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve as run_server
            run_server(port=args.port, audit_dir=args.audit_dir)
            return 0
        settings = (load_json(args.settings) if args.settings else {
            "tolerance": args.tolerance, "overrides": {},
            "reviewer": args.reviewer, "reason": args.reason,
            "conditions": {}, "not_applicable": {},
        })
        result = run_review(load_json(args.input), load_json(args.rules), settings)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # A previous run is never silently overwritten.
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print(json.dumps({"run_id": result["run_id"], "output": str(args.output),
                          "summary": result["summary"]}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError) as error:
        print("Review could not run: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
