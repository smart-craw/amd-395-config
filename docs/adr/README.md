# Architecture decision records

Short records of decisions that are expensive to reverse, written down so the
reasoning outlives the pull request. The reasoning is the point: a decision with no
alternatives listed has not been decided, only noticed.

## Index

| # | title | status | beads |
| - | ----- | ------ | ----- |
| [0001](./0001-prometheus-grafana-owns-metrics.md) | Prometheus + Grafana own `/metrics`; `monitor/serve.py` becomes a scrape passthrough + live-status page | Accepted | `workspace-szw.1` |

## How decisions get recorded here

* One file per decision: `NNNN-short-slug.md`, numbered in the order accepted.
* Status is one of `Proposed`, `Accepted`, `Superseded by NNNN`, `Deprecated`,
  `Amended by NNNN` — and it is the first line of the table at the top of the file.
* Accepted decisions are not edited after the fact. New information goes into a new
  ADR that supersedes or amends the old one, and the old file's Status line is
  updated to point at it. Contracts (interfaces, names, budgets) are the exception:
  they are living tables, and the ADR is their canonical home, so a contract row may
  be corrected in place with a note in the commit message.
* Every ADR links the beads issue that carried it, so `bd show <id>` and the file
  agree.
* Write the "Alternatives considered" section even when the answer is obvious, and
  write "Revisit when" — it is what stops a decision from becoming a load-bearing
  wall nobody remembers building.

## Relationship to the READMEs

The READMEs describe how the system works *today*. An ADR explains why it works that
way and what was rejected. When they disagree, the code and the READMEs are the
present tense and the ADR is the record of intent; either the code is wrong or the
ADR needs amending — never "both are fine".
