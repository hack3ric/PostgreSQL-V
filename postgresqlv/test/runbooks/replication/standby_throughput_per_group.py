#!/usr/bin/env python3
"""Aggregate standby search throughput into per-group windows.

Experiment B's standby driver (replica_throughput_eval --role standby) writes
standby_throughput.csv: per-second QPS on the *primary* DB clock (one row per
second with >= 1 completed query, `timestamp_ms,qps`). The primary driver writes
step_boundaries.csv on the same timeline. Because both share one clock, we can
slice the continuous standby query stream by the primary's group boundaries.

For each mixed group i, the window is [t[i], t[i+1]) (the final group is closed
at the last standby second + 1s, since the standby keeps querying past the
primary's last boundary until SIGINT). Each per-second standby bucket is assigned
to the window containing its timestamp_ms.

Since each standby row is *already* a per-second QPS reading, the group's
throughput is the mean of the per-second buckets that fall in its window
(avg_qps = total_queries / n_seconds), which stays bounded by min/max. Dividing
total_queries by the exact window duration would inflate the rate whenever the
boundaries don't align to whole-second marks (a 3.8s window can hold 4 one-second
buckets), so we deliberately do not do that.

Standby buckets that land before the first boundary (warmup, before the mixed
workload starts) are summarized separately and not attributed to any group.

Output columns:
  group_lead_step, start_ms, end_ms, duration_s,
  n_seconds, total_queries, avg_qps, min_qps, max_qps
"""
import argparse
import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_boundaries(path):
    """Return (group_rows, end_ms).

    group_rows is [(step, timestamp_ms), ...] sorted by time for real group
    starts. end_ms is the driver's workload-completion timestamp (sentinel row
    with step == -1, which closes the final group's window), or None on older
    CSVs that predate the marker.
    """
    rows, end_ms = [], None
    with open(path) as f:
        for r in csv.DictReader(f):
            step, ts = int(r["step"]), int(r["timestamp_ms"])
            if step < 0:                 # end-of-workload marker
                end_ms = ts if end_ms is None else max(end_ms, ts)
            else:
                rows.append((step, ts))
    rows.sort(key=lambda x: x[1])  # sort by time
    return rows, end_ms


def load_standby(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append((int(r["timestamp_ms"]), int(r["qps"])))
    rows.sort(key=lambda x: x[0])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boundaries", default=os.path.join(HERE, "results", "step_boundaries.csv"))
    ap.add_argument("--standby", default=os.path.join(HERE, "results", "standby_throughput.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "results", "standby_throughput_per_group.csv"))
    args = ap.parse_args()

    bounds, end_ms = load_boundaries(args.boundaries)
    standby = load_standby(args.standby)
    if not bounds:
        raise SystemExit("no boundary rows found")
    if not standby:
        raise SystemExit("no standby rows found")

    last_ts = standby[-1][0]
    # Final group's closing edge: the driver's end-of-workload marker if present
    # (so the window stops when the primary finished, excluding the standby's
    # post-workload tail); otherwise fall back to the last standby bucket + 1s.
    final_end = end_ms if end_ms is not None else last_ts + 1000
    # Window edges: each group's [start, next_start); final group uses final_end.
    edges = []
    for i, (lead, t_start) in enumerate(bounds):
        t_end = bounds[i + 1][1] if i + 1 < len(bounds) else final_end
        edges.append((lead, t_start, t_end))

    first_start = edges[0][1]
    warmup_q = sum(q for ts, q in standby if ts < first_start)
    warmup_sec = sum(1 for ts, q in standby if ts < first_start)
    tail_q = sum(q for ts, q in standby if ts >= edges[-1][2])

    out_rows = []
    for lead, t_start, t_end in edges:
        in_win = [q for ts, q in standby if t_start <= ts < t_end]
        dur_s = (t_end - t_start) / 1000.0
        total_q = sum(in_win)
        out_rows.append({
            "group_lead_step": lead,
            "start_ms": t_start,
            "end_ms": t_end,
            "duration_s": round(dur_s, 3),
            "n_seconds": len(in_win),
            "total_queries": total_q,
            "avg_qps": round(total_q / len(in_win), 1) if in_win else "",
            "min_qps": min(in_win) if in_win else 0,
            "max_qps": max(in_win) if in_win else 0,
        })

    fields = ["group_lead_step", "start_ms", "end_ms", "duration_s",
              "n_seconds", "total_queries", "avg_qps", "min_qps", "max_qps"]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    measured = [r for r in out_rows if r["avg_qps"] != ""]
    avg = sum(float(r["avg_qps"]) for r in measured) / len(measured) if measured else 0.0
    print(f"{len(out_rows)} group windows, mean standby throughput {avg:,.0f} q/s -> {args.out}")
    if warmup_sec:
        print(f"note: {warmup_q:,} queries over {warmup_sec}s before the first boundary "
              "(warmup) were not attributed to any group.")
    if tail_q:
        where = ("after the workload-completion marker" if end_ms is not None
                 else "after the last boundary")
        included = "excluded from" if end_ms is not None else "inside"
        print(f"note: {tail_q:,} queries {where} were {included} the final group's "
              "window (standby kept running until SIGINT).")


if __name__ == "__main__":
    main()
