#!/usr/bin/env bash
# Read-only snapshot of the usual causes of macOS input lag. Changes nothing.
# Usage: diagnose.sh [--no-taps]
#   --no-taps  skip the event-tap scan (it needs swiftc from the Xcode Command Line Tools)
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
want_taps=1
[ "${1:-}" = "--no-taps" ] && want_taps=0

section() { printf '\n== %s ==\n' "$1"; }
name_of() { ps -o comm= -p "$1" 2>/dev/null | awk -F/ '{print $NF}'; }

section "Machine"
echo "$(sysctl -n machdep.cpu.brand_string): $(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || echo '?') performance + $(sysctl -n hw.perflevel1.logicalcpu 2>/dev/null || echo '?') efficiency cores, $(( $(sysctl -n hw.memsize) / 1073741824 )) GB RAM"
uptime | sed 's/^ *//'
sysctl -n vm.loadavg | tr -d '{}' | awk '$1 > 2 * $3 && $1 > 20 {
  print "! 1-min load " $1 " vs " $3 " over 15 min: something started or woke up recently. Only a lead if idle CPU is low or something shows under \"Started in the last 15 minutes\"" }'
df -h /System/Volumes/Data 2>/dev/null | awk 'NR==2 {print "disk: " $4 " free of " $2}'

section "CPU (second top sample; the first is always wrong)"
snap="$(top -l 2 -s 2 -n 15 -o cpu -stats pid,cpu,mem,th,command 2>/dev/null | awk '/^Processes:/ {c++} c==2')"
grep -E '^(Load Avg|CPU usage|PhysMem)' <<<"$snap"
printf '%7s %6s %7s %7s  %s\n' PID CPU% MEM THREADS PROCESS
awk '$1 ~ /^[0-9]+$/' <<<"$snap" | while read -r pid cpu mem th cmd; do
  full="$(name_of "$pid")"
  printf '%7s %6s %7s %7s  %s\n' "$pid" "$cpu" "$mem" "$th" "${full:-$cmd}"
done

section "Processes"
echo "total $(ps -A -o pid= | wc -l | tr -d ' '), runnable $(ps -A -o stat= | grep -c '^R')"
echo "most duplicated:"
ps -A -o comm= | awk -F/ '{print $NF}' | sort | uniq -c | sort -rn | head -10

section "Started in the last 15 minutes, using CPU"
# etime is mm:ss for anything younger than an hour. The parent's name shows who
# launched it (an agent session, a terminal, an app).
recent="$(ps -A -o pid=,ppid=,pcpu=,etime=,args= \
  | awk -v self=$$ '$4 ~ /^[0-9]+:[0-9]+$/ && ($4 + 0) < 15 && $3 >= 1 && $1 != self && $2 != self' \
  | sort -k3 -rn | head -10)"
if [ -n "$recent" ]; then
  while read -r pid ppid cpu age args; do
    printf '%7s %6s%% %6s  parent %-22s %s\n' "$pid" "$cpu" "$age" \
      "$(name_of "$ppid" | cut -c1-22)" "$(cut -c1-110 <<<"$args")"
  done <<<"$recent"
else
  echo "nothing busy"
fi

section "Memory"
echo "swap: $(sysctl -n vm.swapusage)"
memory_pressure 2>/dev/null | grep -i 'free percentage' || true

section "Displays (Resolution is the rendered framebuffer)"
system_profiler SPDisplaysDataType 2>/dev/null | awk '
  /Displays:/ {d=1; next}
  d && /^        [^ ].*:$/ {sub(/^ +/, ""); name=$0}
  d && /Resolution:|UI Looks like:/ {sub(/^ +/, ""); print name " " $0}'

section "Chrome"
if pgrep -x "Google Chrome" >/dev/null; then
  installed="$(defaults read "/Applications/Google Chrome.app/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo unknown)"
  echo "installed version: $installed"
  for pid in $(pgrep -x "Google Chrome"); do
    args="$(ps -o args= -p "$pid")"
    profile="$(grep -oE -- '--user-data-dir=[^ ]+' <<<"$args" | cut -d= -f2- || true)"
    if [ -n "$profile" ]; then label="automation profile $profile"; else label="MAIN (the user's browser)"; fi
    echo "pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' '), $(pgrep -P "$pid" | wc -l | tr -d ' ') helpers: $label"
    gpu="$(pgrep -P "$pid" -f 'type=gpu-process' | head -1)"
    if [ -n "$gpu" ]; then
      running="$(ps -o args= -p "$gpu" | grep -oE 'Versions/[0-9.]+' | head -1 | cut -d/ -f2)"
      echo "  GPU process $gpu: $(ps -o pcpu= -p "$gpu" | tr -d ' ')% CPU (1-min avg), up $(ps -o etime= -p "$gpu" | tr -d ' '), version ${running:-?}"
      if [ -n "$running" ] && [ "$running" != "$installed" ]; then
        echo "  ! running $running but $installed is installed: update pending, a relaunch applies it"
      fi
    fi
  done
else
  echo "not running"
fi

section "Containers"
if command -v docker >/dev/null 2>&1; then
  # What the containers cost the Mac: CPU of the VM's host process. docker stats percentages
  # can be far off on OrbStack (4057% seen on a 10-core Mac while OrbStack Helper used 30%).
  ps -A -o pcpu=,comm= | awk '/OrbStack Helper$|Virtualization\.VirtualMachine$|com\.docker\.(hyperkit|virtualization)$|qemu-system/ {
    n = split($0, p, "/"); printf "host cost: %s at %s%% CPU\n", p[n], $1 }'
  # docker stats can hang for minutes while the VM is busy, so cap it at 20 s.
  perl -e 'alarm 20; exec @ARGV' docker stats --no-stream --format '{{.CPUPerc}}\t{{.MemUsage}}\t{{.Name}}' 2>/dev/null | sort -rn | head -10 \
    || echo "docker stats gave no answer within 20 s (daemon stopped or VM busy)"
else
  echo "docker not installed"
fi

section "Agent tooling"
echo "claude processes: $(ps -A -o comm= | awk -F/ '$NF=="claude"' | wc -l | tr -d ' ')"
# Only the executable and its first two arguments are checked, so sessions that merely
# pass MCP flags or config paths further along their command line aren't counted.
ps -A -o pid=,ppid=,rss=,args= | awk '
  {
    hit = 0
    for (i = 4; i <= 6 && i <= NF; i++) if ($i !~ /^-/ && tolower($i) ~ /mcp/) hit = 1
    if (!hit) next
    n++; mb += $3 / 1024
    if ($2 == 1) {o++; printf "  orphan pid %s: %s\n", $1, substr($0, index($0, $4), 100)}
  }
  END {printf "MCP server processes: %d using %d MB, %d orphaned (parent gone)\n", n, mb, o}'

section "Keyboard/mouse event taps"
if [ "$want_taps" = 1 ]; then
  # shellcheck source=lib.sh
  source "$here/lib.sh"
  if bin="$(helper event-taps)"; then
    "$bin"
  else
    echo "skipped: could not build event-taps.swift (needs swiftc: xcode-select --install)"
  fi
else
  echo "skipped (--no-taps)"
fi
