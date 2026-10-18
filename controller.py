"""
Store controller (HMI) — the legitimate operator system. Runs on Conner's laptop.

This is the normal traffic Cold Guard should always allow. It connects to the
watchdog (never straight to the PLC), reads the case temperature once a second,
and cycles the compressor to hold the setpoint. Every so often it runs a short,
spaced-out defrost, exactly the kind of well-behaved pattern the rules expect.

Run:  python controller.py            (reads profile.json for addresses)

Note: this connects to the watchdog's "listen" address. In the demo it must
bind to an allowed writer IP (profile.json -> allowed_writers). On one machine
that means a loopback alias; across the switch it is just this laptop's IP.
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
IR_CASE_TEMP = 0

SETPOINT = -200          # -20.0 C in tenths
HYSTERESIS = 10          # +/- 1.0 C band around the setpoint
DEFROST_EVERY = 150      # seconds between defrosts (safely over the rule's limit)
DEFROST_LENGTH = 8       # seconds a defrost runs


def to_signed(v):
    return v - 65536 if v > 32767 else v


def connect():
    # Dial Cold Guard, never the PLC directly. "watchdog" is the address
    # clients use to reach it (e.g. 192.168.50.2:502 on the demo network).
    host, port = PROFILE["watchdog"].rsplit(":", 1)
    return ModbusTcpClient(host, port=int(port))


def main():
    client = connect()
    if not client.connect():
        print("controller: could not reach the watchdog")
        return
    print("Store controller running (normal operations)", flush=True)

    # Make sure the setpoint is sane at startup.
    client.write_register(HR_SETPOINT, SETPOINT & 0xFFFF)

    last_defrost = time.monotonic()
    defrosting_until = 0.0

    try:
        while True:
            temp = to_signed(client.read_input_registers(IR_CASE_TEMP, count=1).registers[0])
            now = time.monotonic()

            # Periodic, short defrost cycle.
            if not defrosting_until and now - last_defrost > DEFROST_EVERY:
                client.write_coil(CO_DEFROST, True)
                client.write_coil(CO_COMPRESSOR, False)
                defrosting_until = now + DEFROST_LENGTH
                last_defrost = now
                print(f"  controller: defrost start (case {temp/10:.1f}C)", flush=True)
            elif defrosting_until and now >= defrosting_until:
                client.write_coil(CO_DEFROST, False)
                defrosting_until = 0.0
                print("  controller: defrost end", flush=True)
            elif not defrosting_until:
                # Normal thermostat: hold the setpoint within the band.
                if temp > SETPOINT + HYSTERESIS:
                    client.write_coil(CO_COMPRESSOR, True)
                elif temp < SETPOINT - HYSTERESIS:
                    client.write_coil(CO_COMPRESSOR, False)

            print(f"  controller: case {temp/10:.1f}C  setpoint {SETPOINT/10:.1f}C", end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStore controller stopped.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
