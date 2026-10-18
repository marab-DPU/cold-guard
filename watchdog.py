"""
Cold Guard watchdog — an inline Modbus TCP proxy that guards a PLC.

It listens where profile.json says ("listen"). For every client that connects,
it opens a matching connection to the real PLC ("plc"). Each Modbus frame is
read whole, checked against six rules, and then either forwarded to the PLC or
blocked with a Modbus exception response. Every decision is printed in color
(green = allowed, red = blocked) and appended to events.jsonl.

Only Python's standard library is used, so the Parrot machine (192.168.50.2)
needs no installs.

The six rules, checked in order (first failure wins):
  1. source      only an allowed writer may send a write
  2. function    reads 1-4 pass; writes only via 5 and 6; anything else blocked
  3. address     FC5 only to known coils; FC6 only to known holding registers
  4. range       a holding-register value must stay within its safe min/max
  5. defrost     the defrost coil can't be turned on faster than its interval
  6. rate        no more than N writes in a short window from one source

Run:  python watchdog.py                 (reads profile.json)
      python watchdog.py other.json      (a different profile)

Toggle bypass live by creating/deleting a file named BYPASS in the folder you
run it from, or set "mode": "bypass" in the profile. In bypass the six rules
are switched off and commands are forwarded (marked BYPASS in the log).

Safety limits (the "safety_limits" block in the profile) sit underneath the six
rules and are enforced in EVERY mode, including bypass. Nothing on the network
can change them:
  * a setpoint above the hard ceiling is always refused
  * Cold Guard reads the case temperature itself every half second; once it
    reaches "trip_at" it refuses every heating command (defrost on, compressor
    off) from anyone, and holds the fridge in its safe state (defrost off,
    compressor on) until it cools to "reset_at". It only ever cools.
"""

import asyncio
import json
import os
import struct
import sys
import time
from collections import deque

PROFILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "profile.json"
with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)

EVENTS_FILE = PROFILE.get("events_file", "events.jsonl")
BYPASS_FLAG = "BYPASS"  # presence of this file forces bypass mode

# Modbus write function codes we understand.
FC_WRITE_COIL = 5
FC_WRITE_REGISTER = 6

# Modbus exception codes.
EXC_ILLEGAL_FUNCTION = 1
EXC_ILLEGAL_ADDRESS = 2
EXC_ILLEGAL_VALUE = 3

# ANSI colors for the live display.
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

# --- per-source state the stateful rules need ------------------------------
_recent_writes = {}   # src_ip -> deque of timestamps (rate limit)
_last_defrost_on = 0.0  # last time defrost was switched on (defrost limit)

_allowed = 0
_blocked = 0

# --- safety limits: enforced in every mode, bypass included ------------------
SAFETY = PROFILE.get("safety_limits")      # None = no safety tier
_safety = {"temp": None, "tripped": False, "tid": 0, "plc_ok": True}


def _addr(table, name):
    """Find an address in the profile by its name, e.g. coils -> 'defrost'."""
    for key, val in PROFILE.get(table, {}).items():
        label = val if isinstance(val, str) else val.get("name")
        if label == name:
            return int(key)
    return None


COIL_COMPRESSOR = _addr("coils", "compressor")
COIL_DEFROST = _addr("coils", "defrost")
REG_SETPOINT = _addr("holding_registers", "setpoint")
IR_CASE_TEMP = _addr("input_registers", "case_temperature")


def split_addr(addr):
    host, port = addr.rsplit(":", 1)
    return host, int(port)


def to_signed(v):
    return v - 65536 if v > 32767 else v


def bypass_active():
    return PROFILE.get("mode") == "bypass" or os.path.exists(BYPASS_FLAG)


def log_event(src_ip, fc, address, value, decision, rule, detail):
    """Append one line to events.jsonl and print a colored line to the screen."""
    global _allowed, _blocked
    event = {
        "time": time.strftime("%H:%M:%S"),
        "src": src_ip,
        "fc": fc,
        "address": address,
        "value": value,
        "decision": decision,   # ALLOW, BLOCK, or BYPASS
        "rule": rule,           # which rule fired, or "-"
        "detail": detail,
    }
    try:
        with open(EVENTS_FILE, "a") as ef:
            ef.write(json.dumps(event) + "\n")
    except OSError:
        pass  # logging must never stop the proxy

    if decision == "SAFETY":
        # Cold Guard's own safety action (trip, reset, or safe state sent).
        print(f"{MAGENTA}{BOLD}SAFETY{RESET} {DIM}{event['time']}{RESET} "
              f"{MAGENTA}{detail}{RESET}", flush=True)
        return

    if decision == "ALLOW":
        _allowed += 1
        color, tag = GREEN, "ALLOW "
    elif decision == "BYPASS":
        _allowed += 1
        color, tag = YELLOW, "BYPASS"
    else:
        _blocked += 1
        color, tag = (MAGENTA if rule == "safety" else RED), "BLOCK "

    line = (
        f"{color}{tag}{RESET} {DIM}{event['time']}{RESET} "
        f"{src_ip:<15} FC{fc} addr {address} val {value}"
    )
    if decision != "ALLOW":
        line += f"  {color}<- {rule}: {detail}{RESET}"
    print(line, flush=True)


def parse_write(pdu):
    """
    For a write (FC 5 or 6), return (address, raw_value).
    FC 5 single coil: 0xFF00 = on, 0x0000 = off.
    FC 6 single register: the raw 16-bit value.
    """
    fc = pdu[0]
    address, raw = struct.unpack(">HH", pdu[1:5])
    if fc == FC_WRITE_COIL:
        value = 1 if raw == 0xFF00 else 0
    else:
        value = raw
    return address, value


def check_rules(src_ip, pdu):
    """
    Return (decision, rule, detail, address, value) for one frame.
    decision is "ALLOW" or "BLOCK". Reads (FC 1-4) always allow.
    """
    global _last_defrost_on
    fc = pdu[0]

    # Reads are always allowed; record nothing, forward.
    if fc in (1, 2, 3, 4):
        return "ALLOW", "-", "read", None, None

    # Rule 2 (function, part 1): is this a function code we permit at all?
    if fc not in PROFILE["allowed_function_codes"]:
        return "BLOCK", "function", f"FC{fc} not permitted", None, None

    # Rule 2 (function, part 2): any non-read must be an approved write code.
    if fc not in PROFILE["write_function_codes"]:
        return "BLOCK", "function", f"FC{fc} is not an allowed write", None, None

    address, value = parse_write(pdu)

    # Rule 1 (source): only allowed writers may write.
    if src_ip not in PROFILE["allowed_writers"]:
        return "BLOCK", "source", f"{src_ip} may not write", address, value

    # Rule 3 (address): the target must be a known coil / register.
    if fc == FC_WRITE_COIL:
        if str(address) not in PROFILE["coils"]:
            return "BLOCK", "address", f"coil {address} not allowed", address, value
    else:  # FC_WRITE_REGISTER
        if str(address) not in PROFILE["holding_registers"]:
            return "BLOCK", "address", f"register {address} not allowed", address, value

    # Rule 4 (range): a holding-register value must be within safe bounds.
    if fc == FC_WRITE_REGISTER:
        limits = PROFILE["holding_registers"][str(address)]
        signed = to_signed(value)
        if signed < limits["min"] or signed > limits["max"]:
            return (
                "BLOCK", "range",
                f"{limits['name']} {signed/10:.1f}C outside "
                f"[{limits['min']/10:.1f},{limits['max']/10:.1f}]",
                address, value,
            )

    # Rule 5 (defrost): the defrost coil can't be switched on too often.
    if fc == FC_WRITE_COIL and PROFILE["coils"].get(str(address)) == "defrost" and value == 1:
        now = time.monotonic()
        interval = PROFILE.get("defrost_min_interval_seconds", 120)
        if now - _last_defrost_on < interval:
            return (
                "BLOCK", "defrost",
                f"defrost again after {now - _last_defrost_on:.0f}s (< {interval}s)",
                address, value,
            )
        _last_defrost_on = now

    # Rule 6 (rate): too many writes in a short window from one source.
    limit = PROFILE.get("rate_limit", {})
    max_writes = limit.get("writes", 5)
    window = limit.get("seconds", 2)
    now = time.monotonic()
    stamps = _recent_writes.setdefault(src_ip, deque())
    stamps.append(now)
    while stamps and now - stamps[0] > window:
        stamps.popleft()
    if len(stamps) > max_writes:
        return (
            "BLOCK", "rate",
            f"{len(stamps)} writes in {window}s (> {max_writes})",
            address, value,
        )

    if fc == FC_WRITE_COIL:
        name = PROFILE["coils"].get(str(address), "coil")
    else:
        name = PROFILE["holding_registers"].get(str(address), {}).get("name", "register")
    return "ALLOW", "-", name, address, value


def check_safety(pdu):
    """
    The safety limits. Unlike the six rules above, these are checked in every
    mode, bypass included. Return None if the command is safe, else the reason.
    """
    if not SAFETY:
        return None
    fc = pdu[0]

    # Bulk writes can't be checked value-by-value here, so they never pass.
    if fc in (15, 16, 22, 23):
        return f"FC{fc} bulk write can't be checked against the safety limits"
    if fc not in (FC_WRITE_COIL, FC_WRITE_REGISTER):
        return None

    address, value = parse_write(pdu)
    ceiling = SAFETY["case_temp_max"]

    # A setpoint above the ceiling is never allowed, at any temperature.
    if fc == FC_WRITE_REGISTER and address == REG_SETPOINT and to_signed(value) > ceiling:
        return (f"setpoint {to_signed(value)/10:.1f}C is above the hard "
                f"ceiling {ceiling/10:.1f}C")

    # Once tripped, nothing may add heat until the case has cooled back down.
    if _safety["tripped"] and fc == FC_WRITE_COIL:
        if address == COIL_DEFROST and value == 1:
            return "defrost on refused: case is at the safety limit"
        if address == COIL_COMPRESSOR and value == 0:
            return "compressor off refused: case is at the safety limit"
    return None


async def _plc_call(reader, writer, pdu):
    """One request on the safety monitor's own PLC connection; returns the reply PDU."""
    _safety["tid"] = (_safety["tid"] + 1) & 0xFFFF
    writer.write(struct.pack(">HHHB", _safety["tid"], 0, len(pdu) + 1, 1) + pdu)
    await writer.drain()
    _, reply = await asyncio.wait_for(read_frame(reader), timeout=2)
    if reply[0] & 0x80:
        raise ValueError(f"PLC refused FC{pdu[0]}")
    return reply


async def safety_monitor():
    """
    Read the case temperature every half second on Cold Guard's own connection.
    At trip_at: latch, refuse all heating commands, and hold the safe state
    (defrost off, compressor on). At reset_at: release. This only ever cools.
    """
    if not SAFETY:
        return
    host, port = split_addr(PROFILE["plc"])
    trip_at = SAFETY["trip_at"]
    reset_at = SAFETY["reset_at"]
    every = SAFETY.get("check_every_seconds", 0.5)
    n_coils = max(COIL_COMPRESSOR, COIL_DEFROST) + 1

    while True:
        writer = None
        try:
            reader, writer = await asyncio.open_connection(host, port)
            if not _safety["plc_ok"]:
                print(f"{MAGENTA}safety monitor reconnected to the PLC{RESET}", flush=True)
                _safety["plc_ok"] = True
            while True:
                reply = await _plc_call(reader, writer, struct.pack(">BHH", 4, IR_CASE_TEMP, 1))
                temp = to_signed(struct.unpack(">H", reply[2:4])[0])
                _safety["temp"] = temp

                if not _safety["tripped"] and temp >= trip_at:
                    _safety["tripped"] = True
                    log_event("cold-guard", None, None, None, "SAFETY", "safety",
                              f"TRIP: case {temp/10:.1f}C reached {trip_at/10:.1f}C "
                              f"- refusing heat, holding defrost off + compressor on")
                elif _safety["tripped"] and temp <= reset_at:
                    _safety["tripped"] = False
                    log_event("cold-guard", None, None, None, "SAFETY", "safety",
                              f"RESET: case back to {temp/10:.1f}C - safety hold released")

                if _safety["tripped"]:
                    reply = await _plc_call(reader, writer, struct.pack(">BHH", 1, 0, n_coils))
                    bit = lambda i: (reply[2 + i // 8] >> (i % 8)) & 1
                    if bit(COIL_DEFROST) or not bit(COIL_COMPRESSOR):
                        await _plc_call(reader, writer, struct.pack(">BHH", 5, COIL_DEFROST, 0x0000))
                        await _plc_call(reader, writer, struct.pack(">BHH", 5, COIL_COMPRESSOR, 0xFF00))
                        log_event("cold-guard", 5, None, None, "SAFETY", "safety",
                                  "safe state sent: defrost off, compressor on")

                await asyncio.sleep(every)
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ValueError, struct.error) as e:
            if _safety["plc_ok"]:
                print(f"{RED}[!] safety monitor can't read the PLC: {e!r}{RESET}", flush=True)
                _safety["plc_ok"] = False
            await asyncio.sleep(1)
        finally:
            if writer is not None:
                writer.close()


def exception_response(header, fc, exc_code):
    """Build a Modbus exception reply: same transaction, FC|0x80, exc code."""
    trans_id, proto_id, _, unit_id = struct.unpack(">HHHB", header)
    pdu = struct.pack(">BB", fc | 0x80, exc_code)
    new_header = struct.pack(">HHHB", trans_id, proto_id, len(pdu) + 1, unit_id)
    return new_header + pdu


async def read_frame(reader):
    header = await reader.readexactly(7)
    trans_id, proto_id, length, unit_id = struct.unpack(">HHHB", header)
    if proto_id != 0 or length < 2 or length > 254:
        raise ValueError(f"not a valid Modbus frame (proto={proto_id}, length={length})")
    pdu = await reader.readexactly(length - 1)
    return header, pdu


def rule_to_exception(rule):
    if rule == "function":
        return EXC_ILLEGAL_FUNCTION
    if rule in ("range", "safety"):
        return EXC_ILLEGAL_VALUE
    if rule in ("address",):
        return EXC_ILLEGAL_ADDRESS
    return EXC_ILLEGAL_FUNCTION  # source, defrost, rate: refuse the function


async def handle_client(client_reader, client_writer):
    src_ip = client_writer.get_extra_info("peername")[0]
    plc_host, plc_port = split_addr(PROFILE["plc"])

    try:
        plc_reader, plc_writer = await asyncio.open_connection(plc_host, plc_port)
    except OSError as e:
        # Fail-closed: no path to the PLC means no commands get through.
        print(f"{RED}[!] can't reach PLC at {PROFILE['plc']}: {e}{RESET}", flush=True)
        client_writer.close()
        return

    try:
        while True:
            header, pdu = await read_frame(client_reader)
            fc = pdu[0]

            if bypass_active():
                address, value = (parse_write(pdu) if fc in (5, 6) else (None, None))
                # The six rules are off, but the safety limits still apply.
                reason = check_safety(pdu)
                if reason:
                    log_event(src_ip, fc, address, value, "BLOCK", "safety", reason)
                    client_writer.write(exception_response(header, fc, EXC_ILLEGAL_VALUE))
                    await client_writer.drain()
                    continue
                log_event(src_ip, fc, address, value, "BYPASS", "-", "bypass mode")
                plc_writer.write(header + pdu)
                await plc_writer.drain()
                reply_h, reply_p = await read_frame(plc_reader)
                client_writer.write(reply_h + reply_p)
                await client_writer.drain()
                continue

            decision, rule, detail, address, value = check_rules(src_ip, pdu)
            if decision == "ALLOW":
                reason = check_safety(pdu)
                if reason:
                    decision, rule, detail = "BLOCK", "safety", reason

            if decision == "ALLOW":
                log_event(src_ip, fc, address, value, "ALLOW", "-", detail)
                plc_writer.write(header + pdu)
                await plc_writer.drain()
                reply_h, reply_p = await read_frame(plc_reader)
                client_writer.write(reply_h + reply_p)
                await client_writer.drain()
            else:
                log_event(src_ip, fc, address, value, "BLOCK", rule, detail)
                # Answer for the PLC with an exception; never touch it.
                client_writer.write(exception_response(header, fc, rule_to_exception(rule)))
                await client_writer.drain()

    except asyncio.IncompleteReadError:
        pass
    except (ValueError, ConnectionError, struct.error) as e:
        print(f"{RED}[!] {src_ip}: {e!r}{RESET}", flush=True)
    finally:
        plc_writer.close()
        client_writer.close()


def banner():
    host, port = split_addr(PROFILE["listen"])
    mode = "BYPASS" if bypass_active() else "ARMED"
    color = YELLOW if mode == "BYPASS" else GREEN
    print(f"{BOLD}=== Cold Guard :: {PROFILE['name']} ==={RESET}", flush=True)
    print(f"guarding PLC {PROFILE['plc']}  |  listening {host}:{port}  |  "
          f"status {color}{mode}{RESET}", flush=True)
    print(f"{DIM}allowed writer(s): {', '.join(PROFILE['allowed_writers'])}{RESET}", flush=True)
    if SAFETY:
        print(f"{MAGENTA}safety limits ON in every mode: hard ceiling "
              f"{SAFETY['case_temp_max']/10:.1f}C, trips at {SAFETY['trip_at']/10:.1f}C, "
              f"resets at {SAFETY['reset_at']/10:.1f}C{RESET}", flush=True)
    print("-" * 64, flush=True)


async def main():
    host, port = split_addr(PROFILE["listen"])
    banner()
    monitor = asyncio.create_task(safety_monitor())  # noqa: F841 (kept alive)
    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{BOLD}Cold Guard stopped.{RESET} "
              f"allowed {_allowed}, blocked {_blocked}.")
