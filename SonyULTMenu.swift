import Cocoa
import IOBluetooth

// ── Configuration ──────────────────────────────────────────────────

private let kDeviceMAC  = "88-92-CC-08-7B-89"
private let kChannel: UInt8 = 18
private let kPollSec: TimeInterval = 5
private let kRefreshSec: TimeInterval = 15

private let kSounds: [String: String] = [
    "nc": "Glass", "ambient": "Tink", "off": "Pop", "ult": "Submarine",
]

// ── Sony V2 framing ───────────────────────────────────────────────

private let SOF: UInt8 = 0x3E, SEOF: UInt8 = 0x3C, SESC: UInt8 = 0x3D
private let MDR: UInt8 = 0x0C, ACK: UInt8 = 0x01

private func chk(_ d: [UInt8]) -> UInt8 {
    UInt8(truncatingIfNeeded: d.reduce(0) { $0 &+ Int($1) })
}

private func esc(_ d: [UInt8]) -> [UInt8] {
    var o: [UInt8] = []
    for b in d {
        if b == SOF || b == SEOF || b == SESC { o.append(SESC); o.append(b & 0xEF) }
        else { o.append(b) }
    }
    return o
}

private func unesc(_ d: [UInt8]) -> [UInt8] {
    var o: [UInt8] = []; var i = 0
    while i < d.count {
        if d[i] == SESC && i + 1 < d.count { o.append(d[i+1] | 0x10); i += 2 }
        else { o.append(d[i]); i += 1 }
    }
    return o
}

private func buildPkt(_ dt: UInt8, _ sq: UInt8, _ pl: [UInt8]) -> Data {
    let len = UInt32(pl.count)
    var inner: [UInt8] = [
        dt, sq,
        UInt8(len >> 24 & 0xFF), UInt8(len >> 16 & 0xFF),
        UInt8(len >> 8 & 0xFF),  UInt8(len & 0xFF),
    ]
    inner += pl
    inner.append(chk(inner))
    return Data([SOF] + esc(inner) + [SEOF])
}

struct Pkt {
    let type: UInt8, seq: UInt8, payload: [UInt8]
}

private func extract(_ buf: inout [UInt8]) -> [Pkt] {
    var pkts: [Pkt] = []
    while true {
        guard let s = buf.firstIndex(of: SOF) else { buf.removeAll(); break }
        guard let e = buf[(s+1)...].firstIndex(of: SEOF) else {
            buf.removeSubrange(..<s); break
        }
        let inner = unesc(Array(buf[(s+1)..<e]))
        buf.removeSubrange(...e)
        guard inner.count >= 7 else { continue }
        let plen = Int(inner[2]) << 24 | Int(inner[3]) << 16
                 | Int(inner[4]) << 8  | Int(inner[5])
        guard 6 + plen < inner.count else { continue }
        let pay = Array(inner[6 ..< 6+plen])
        if inner[6+plen] == chk(Array(inner[0 ..< 6+plen])) {
            pkts.append(Pkt(type: inner[0], seq: inner[1], payload: pay))
        }
    }
    return pkts
}

// ── RFCOMM controller ─────────────────────────────────────────────

struct ANCState { let enabled: Bool, ambient: Bool, voiceFocus: Bool }
struct ULTState { let mode: Int }
struct BatState { let level: Int, charging: Bool }

class Controller: NSObject {
    private var ch: IOBluetoothRFCOMMChannel?
    private var rxBuf: [UInt8] = []
    private var rxPkts: [Pkt] = []
    private var up = false
    private var seq: UInt8 = 0

    func connect() -> Bool {
        guard let dev = IOBluetoothDevice(addressString: kDeviceMAC) else { return false }
        var rfch: IOBluetoothRFCOMMChannel?
        let r = dev.openRFCOMMChannelSync(&rfch, withChannelID: kChannel, delegate: self)
        guard r == 0 else { return false }
        pump(3) { self.up }
        guard up else { return false }
        ch = rfch
        _ = send([0x00, 0x00])
        return true
    }

    func close() { ch?.close(); ch = nil; up = false }

    func send(_ pl: [UInt8], timeout: TimeInterval = 2) -> [Pkt] {
        let pkt = buildPkt(MDR, seq, pl)
        seq = 1 - seq
        rxPkts.removeAll()
        _ = pkt.withUnsafeBytes { ptr in
            ch?.writeSync(UnsafeMutableRawPointer(mutating: ptr.baseAddress!),
                          length: UInt16(pkt.count))
        }
        pump(timeout) { self.rxPkts.contains { $0.type == MDR } }
        pump(0.15)
        var resp: [Pkt] = []
        for p in rxPkts where p.type == MDR {
            let a = buildPkt(ACK, 1 - p.seq, [])
            _ = a.withUnsafeBytes { ptr in
                self.ch?.writeSync(UnsafeMutableRawPointer(mutating: ptr.baseAddress!),
                                   length: UInt16(a.count))
            }
            resp.append(p)
        }
        rxPkts.removeAll()
        return resp
    }

    private func pump(_ sec: TimeInterval, until cond: (() -> Bool)? = nil) {
        let dl = Date(timeIntervalSinceNow: sec)
        while Date() < dl {
            if cond?() == true { return }
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
        }
    }

    // ── queries ──

    func ancStatus() -> ANCState? {
        for r in send([0x66, 0x17]) {
            let p = r.payload
            if p.count >= 7 && p[0] == 0x67 {
                return ANCState(enabled: p[3] != 0, ambient: p[4] != 0, voiceFocus: p[6] != 0)
            }
        }
        return nil
    }

    func ultStatus() -> ULTState? {
        for r in send([0x56, 0x03]) {
            let p = r.payload
            if p.count >= 5 && p[0] == 0x57 { return ULTState(mode: Int(p[3])) }
        }
        return nil
    }

    func battery() -> BatState? {
        for r in send([0x22, 0x00]) {
            let p = r.payload
            if p.count >= 4 && p[0] == 0x23 {
                return BatState(level: Int(p[2]), charging: p[3] != 0)
            }
        }
        return nil
    }

    // ── setters ──

    func setANC()                    { _ = send([0x68,0x17,0x01,0x01,0x00,0x02,0x00,0x00]) }
    func setAmbient(voice: Bool)     { _ = send([0x68,0x17,0x01,0x01,0x01,0x02, voice ? 0x01 : 0x00, 0x00]) }
    func setOff()                    { _ = send([0x68,0x17,0x01,0x00,0x00,0x02,0x00,0x00]) }
    func setULT(_ m: UInt8)          { _ = send([0x58,0x03,0x00,m,0x06,0x0A,0x0A,0x0A,0x0A,0x0A,0x0A]) }

    // ── RFCOMM delegate ──

    @objc func rfcommChannelOpenComplete(_ c: IOBluetoothRFCOMMChannel!,
                                         status s: IOReturn) {
        up = (s == 0); if up { ch = c }
    }
    @objc func rfcommChannelData(_ c: IOBluetoothRFCOMMChannel!,
                                 data ptr: UnsafeMutableRawPointer!,
                                 length len: Int) {
        rxBuf += Array(Data(bytes: ptr, count: len))
        rxPkts += extract(&rxBuf)
    }
    @objc func rfcommChannelClosed(_ c: IOBluetoothRFCOMMChannel!) {
        up = false; ch = nil
    }
}

// ── Menu bar app ──────────────────────────────────────────────────

internal class App: NSObject, NSApplicationDelegate {
    let si = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var ctl: Controller?
    var linked = false, busy = false, lastRefresh: TimeInterval = 0

    var deviceIt: NSMenuItem!, batIt: NSMenuItem!
    var ncIt: NSMenuItem!, ambIt: NSMenuItem!, voiceIt: NSMenuItem!, offIt: NSMenuItem!
    var uOffIt: NSMenuItem!, u1It: NSMenuItem!, u2It: NSMenuItem!

    func applicationDidFinishLaunching(_ n: Notification) {
        si.isVisible = false
        buildMenu()
        Timer.scheduledTimer(withTimeInterval: kPollSec, repeats: true) { [weak self] _ in
            self?.tick()
        }
        tick()
    }

    private func buildMenu() {
        let m = NSMenu()
        deviceIt = info(m, "Sony ULT WEAR")
        batIt    = info(m, "")
        m.addItem(.separator())
        ncIt    = act(m, "Noise Cancelling",       #selector(doNC))
        ambIt   = act(m, "Ambient Sound",           #selector(doAmb))
        voiceIt = act(m, "Ambient + Voice Focus",   #selector(doVoice))
        offIt   = act(m, "Off",                     #selector(doOff))
        m.addItem(.separator())
        uOffIt = act(m, "ULT OFF", #selector(doUOff))
        u1It   = act(m, "ULT 1",   #selector(doU1))
        u2It   = act(m, "ULT 2",   #selector(doU2))
        m.addItem(.separator())
        _ = act(m, "Quit", #selector(doQuit))
        si.menu = m
    }

    private func info(_ m: NSMenu, _ t: String) -> NSMenuItem {
        let i = m.addItem(withTitle: t, action: nil, keyEquivalent: "")
        i.isEnabled = false; return i
    }
    private func act(_ m: NSMenu, _ t: String, _ s: Selector) -> NSMenuItem {
        let i = m.addItem(withTitle: t, action: s, keyEquivalent: "")
        i.target = self; return i
    }

    // ── Connection ──

    private func tick() {
        guard !busy else { return }
        let dev = IOBluetoothDevice(addressString: kDeviceMAC)
        let btUp = dev?.isConnected() ?? false

        if btUp && !linked         { open() }
        else if !btUp && linked    { shut() }
        else if linked, ProcessInfo.processInfo.systemUptime - lastRefresh > kRefreshSec {
            refresh()
        }
    }

    private func open() {
        let c = Controller()
        guard c.connect() else { return }
        ctl = c; linked = true
        si.button?.title = "\u{1F3A7}"
        si.isVisible = true
        refresh()
    }

    private func shut() {
        ctl?.close(); ctl = nil; linked = false
        si.isVisible = false
    }

    // ── Refresh ──

    private func refresh() {
        lastRefresh = ProcessInfo.processInfo.systemUptime
        guard linked, let c = ctl else { return }

        if let b = c.battery() {
            batIt.title = "Battery: \(b.level)%\(b.charging ? " ⚡" : "")"
        }

        for i in [ncIt!, ambIt!, voiceIt!, offIt!] { i.state = .off }
        if let a = c.ancStatus() {
            if      !a.enabled        { offIt.state = .on }
            else if  a.ambient        { (a.voiceFocus ? voiceIt : ambIt).state = .on }
            else                      { ncIt.state = .on }
        }

        for i in [uOffIt!, u1It!, u2It!] { i.state = .off }
        if let u = c.ultStatus() { [uOffIt, u1It, u2It][u.mode]?.state = .on }
    }

    // ── Commands ──

    private func run(_ fn: (Controller) -> Void, _ snd: String) {
        guard !busy, linked, let c = ctl else { return }
        busy = true
        fn(c)
        NSSound(named: NSSound.Name(snd))?.play()
        refresh()
        busy = false
    }

    @objc func doNC()   { run({ $0.setANC() },               "Glass") }
    @objc func doAmb()  { run({ $0.setAmbient(voice: false) }, "Tink") }
    @objc func doVoice(){ run({ $0.setAmbient(voice: true) },  "Tink") }
    @objc func doOff()  { run({ $0.setOff() },                 "Pop") }
    @objc func doUOff() { run({ $0.setULT(0) },              "Submarine") }
    @objc func doU1()   { run({ $0.setULT(1) },              "Submarine") }
    @objc func doU2()   { run({ $0.setULT(2) },              "Submarine") }

    @objc func doQuit() { shut(); NSApp.terminate(nil) }
}

// ── Entry point ───────────────────────────────────────────────────

let nsApp = NSApplication.shared
nsApp.setActivationPolicy(.prohibited)
let appDelegate = App()
nsApp.delegate = appDelegate
nsApp.run()
