---
name: mac-input-lag
description: Diagnose and fix input lag on macOS, such as delayed keystrokes, laggy typing, a stuttering cursor or mouse, beachballs, or a Mac that feels sluggish. Measures WindowServer load, keyboard/mouse event taps installed by apps (Logi Options+, Raycast, dictation and key-remapping tools), long-running Chrome GPU processes, memory pressure, busy Docker/OrbStack containers and leftover MCP servers. Then restarts the culprits the user approves and measures again. For lag that comes and goes, it records the actual input delay alongside a process timeline to find what triggers it. Use this whenever the user says their input, typing, keyboard, mouse, cursor or computer is lagging, slow, delayed or stuttering, even if they don't mention macOS or ask for a diagnosis.
argument-hint: "[app or symptom, optional]"
allowed-tools: Bash, Read, AskUserQuestion
---

# Mac input lag

Every keystroke and mouse event on macOS passes through WindowServer, then through each app that installed a blocking event tap, before reaching the focused app. Lag almost always comes from one of those stages being slow: WindowServer busy redrawing, an app's tap callback stalling, or the whole machine short on memory. This skill measures each stage, fixes what the user approves, and measures again.

The scripts live in this skill's base directory (shown when the skill loads). Below, `$SKILL` stands for that path.

## 1. Diagnose

Run the read-only diagnostic. It changes nothing.

```bash
bash "$SKILL/scripts/diagnose.sh"
```

Give it a 5-minute timeout. The first run compiles a small Swift helper for the event-tap scan and caches it in `$TMPDIR/mac-input-lag/`. That can take a minute on a loaded machine, and later runs take about 15 seconds. Pass `--no-taps` if `swiftc` isn't installed.

If the user says the lag is in one app only (a terminal, an editor, one browser), still run the full diagnostic, because system-wide causes are common even when one app feels worst. Then look at that app's own processes.

### When lag comes and goes

A snapshot taken after the lag has passed often misses the cause. Record a timeline while the user keeps working when:
- the lag is intermittent,
- the user can't say what triggers it, or
- the snapshot doesn't explain it.

```bash
bash "$SKILL/scripts/record.sh" 30 --keys    # minutes; prints the recording directory
```

Run it with `run_in_background: true`. Ask the user to carry on normally, or repeat whatever they suspect, and to tell you when they feel lag. The recording captures:
- How late mouse events reach the end of the input pipeline, after every app's tap has passed them on. With `--keys`, keystrokes too. This is the delay the user feels. Only timing is recorded, never which keys.
- How late a high-priority timer fires each second. This shows scheduler stalls even when nobody is touching the computer.
- The busiest processes every 3 seconds.
- Every 10 seconds: load, newly started or busy processes together with the process that launched them, and tap counts.

`--keys` needs Input Monitoring permission for the app that runs the recorder. Without it, the recorder asks macOS to show the permission prompt and times mouse events only. Have the user allow it in System Settings → Privacy & Security → Input Monitoring, then restart the recording. Mouse timing alone catches system-wide stalls, but only keystroke timing can separate those from an app holding keystrokes.

When the user reports lag, or the recording ends, run the report. It also works while the recording continues:

```bash
python3 "$SKILL/scripts/report.py" <recording dir>
```

The report lists lag episodes, with what was running and what had just started (including what launched it). It also lists the processes that ran hotter during lag than outside it. Read it like this:
- One episode is a lead. Several episodes pointing at the same process is evidence.
- Keystrokes slow while the mouse was fine means one of the apps holding keystrokes is the cause.
- No lag recorded although the user felt it means the delay was inside an app (slow drawing), not in input delivery. Find the app they were typing in (`lsappinfo front`) and its busiest process in `top.tsv`. A single browser tab using a full core makes that browser lag while the rest of the Mac is fine. To find which tab it is, use the browser's task manager (Chrome: Window → Task Manager), which shows the process ID.

Stop a recording early with `kill "$(cat <recording dir>/pid)"`.

## 2. Read the report

The sections below are roughly in order of impact.

**Spike or steady state?** Decide this first, because it changes what to fix. The diagnostic warns when the 1-minute load is far above the 15-minute load (for example 539 vs 141); that means something started in the last few minutes. Check "Started in the last 15 minutes" and the parent of each busy process. Common short-lived culprits:
- Agent sessions running heavy git or file work (`git worktree remove`, clones, big checkouts), which also keep fseventsd and Spotlight busy.
- Containers bursting across many cores.
- Computer-use sessions, which take frequent screenshots and install their own input hooks.

These usually end on their own, so wait them out rather than killing them; killing a git operation partway can leave a repo or worktree half-done. Lag that comes in bursts usually means one of these, while lag that never goes away points to the steady causes below.

A load jump alone proves nothing. If idle CPU stays high and nothing busy shows up under "Started in the last 15 minutes", the jump is background threads across many processes waking at the same moment, and it isn't the cause. This has been seen at a load of 550 on a machine that felt smooth, with 65% idle CPU and only 4–8 threads runnable at any instant.

**WindowServer CPU.** This is the most direct signal. Under ~20% is normal. Sustained 50%+ means input events are queuing behind screen redraws. WindowServer is rarely the root cause itself, so find what keeps it redrawing:
- A Chrome or Electron GPU process burning CPU (see Chrome below). Electron apps that animate constantly show up as `<App> Helper (Renderer)` or `<App> Helper` with steady CPU.
- Display scaling. In the Displays section, `Resolution` is the framebuffer macOS renders. If it's larger than the monitor's native panel (for example 6016×3384 on a 4K monitor, from a scaled "looks like 3008×1692" mode), WindowServer renders oversized frames and downsamples every one. Mention this as an option for the user, since switching to the native "looks like" mode changes how big things look. Don't change it yourself.

**Event taps.** Apps like Logi Options+, Raycast, Alfred, Karabiner, BetterTouchTool, dictation tools and window managers install taps. A blocking tap (enabled, not listen-only) holds each matching event until the app's callback returns. Read the table like this:
- **DISABLED piling up (3+ in one process)** is the dependable signal. macOS disables a tap whose callback times out, and apps that re-create their tap afterwards accumulate dead ones. A pile means the app has been stalling input repeatedly. Restarting it clears the pile.
- **HOLDS** shows which apps sit in the path of every keystroke or mouse event. That alone isn't a problem, since most Macs have a few.
- **macOS's per-tap "latency" figure is left out on purpose.** It looks like a delay but behaves like the time since the tap last saw an event. It freezes when nothing moves and climbs while other input flows, and it reaches tens of seconds on machines where typing is fine. To find out whether an app delays typing, measure keystroke delay directly with `record.sh --keys` (see "When lag comes and goes").
- **Cause or victim?** A blocking tap needs its app to get CPU time before it can pass each event on. On a calm machine that's instant. During a system-wide spike, keystrokes queue behind the load whichever apps hold them. Fix the spike first. Only blame an app if keystrokes are slow while the mouse is fine, or if lag continues once the machine is calm.

**Load average vs. idle CPU.** On Apple Silicon, ignore the size of the load average; only its trend (see "Spike or steady state?") is useful. Background-priority threads only run on the efficiency cores, and there are just 2 on M1 Pro and M1 Max. Hundreds of threads can queue there and push the load average into the hundreds while the performance cores sit idle. Look at `CPU usage ... idle` instead: under ~20% idle means the machine really is CPU-bound.

**Chrome.** The report labels each Chrome instance. The one without `--user-data-dir` is the user's browser. The others usually belong to automation (chrome-devtools-mcp, Playwright, Codex recordings); leave them alone unless the user asks. These are worth acting on:
- A GPU process at a sustained 30%+ CPU, especially after days of uptime.
- "update pending": the running version is older than the installed one, and a relaunch applies the update.

**Memory.** Under ~1 GB unused, a large compressor and growing swap mean memory pressure, which slows everything, input included. Restarting the biggest long-lived apps frees memory quickly. To see which apps and agent sessions hold the memory, or to record it over hours, use the `mac-memory` skill.

**Containers.** A container with steady CPU while the user isn't working on it (often a local database or analytics service) adds load. Stopping it is the user's call because it may be part of their dev stack. Judge the cost by the `host cost` line, which is the VM's own process on the Mac. `docker stats` percentages swing between samples (1409% then 120%), and on OrbStack they can be far off: 4057% was seen on a 10-core Mac while OrbStack Helper used 30%. Don't act on them alone.

**Agent tooling.** Each Claude Code, Codex or desktop-app session starts its own MCP servers, so dozens of them is normal with many sessions open. Processes listed as orphans (parent gone) are safe to kill. The rest belong to live sessions, and killing them breaks those sessions; suggest closing idle sessions instead.

**Duplicates and uptime.** Dozens of copies of the same Apple helper at 0% CPU are clutter, not lag. Don't kill system daemons one by one; a restart clears them. After a week or more of uptime with several of the issues above, recommend restarting the Mac as the thorough fix.

**Headroom.** Steady loads such as display scaling, apps that redraw constantly and idle containers often don't cause lag by themselves. They do leave less headroom, so a short burst tips the machine into lag sooner. Present them as background contributors worth trimming, not as the cause.

## 3. Ask before fixing

Summarize the findings in a few lines, ranked by likely impact and backed by numbers. Then use a multi-select `AskUserQuestion` to ask which fixes to apply:
- List the safest high-impact fix first and mark it recommended.
- Make the question self-contained, because the user may only see the dialog.
- Say what each fix costs, for example "tabs restore, but unsaved form input is lost".
- For an app you only suspect, such as one holding keystrokes, offer quitting it as a test rather than as a fix.

Quitting apps can lose unsaved work, so only restart what the user picks, even when something looks obviously safe.

## 4. Apply

| Target | How | Watch out for |
|---|---|---|
| Normal app | `osascript -e 'quit app "Name"'`, then `open -a "Name"` | Some apps refuse an AppleScript quit with error -128 "User canceled" (Raycast does). Use `pkill -TERM -x Name`, check that it's gone, then reopen it. |
| launchd-managed agent (e.g. `logioptionsplus_agent`) | `killall <agent>` | launchd restarts it on its own. Confirm that a new PID appears. |
| Chrome, main instance | `kill -TERM <main pid>`, wait until it exits, then `open -n -a "Google Chrome" --args --restore-last-session` | Chrome treats SIGTERM as a clean quit and saves the session. `-n` is needed when automation Chrome instances are running; without it `open` just focuses one of those. `--restore-last-session` brings the tabs back whatever the user's startup setting. Avoid `osascript quit`, which can hit the wrong instance when there are several, and `kill -9`, which loses the session. |
| Docker / OrbStack container | `docker stop <name>` | Only with explicit approval. |
| Orphaned MCP servers | `kill <pid>` for the listed orphans only | |

Do the quits in one Bash call, check what exited in the next, then relaunch. Apps the user asked to quit (rather than restart) stay closed.

## 5. Verify

Relaunched apps cause a short spike, especially Chrome while it restores tabs, so wait before measuring. Run the re-check in the background with `run_in_background: true`, and read its output when you're notified:

```bash
sleep 75; bash "$SKILL/scripts/diagnose.sh"
```

## 6. Report

Start with a before/after table of the numbers that changed: WindowServer CPU, idle CPU, load average, unused memory, and tap counts per app. Then cover these briefly:
- What was wrong, as a cause chain (for example "WindowServer at 90% because Chrome's GPU process had been rendering at 50% for 5 days").
- What you did, including what you deliberately left alone.
- How sure you are of the cause. If several things changed between the two measurements (your fixes, plus transient work that finished on its own), say that the result can't separate them. Don't give all the credit to the fix you applied.
- What's still worth doing, such as busy containers, idle sessions, display scaling or a restart, with the command if there is one.

Finish by asking the user to say if the lag continues in a particular app.

## 7. Confirm the culprit

When an app you quit or restarted might be the cause but the evidence is mixed, offer a one-variable test once the machine is calm. Reopen that one app (or leave it running) and have the user work normally:
- If typing stays smooth, the app wasn't the cause by itself. Keep it.
- If lag returns without a spike, it is the cause. Check its extensions or settings that react to every keystroke, such as clipboard history, text expansion and hotkeys, and check for an update.

Test one suspect at a time. A `record.sh --keys` recording makes the test concrete: compare keystroke delay with the app running and with it quit. If the lag hasn't come back by the end of the session, leave a recording running so that the next episode is captured instead of described afterwards.

## User's description

$ARGUMENTS
