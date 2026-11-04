#!/usr/bin/env python3
"""Summarise a record.sh recording: when input was slow, and what was running then.

Usage: report.py <recording dir> [--threshold-ms N]    (default 50)

A second counts as laggy when mouse or key events took longer than the threshold to
arrive (95th percentile), any single event took over 3x the threshold, or the
once-a-second high-priority timer fired more than the threshold late.
Works on a recording that is still running.
"""
import collections
import os
import sys
import time


def rows(path):
    try:
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[0].isdigit():
                    yield int(parts[0]), parts[1], parts[2:]
    except FileNotFoundError:
        return


def etime_seconds(s):
    days, _, rest = s.rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return (int(days) if days else 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def nearest(series, start, end, slack=10):
    """Key of `series` closest to the window [start, end], within `slack` seconds."""
    best = None
    for t in series:
        gap = 0 if start <= t <= end else min(abs(t - start), abs(t - end))
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, t)
    return best[1] if best else None


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__)
        sys.exit(2)
    folder = args[0]
    threshold = float(args[args.index("--threshold-ms") + 1]) if "--threshold-ms" in args else 50.0

    ticks, inputs = {}, collections.defaultdict(dict)
    for t, kind, rest in rows(os.path.join(folder, "latency.tsv")):
        if kind == "tick":
            ticks[t] = float(rest[0])
        elif len(rest) >= 4:
            inputs[t][kind] = (int(rest[0]), float(rest[1]), float(rest[2]), float(rest[3]))

    idle, procs = {}, collections.defaultdict(dict)
    for t, kind, rest in rows(os.path.join(folder, "top.tsv")):
        if kind == "idle":
            idle[t] = float(rest[0])
        elif kind == "proc" and len(rest) >= 3 and float(rest[1]) < 5000:  # top sometimes prints garbage like 7532104
            procs[t][rest[2]] = procs[t].get(rest[2], 0.0) + float(rest[1])

    load, ps, taps = {}, collections.defaultdict(dict), collections.defaultdict(list)
    for t, kind, rest in rows(os.path.join(folder, "system.tsv")):
        if kind == "load":
            load[t] = float(rest[0])
        elif kind == "ps":
            f = rest[0].split(None, 4)
            if len(f) >= 4:
                ps[t][f[0]] = {"ppid": f[1], "cpu": float(f[2]), "age": etime_seconds(f[3]),
                               "args": f[4] if len(f) > 4 else ""}
        elif kind == "tap" and len(rest) >= 6:
            taps[t].append(rest)  # process, pid, taps, disabled, blocking, watches

    err = os.path.join(folder, "latency.err")
    if os.path.exists(err) and os.path.getsize(err):
        print(open(err).read().strip(), "\n")

    if not ticks:
        print("No data yet. Is record.sh running, and did it print errors?")
        return

    # Which seconds were laggy, and how badly.
    lag = {}
    for t in sorted(ticks):
        worst = {}
        for kind, (count, p50, p95, mx) in inputs.get(t, {}).items():
            if p95 > threshold or mx > 3 * threshold:
                worst[kind] = (p95, mx)
        if ticks[t] > threshold:
            worst["timer"] = (ticks[t], ticks[t])
        if worst:
            lag[t] = worst

    span = max(ticks) - min(ticks) + 1
    mouse = [v["mouse"] for v in inputs.values() if "mouse" in v]
    keys = [v["keys"] for v in inputs.values() if "keys" in v]
    print(f"Recording {clock(min(ticks))}-{clock(max(ticks))} ({span // 60} min {span % 60} s), "
          f"threshold {threshold:.0f} ms")
    for name, data in (("mouse", mouse), ("keys", keys)):
        if data:
            typical = sorted(d[1] for d in data)[len(data) // 2]
            print(f"  {name}: {sum(d[0] for d in data)} events over {len(data)} s, typical delay {typical:.1f} ms")
        elif name == "mouse":
            print("  mouse: no events seen. Was anyone using the mouse? Timer stalls are still measured.")
    print(f"  timer: typically {sorted(ticks.values())[len(ticks) // 2]:.1f} ms late, worst {max(ticks.values()):.0f} ms")
    print(f"  laggy seconds: {len(lag)} of {span}")

    # Taps that macOS disabled during the recording: the app timed out on input.
    key_holders = []
    if taps:
        first, last = taps[min(taps)], taps[max(taps)]
        before = {(r[0], r[1]): int(r[3]) for r in first}
        for row in last:
            name, pid, _, disabled, blocking, holds = row[:6]
            if int(disabled) > before.get((name, pid), int(disabled)):
                print(f"  {name} ({pid}): disabled taps went from {before[(name, pid)]} to {disabled}, so it timed out on input while recording")
            if int(blocking) > 0 and "keys" in holds:
                key_holders.append(name)

    if not lag:
        print("\nNo lag recorded. If the user felt lag, it was inside an app (slow drawing), not in input delivery.")
        return

    # Group laggy seconds into episodes, allowing short gaps.
    episodes = []
    for t in sorted(lag):
        if episodes and t - episodes[-1][-1] <= 5:
            episodes[-1].append(t)
        else:
            episodes.append([t])

    # Processes that run hotter while input is laggy. A top sample covers the 3 s before it.
    def in_lag(t):
        return any(s in lag for s in range(t - 3, t + 1))

    with_lag, without = collections.defaultdict(list), collections.defaultdict(list)
    lag_samples = [t for t in procs if in_lag(t)]
    calm_samples = [t for t in procs if not in_lag(t)]
    names = {name for sample in procs.values() for name in sample}
    for group, samples in ((with_lag, lag_samples), (without, calm_samples)):
        for t in samples:
            for name in names:
                group[name].append(procs[t].get(name, 0.0))
    if lag_samples and calm_samples:
        mean = lambda xs: sum(xs) / len(xs)
        diffs = sorted(((mean(with_lag[n]) - mean(without[n]), n) for n in with_lag), reverse=True)
        hot = [(d, n) for d, n in diffs if d >= 5][:6]
        idle_lag = mean([idle[t] for t in lag_samples if t in idle] or [0])
        idle_calm = mean([idle[t] for t in calm_samples if t in idle] or [0])
        print(f"\nIdle CPU: {idle_lag:.0f}% during lag vs {idle_calm:.0f}% otherwise")
        if hot:
            print("Running hotter during lag than otherwise (avg CPU %):")
            for d, n in hot:
                print(f"  {n:<20} {mean(with_lag[n]):6.1f} vs {mean(without[n]):6.1f}")

    # The worst episodes, in time order.
    severity = lambda ep: max(max(v[1] for v in lag[t].values()) for t in ep)
    worst = sorted(sorted(episodes, key=severity, reverse=True)[:8])
    print(f"\n{len(episodes)} lag episode(s); the {len(worst)} worst:")
    for ep in worst:
        start, end = ep[0], ep[-1]
        kinds = collections.defaultdict(lambda: (0.0, 0.0))
        for t in ep:
            for kind, (p95, mx) in lag[t].items():
                kinds[kind] = (max(kinds[kind][0], p95), max(kinds[kind][1], mx))
        what = ", ".join(f"{k} p95 {p:.0f} ms / max {m:.0f} ms" if k != "timer" else f"timer {m:.0f} ms late"
                         for k, (p, m) in kinds.items())
        print(f"\n{clock(start)}-{clock(end)} ({end - start + 1} s): {what}")
        if "keys" in kinds and "mouse" not in kinds and any("mouse" in inputs.get(t, {}) for t in ep):
            holders = ", ".join(key_holders) or "none recorded"
            print(f"  keys slow while the mouse was fine: points at an app holding keystrokes ({holders})")

        sample = nearest([t for t in procs if start <= t <= end + 3] or procs, start, end + 3)
        if sample:
            top5 = sorted(procs[sample].items(), key=lambda kv: -kv[1])[:5]
            extra = f", load {load[l]:.0f}" if (l := nearest(load, start, end)) else ""
            print(f"  idle CPU {idle.get(sample, float('nan')):.0f}%{extra}; busiest: "
                  + ", ".join(f"{n} {c:.0f}%" for n, c in top5))

        snap = nearest(ps, start, end)
        if snap:
            fresh = [p for p in ps[snap].values()
                     if start - 120 <= snap - p["age"] <= end and p["cpu"] >= 1]
            for p in sorted(fresh, key=lambda p: -p["cpu"])[:6]:
                parent = ps[snap].get(p["ppid"], {}).get("args", f"pid {p['ppid']}")
                print(f"  started {clock(snap - p['age'])}: {p['args'][:90]} ({p['cpu']:.0f}%)"
                      f"\n      parent: {parent[:90]}")


if __name__ == "__main__":
    main()
