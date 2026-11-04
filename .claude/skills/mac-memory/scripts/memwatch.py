#!/usr/bin/env python3
"""Record where a Mac's memory goes over time, and find what caused the spikes.

Usage:
  memwatch.py now                                    memory by app right now
  memwatch.py start [--hours H] [--interval S]       record in the background (default 2 h, every 15 s)
  memwatch.py record [--hours H] [--interval S]      same, in the foreground
  memwatch.py stop [DIR]                             stop a background recording
  memwatch.py report [DIR] [--threshold-gb G] [--no-open]
                                                     text summary + DIR/report.html; works while recording

Recordings go to ~/Library/Logs/mac-memory/. DIR defaults to the newest one.

Memory per process is its footprint, the "Memory" column in Activity Monitor. Processes owned by
other users (system daemons) can't be read that way, so they fall back to resident size, which
undercounts. Each process is credited to the app or agent that launched it: Chrome's helpers to
Chrome, an MCP server to the Claude Code or Codex session that started it, a dev server to the
terminal it runs in.
"""
import ctypes
import datetime
import html
import json
import os
import re
import signal
import subprocess
import sys
import time

ROOT = os.path.expanduser("~/Library/Logs/mac-memory")
MB = 1024 * 1024
KEEP_MB = 25  # processes smaller than this only count towards their app's total

AGENTS = {"claude": "Claude Code", "codex": "Codex"}
TERMINALS = {"Terminal", "iTerm", "iTerm2", "Ghostty", "Warp", "WezTerm", "Alacritty", "kitty",
             "Hyper", "Tabby", "Rex", "Rex Beta", "Cursor", "Visual Studio Code", "Zed"}
INTERPRETERS = {"node", "bun", "deno", "python", "python3", "Python", "ruby", "java", "uv", "uvx",
                "npx", "npm", "pnpm", "tsx", "perl", "electron", "Electron"}
GENERIC_FILES = {"index.js", "index.mjs", "index.cjs", "cli.js", "cli.mjs", "main.js", "main.mjs",
                 "server.js", "server.mjs", "run.js", "start.js", "app.js", "__main__.py", "main.py",
                 "server.py", "app.py", "cli.py"}
GENERIC_DIRS = {"dist", "build", "bin", "lib", "src", "out", "cli", ".bin", "esm", "cjs", "server"}
SYSTEM_PREFIXES = ("/System/", "/usr/", "/sbin/", "/bin/", "/Library/Apple/", "/private/var/db/")

_proc = ctypes.CDLL("/usr/lib/libproc.dylib")
_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libc.mach_absolute_time.restype = ctypes.c_uint64


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_tb = _Timebase()
_libc.mach_timebase_info(ctypes.byref(_tb))
NS_PER_TICK = _tb.numer / _tb.denom


# ---------------------------------------------------------------- sampling

def _pids():
    n = _proc.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 512))()
    n = _proc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:n] if p > 0]


def _rusage(pid, buf):
    """(footprint bytes, start time in mach ticks), or None if not readable."""
    if _proc.proc_pid_rusage(pid, 0, buf) != 0:
        return None
    raw = buf.raw
    return int.from_bytes(raw[72:80], "little"), int.from_bytes(raw[80:88], "little")


def _pidpath(pid, buf):
    n = _proc.proc_pidpath(pid, buf, 4096)
    return buf.value.decode("utf-8", "replace") if n > 0 else ""


def app_of(path):
    """Outermost .app bundle in a path: Chrome's helpers belong to Google Chrome."""
    i = path.find(".app/")
    return path[:i].rsplit("/", 1)[-1] if i >= 0 else None


def _script_name(tok):
    if "node_modules/" in tok:
        parts = tok.rsplit("node_modules/", 1)[1].split("/")
        if parts[0] == ".bin" and len(parts) > 1:
            return parts[1]
        if parts[0].startswith("@") and len(parts) > 1:
            return parts[0] + "/" + parts[1]
        return parts[0]
    parts = [p for p in tok.split("/") if p]
    if not parts:
        return tok
    if parts[-1] in GENERIC_FILES:
        for p in reversed(parts[:-1]):
            if p not in GENERIC_DIRS:
                return p
    return parts[-1]


def label_of(exe, args):
    """A readable name: the script an interpreter runs, else the executable's name."""
    base = exe.rsplit("/", 1)[-1] or (args.split()[0].rsplit("/", 1)[-1] if args else "?")
    interpreter = lambda name: name in INTERPRETERS or re.match(r"[Pp]ython3?(\.\d+)?$", name)
    if interpreter(base):
        first = args.split()[0].rsplit("/", 1)[-1] if args else base
        if not interpreter(first):  # the process renamed itself, e.g. "next-server (v16.4.1)"
            return first
        toks = args.split()[1:]
        for k, tok in enumerate(toks):
            if tok == "-m" and k + 1 < len(toks):
                return toks[k + 1]
        for tok in toks:
            if tok.startswith("-") or tok in ("run", "exec", "x", "tool", "dlx"):
                continue
            return _script_name(tok)
    return base


def sample():
    now_mach = _libc.mach_absolute_time()
    t = time.time()
    ps = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss=,args="], capture_output=True, text=True).stdout
    info = {}
    for line in ps.splitlines():
        f = line.split(None, 3)
        if len(f) >= 3 and f[0].isdigit():
            info[int(f[0])] = (int(f[1]), int(f[2]) * 1024, f[3] if len(f) > 3 else "")

    rbuf, pbuf = ctypes.create_string_buffer(512), ctypes.create_string_buffer(4096)
    procs = {}
    for pid in set(_pids()) | set(info):
        ppid, rss, args = info.get(pid, (0, 0, ""))
        ru = _rusage(pid, rbuf)
        exe = _pidpath(pid, pbuf) or (args.split()[0] if args else "")
        procs[pid] = {
            "ppid": ppid, "exe": exe, "args": args,
            "mem": ru[0] if ru else rss, "approx": ru is None, "start": ru[1] if ru else 0,
            "base": exe.rsplit("/", 1)[-1], "app": app_of(exe),
        }

    owners = {}

    def owner(pid):
        if pid in owners:
            return owners[pid]
        chain, p = [], pid
        while p in procs and p > 1 and p not in chain:
            chain.append(p)
            p = procs[p]["ppid"]
        result = None
        for q in chain:  # an agent session owns everything it launched, browsers included
            if procs[q]["base"] in AGENTS:
                result = AGENTS[procs[q]["base"]]
                break
        if result is None:
            for q in chain:
                app = procs[q]["app"]
                if app:
                    result = "Terminal commands (%s)" % app if (app in TERMINALS and q != pid) else app
                    break
        if result is None:
            me = procs[pid]
            if pid == 0 or me["exe"].startswith(SYSTEM_PREFIXES):
                result = "macOS"
            else:
                result = label_of(me["exe"], me["args"])
        owners[pid] = result
        return result

    groups, counts, rows = {}, {}, []
    for pid, p in procs.items():
        g = owner(pid)
        mb = p["mem"] / MB
        groups[g] = groups.get(g, 0.0) + mb
        counts[g] = counts.get(g, 0) + 1
        if mb >= KEEP_MB:
            label = label_of(p["exe"], p["args"])
            flags = ("a" if p["approx"] else "") + ("m" if "mcp" in (label + " " + " ".join(p["args"].split()[:3])).lower() else "")
            rows.append([pid, p["start"], round(mb, 1), g, label, flags, p["ppid"]])

    return {"t": round(t, 1), "mach": now_mach, "sys": system_memory(),
            "g": {k: round(v, 1) for k, v in groups.items()}, "n": counts, "p": rows}


def system_memory():
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    v = {}
    for line in out.splitlines()[1:]:
        k, _, val = line.partition(":")
        val = val.strip().rstrip(".")
        if val.isdigit():
            v[k.strip().strip('"')] = int(val)
    mb = lambda pages: round(pages * page / MB, 1)
    swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    used = re.search(r"used = ([\d.]+)M", swap)
    level = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True).stdout.strip()
    return {
        "app": mb(v.get("Anonymous pages", 0) - v.get("Pages purgeable", 0)),
        "wired": mb(v.get("Pages wired down", 0)),
        "compressed": mb(v.get("Pages occupied by compressor", 0)),
        "stored": mb(v.get("Pages stored in compressor", 0)),
        "cached": mb(v.get("File-backed pages", 0) + v.get("Pages purgeable", 0)),
        "free": mb(v.get("Pages free", 0) + v.get("Pages speculative", 0)),
        "swapins": v.get("Swapins", 0), "swapouts": v.get("Swapouts", 0), "page": page,
        "swap_used": float(used.group(1)) if used else 0.0,
        "pressure": int(level) if level.isdigit() else 0,
    }


def ram_mb():
    return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / MB


# ---------------------------------------------------------------- recording

def record(folder, seconds, interval):
    os.makedirs(folder, exist_ok=True)
    meta = {"started": time.time(), "planned_seconds": seconds, "interval": interval,
            "ram_mb": ram_mb(), "ns_per_tick": NS_PER_TICK, "pid": os.getpid()}
    with open(os.path.join(folder, "meta.json"), "w") as f:
        json.dump(meta, f)
    with open(os.path.join(folder, "pid"), "w") as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    end = time.time() + seconds
    with open(os.path.join(folder, "samples.jsonl"), "a") as out:
        while time.time() < end:
            began = time.time()
            try:
                out.write(json.dumps(sample(), separators=(",", ":")) + "\n")
                out.flush()
            except Exception as e:  # one bad sample shouldn't end a two-hour recording
                print("sample failed:", repr(e), file=sys.stderr, flush=True)
            time.sleep(max(1.0, interval - (time.time() - began)))
    print("done", flush=True)


def new_folder():
    return os.path.join(ROOT, "rec-" + time.strftime("%Y%m%d-%H%M%S"))


def latest_folder():
    try:
        recs = sorted(d for d in os.listdir(ROOT) if d.startswith("rec-"))
    except FileNotFoundError:
        recs = []
    if not recs:
        sys.exit("No recordings in %s yet. Start one with: memwatch.py start" % ROOT)
    return os.path.join(ROOT, recs[-1])


def running(folder):
    try:
        pid = int(open(os.path.join(folder, "pid")).read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- analysis

def load(folder):
    samples = []
    with open(os.path.join(folder, "samples.jsonl")) as f:
        for line in f:
            try:
                samples.append(json.loads(line))
            except ValueError:
                pass  # a line cut short by a stop
    meta = json.load(open(os.path.join(folder, "meta.json")))
    return meta, samples


def clock(t):
    return time.strftime("%H:%M", time.localtime(t))


def gb(mb):
    return "%.1f GB" % (mb / 1024) if abs(mb) >= 1024 else "%d MB" % round(mb)


def analyse(meta, samples, threshold_gb):
    ns = meta.get("ns_per_tick", NS_PER_TICK)
    total = [sum(s["g"].values()) for s in samples]
    times = [s["t"] for s in samples]

    def started(s, start_ticks):
        return s["t"] - (s["mach"] - start_ticks) * ns / 1e9 if start_ticks else None

    def procs_of(s):
        return {(r[0], r[1]): r for r in s["p"]}

    # Spikes: total app memory rises by >= threshold within 10 minutes.
    thr = threshold_gb * 1024
    window = 600
    hits = []
    for i in range(len(samples)):
        j = min((k for k in range(i + 1) if times[i] - times[k] <= window), key=lambda k: total[k])
        if total[i] - total[j] >= thr:
            hits.append((i, j))
    episodes = []
    for i, j in hits:
        if episodes and i - episodes[-1]["last"] <= 2:
            ep = episodes[-1]
            ep["last"] = i
            if total[i] > total[ep["peak"]]:
                ep["peak"] = i
        else:
            episodes.append({"base": j, "peak": i, "last": i, "first": i})
    for ep in episodes:
        # Tighten to the climb itself: from the last sample still near the low point
        # to the first sample within 10% of the top.
        low, first_hit = total[ep["base"]], ep["first"]
        top = max(total[k] for k in range(ep["base"], ep["last"] + 1))
        start = ep["base"]
        for k in range(ep["base"], first_hit + 1):
            if total[k] - low < 0.25 * thr:
                start = k
        ep["base"] = start
        ep["peak"] = next(k for k in range(start, ep["last"] + 1) if total[k] >= low + 0.9 * (top - low))
        a, b = samples[ep["base"]], samples[ep["peak"]]
        ep["rise"] = total[ep["peak"]] - total[ep["base"]]
        # It lasts until memory falls back below halfway between the low point and the peak.
        half = low + 0.5 * (total[ep["peak"]] - low)
        ep["end"] = next((k for k in range(ep["peak"], len(total)) if total[k] < half), len(total) - 1)
        ep["lasted"] = times[ep["end"]] - times[ep["base"]]
        ep["when"] = (("at %s" % clock(times[ep["peak"]])) if clock(times[ep["base"]]) == clock(times[ep["peak"]])
                      else "from %s to %s" % (clock(times[ep["base"]]), clock(times[ep["peak"]])))
        ep["still"] = ep["end"] == len(total) - 1 and total[-1] >= half
        ep["groups"] = sorted(((b["g"].get(g, 0) - a["g"].get(g, 0), g) for g in set(a["g"]) | set(b["g"])), reverse=True)[:5]
        before, after = procs_of(a), procs_of(b)
        grew = []
        for key, r in after.items():
            old = before.get(key)
            d = r[2] - (old[2] if old else 0)
            if d >= 50:
                grew.append({"label": r[4], "owner": r[3], "delta": d, "now": r[2], "new": old is None,
                             "started": started(b, r[1]), "pid": r[0]})
        ep["procs"] = sorted(grew, key=lambda x: -x["delta"])[:8]
        ep["after"] = sum(1 for k in after if k not in before)

    # Processes that kept growing (leak suspects): present for >= 30 min, grew >= 300 MB.
    seen = {}
    for s in samples:
        for key, r in procs_of(s).items():
            first = seen.setdefault(key, {"t0": s["t"], "m0": r[2], "label": r[4], "owner": r[3], "pid": r[0], "max": r[2]})
            first["t1"], first["m1"], first["max"] = s["t"], r[2], max(first["max"], r[2])
    growers = [v for v in seen.values() if v.get("t1", v["t0"]) - v["t0"] >= 1800 and v["m1"] - v["m0"] >= 300]
    growers.sort(key=lambda v: -(v["m1"] - v["m0"]))

    # Apps ranked by peak; the chart shows the top 8 and folds the rest into Other.
    names = set()
    for s in samples:
        names.update(s["g"])
    stats = {}
    for g in names:
        vals = [s["g"].get(g, 0.0) for s in samples]
        stats[g] = {"avg": sum(vals) / len(vals), "peak": max(vals), "end": vals[-1], "start": vals[0],
                    "count": samples[-1]["n"].get(g, 0)}
    ranked = sorted(names, key=lambda g: -stats[g]["peak"])

    mcp = []
    for s in samples:
        mcp.append(sum(r[2] for r in s["p"] if "m" in r[5]))

    peak_i = max(range(len(samples)), key=lambda i: total[i])
    peak_procs = sorted(samples[peak_i]["p"], key=lambda r: -r[2])[:12]

    swap_written = []
    for i, s in enumerate(samples):
        d = s["sys"]["swapouts"] - samples[i - 1]["sys"]["swapouts"] if i else 0
        swap_written.append(max(0, d) * s["sys"]["page"] / MB)

    return {"total": total, "times": times, "episodes": episodes, "growers": growers[:10],
            "stats": stats, "ranked": ranked, "mcp": mcp, "peak_i": peak_i, "peak_procs": peak_procs,
            "swap_written": swap_written}


def text_report(meta, samples, a, threshold_gb):
    sysv = [s["sys"] for s in samples]
    span = samples[-1]["t"] - samples[0]["t"]
    warn = sum(1 for v in sysv if v["pressure"] >= 2) * meta["interval"]
    lines = [
        "Recording %s-%s (%d min, %d samples), %s RAM" % (clock(samples[0]["t"]), clock(samples[-1]["t"]),
                                                          span // 60, len(samples), gb(meta["ram_mb"])),
        "  apps' memory: %s at start, peak %s at %s, %s now" % (gb(a["total"][0]), gb(max(a["total"])),
                                                              clock(a["times"][a["peak_i"]]), gb(a["total"][-1])),
        "  compressed: peak %s; swap used: peak %s; swap written: %s" % (
            gb(max(v["compressed"] for v in sysv)), gb(max(v["swap_used"] for v in sysv)), gb(sum(a["swap_written"]))),
        "  memory pressure warning: %s" % ("never" if not warn else "%d min" % (warn // 60 or 1)),
        "  MCP servers (all agents): %s at the end" % gb(a["mcp"][-1]),
        "",
        "Biggest apps (average / peak / now, processes):",
    ]
    for g in a["ranked"][:12]:
        st = a["stats"][g]
        lines.append("  %-34s %9s %9s %9s  %d" % (g[:34], gb(st["avg"]), gb(st["peak"]), gb(st["end"]), st["count"]))
    lines.append("")
    if a["episodes"]:
        lines.append("Spikes (apps' memory up %.0f GB+ within 10 min):" % threshold_gb)
        for k, ep in enumerate(a["episodes"], 1):
            lines.append("  %d. +%s %s, %s" % (k, gb(ep["rise"]), ep["when"], "still up" if ep["still"] else "stayed up %d min" % (ep["lasted"] // 60 + 1)))
            lines.append("     by app: " + ", ".join("%s %+.1f GB" % (g, d / 1024) for d, g in ep["groups"] if d > 100))
            for p in ep["procs"][:5]:
                lines.append("     %s %s (%s): %s%s" % ("new" if p["new"] else "grew", p["label"], p["owner"],
                                                        ("+" if not p["new"] else ""), gb(p["delta"])))
    else:
        lines.append("No spikes of %.0f GB+ within 10 minutes." % threshold_gb)
    if a["growers"]:
        lines.append("")
        lines.append("Kept growing for 30+ min (possible leaks):")
        for v in a["growers"][:6]:
            lines.append("  %s (%s, pid %d): %s -> %s over %d min" % (v["label"], v["owner"], v["pid"], gb(v["m0"]),
                                                                   gb(v["m1"]), (v["t1"] - v["t0"]) // 60))
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML

SLOTS = 8
CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--other:#c3c2b7;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;--series-6:#008300;
--series-7:#6250d6;--series-8:#e34948;--good:#22a55b;--warning:#fab219;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-1:#1a1a19;
--page:#0d0d0d;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--other:#52514e;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;
--series-6:#008300;--series-7:#9085e9;--series-8:#e66767}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--other:#52514e;--series-1:#3987e5;
--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#008300;--series-7:#9085e9;--series-8:#e66767}
html,body{margin:0;background:var(--page)}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.viz-root{background:var(--page);color:var(--text-primary);max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.sub{color:var(--text-secondary);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px;
box-shadow:inset 0 3px 0 var(--accent,transparent)}
.tile .l{color:var(--text-secondary);font-size:13px}.tile .v{font-size:22px;font-weight:600}
.tile .d{color:var(--muted);font-size:12px}
.tile .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;vertical-align:2px}
.tile .bar{display:flex;gap:1px;height:8px;border-radius:4px;overflow:hidden;margin:7px 0 5px;background:var(--grid)}
.tile .bar i{display:block;height:100%}
.tile .strip{position:relative;height:8px;border-radius:4px;margin:7px 0 5px;background:var(--grid)}
.tile .strip i{position:absolute;top:0;bottom:0;min-width:3px;border-radius:2px;background:var(--text-secondary)}
.tile svg.spark{height:26px;margin:4px 0 3px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px 12px 4px;position:relative}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 8px;padding:0;list-style:none;color:var(--text-secondary);font-size:13px}
.legend li{display:flex;align-items:center;gap:6px}.key{width:14px;height:3px;border-radius:2px;display:inline-block}
.band{display:inline-block;width:12px;height:12px;border-radius:2px;opacity:.5}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;
padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;min-width:180px;z-index:2}
.tip .h{color:var(--text-secondary);margin-bottom:4px}.tip .r{display:flex;align-items:center;gap:6px;white-space:nowrap}
.tip .r b{font-variant-numeric:tabular-nums;min-width:58px;text-align:right}.tip .r span{color:var(--text-secondary)}
table{width:100%;border-collapse:collapse;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text-secondary);font-weight:500;font-size:13px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.spike{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:12px;margin:0 0 10px}
.spike h3{margin:0 0 6px;font-size:15px}.spike ul{margin:4px 0 0;padding-left:18px}.muted{color:var(--text-secondary)}
.scroll{overflow-x:auto}
"""

JS = r"""
document.querySelectorAll('[data-chart]').forEach(function(card){
  var d = JSON.parse(card.querySelector('script').textContent);
  var svg = card.querySelector('svg'), tip = card.querySelector('.tip'), line = svg.querySelector('.cross');
  function fmt(v){ return v >= 1024 ? (v/1024).toFixed(1) + ' GB' : Math.round(v) + ' MB'; }
  function hide(){ tip.style.display='none'; line.setAttribute('opacity', 0); }
  svg.addEventListener('pointerleave', hide);
  svg.addEventListener('pointermove', function(e){
    var pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    var p = pt.matrixTransform(svg.getScreenCTM().inverse());
    if (p.x < d.x0 || p.x > d.x1) { hide(); return; }
    var best = 0, bd = Infinity;
    for (var i = 0; i < d.x.length; i++) { var dd = Math.abs(d.x[i] - p.x); if (dd < bd) { bd = dd; best = i; } }
    line.setAttribute('x1', d.x[best]); line.setAttribute('x2', d.x[best]); line.setAttribute('opacity', 1);
    tip.textContent = '';
    var h = document.createElement('div'); h.className = 'h'; h.textContent = d.labels[best]; tip.appendChild(h);
    d.series.slice().reverse().forEach(function(s){
      var r = document.createElement('div'); r.className = 'r';
      var k = document.createElement('i'); k.className = 'key'; k.style.background = s.color; r.appendChild(k);
      var b = document.createElement('b'); b.textContent = fmt(s.v[best]); r.appendChild(b);
      var n = document.createElement('span'); n.textContent = s.name; r.appendChild(n);
      tip.appendChild(r);
    });
    (d.extra || []).forEach(function(x){
      if (!x.v[best]) return;
      var r = document.createElement('div'); r.className = 'r';
      var n = document.createElement('span'); n.textContent = x.v[best]; r.appendChild(n); tip.appendChild(r);
    });
    tip.style.display = 'block';
    var rect = card.getBoundingClientRect(), left = e.clientX - rect.left + 14;
    if (left + tip.offsetWidth > rect.width) left = e.clientX - rect.left - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, left) + 'px'; tip.style.top = (e.clientY - rect.top + 10) + 'px';
  });
});
"""


def nice_step(maxv, target=5):
    for step in (0.5, 1, 2, 4, 5, 8, 10, 16, 20, 32, 50, 64, 100):
        if maxv / step <= target:
            return step
    return maxv / target


def svg_chart(times, series, *, height=300, stacked=True, bands=(), marks=(), extra=None):
    """series: list of (name, css color, values in MB). Returns the chart card's HTML."""
    W, H, L, R, T, B = 960, height, 52, 12, 18, 26
    t0, t1 = times[0], max(times[-1], times[0] + 1)
    x = lambda t: L + (t - t0) / (t1 - t0) * (W - L - R)
    n = len(times)
    cum = [[0.0] * n]
    for _, _, vals in series:
        cum.append([cum[-1][i] + (vals[i] if stacked else 0) for i in range(n)] if stacked else vals)
    top = max(max(c) for c in cum[1:]) if n else 1
    step = nice_step(top / 1024)
    ymax = max(step, (int(top / 1024 / step) + 1) * step) * 1024
    y = lambda v: T + (H - T - B) * (1 - v / ymax)

    gap = max(120, 5 * (times[1] - times[0]) if n > 1 else 120)
    segs, start = [], 0
    for i in range(1, n):
        if times[i] - times[i - 1] > gap:
            segs.append((start, i))
            start = i
    segs.append((start, n))

    out = ['<svg viewBox="0 0 %d %d" role="img">' % (W, H)]
    for x0, x1, color, label in bands:
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" opacity=".18"/>' % (
            x(x0), T, max(2, x(x1) - x(x0)), H - T - B, color))
        if label:
            out.append('<text x="%.1f" y="%d" text-anchor="middle" style="fill:var(--text-secondary);font-weight:600">%s</text>' % (
                (x(x0) + x(x1)) / 2, T - 5, html.escape(label)))
    v = 0.0
    while v <= ymax / 1024 + 1e-9:
        yy = y(v * 1024)
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="var(--%s)" stroke-width="1"/>' % (
            L, W - R, yy, yy, "axis" if v == 0 else "grid"))
        out.append('<text x="%d" y="%.1f" text-anchor="end" dy="4">%s GB</text>' % (L - 6, yy, ("%g" % v)))
        v += step
    span = t1 - t0
    tick = next((m for m in (5, 10, 15, 30, 60, 120, 240) if span / (m * 60) <= 8), 480) * 60
    first = (int(t0 // tick) + 1) * tick
    tz = datetime.datetime.now().astimezone().utcoffset().total_seconds()
    first = int((t0 + tz) // tick + 1) * tick - tz
    t = first
    while t <= t1:
        out.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x(t), H - 8, clock(t)))
        t += tick
    for k, (name, color, vals) in enumerate(series, 1):
        for a, b in segs:
            upper = ["%.1f,%.1f" % (x(times[i]), y(cum[k][i] if stacked else vals[i])) for i in range(a, b)]
            if stacked:
                lower = ["%.1f,%.1f" % (x(times[i]), y(cum[k - 1][i])) for i in range(b - 1, a - 1, -1)]
                out.append('<polygon points="%s" fill="%s" fill-opacity=".16"/>' % (" ".join(upper + lower), color))
            out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>' % (
                " ".join(upper), color))
    for t, label in marks:
        out.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x(t), T - 5, html.escape(label)))
    out.append('<line class="cross" x1="0" x2="0" y1="%d" y2="%d" stroke="var(--text-secondary)" stroke-width="1" opacity="0"/>' % (T, H - B))
    out.append('</svg>')
    data = {"x": [round(x(t), 1) for t in times], "x0": L, "x1": W - R,
            "labels": [time.strftime("%H:%M:%S", time.localtime(t)) for t in times],
            "series": [{"name": nm, "color": c, "v": [round(v, 1) for v in vals]} for nm, c, vals in series],
            "extra": extra or []}
    blob = json.dumps(data).replace("</", "<\\/")
    return '<div class="card" data-chart><script type="application/json">%s</script>%s<div class="tip"></div></div>' % (blob, "".join(out))


def legend(items):
    return '<ul class="legend">' + "".join(
        '<li><i class="%s" style="background:%s"></i>%s</li>' % (cls, color, html.escape(name)) for name, color, cls in items) + "</ul>"


def html_report(meta, samples, a, threshold_gb):
    e = html.escape
    times, sysv = a["times"], [s["sys"] for s in samples]
    shown = a["ranked"][:SLOTS]
    order = sorted(shown, key=lambda g: -a["stats"][g]["avg"])  # biggest steady users at the bottom
    colors = {g: "var(--series-%d)" % (shown.index(g) + 1) for g in shown}
    series = [(g, colors[g], [s["g"].get(g, 0.0) for s in samples]) for g in order]
    rest = [a["total"][i] - sum(samples[i]["g"].get(g, 0.0) for g in shown) for i in range(len(samples))]
    series.append(("Everything else", "var(--other)", rest))
    spike_bands = [(times[ep["base"]], times[ep["end"]], "var(--muted)", str(k)) for k, ep in enumerate(a["episodes"], 1)]
    chart1 = svg_chart(times, series, bands=spike_bands)

    pressure_bands = []
    for i, v in enumerate(sysv):
        if v["pressure"] < 2:
            continue
        color = "var(--critical)" if v["pressure"] >= 4 else "var(--warning)"
        t_end = times[i + 1] if i + 1 < len(times) else times[i] + meta["interval"]
        if pressure_bands and pressure_bands[-1][2] == color and pressure_bands[-1][1] == times[i]:
            pressure_bands[-1] = (pressure_bands[-1][0], t_end, color, "")
        else:
            pressure_bands.append((times[i], t_end, color, ""))
    extra = [{"v": ["memory pressure: " + {2: "warning", 4: "critical"}.get(v["pressure"], "normal") if v["pressure"] >= 2 else ""
                    for v in sysv]},
             {"v": ["swap written: " + gb(w) if w >= 1 else "" for w in a["swap_written"]]}]
    chart2 = svg_chart(times, [("Compressed", "var(--series-1)", [v["compressed"] for v in sysv]),
                               ("Swap used", "var(--series-2)", [v["swap_used"] for v in sysv])],
                       height=200, stacked=False, bands=pressure_bands, extra=extra)

    warn = sum(1 for v in sysv if v["pressure"] >= 2) * meta["interval"]
    span = times[-1] - times[0]

    def mix_bar(i):
        """The sample's memory split by app, in the chart's colors."""
        parts = [(g, colors[g], samples[i]["g"].get(g, 0.0)) for g in shown] + [("Everything else", "var(--other)", rest[i])]
        total = sum(v for _, _, v in parts) or 1
        return '<div class="bar">' + "".join('<i style="width:%.2f%%;background:%s" title="%s: %s"></i>' % (
            100 * v / total, c, e(g), gb(v)) for g, c, v in parts if v > 0) + '</div>'

    def spark(vals, color):
        step = max(1, len(vals) // 200)
        pts = vals[::step]
        top = max(pts) or 1
        xy = ["%.1f,%.1f" % (200 * k / max(1, len(pts) - 1), 25 - 23 * v / top) for k, v in enumerate(pts)]
        return ('<svg class="spark" viewBox="0 0 200 26" preserveAspectRatio="none" aria-hidden="true">'
                '<polygon points="0,26 %s 200,26" fill="%s" fill-opacity=".16"/>'
                '<polyline points="%s" fill="none" stroke="%s" stroke-width="2" vector-effect="non-scaling-stroke" '
                'stroke-linejoin="round"/></svg>') % (" ".join(xy), color, " ".join(xy), color)

    worst = max(v["pressure"] for v in sysv)
    status = "var(--critical)" if worst >= 4 else "var(--warning)" if worst >= 2 else "var(--good)"
    strip = '<div class="strip">' + "".join('<i style="left:%.2f%%;width:%.2f%%" title="Spike %d"></i>' % (
        100 * (times[ep["base"]] - times[0]) / max(1, span), 100 * (times[ep["end"]] - times[ep["base"]]) / max(1, span), k)
        for k, ep in enumerate(a["episodes"], 1)) + '</div>'
    # (label, value, detail, accent color, extra markup)
    tiles = [
        ("Apps' memory at peak", gb(max(a["total"])), "at %s, of %s RAM" % (clock(times[a["peak_i"]]), gb(meta["ram_mb"])),
         None, mix_bar(a["peak_i"])),
        ("Now", gb(a["total"][-1]), "started at %s" % gb(a["total"][0]), None, mix_bar(len(samples) - 1)),
        ("Compressed, peak", gb(max(v["compressed"] for v in sysv)), "holding %s of data" % gb(max(v["stored"] for v in sysv)),
         "var(--series-1)", spark([v["compressed"] for v in sysv], "var(--series-1)")),
        ("Swap used, peak", gb(max(v["swap_used"] for v in sysv)), "%s written while recording" % gb(sum(a["swap_written"])),
         "var(--series-2)", spark([v["swap_used"] for v in sysv], "var(--series-2)")),
        ("Memory pressure", "never warned" if not warn else "%d min" % max(1, warn // 60), "at warning or worse",
         status, None),
        ("Spikes", str(len(a["episodes"])), "%g GB+ within 10 min" % threshold_gb, None, strip),
    ]

    def tile(label, value, detail, accent, extra):
        dot = '<i class="dot" style="background:%s"></i>' % accent if label == "Memory pressure" else ""
        return '<div class="tile"%s><div class="l">%s</div><div class="v">%s%s</div>%s<div class="d">%s</div></div>' % (
            ' style="--accent:%s"' % accent if accent else "", e(label), dot, e(value), extra or "", e(detail))

    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Memory recording</title><style>%s</style></head><body><main class="viz-root">' % CSS,
             '<h1>Where the memory went</h1>',
             '<p class="sub">%s, %s to %s (%d min, a sample every %ds). Memory is each process\'s footprint, the figure Activity Monitor shows, '
             'credited to the app or agent session that launched it.</p>' % (
                 time.strftime("%a %d %b", time.localtime(times[0])), clock(times[0]), clock(times[-1]), span // 60, meta["interval"]),
             '<div class="tiles">' + "".join(tile(*t) for t in tiles) + '</div>',
             '<h2>Memory by app</h2>',
             legend([(g, c, "key") for g, c, _ in reversed(series)] + ([("Spike (numbered below)", "var(--muted)", "band")] if spike_bands else [])),
             chart1,
             '<h2>Compressed memory and swap</h2>',
             '<p class="sub">When apps need more than fits, macOS compresses memory, then writes it to disk (swap). '
             'Swap writes and pressure warnings are when the Mac slows down.</p>',
             legend([("Compressed", "var(--series-1)", "key"), ("Swap used", "var(--series-2)", "key"),
                     ("Pressure warning", "var(--warning)", "band"), ("Pressure critical", "var(--critical)", "band")]),
             chart2]

    parts.append('<h2>Spikes</h2>')
    if not a["episodes"]:
        parts.append('<p class="muted">No rise of %g GB or more within 10 minutes. Lower the bar with --threshold-gb 1.</p>' % threshold_gb)
    for k, ep in enumerate(a["episodes"], 1):
        parts.append('<div class="spike"><h3>%d. +%s %s, %s</h3>' % (k, gb(ep["rise"]), ep["when"], "still up" if ep["still"] else "stayed up %d min" % (ep["lasted"] // 60 + 1)))
        parts.append('<div class="muted">By app: %s</div>' % e(", ".join("%s %+.1f GB" % (g, d / 1024) for d, g in ep["groups"] if d > 100) or "spread thin"))
        if ep["procs"]:
            parts.append('<ul>' + "".join('<li>%s <b>%s</b> (%s, pid %d)%s: %s</li>' % (
                "New:" if p["new"] else "Grew:", e(p["label"]), e(p["owner"]), p["pid"],
                (", started " + clock(p["started"])) if p["new"] and p["started"] else "",
                gb(p["delta"]) if p["new"] else "+" + gb(p["delta"]) + " to " + gb(p["now"])) for p in ep["procs"]) + '</ul>')
        parts.append('</div>')

    if a["growers"]:
        parts.append('<h2>Kept growing (possible leaks)</h2><p class="sub">Processes alive 30+ minutes whose memory rose 300 MB or more.</p>')
        parts.append('<div class="scroll"><table><tr><th>Process</th><th>App</th><th class="n">From</th><th class="n">To</th><th class="n">Over</th></tr>' + "".join(
            '<tr><td>%s <span class="muted">pid %d</span></td><td>%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%d min</td></tr>' % (
                e(v["label"]), v["pid"], e(v["owner"]), gb(v["m0"]), gb(v["m1"]), (v["t1"] - v["t0"]) // 60) for v in a["growers"]) + '</table></div>')

    parts.append('<h2>Apps</h2><div class="scroll"><table><tr><th>App</th><th class="n">Average</th><th class="n">Peak</th><th class="n">Now</th><th class="n">Change</th><th class="n">Processes</th></tr>')
    for g in a["ranked"][:25]:
        st = a["stats"][g]
        parts.append('<tr><td>%s%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%+.1f GB</td><td class="n">%d</td></tr>' % (
            '<i class="key" style="background:%s;margin-right:8px;vertical-align:middle"></i>' % colors[g] if g in colors else "",
            e(g), gb(st["avg"]), gb(st["peak"]), gb(st["end"]), (st["end"] - st["start"]) / 1024, st["count"]))
    parts.append('</table></div>')
    parts.append('<p class="sub" style="margin-top:8px">MCP servers across all agent sessions: %s at the end.</p>' % gb(a["mcp"][-1]))

    parts.append('<h2>Biggest processes at the peak (%s)</h2><div class="scroll"><table><tr><th>Process</th><th>App</th><th class="n">Memory</th></tr>' % clock(times[a["peak_i"]]))
    for r in a["peak_procs"]:
        parts.append('<tr><td>%s <span class="muted">pid %d</span></td><td>%s</td><td class="n">%s%s</td></tr>' % (
            e(r[4]), r[0], e(r[3]), "≈" if "a" in r[5] else "", gb(r[2])))
    parts.append('</table></div><p class="sub" style="margin-top:8px">≈ resident size only; macOS doesn\'t let other users read system processes\' full footprint.</p>')
    parts.append('<script>%s</script></main></body></html>' % JS)
    return "".join(parts)


# ---------------------------------------------------------------- commands

def cmd_now():
    s = sample()
    total = sum(s["g"].values())
    v = s["sys"]
    print("Apps' memory: %s   compressed %s (holding %s)   swap used %s   pressure %s" % (
        gb(total), gb(v["compressed"]), gb(v["stored"]), gb(v["swap_used"]),
        {1: "normal", 2: "warning", 4: "critical"}.get(v["pressure"], "?")))
    print("\nBy app (including what it launched):")
    for g, mb in sorted(s["g"].items(), key=lambda kv: -kv[1])[:15]:
        print("  %9s  %4d procs  %s" % (gb(mb), s["n"][g], g))
    print("\nBiggest processes:")
    for r in sorted(s["p"], key=lambda r: -r[2])[:15]:
        print("  %9s  %-36s %s%s" % (gb(r[2]), r[4][:36], r[3], "  (approx.)" if "a" in r[5] else ""))
    mcp = [r for r in s["p"] if "m" in r[5]]
    print("\nMCP servers over %d MB: %d, using %s" % (KEEP_MB, len(mcp), gb(sum(r[2] for r in mcp))))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]

    def opt(name, default):
        return type(default)(rest[rest.index(name) + 1]) if name in rest else default

    positional = [r for i, r in enumerate(rest) if not r.startswith("--") and (i == 0 or not rest[i - 1].startswith("--"))]
    if cmd == "now":
        cmd_now()
    elif cmd in ("record", "start"):
        seconds = int(opt("--hours", 2.0) * 3600)
        interval = opt("--interval", 15)
        folder = opt("--dir", "") or new_folder()
        if cmd == "record":
            record(folder, seconds, interval)
            return
        os.makedirs(folder, exist_ok=True)
        log = open(os.path.join(folder, "log.txt"), "a")
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "record", "--dir", folder,
                                 "--hours", str(seconds / 3600), "--interval", str(interval)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        print(folder)
        print("recording in the background (pid %d) for %g h, a sample every %d s" % (proc.pid, seconds / 3600, interval))
    elif cmd == "stop":
        folder = positional[0] if positional else latest_folder()
        pid = running(folder)
        if pid:
            os.kill(pid, signal.SIGTERM)
            print("stopped", folder)
        else:
            print("not running:", folder)
    elif cmd == "report":
        folder = positional[0] if positional else latest_folder()
        threshold = opt("--threshold-gb", 2.0)
        meta, samples = load(folder)
        if len(samples) < 2:
            sys.exit("Only %d sample(s) so far in %s. Give it a minute." % (len(samples), folder))
        a = analyse(meta, samples, threshold)
        print(text_report(meta, samples, a, threshold))
        out = os.path.join(folder, "report.html")
        with open(out, "w") as f:
            f.write(html_report(meta, samples, a, threshold))
        state = "still recording" if running(folder) else "finished"
        print("\n%s (%s)" % (out, state))
        if "--no-open" not in rest:
            subprocess.run(["open", out])
    else:
        sys.exit("unknown command %r\n%s" % (cmd, __doc__))


if __name__ == "__main__":
    main()
