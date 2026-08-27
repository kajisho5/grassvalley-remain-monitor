#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SW GRASSVALLEY MONITOR  v0.1.0
Grass Valley T2 / T3 シリーズ放送用ビデオサーバーを LAN 経由で監視する
読み取り専用モニター(試験版)。

*** リポジトリ未作成のため、このファイルはローカル準備のみ (GitHub未push) ***

VDCP (Video Disk Control Protocol) を TCP/IP 経由で使用します。T2 4K は
Configuration > Miscellaneous > AMP/VDCP > VDCP > Port Number でTCP/IPを
有効化できます(既定ポート 8000、変更後は T2 の再起動が必要)。

*** プロトコルの確度について ***
VDCP はグラスバレー公式のPDFには framing/checksum の詳細が無く(製品設定ガイド
のみ)、放送業界標準として広く使われている Imagine Communications 社の
"VDCP Protocol Guide" と DVS社の実装リファレンスから、バイト単位の仕様を
横断確認して実装しています。実機での動作確認はできていません。
詳細は README.md を参照してください。

このツールは Sense Request (状態問い合わせ) のみを送信します。Play/Stop 等の
制御コマンドは一切送信しません。

Usage:
    python sw_grassvalley_monitor.py                          # 設定ウィンドウ
    python sw_grassvalley_monitor.py --host 192.168.0.50 --port 8000 --console
    python sw_grassvalley_monitor.py --demo                    # ダミーデータで動作確認(UIのみ)
    python sw_grassvalley_monitor.py --try 192.168.0.50:8000   # 接続診断 + 生バイト表示
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
DEFAULT_PORT = 8000
DEFAULT_WEB_PORT = 8810
DEFAULT_FPS = 30  # T2/T3のプロジェクト設定に合わせて --fps で調整可
POLL_HZ = 4.0
CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "SEVENTHWELL", "sw_grassvalley_monitor.json")

BG, FG, DIM, LINE = "#050505", "#f0f0f0", "#6a6a6a", "#1e1e1e"

# VDCP command bytes (type nibble 3 = Sense Request; low nibble = unit address)
CMD_STATUS = 0x05        # 3X.05 PORT STATUS
CMD_POSITION = 0x06      # 3X.06 POSITION REQUEST
CMD_ACTIVE_ID = 0x07     # 3X.07 ACTIVE ID REQUEST
TIMETYPE_REMAIN = 0x00
TIMETYPE_TIMECODE = 0x01

STATUS_BITS = {
    0: "IDLE", 1: "CUE/INIT", 2: "PLAY/RECORD", 3: "STILL",
    4: "JOG", 5: "VARIABLE PLAY", 6: "PORT BUSY", 7: "CUE/INIT DONE",
}


# ---------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def fmt_hms(sec):
    if sec is None:
        return None
    sec = max(0, sec)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def _bcd_to_int(b):
    return (b >> 4) * 10 + (b & 0x0F)


# ---------------------------------------------------------------- VDCP wire protocol
class VDCPError(Exception):
    pass


def build_command(cmd1, cmd2, data=b""):
    """STX BC CMD1 CMD2 [DATA] CHECKSUM. BC counts CMD1..last data byte.
    CHECKSUM = two's complement of the low byte of the sum of those bytes."""
    body = bytes([cmd1, cmd2]) + data
    bc = len(body)
    checksum = (0x100 - (sum(body) & 0xFF)) & 0xFF
    return bytes([0x02, bc]) + body + bytes([checksum])


class VDCPClient:
    """
    Grass Valley T2/T3 VDCP-over-TCP client (Sense Requests only -- read-only).

    Framing/checksum/opcodes per the Imagine Communications "VDCP Protocol Guide"
    (broadcast-industry standard for this protocol lineage), cross-checked against
    a DVS Digital Video Systems implementer reference. NOT verified against real
    Grass Valley hardware -- see README "Confidence / what's verified".
    """

    def __init__(self, host, port=DEFAULT_PORT, unit=0, timeout=3.0):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise VDCPError("接続が切断されました")
            buf += chunk
        return buf

    def sense(self, cmd2, data=b""):
        """Send a type-3 (sense request) command and return the reply body
        (everything between CMD1/CMD2-echo and the checksum), raw bytes.
        Raises VDCPError on NAK, timeout, or checksum mismatch."""
        cmd1 = 0x30 | (self.unit & 0x0F)
        frame = build_command(cmd1, cmd2, data)
        self.sock.sendall(frame)

        first = self._recv_exact(1)
        if first == b"\x05":
            raise VDCPError("NAK (機体がコマンドを拒否しました)")
        if first != b"\x04":
            raise VDCPError("想定外の応答開始バイト: 0x%02X" % first[0])
        bc = self._recv_exact(1)[0]
        body = self._recv_exact(bc)
        checksum = self._recv_exact(1)[0]
        expect = (0x100 - (sum(body) & 0xFF)) & 0xFF
        if checksum != expect:
            raise VDCPError("チェックサム不一致 (受信 0x%02X, 期待 0x%02X) -- "
                            "応答の解釈がずれている可能性があります" % (checksum, expect))
        # body[0:2] はコマンドのエコー(CMD1, CMD2|0x80)、以降がデータ
        return body[2:], body

    def get_status(self):
        raw, _ = self.sense(CMD_STATUS, b"\x00")
        if not raw:
            raise VDCPError("PORT STATUS: データが空です")
        bits = raw[0]
        flags = {name: bool(bits & (1 << i)) for i, name in STATUS_BITS.items()}
        return flags

    def get_position(self, timetype=TIMETYPE_REMAIN):
        raw, _ = self.sense(CMD_POSITION, bytes([timetype]))
        # raw = [timetype_echo, frames, sec, min, hours] の想定(BCD)。
        # 実機での並び/桁数が未確認のため、長さに応じて緩く解釈する。
        if len(raw) < 5:
            raise VDCPError("POSITION REQUEST: 応答が短すぎます (%d bytes)" % len(raw))
        _echo, frames, sec, minute, hours = raw[0], raw[1], raw[2], raw[3], raw[4]
        return {
            "hours": _bcd_to_int(hours), "minutes": _bcd_to_int(minute),
            "seconds": _bcd_to_int(sec), "frames": _bcd_to_int(frames),
        }

    def get_active_id(self):
        raw, _ = self.sense(CMD_ACTIVE_ID)
        if not raw:
            return False, ""
        active = bool(raw[0])
        name = raw[1:].split(b"\x00")[0].decode("ascii", "replace").strip()
        return active, name


def tc_to_seconds(tc, fps):
    return tc["hours"] * 3600 + tc["minutes"] * 60 + tc["seconds"] + tc["frames"] / float(fps)


# ---------------------------------------------------------------- monitor
class Monitor(threading.Thread):
    daemon = True

    def __init__(self, host, port, unit=0, fps=DEFAULT_FPS, demo=False):
        super().__init__()
        self.host, self.port, self.unit, self.fps, self.demo = host, port, unit, fps, demo
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.rev = 0
        self._state = {"connected": False, "error": None, "name": None,
                        "state": "NO LINK", "elapsed": None, "remain": None,
                        "duration": None, "host": host, "port": port}
        self._demo_t0 = time.monotonic()

    def stop(self):
        self._stop.set()

    def set_target(self, host, port):
        with self._lock:
            self.host, self.port = host, port
            self._state["host"], self._state["port"] = host, port

    def snapshot(self):
        with self._lock:
            d = dict(self._state)
        d["rev"] = self.rev
        return d

    def _publish(self, **kw):
        with self._lock:
            self._state.update(kw)
            self.rev += 1

    def run(self):
        if self.demo:
            self._run_demo()
            return
        client = None
        while not self._stop.is_set():
            try:
                if client is None:
                    client = VDCPClient(self.host, self.port, self.unit)
                    client.connect()
                status = client.get_status()
                remain_tc = client.get_position(TIMETYPE_REMAIN)
                active, name = client.get_active_id()
                remain = tc_to_seconds(remain_tc, self.fps)
                state = ("PLAY/RECORD" if status.get("PLAY/RECORD") else
                          "CUE/INIT" if status.get("CUE/INIT") else
                          "STILL" if status.get("STILL") else
                          "IDLE" if status.get("IDLE") else "UNKNOWN")
                self._publish(connected=True, error=None, name=name if active else None,
                              state=state, elapsed=None, remain=remain, duration=None)
            except (VDCPError, OSError, socket.timeout) as e:
                self._publish(connected=False, error=str(e), state="NO LINK",
                              elapsed=None, remain=None, duration=None)
                if client:
                    client.close()
                client = None
                time.sleep(1.0)  # 再接続まで少し待つ
                continue
            time.sleep(1.0 / POLL_HZ)
        if client:
            client.close()

    def _run_demo(self):
        """UI-only simulated data -- does NOT speak the real VDCP wire protocol."""
        dur = 180.0
        while not self._stop.is_set():
            t = (time.monotonic() - self._demo_t0) % (dur + 5)
            if t < dur:
                self._publish(connected=True, error=None, name="DEMO_CLIP_A",
                              state="PLAY/RECORD", elapsed=t, remain=dur - t, duration=dur)
            else:
                self._publish(connected=True, error=None, name="DEMO_CLIP_A",
                              state="IDLE", elapsed=0, remain=None, duration=dur)
            time.sleep(1.0 / POLL_HZ)


# ---------------------------------------------------------------- web UI
HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SW GRASSVALLEY MONITOR</title>
<style>
  :root{--bg:#050505;--fg:#f0f0f0;--dim:#6a6a6a;--line:#1e1e1e;--warn:#e0a020;--crit:#e03a2f}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--fg);overflow:hidden;
    font-family:Consolas,"SF Mono",Menlo,ui-monospace,"MS Gothic",monospace;-webkit-font-smoothing:antialiased}
  .app{display:flex;flex-direction:column;height:100%}
  header{display:flex;align-items:center;flex-wrap:wrap;gap:10px 18px;padding:10px 18px;
    border-bottom:1px solid var(--line);flex:0 0 auto}
  .brand{letter-spacing:.34em;font-size:12px;text-transform:uppercase}
  .ver{color:var(--dim);letter-spacing:.2em;font-size:10px}
  .spacer{flex:1}
  .conn{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:.14em;color:var(--dim)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--crit);box-shadow:0 0 8px currentColor}
  .dot.ok{background:var(--fg)}
  input,button{font:inherit;font-size:11px;background:transparent;color:var(--fg);
    border:1px solid var(--line);padding:5px 9px;letter-spacing:.1em}
  input{width:170px} input.port{width:70px} input.unit{width:40px} button{cursor:pointer}
  button:hover{border-color:var(--fg)}
  main{flex:1;display:flex;min-height:0;padding:20px 26px 16px;flex-direction:column}
  .hero{flex:1;display:flex;align-items:center;gap:34px;min-height:0}
  .big{flex:1 1 0;min-width:0;text-align:center;display:flex;flex-direction:column;justify-content:center}
  .big .lab{font-size:11px;letter-spacing:.42em;color:var(--dim);margin-bottom:10px}
  .big .val{font-size:min(18vw,120px);line-height:.92;font-weight:700;font-variant-numeric:tabular-nums;
    letter-spacing:-.015em;white-space:nowrap}
  .big.warn .val{color:var(--warn)} .big.crit .val{color:var(--crit)}
  .side{flex:0 0 auto;width:clamp(190px,23vw,320px);display:flex;flex-direction:column;gap:18px}
  .side .lab{font-size:10px;letter-spacing:.32em;color:var(--dim);margin-bottom:5px}
  .side .val{font-size:clamp(19px,2.7vw,36px);font-variant-numeric:tabular-nums;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .side .txt{font-size:15px;letter-spacing:.04em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  @media (max-width:1000px){.hero{flex-direction:column;justify-content:center;gap:14px}
    .side{width:100%;flex-direction:row;flex-wrap:wrap;gap:12px 30px}.side>div{min-width:150px}}
  .msg{color:var(--dim);font-size:12px;letter-spacing:.16em;text-align:center;padding:36px}
  .msg b{color:var(--crit)}
  .badge{display:inline-block;font-size:10px;letter-spacing:.2em;color:var(--warn);
    border:1px solid var(--warn);padding:4px 10px;white-space:nowrap;flex:0 0 auto}
  body.focus header,body.focus .badge{display:none} body.focus main{padding:0}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">GRASSVALLEY T2/T3 MONITOR</div>
    <div class="ver">v__VER__</div>
    <div class="spacer"></div>
    <div class="conn"><span id="dot" class="dot"></span><span id="cstat">NO LINK</span></div>
    <input id="host" placeholder="192.168.0.50">
    <input id="port" class="port" placeholder="8000">
    <input id="unit" class="unit" placeholder="0" title="Unit address">
    <button id="apply">接続</button>
    <button id="full">全画面 (F)</button>
  </header>
  <main>
    <div class="hero" id="hero">
      <div class="big" id="remainBox">
        <div class="lab">REMAIN</div>
        <div class="val" id="remainVal">--:--:--</div>
      </div>
      <div class="side">
        <div><div class="lab">CLIP</div><div class="txt" id="nowname">—</div></div>
        <div><div class="lab">STATE</div><div class="val" id="mode">—</div></div>
      </div>
    </div>
    <div class="msg" id="msg" style="display:none"></div>
    <div class="badge">VDCP実機未検証 — README参照</div>
  </main>
</div>
<script>
function hms(sec){
  if(sec===null||sec===undefined||isNaN(sec)) return "--:--:--";
  sec=Math.max(0,sec);
  const p=n=>String(n).padStart(2,"0");
  return p(Math.floor(sec/3600))+":"+p(Math.floor(sec%3600/60))+":"+p(Math.floor(sec%60));
}
let S=null;
function render(){
  const dot=document.getElementById("dot"), cstat=document.getElementById("cstat");
  const msg=document.getElementById("msg"), hero=document.getElementById("hero");
  const ok=S&&S.connected;
  dot.className="dot"+(ok?" ok":"");
  cstat.textContent=S?((S.host||"")+":"+(S.port||"")):"NO LINK";
  if(!ok){
    msg.style.display="block"; hero.style.display="none";
    msg.innerHTML="接続できません &nbsp;<b>"+((S&&S.error)||"")+"</b><br><br>"+
      "T2/T3 の Configuration &gt; Miscellaneous &gt; AMP/VDCP で TCP/IP の VDCP を"+
      "有効化し、ポート番号を確認してください(設定変更後は再起動が必要です)。";
  } else {
    msg.style.display="none"; hero.style.display="";
    const rem=S.remain;
    document.getElementById("remainVal").textContent=hms(rem);
    const box=document.getElementById("remainBox");
    box.className="big"+(rem===null?"":rem<=10?" crit":rem<=60?" warn":"");
    document.getElementById("nowname").textContent=S.name||"—";
    document.getElementById("mode").textContent=S.state||"—";
  }
}
async function poll(){
  try{ const r=await fetch("/api/state",{cache:"no-store"}); S=await r.json(); }
  catch(e){ S=null; }
  render();
}
setInterval(poll,300); poll();
document.getElementById("apply").onclick=async()=>{
  const host=document.getElementById("host").value.trim()||document.getElementById("host").placeholder;
  const port=document.getElementById("port").value.trim()||document.getElementById("port").placeholder;
  await fetch("/api/connect",{method:"POST",body:JSON.stringify({host,port:parseInt(port)})});
  poll();
};
document.getElementById("full").onclick=()=>{
  document.body.classList.toggle("focus");
  if(document.body.classList.contains("focus")&&!document.fullscreenElement)
    document.documentElement.requestFullscreen().catch(()=>{});
  else if(document.fullscreenElement) document.exitFullscreen().catch(()=>{});
};
addEventListener("keydown",e=>{
  if(e.key==="f"||e.key==="F") document.getElementById("full").onclick();
  if(e.key==="Escape") document.body.classList.remove("focus");
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    monitor = None
    server_version = "SWGrassValleyMonitor/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(self.monitor.snapshot()), "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            self._send(200, HTML.replace("__VER__", VERSION), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if self.path == "/api/connect":
            host, port = body.get("host"), int(body.get("port") or DEFAULT_PORT)
            if host:
                self.monitor.set_target(host, port)
                save_config({"host": host, "port": port})
            self._send(200, '{"ok":true}', "application/json")
        else:
            self._send(404, "not found", "text/plain")


def has_tkinter():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def run_gui(host, port, unit, fps, web_port, demo):
    if not has_tkinter():
        return False
    import tkinter as tk

    cfg = load_config()
    root = tk.Tk()
    root.title("SW GRASSVALLEY MONITOR v" + VERSION)
    root.configure(bg=BG)
    root.geometry("600x400")
    mono = ("Consolas", 10)
    state = {"httpd": None, "monitor": None, "web_port": web_port}

    def lab(parent, text, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=kw.pop("fg", FG), font=kw.pop("font", mono), **kw)

    wrap = tk.Frame(root, bg=BG, padx=22, pady=18)
    wrap.pack(fill="both", expand=True)
    lab(wrap, "SW GRASSVALLEY MONITOR", font=("Consolas", 12, "bold")).pack(anchor="w")
    lab(wrap, "v" + VERSION + "  (VDCP実機未検証)", fg="#e0a020", font=("Consolas", 8)).pack(anchor="w", pady=(0, 12))

    row = tk.Frame(wrap, bg=BG); row.pack(fill="x")
    for i, t in enumerate(["T2/T3 の IP", "PORT", "Unit"]):
        lab(row, t, fg=DIM, font=("Consolas", 9)).grid(row=0, column=i, sticky="w",
                                                        padx=(0 if i == 0 else 12, 0))
    host_var = tk.StringVar(value=host or cfg.get("host", ""))
    port_var = tk.StringVar(value=str(port))
    unit_var = tk.StringVar(value=str(unit))
    for col, var, w in ((0, host_var, 20), (1, port_var, 8), (2, unit_var, 5)):
        tk.Entry(row, textvariable=var, width=w, font=mono, bg="#101010", fg=FG,
                 insertbackground=FG, relief="flat").grid(row=1, column=col, sticky="w",
                                                          padx=(0 if col == 0 else 12, 0), pady=(3, 0))

    status = lab(wrap, "待機中", fg=DIM)
    warn = lab(wrap, "T2/T3 側で Configuration > Miscellaneous > AMP/VDCP を\n"
                     "TCP/IP・有効に設定してください(変更後は再起動が必要)。",
               fg=DIM, font=("Consolas", 8), justify="left")

    def open_ui():
        if state["httpd"]:
            webbrowser.open("http://localhost:%d/" % state["web_port"])

    def start(open_browser=True):
        h = host_var.get().strip()
        if not h and not demo:
            status.configure(text="T2/T3 の IP を入力してください", fg="#e0a020")
            return
        try:
            p = int(port_var.get().strip() or DEFAULT_PORT)
            u = int(unit_var.get().strip() or 0)
        except ValueError:
            p, u = DEFAULT_PORT, 0
        save_config({"host": h, "port": p, "unit": u})
        if state["monitor"] is None:
            mon = Monitor(h or "demo", p, unit=u, fps=fps, demo=demo)
            mon.start()
            Handler.monitor = mon
            state["monitor"] = mon
            wp = state["web_port"]
            for cand in range(wp, wp + 20):
                try:
                    state["httpd"] = ThreadingHTTPServer(("0.0.0.0", cand), Handler)
                    state["web_port"] = cand
                    break
                except OSError:
                    continue
            threading.Thread(target=state["httpd"].serve_forever, daemon=True).start()
        else:
            state["monitor"].set_target(h, p)
        status.configure(text="起動しました  http://localhost:%d/" % state["web_port"], fg=FG)
        if open_browser:
            open_ui()

    btns = tk.Frame(wrap, bg=BG); btns.pack(fill="x", pady=(16, 0))
    tk.Button(btns, text="接続してモニター起動", command=start, font=mono, bg="#111111", fg=FG,
              relief="flat", padx=14, pady=6, cursor="hand2",
              highlightthickness=1, highlightbackground=LINE).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="ブラウザで開く", command=open_ui, font=mono, bg="#111111", fg=FG,
              relief="flat", padx=14, pady=6, cursor="hand2",
              highlightthickness=1, highlightbackground=LINE).pack(side="left")
    status.pack(anchor="w", pady=(16, 2))
    warn.pack(anchor="w", pady=(8, 0))
    root.after(300, lambda: start(open_browser=True) if (host_var.get().strip() or demo) else None)
    root.mainloop()
    os._exit(0)


def try_connect(target, unit, fps):
    host, _, ps = target.partition(":")
    port = int(ps) if ps.isdigit() else DEFAULT_PORT
    print("SW GRASSVALLEY MONITOR v%s  接続診断  %s:%d (unit %d)" % (VERSION, host, port, unit))
    client = VDCPClient(host, port, unit, timeout=5.0)
    try:
        client.connect()
        print("TCP接続: OK")
        status = client.get_status()
        print("PORT STATUS: %s" % status)
        raw_pos, body_pos = client.sense(CMD_POSITION, bytes([TIMETYPE_REMAIN]))
        print("POSITION REQUEST 生バイト: %s" % body_pos.hex(" "))
        pos = client.get_position(TIMETYPE_REMAIN)
        print("  解釈結果 (BCD前提, 未検証): %02d:%02d:%02d:%02d" %
              (pos["hours"], pos["minutes"], pos["seconds"], pos["frames"]))
        raw_id, body_id = client.sense(CMD_ACTIVE_ID)
        print("ACTIVE ID REQUEST 生バイト: %s" % body_id.hex(" "))
        active, name = client.get_active_id()
        print("  解釈結果: active=%s name=%r" % (active, name))
        print("\n→ 生バイトと解釈結果が食い違う場合、get_position()/get_active_id() の")
        print("  オフセットを実機の応答に合わせて書き換えてください。")
    except (VDCPError, OSError, socket.timeout) as e:
        print("エラー: %s" % e)
        print("→ VDCPがTCP/IPで有効か、ポート番号、Unit Address(既定0)を確認してください。")
    finally:
        client.close()


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="SW GRASSVALLEY MONITOR v" + VERSION)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=cfg.get("port", DEFAULT_PORT))
    ap.add_argument("--unit", type=int, default=cfg.get("unit", 0), help="VDCP Unit Address (既定0)")
    ap.add_argument("--fps", type=float, default=cfg.get("fps", DEFAULT_FPS))
    ap.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--try", dest="try_target", metavar="IP:PORT", help="接続診断 + 生バイト表示")
    args = ap.parse_args()

    if args.try_target:
        try_connect(args.try_target, args.unit, args.fps)
        return

    host = args.host or cfg.get("host") or ("demo" if args.demo else "")

    want_gui = (args.gui or (len(sys.argv) == 1 and not args.console)) and not args.demo
    if want_gui:
        if run_gui(host, args.port, args.unit, args.fps, args.web_port, args.demo) is not False:
            return
        args.no_browser = False

    mon = Monitor(host or "127.0.0.1", args.port, unit=args.unit, fps=args.fps, demo=args.demo)
    mon.start()
    Handler.monitor = mon
    httpd = ThreadingHTTPServer(("0.0.0.0", args.web_port), Handler)
    url = "http://localhost:%d/" % args.web_port
    print("=" * 58)
    print(" SW GRASSVALLEY MONITOR  v%s %s" % (VERSION, "(DEMO)" if args.demo else ""))
    print(" T2/T3    : %s:%s (unit %d)" % (host, args.port, args.unit))
    print(" UI       : %s" % url)
    print(" 終了     : Ctrl+C")
    print(" *** VDCP実機未検証: README.md の「確度について」を参照 ***")
    print("=" * 58)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        mon.stop()


if __name__ == "__main__":
    main()
