"""
In-process test of the full stack. All clients connect from 127.0.0.1.
The source rule is exercised by swapping the allowlist in memory.
Writes results to /tmp/selftest.out and exits.
"""
import asyncio, json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = ["x", os.path.join(HERE, "profile.test.json")]

prof = json.load(open(os.path.join(HERE, "profile.json")))
prof["plc"] = "127.0.0.1:17020"
prof["listen"] = "127.0.0.1:17502"
prof["events_file"] = "/tmp/ev.jsonl"
prof["state_file"] = "/tmp/st.json"
json.dump(prof, open(os.path.join(HERE, "profile.test.json"), "w"))
open("/tmp/ev.jsonl", "w").close()

import importlib.util
def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
plc = load("plc"); wd = load("watchdog")

OUT = open("/tmp/selftest.out", "w")
def say(*a): print(*a, file=OUT)


async def send(fc, addr, value):
    reader, writer = await asyncio.open_connection("127.0.0.1", 17502)
    raw = (0xFF00 if value else 0x0000) if fc == 5 else (value & 0xFFFF)
    pdu = struct.pack(">BHH", fc, addr, raw)
    writer.write(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
    await writer.drain()
    head = await asyncio.wait_for(reader.readexactly(7), timeout=3)
    _, _, length, _ = struct.unpack(">HHHB", head)
    rest = await asyncio.wait_for(reader.readexactly(length - 1), timeout=3)
    writer.close()
    return "BLOCKED" if rest[0] & 0x80 else "forwarded"


async def body():
    plc.coils[plc.CO_COMPRESSOR] = 1
    plc.holding[plc.HR_SETPOINT] = plc.to_reg(plc.START_SETPOINT)
    plc.input_regs[plc.IR_CASE_TEMP] = plc.to_reg(plc.START_TEMP)
    plc_server = await asyncio.start_server(plc.handle_client, "127.0.0.1", 17020)
    wd_server = await asyncio.start_server(wd.handle_client, "127.0.0.1", 17502)

    # allow this test's source (127.0.0.1) for the value/function/address tests
    wd.PROFILE["allowed_writers"] = ["127.0.0.1"]
    await asyncio.sleep(0.2)

    say("=== legitimate + per-rule checks (client = allowed writer) ===")
    checks = [
        ("read case temp (FC4)",            4, 0, 1,   "forwarded"),
        ("setpoint -20C (legit)",           6, 0, (-200) & 0xFFFF, "forwarded"),
        ("compressor on (legit)",           5, 0, 1,   "forwarded"),
        ("setpoint +50C -> RANGE",          6, 0, 500, "BLOCKED"),
        ("write coil 5 -> ADDRESS",         5, 5, 1,   "BLOCKED"),
        ("write reg 2 -> ADDRESS",          6, 2, 1,   "BLOCKED"),
        ("FC16 multi-write -> FUNCTION",    16, 0, 1,  "BLOCKED"),
    ]
    passed = 0
    for label, fc, addr, val, expect in checks:
        r = await send(fc, addr, val)
        ok = (r == expect)
        passed += ok
        say(f"  [{'PASS' if ok else 'FAIL'}] {label:34} -> {r} (want {expect})")

    say("\n=== defrost limit (second defrost-on too soon) ===")
    r1 = await send(5, 1, 1)                       # first defrost on
    r2 = await send(5, 1, 1)                       # immediately again
    ok = (r1 == "forwarded" and r2 == "BLOCKED")
    passed += ok
    say(f"  [{'PASS' if ok else 'FAIL'}] 1st={r1}, 2nd={r2} (want forwarded, BLOCKED)")

    say("\n=== rate limit (10 rapid setpoint writes) ===")
    blocked = 0
    for _ in range(10):
        if await send(6, 0, (-200) & 0xFFFF) == "BLOCKED":
            blocked += 1
    ok = blocked > 0
    passed += ok
    say(f"  [{'PASS' if ok else 'FAIL'}] {blocked} of 10 blocked by rate rule (want > 0)")

    say("\n=== source rule (same write, now from a non-allowed source) ===")
    wd.PROFILE["allowed_writers"] = ["192.168.50.20"]   # 127.0.0.1 no longer allowed
    await asyncio.sleep(2.1)                             # let the rate window clear
    r = await send(6, 0, (-200) & 0xFFFF)
    ok = (r == "BLOCKED")
    passed += ok
    say(f"  [{'PASS' if ok else 'FAIL'}] legit write from wrong source -> {r} (want BLOCKED)")

    say(f"\n=== {passed}/11 checks passed ===")

    plc_server.close(); wd_server.close()
    await asyncio.sleep(0.1)


async def main():
    try:
        await asyncio.wait_for(body(), timeout=30)
    except Exception as e:
        say("ERROR:", repr(e))
    OUT.flush(); OUT.close()


asyncio.run(main())
os.remove(os.path.join(HERE, "profile.test.json"))
print("selftest complete")
