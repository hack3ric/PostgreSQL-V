#!/usr/bin/env python3
"""Compute primary-node operation throughput for every mixed-mode group.

Experiment B's primary driver (replica_throughput_eval --role primary) writes
step_boundaries.csv: one row per mixed group, `step,timestamp_ms`, where the
timestamp is the primary-DB-clock instant at which the group *started*. A group
therefore spans [t[i], t[i+1]); its member steps are every runbook step whose
number falls in [lead_i, lead_{i+1}). This auto-derives the group size (mixed_size)
from the boundaries themselves, so no --mixed-size flag is needed.

The boundary CSV does not record how many operations each step performed, so the
op counts come from the runbook YAML:
  - search          -> --num-queries (the query-set size; not stored anywhere else)
  - insert / delete -> end - start

Throughput for a group = (total operations in the group) / (group duration in
seconds), where all op types (insert + delete + search) are counted together
since they run concurrently in shuffled mixed mode.

The final group has no closing boundary (the driver only logs completion to
stderr), so its duration and throughput are left blank while its op count is
still reported.

Output columns:
  group_lead_step, steps, start_ms, end_ms, duration_s, total_ops, total_ops_s
"""
import argparse
import csv
import math
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def load_boundaries(path):
    """Return (group_rows, end_ms).

    group_rows is [(step, timestamp_ms), ...] sorted by step for real group
    starts. end_ms is the workload-completion timestamp emitted by the driver as
    a sentinel row with step == -1 (closes the final group), or None for older
    CSVs that predate that marker.
    """
    rows, end_ms = [], None
    with open(path) as f:
        for r in csv.DictReader(f):
            step, ts = int(r["step"]), int(r["timestamp_ms"])
            if step < 0:                 # end-of-workload marker
                end_ms = ts if end_ms is None else max(end_ms, ts)
            else:
                rows.append((step, ts))
    rows.sort(key=lambda x: x[0])
    return rows, end_ms


def load_runbook(path, dataset_name):
    with open(path) as f:
        doc = yaml.safe_load(f)
    if dataset_name not in doc:
        raise SystemExit(
            f"dataset '{dataset_name}' not in runbook; keys: {list(doc)}"
        )
    steps = {}
    for k, v in doc[dataset_name].items():
        # The dataset block carries metadata keys (e.g. 'max_pts') alongside the
        # numeric step entries; keep only the steps.
        if isinstance(k, int) or (isinstance(k, str) and k.isdigit()):
            steps[int(k)] = v
    return steps


def op_count(step, num_queries):
    op = step.get("operation")
    if op == "search":
        return op, num_queries
    return op, int(step["end"]) - int(step["start"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boundaries", default=os.path.join(HERE, "results", "step_boundaries.csv"))
    ap.add_argument("--runbook", default=os.path.join(HERE, "..", "msturing-10M_slidingwindow_runbook.yaml"))
    ap.add_argument("--dataset-name", default="msturing-10M")
    ap.add_argument("--num-queries", type=int, default=10000,
                    help="search op count per search step = size of the query set (default 10000)")
    ap.add_argument("--out", default=os.path.join(HERE, "results", "primary_throughput_per_group.csv"))
    args = ap.parse_args()

    bounds, end_ms = load_boundaries(args.boundaries)
    runbook = load_runbook(args.runbook, args.dataset_name)
    if not bounds:
        raise SystemExit("no boundary rows found")
    max_step = max(runbook)

    out_rows = []
    for i, (lead, t_start) in enumerate(bounds):
        # Steps belonging to this group: [lead, next_lead) (last group: through max_step).
        next_lead = bounds[i + 1][0] if i + 1 < len(bounds) else max_step + 1
        member_steps = [s for s in range(lead, next_lead) if s in runbook]

        total = 0
        for s in member_steps:
            _op, n = op_count(runbook[s], args.num_queries)
            total += n

        # Closing edge: next group's start, or the driver's end-of-workload
        # marker for the final group (None on older CSVs -> blank).
        if i + 1 < len(bounds):
            t_end = bounds[i + 1][1]
        elif end_ms is not None:
            t_end = end_ms
        else:
            t_end = math.nan
        dur_s = (t_end - t_start) / 1000.0 if not math.isnan(t_end) else math.nan

        ops_s = total / dur_s if not math.isnan(dur_s) and dur_s > 0 else math.nan

        out_rows.append({
            "group_lead_step": lead,
            "steps": "|".join(str(s) for s in member_steps),
            "start_ms": t_start,
            "end_ms": "" if isinstance(t_end, float) and math.isnan(t_end) else t_end,
            "duration_s": "" if math.isnan(dur_s) else round(dur_s, 3),
            "total_ops": total,
            "total_ops_s": "" if math.isnan(ops_s) else round(ops_s, 1),
        })

    fields = ["group_lead_step", "steps", "start_ms", "end_ms", "duration_s",
              "total_ops", "total_ops_s"]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    measured = [r for r in out_rows if r["total_ops_s"] != ""]
    if measured:
        avg = sum(float(r["total_ops_s"]) for r in measured) / len(measured)
        print(f"{len(out_rows)} groups ({len(measured)} with a closing boundary), "
              f"mean total throughput {avg:,.0f} ops/s -> {args.out}")
    else:
        print(f"{len(out_rows)} groups -> {args.out}")
    if isinstance(out_rows[-1]["duration_s"], str) and out_rows[-1]["duration_s"] == "":
        print(f"note: final group (step {out_rows[-1]['group_lead_step']}) has no closing "
              "boundary; duration/throughput left blank.")


if __name__ == "__main__":
    main()
