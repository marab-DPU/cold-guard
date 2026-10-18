"""
Store controller (HMI) — the legitimate operator system. Runs on Conner's laptop.

This is the normal traffic Cold Guard should always allow. It talks to the
watchdog (never straight to the PLC) and runs a closed loop once a second:

  * it holds the setpoint at a safe value, re-asserting it if something changed it
  * it turns OFF any defrost it did not start itself
  * it cycles the compressor to keep the case near the setpoint
  * on its own schedule it runs a short, spaced-out defrost

It only WRITES when a value is actually wrong, so in steady state it sends very
little traffic (well under the watchdog's rate limit). This is also what makes
the fridge recover on its own after an attack: while the watchdog is bypassed
the attacker keeps overriding it, but the moment Cold Guard is armed again the
attacker is blocked, the controller's corrections get through, and the case is
driven back to a safe temperature with no manual step.

Run:  python3 controller.py            (reads profile.json for addresses)
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

SETPOINT = -200          # -20.0 C in tenths, the safe target
HYSTERESIS = 10          # +/- 1.0 C band around the setpoint
DEFROST_EVERY = 150      # seconds between the controller's own defrosts
DEFROST_LENGTH = 8       # seconds a scheduled defrost runs


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

    last_defrost = time.monotonic()
    defrosting_until = 0.0

    try:
        while True:
            now = time.monotonic()

            # --- read the current state (reads are always allowed) ---
            temp = to_signed(client.read_input_registers(IR_CASE_TEMP, count=1).registers[0])
            setpoint = to_signed(client.read_holding_registers(HR_SETPOINT, count=1).registers[0])
            defrost_on = bool(client.read_coils(CO_DEFROST, count=1).bits[0])
            compressor_on = bool(client.read_coils(CO_COMPRESSOR, count=1).bits[0])

            # --- 1. keep the setpoint where it belongs (undo any tampering) ---
            if setpoint != SETPOINT:
                client.write_register(HR_SETPOINT, SETPOINT & 0xFFFF)

            # --- 2. defrost: run our own schedule, cancel anyone else's ---
            if defrosting_until:
                if now >= defrosting_until:
                    if defrost_on:
                        client.write_coil(CO_DEFROST, False)
                    defrosting_until = 0.0
            else:
                if now - last_defrost > DEFROST_EVERY:
                    client.write_coil(CO_DEFROST, True)
                    client.write_coil(CO_COMPRESSOR, False)
                    defrosting_until = now + DEFROST_LENGTH
                    last_defrost = now
                elif defrost_on:
                    # A defrost we did not start (an attacker). Shut it off.
                    client.write_coil(CO_DEFROST, False)

            # --- 3. thermostat: hold the band (only when not defrosting) ---
            if not defrosting_until:
                if temp > SETPOINT + HYSTERESIS and not compressor_on:
                    client.write_coil(CO_COMPRESSOR, True)
                elif temp < SETPOINT - HYSTERESIS and compressor_on:
                    client.write_coil(CO_COMPRESSOR, False)

            state = "DEFROST" if defrost_on else ("cooling" if compressor_on else "idle")
            print(f"  controller: case {temp/10:+.1f}C  target {SETPOINT/10:.1f}C  {state}    ",
                  end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStore controller stopped.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
