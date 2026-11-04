// Measures how late input events reach the end of the macOS event pipeline.
//
// A listen-only tap at the annotated-session location sees each event after every other
// app's tap has released it, so (now - event timestamp) is how long the event took from
// the hardware to here. A once-a-second high-priority timer also records how late it
// fires, which shows scheduler stalls even when nobody is touching the mouse.
//
// Records timing only: never which keys were pressed or where the pointer was.
//
// Usage: input-latency [--seconds N] [--keys]
//   --seconds N  stop after N seconds (default: run until killed)
//   --keys       also time keystrokes. Needs Input Monitoring permission for the app that
//                launched this (System Settings > Privacy & Security > Input Monitoring).
// Output, tab-separated, one line per second:
//   <epoch> tick <late_ms>
//   <epoch> mouse|keys <count> <p50_ms> <p95_ms> <max_ms>    (only for seconds with events)
import CoreGraphics
import Darwin
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

var seconds = 0.0
var withKeys = false
var args = CommandLine.arguments.dropFirst()
while let arg = args.popFirst() {
    switch arg {
    case "--seconds": seconds = Double(args.popFirst() ?? "") ?? 0
    case "--keys": withKeys = true
    default: fail("unknown argument: \(arg)")
    }
}

pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
setvbuf(stdout, nil, _IOLBF, 0)

var timebase = mach_timebase_info_data_t()
mach_timebase_info(&timebase)

// Event timestamps are documented as nanoseconds since boot, but some machines report
// mach ticks instead. Whichever reading gives a plausible age is the right one.
func ageMs(_ ts: CGEventTimestamp) -> Double? {
    let limitMs = 120_000.0
    let nowNs = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
    if nowNs >= ts, Double(nowNs - ts) / 1e6 < limitMs {
        return Double(nowNs - ts) / 1e6
    }
    let nowTicks = mach_absolute_time()
    if nowTicks >= ts {
        let ms = Double(nowTicks - ts) * Double(timebase.numer) / Double(timebase.denom) / 1e6
        if ms < limitMs { return ms }
    }
    return nil
}

var mouseMs: [Double] = []
var keyMs: [Double] = []
var tapRef: CFMachPort?

let mouseTypes: [CGEventType] = [.mouseMoved, .leftMouseDown, .leftMouseUp, .rightMouseDown,
                                 .rightMouseUp, .leftMouseDragged, .rightMouseDragged, .scrollWheel]
let keyTypes: [CGEventType] = [.keyDown, .flagsChanged]
func maskOf(_ types: [CGEventType]) -> CGEventMask {
    types.reduce(0) { $0 | (CGEventMask(1) << $1.rawValue) }
}

var mask = maskOf(mouseTypes)
if withKeys {
    if CGPreflightListenEventAccess() {
        mask |= maskOf(keyTypes)
    } else {
        CGRequestListenEventAccess()
        FileHandle.standardError.write("""
            keystroke timing needs Input Monitoring permission for the app running this
            (System Settings > Privacy & Security > Input Monitoring). Allow it, then restart
            the recording. Timing mouse events only for now.\n
            """.data(using: .utf8)!)
    }
}

let callback: CGEventTapCallBack = { _, type, event, _ in
    switch type {
    case .tapDisabledByTimeout, .tapDisabledByUserInput:
        if let tap = tapRef { CGEvent.tapEnable(tap: tap, enable: true) }
    case .keyDown, .flagsChanged:
        if let ms = ageMs(event.timestamp) { keyMs.append(ms) }
    default:
        if let ms = ageMs(event.timestamp) { mouseMs.append(ms) }
    }
    return Unmanaged.passUnretained(event)
}

guard let tap = CGEvent.tapCreate(tap: .cgAnnotatedSessionEventTap, place: .tailAppendEventTap,
                                  options: .listenOnly, eventsOfInterest: mask,
                                  callback: callback, userInfo: nil) else {
    fail("could not create an event tap")
}
tapRef = tap
CFRunLoopAddSource(CFRunLoopGetCurrent(), CFMachPortCreateRunLoopSource(nil, tap, 0), .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)

func summary(_ xs: [Double]) -> (p50: Double, p95: Double, max: Double) {
    let s = xs.sorted()
    let at = { (p: Double) in s[min(s.count - 1, Int(Double(s.count) * p))] }
    return (at(0.5), at(0.95), s[s.count - 1])
}

let start = Date()
var expected = start.addingTimeInterval(1)
let timer = Timer(fire: expected, interval: 1, repeats: true) { _ in
    let now = Date()
    let lateMs = max(0, now.timeIntervalSince(expected) * 1000)
    expected = expected.addingTimeInterval(1)
    if now.timeIntervalSince(expected) > 1 { expected = now.addingTimeInterval(1) }  // after sleep
    let t = Int(now.timeIntervalSince1970)
    print(String(format: "%d\ttick\t%.1f", t, lateMs))
    for (kind, xs) in [("mouse", mouseMs), ("keys", keyMs)] where !xs.isEmpty {
        let s = summary(xs)
        print(String(format: "%d\t%@\t%d\t%.1f\t%.1f\t%.1f", t, kind, xs.count, s.p50, s.p95, s.max))
    }
    mouseMs.removeAll(keepingCapacity: true)
    keyMs.removeAll(keepingCapacity: true)
    if seconds > 0 && now.timeIntervalSince(start) >= seconds { exit(0) }
}
timer.tolerance = 0
RunLoop.current.add(timer, forMode: .common)
CFRunLoopRun()
