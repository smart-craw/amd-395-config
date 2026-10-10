#!/usr/bin/env python3
"""Render the committed alert rules from their template + threshold values.

    python3 monitoring/render_rules.py            # write rules.yml
    python3 monitoring/render_rules.py --check    # CI: fail on drift

Why a generator: the thresholds have to be tunable per box (the ticket's
"values file, not inline"), and Prometheus does **not** expand environment
variables in rule files — it only expands them in ``global.external_labels``
(see ``config/config.go`` in the Prometheus source, and the same note in
monitoring/README.md). So the substitution has to happen before the file is read
by Prometheus, and the only way to keep ``docker compose up`` a single command is
to commit the rendered file and have CI verify it still matches the values. That
is the same trade-off as a lockfile: generated, committed, and checked for drift.

Standard library only, like the rest of this repo.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = HERE / "prometheus" / "rules.yml.tmpl"
DEFAULT_VALUES = HERE / "prometheus" / "alert_thresholds.env"
DEFAULT_OUT = HERE / "prometheus" / "rules.yml"
#: monitoring/.env, if present, overrides the committed values file.
DOT_ENV = HERE / ".env"

LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def read_values(path: Path) -> dict[str, str]:
    """Parse a shell-ish KEY=VALUE file (comments, blank lines, quotes)."""
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(raw)
        if not match:
            raise SystemExit(f"{path}:{number}: not a KEY=VALUE line: {raw!r}")
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def render(template: str, values: dict[str, str], *, source: str) -> str:
    unknown: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            unknown.append(key)
            return match.group(0)
        return values[key]

    out = PLACEHOLDER.sub(replace, template)
    if unknown:
        missing = ", ".join(sorted(set(unknown)))
        raise SystemExit(
            f"error: {source}: placeholders with no value in the values file: {missing}"
        )
    leftover = PLACEHOLDER.findall(out)
    if leftover:
        raise SystemExit(f"error: {source}: unsubstituted placeholders: {leftover}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--values",
        type=Path,
        action="append",
        default=None,
        help="KEY=VALUE file(s); later files win (default: alert_thresholds.env, .env)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if --out differs from the render",
    )
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args(argv)

    value_files = args.values or [p for p in (DEFAULT_VALUES, DOT_ENV) if p.is_file()]
    values: dict[str, str] = {}
    for path in value_files:
        values.update(read_values(path))

    template_text = args.template.read_text(encoding="utf-8")
    rendered = render(template_text, values, source=str(args.template))

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else None
        if current != rendered:
            print(
                f"error: {args.out} is out of date with "
                f"{args.template} + {', '.join(str(p) for p in value_files)}.\n"
                "       run: python3 monitoring/render_rules.py && git add -A",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out} matches its template and values file")
        return 0

    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} ({len(values)} values from {len(value_files)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
