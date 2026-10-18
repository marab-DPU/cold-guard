"""
Store controller (HMI) — the legitimate operator system. Runs on the Linux
plant box (192.168.50.20), alongside the PLC and dashboard.

This is the normal traffic Cold Guard should always allow. It runs a closed
loop once a second:

  * it holds the setpoint at a safe value, re-asserting it if something changed it
  * it turns OFF any defrost it did not start itself
  * it cycles the compressor to keep the case near the setpoint
  * on its own schedule it runs a short, spaced-out defrost

It only WRITES when a value is actually wrong, so in steady state it sends very
little traffic (well under the watchdog's rate limit). That is also what makes
the fridge recover on its own after an attack: once Cold Guard is armed again,
the attacker is blocked and these corrections pull the case back down.

FAIL-OPEN: the controller normally talks to the PLC through Cold Guard. If Cold
Guard stops answering (crashed, unplugged, rebooting), the controller switches
to talking to the PLC directly so the fridge keeps running, and every few
seconds it checks whether Cold Guard is back and switches back when it is.
Cold Guard can never be the reason the store's refrigeration stops.

Run:  python3 controller.py            (reads profile.json for addresses)
"""

import json
import sys
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

PROFILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "profile.json"
with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)

STATUS_FILE = "controller.json"   # which path we're on, for the dashboard

HR_SETPOINT = 0
CO_COMPRESSOR = 0
CO_DEFROST = 1
IR_CASE_TEMP = 0

SETPOINT = -200          # -20.0 C in tenths, the safe target
HYSTERESIS = 10          # +/- 1.0 C band around the setpoint
DEFROST_EVERY = 150      # seconds between the controller's own defrosts
DEFROST_LENGTH = 8       # seconds a scheduled defrost runs
PROBE_EVERY = 5          # while fail-open, seconds between checks for Cold Guard

GUARDED, DIRECT = "through Cold Guard", "DIRECT to PLC (fail-open)"


def to_signed(v):
    return v - 65536 if v > 32767 else v


def make_client(addr):
    host, port = addr.rsplit(":", 1)
    # Short timeout so a dead Cold Guard is noticed in about a second.
    return ModbusTcpClient(host, port=int(port), timeout=1, retries=1)


def write_status(path):
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump({"path": "direct" if path == DIRECT else "cold-guard",
                       "since": time.strftime("%H:%M:%S")}, f)
    except OSError:
        pass


def read_one(fn, address):
    """A read that raises if it fails, so a dead link is noticed."""
    r = fn(address, count=1)
    if r.isError():
        raise ModbusException(f"read failed: {r}")
    return r


def open_path(path):
    """Connect via Cold Guard or directly to the PLC and prove it with a read."""
    client = make_client(PROFILE["watchdog"] if path == GUARDED else PROFILE["plc"])
    try:
        if client.connect():
            read_one(client.read_input_registers, IR_CASE_TEMP)
            return client
    except (ModbusException, OSError):
        pass
    client.close()
    return None


def control_step(client, st):
    """One pass of the control loop. Raises if the link to the PLC is broken."""
    now = time.monotonic()

    # --- read the current state ---
    temp = to_signed(read_one(client.read_input_registers, IR_CASE_TEMP).registers[0])
    setpoint = to_signed(read_one(client.read_holding_registers, HR_SETPOINT).registers[0])
    defrost_on = bool(read_one(client.read_coils, CO_DEFROST).bits[0])
    compressor_on = bool(read_one(client.read_coils, CO_COMPRESSOR).bits[0])

    # --- 1. keep the setpoint where it belongs (undo any tampering) ---
    if setpoint != SETPOINT:
        client.write_register(HR_SETPOINT, SETPOINT & 0xFFFF)

    # --- 2. defrost: run our own schedule, cancel anyone else's ---
    if st["defrosting_until"]:
        if now >= st["defrosting_until"]:
            if defrost_on:
                client.write_coil(CO_DEFROST, False)
            st["defrosting_until"] = 0.0
    else:
        if now - st["last_defrost"] > DEFROST_EVERY:
            client.write_coil(CO_DEFROST, True)
            client.write_coil(CO_COMPRESSOR, False)
            st["defrosting_until"] = now + DEFROST_LENGTH
            st["last_defrost"] = now
        elif defrost_on:
            # A defrost we did not start (an attacker). Shut it off.
            client.write_coil(CO_DEFROST, False)

    # --- 3. thermostat: hold the band (only when not defrosting) ---
    if not st["defrosting_until"]:
        if temp > SETPOINT + HYSTERESIS and not compressor_on:
            client.write_coil(CO_COMPRESSOR, True)
        elif temp < SETPOINT - HYSTERESIS and compressor_on:
            client.write_coil(CO_COMPRESSOR, False)

    return temp, defrost_on, compressor_on


def main():
    st = {"last_defrost": time.monotonic(), "defrosting_until": 0.0}
    path, client = GUARDED, open_path(GUARDED)
    if client is None:
        print("controller: Cold Guard not answering -> FAIL-OPEN, going direct to the PLC")
        path, client = DIRECT, open_path(DIRECT)
    if client is not None:
        write_status(path)
    print("Store controller running (normal operations)", flush=True)
    next_probe = time.monotonic() + PROBE_EVERY

    try:
        while True:
            if client is None:
                # Neither path worked last time: try Cold Guard first, then direct.
                for p in (GUARDED, DIRECT):
                    client = open_path(p)
                    if client is not None:
                        path = p
                        write_status(path)
                        print(f"\ncontroller: reconnected {path}", flush=True)
                        break
                else:
                    print("  controller: can't reach the PLC by any path, retrying...   ",
                          end="\r", flush=True)
                    time.sleep(1)
                    continue

            try:
                temp, defrost_on, compressor_on = control_step(client, st)
            except (ModbusException, OSError, AttributeError, IndexError):
                client.close()
                client = None
                if path == GUARDED:
                    print("\ncontroller: Cold Guard stopped answering -> FAIL-OPEN, "
                          "going direct to the PLC", flush=True)
                    client = open_path(DIRECT)
                    if client is not None:
                        path = DIRECT
                        write_status(path)
                        next_probe = time.monotonic() + PROBE_EVERY
                continue

            # While fail-open, keep checking whether Cold Guard is back.
            if path == DIRECT and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + PROBE_EVERY
                guarded = open_path(GUARDED)
                if guarded is not None:
                    client.close()
                    client, path = guarded, GUARDED
                    write_status(path)
                    print("\ncontroller: Cold Guard is back -> routing through it again",
                          flush=True)

            state = "DEFROST" if defrost_on else ("cooling" if compressor_on else "idle")
            print(f"  controller: case {temp/10:+.1f}C  target {SETPOINT/10:.1f}C  "
                  f"{state:<8} [{path}]    ", end="\r", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStore controller stopped.")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
