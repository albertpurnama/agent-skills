---
name: mac-memory
description: Find out where a Mac's memory (RAM) goes and what causes memory spikes. Breaks memory down by app, crediting helpers, MCP servers and child processes to the app or agent session (Claude Code, Codex) that launched them, and records it over hours to find spikes, leaks, swap and memory-pressure warnings, with an HTML report. Use whenever the user mentions RAM, memory, swap, memory pressure, running out of memory, "which app is using all my memory", a memory leak, or a Mac slowing down after many apps or agent sessions are open, even if they don't ask for a recording.
argument-hint: "[now | record for N hours | report]"
allowed-tools: Bash, Read
---

# Mac memory

`$SKILL` below is this skill's base directory (shown when the skill loads). The tool is one script:

```bash
python3 "$SKILL/scripts/memwatch.py" now                     # memory by app right now (about 5 s)
python3 "$SKILL/scripts/memwatch.py" start --hours 2         # record in the background, a sample every 15 s
python3 "$SKILL/scripts/memwatch.py" report                  # summary + HTML report of the newest recording
python3 "$SKILL/scripts/memwatch.py" stop                    # end a recording early
```

Recordings live in `~/Library/Logs/mac-memory/rec-*`. `start` detaches, so the recording keeps going if the session ends. `report` works while a recording is still running, prints a text summary, writes `report.html` into the recording folder and opens it (pass `--no-open` to skip that). `--threshold-gb 1` makes spike detection more sensitive (the default is 2 GB within 10 minutes).

## Which to run

- "Where is my RAM going?" Run `now`, then summarize the top apps and the biggest processes.
- Memory that runs out at some point, spikes, or a slow leak: `start` a recording (2 hours is a good default), tell the user it runs on its own and where the report will be, and run `report` when they come back or when it finishes.

## Reading the numbers

- **Memory per process** is its footprint, the "Memory" column in Activity Monitor. It includes compressed memory. Other users' processes (system daemons) can't be read that way and fall back to resident size, which undercounts. They're marked ≈ and grouped as "macOS".
- **Credit goes to whoever launched a process.** An agent session owns everything it started, MCP servers and browsers included, so "Claude Code" and "Codex" totals are the real cost of the open sessions. Commands run in a terminal show as "Terminal commands (<app>)". Many idle agent sessions are a common hidden cost: each starts its own set of MCP servers.
- **Used RAM near 100% is normal on macOS**, because it fills spare RAM with file cache. The signs of real shortage are:
  - compressed memory growing (the report shows how much data it holds),
  - swap being written,
  - memory pressure at warning or critical.
  Those are when the Mac slows down.
- **Spikes** are rises of 2 GB+ within 10 minutes. Each spike lists the apps that grew and the processes that started or grew, so name the actual process and what launched it.
- **"Kept growing"** lists processes alive 30+ minutes that grew 300 MB or more. These are leak candidates, typically a long-running dev server, a type checker (`tsc`), or a browser tab.
- **Containers** show as the VM process (OrbStack or Docker). Its footprint is the RAM the VM has taken from macOS, which can stay high after containers free memory inside it.

## Suggesting fixes

Rank by size and say what each would free, based on the numbers in the report. Common wins:
- closing idle agent sessions, which takes their MCP servers with them;
- restarting a leaking dev server or type checker;
- reloading or closing a heavy browser tab;
- stopping containers that aren't in use;
- quitting chat apps that idle at hundreds of MB each.

Quitting apps and stopping containers is the user's call, so ask first, and never kill processes that belong to another live session.

## User's request

$ARGUMENTS
