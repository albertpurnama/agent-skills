// Lists macOS event taps (CGEventTap), grouped by the process that installed them.
//
// An enabled tap that is not listen-only is "blocking": every matching keystroke or
// mouse event waits for that process's callback before it reaches the focused app.
// macOS disables a tap whose callback times out, so a process holding many disabled
// taps has usually been stalling input and re-creating its tap each time.
//
// macOS also reports a per-tap "latency" figure. It isn't shown here because it doesn't
// behave like a delay: it tracks how long since the tap last saw an event (it freezes
// when nothing moves and climbs while other input flows). To measure how late keystrokes
// actually arrive, use record.sh --keys.
//
// Usage: event-taps [--tsv]
//   --tsv  one tab-separated row per process: process, pid, taps, disabled, blocking, watches
import CoreGraphics
import Darwin
import Foundation

let disabledTapPile = 3

func processName(_ pid: pid_t) -> String {
    var buf = [CChar](repeating: 0, count: 4096)
    guard proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 else { return "pid \(pid)" }
    return URL(fileURLWithPath: String(cString: buf)).lastPathComponent
}

func pad(_ s: String, _ width: Int) -> String {
    s.count >= width ? s : s + String(repeating: " ", count: width - s.count)
}

var count: UInt32 = 0
guard CGGetEventTapList(0, nil, &count) == .success else {
    print("CGGetEventTapList failed")
    exit(1)
}
var taps = [CGEventTapInformation](repeating: CGEventTapInformation(), count: Int(count))
CGGetEventTapList(count, &taps, &count)

let keyboardMask: UInt64 = (1 << 10) | (1 << 11) | (1 << 12)  // keyDown, keyUp, flagsChanged
let mouseMask: UInt64 = [1, 2, 3, 4, 5, 6, 7, 22, 25, 26, 27].reduce(0) { $0 | (1 << $1) }

struct Summary {
    var total = 0, enabled = 0, blocking = 0
    var keys = false, mouse = false
}

var byPid: [pid_t: Summary] = [:]
for tap in taps.prefix(Int(count)) {
    var s = byPid[tap.tappingProcess, default: Summary()]
    s.total += 1
    if tap.enabled {
        s.enabled += 1
        if tap.options.rawValue & CGEventTapOptions.listenOnly.rawValue == 0 {
            s.blocking += 1
            s.keys = s.keys || tap.eventsOfInterest & keyboardMask != 0
            s.mouse = s.mouse || tap.eventsOfInterest & mouseMask != 0
        }
    }
    byPid[tap.tappingProcess] = s
}

let rows = byPid
    .map { (pid: $0.key, name: processName($0.key), s: $0.value) }
    .sorted { ($0.s.total - $0.s.enabled, $0.s.blocking) > ($1.s.total - $1.s.enabled, $1.s.blocking) }

func watches(_ s: Summary) -> String {
    if s.blocking == 0 { return "-" }
    let parts = [s.keys ? "keys" : nil, s.mouse ? "mouse" : nil].compactMap { $0 }
    return parts.isEmpty ? "other" : parts.joined(separator: "+")
}

if CommandLine.arguments.contains("--tsv") {
    for r in rows {
        print([r.name, "\(r.pid)", "\(r.s.total)", "\(r.s.total - r.s.enabled)", "\(r.s.blocking)", watches(r.s)]
            .joined(separator: "\t"))
    }
    exit(0)
}

print(pad("PROCESS", 30) + pad("PID", 8) + pad("TAPS", 6) + pad("DISABLED", 10) + pad("BLOCKING", 10) + "HOLDS")
for r in rows {
    print(pad(r.name, 30) + pad("\(r.pid)", 8) + pad("\(r.s.total)", 6)
          + pad("\(r.s.total - r.s.enabled)", 10) + pad("\(r.s.blocking)", 10) + watches(r.s))
}

var warned = false
for r in rows where r.s.total - r.s.enabled >= disabledTapPile {
    print("! \(r.name) (\(r.pid)): \(r.s.total - r.s.enabled) disabled taps piled up; it has likely been timing out on input. Restart it.")
    warned = true
}
if !warned { print("no leaked taps") }
let holders = rows.filter { $0.s.keys }.map { $0.name }
if !holders.isEmpty {
    print("apps holding every keystroke: \(holders.joined(separator: ", ")). To check whether one delays typing, run record.sh --keys.")
}
