# SW Grass Valley Monitor

Read-only status monitor for Grass Valley T2/T3 series broadcast video servers.

**⚠️ Experimental — not yet tested against real T2/T3 hardware.** See
[Confidence / what's verified](#confidence--whats-verified) below.

A standalone tool that watches a T2/T3 server over LAN via VDCP and puts a big, readable
countdown on any screen in the room.

**Single Python file with no dependencies** (standard library only) and serves a browser UI,
so any device on the same network can watch the same numbers.

## How it works

T2/T3 servers are controlled via **VDCP** (Video Disk Control Protocol), a decades-old
broadcast-industry binary protocol (same lineage as Sony 9-pin VTR control), available over
TCP/IP as well as the traditional RS-422 serial link. This tool only ever sends **Sense
Requests** (read-only status queries) — it never sends Play/Stop/Record or any other
transport command.

It polls three VDCP sense commands:
- `3X.05` **PORT STATUS** — transport state (playing/idle/still/etc.)
- `3X.06` **POSITION REQUEST** (time-remaining variant) — REMAIN readout
- `3X.07` **ACTIVE ID REQUEST** — currently loaded clip name

## Setup (T2/T3 side)

`Configuration → Miscellaneous → AMP/VDCP → VDCP` — set to **TCP/IP** (or "both"), and note
the port number (default **8000**). **The server must be restarted** for a port change to
take effect.

## Quick start

```bash
python sw_grassvalley_monitor.py --host 192.168.0.50 --port 8000 --console

# Try it without hardware — fabricates plausible values for the UI only
python sw_grassvalley_monitor.py --demo

# Connection diagnostic: sends the sense requests once and prints raw bytes + parsed result
python sw_grassvalley_monitor.py --try 192.168.0.50:8000
```

On Windows, double-click `SW-GRASSVALLEY-MONITOR.bat` for a small settings window (no
console), or `SW-GRASSVALLEY-TEST.bat` to run the connection diagnostic.

## Confidence / what's verified

| Piece | Status | Source |
|---|---|---|
| Frame format `STX BC CMD1 CMD2 [DATA] CHECKSUM`, checksum = two's complement of the sum | **Confirmed against a worked example** | Imagine Communications' *VDCP Protocol Guide* (the modern steward of this protocol lineage); this repo's checksum function reproduces the guide's own worked example (`02 02 10 01 EF`) byte-for-byte — see the test in the script's history |
| Sense-reply framing (`ACK 0x04` + `BC` + body + checksum, vs. plain `NAK 0x05`) | **Confirmed**, cross-checked against a DVS Digital Video Systems implementer reference | Same source, corroborated independently |
| `3X.05` PORT STATUS bit layout | **Confirmed**, and independently corroborated in Grass Valley's own T2 4K VDCP configuration draft PDF | Imagine guide + T2 4K draft PDF |
| `3X.06` POSITION REQUEST (`SEND DATA 1 = 0x00` selects time-remaining) | **Confirmed** | Imagine guide |
| `3X.07` ACTIVE ID REQUEST (flag byte + ASCII name) | **Confirmed**, T2 4K compat table marks it supported | Imagine guide + T2 4K draft PDF |
| T2 4K default VDCP-over-Ethernet port 8000 | **Confirmed** | Grass Valley's own T2 4K VDCP configuration draft PDF |
| T3's default port | **Not confirmed** — the T3 datasheet was unreachable during research (blocked by a Cloudflare bot-check) | — pass `--port` explicitly if it differs from T2's |
| Frame rate for the BCD frames field | **Not confirmed** | Defaults to 30 via `--fps`; adjust to your server's actual project frame rate |
| **None of this has been exercised against a real T2/T3** | — | The wire protocol was unit-tested end-to-end against a hand-written fake VDCP server reproducing the documented framing, and produces correct output there. What's unverified is whether real hardware's replies match this documented shape exactly. |

Run `--try` against real hardware first — it prints the **raw response bytes** alongside the
parsed interpretation, so you can immediately tell if the byte layout needs adjusting.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Not affiliated with, endorsed by, or supported by Grass Valley. "Grass Valley", "T2", and "T3"
are trademarks of their respective owner. This tool only sends read-only VDCP sense requests,
never transport-control commands — but it has not been verified against real hardware. Test
thoroughly in your own rig before relying on this in a show.
