#!/usr/bin/env bash
# Records an input-lag timeline so lag that comes and goes can be traced afterwards.
# Usage: record.sh [duration] [--keys]
#   duration  minutes, or seconds with an "s" suffix (default 30; "45s" for a quick test)
#   --keys    also time keystrokes. Needs Input Monitoring permission for the app running
#             this; records timing only, never which keys.
# Prints the recording directory first. Pass it to report.py at any point, even while
# recording. Stop early with: kill "$(cat <dir>/pid)"
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib.sh
source "$here/lib.sh"

duration="${1:-30}"
keys=""
[ "${2:-}" = "--keys" ] && keys="--keys"
case "$duration" in
  *s) secs="${duration%s}" ;;
  *) secs=$(( duration * 60 )) ;;
esac

out="${TMPDIR:-/tmp}/mac-input-lag/rec-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$out"
echo "$out"
echo $$ >"$out/pid"

lat="$(helper input-latency)" || { echo "could not build input-latency.swift (needs swiftc)"; exit 1; }
taps="$(helper event-taps)" || taps=""

trap 'kill $(jobs -p) 2>/dev/null' EXIT

# Input delay and timer lateness, once a second.
"$lat" --seconds "$secs" $keys >"$out/latency.tsv" 2>"$out/latency.err" &

# Top processes every 3 s. top's first sample reports CPU since boot, so it's dropped.
# srand() returns the previous seed, so calling it twice yields the current epoch.
top -l $(( secs / 3 + 1 )) -s 3 -n 12 -o cpu -stats pid,cpu,command 2>/dev/null | awk '
  /^Processes:/ { n++; srand(); t = srand(); next }
  n < 2 { next }
  /^CPU usage:/ { idle = $7; sub(/%/, "", idle); print t "\tidle\t" idle; fflush(); next }
  $1 ~ /^[0-9]+$/ {
    pid = $1; cpu = $2; $1 = ""; $2 = ""; sub(/^ +/, "")
    print t "\tproc\t" pid "\t" cpu "\t" $0; fflush()
  }' >"$out/top.tsv" &

# Every 10 s: load, busy or newly started processes plus their parents, and tap readings.
end=$(( $(date +%s) + secs ))
while [ "$(date +%s)" -lt "$end" ]; do
  t="$(date +%s)"
  sysctl -n vm.loadavg | tr -d '{}' | awk -v t="$t" '{print t "\tload\t" $1}' >>"$out/system.tsv"
  ps -A -o pid=,ppid=,pcpu=,etime=,args= | awk -v t="$t" -v self=$$ '
    $2 == self || $1 == self { next }
    { line[$1] = $0; parent[$1] = $2 }
    $3 >= 5 || ($4 ~ /^[0-9]+:[0-9]+$/ && ($4 + 0) < 5) { picked[$1] = 1 }
    END {
      for (p in picked) { keep[p] = 1; keep[parent[p]] = 1 }
      for (p in keep) if (p in line) print t "\tps\t" substr(line[p], 1, 300)
    }' >>"$out/system.tsv"
  if [ -n "$taps" ]; then
    "$taps" --tsv | awk -v t="$t" '{print t "\ttap\t" $0}' >>"$out/system.tsv"
  fi
  sleep 9
done
wait
echo "done: $out"
