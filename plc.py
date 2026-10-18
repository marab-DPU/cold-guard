"""
Fridge PLC — the simulated refrigeration controller. Runs on Moe's Mac.

A small Modbus TCP server written on plain sockets (standard library only, no
pymodbus) so it behaves identically on any machine and shares its frame format
with the watchdog. It holds the fridge's four data tables and runs a background
loop that fakes the physics of a refrigerated case, writing state.json each
second so the dashboard can read the fridge without speaking Modbus.

Data model (addresses from profile.json):
  coil 0            compressor   1 = on,  0 = off
  coil 1            defrost      1 = on,  0 = off
  holding reg 0     setpoint     deg C x 10, signed
  input reg 0       case temp    deg C x 10, signed (read-only sensor)

Supported function codes: 1,2 (read coils/discrete), 3,4 (read holding/input),
5 (write single coil), 6 (write single register).

Run:  python plc.py            (reads profile.json for addresses)
"""

import asyncio
import json
import struct
import sys

PROFILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "profile.json"
with open(PROFILE_PATH) as f:
    PROFILE = json.load(f)

STATE_FILE = PROFILE.get("state_file", "state.json")

CO_COMPRESSOR = 0
CO_DEFROST = 1
HR_SETPOINT = 0
IR_CASE_TEMP = 0

START_TEMP = -180      # -18.0 C in tenths
START_SETPOINT = -200  # -20.0 C in tenths

TABLE_SIZE = 16
EXC_ILLEGAL_FUNCTION = 1

# Four data tables.
coils = [0] * TABLE_SIZE
discrete_inputs = [0] * TABLE_SIZE
holding = [0] * TABLE_SIZE
input_regs = [0] * TABLE_SIZE


def to_signed(v):
    return v - 65536 if v > 32767 else v


def to_reg(v):
    return v & 0xFFFF


def handle_pdu(pdu):
    """Take a Modbus PDU (function code + data), return the response PDU."""
    fc = pdu[0]
    try:
        if fc in (1, 2):  # read coils / discrete inputs
            address, count = struct.unpack(">HH", pdu[1:5])
            table = coils if fc == 1 else discrete_inputs
            bits = table[address:address + count]
            nbytes = (count + 7) // 8
            packed = bytearray(nbytes)
            for i, bit in enumerate(bits):
                if bit:
                    packed[i // 8] |= 1 << (i % 8)
            return struct.pack(">BB", fc, nbytes) + bytes(packed)

        if fc in (3, 4):  # read holding / input registers
            address, count = struct.unpack(">HH", pdu[1:5])
            table = holding if fc == 3 else input_regs
            regs = table[address:address + count]
            body = b"".join(struct.pack(">H", r) for r in regs)
            return struct.pack(">BB", fc, count * 2) + body

        if fc == 5:  # write single coil
            address, raw = struct.unpack(">HH", pdu[1:5])
            coils[address] = 1 if raw == 0xFF00 else 0
            return pdu  # echo the request

        if fc == 6:  # write single register
            address, value = struct.unpack(">HH", pdu[1:5])
            holding[address] = value
            return pdu  # echo the request

    except (struct.error, IndexError):
        pass

    # Unsupported or malformed: Modbus exception.
    return struct.pack(">BB", fc | 0x80, EXC_ILLEGAL_FUNCTION)


async def handle_client(reader, writer):
    try:
        while True:
            header = await reader.readexactly(7)
            trans_id, proto_id, length, unit_id = struct.unpack(">HHHB", header)
            pdu = await reader.readexactly(length - 1)
            resp_pdu = handle_pdu(pdu)
            resp_header = struct.pack(">HHHB", trans_id, proto_id, len(resp_pdu) + 1, unit_id)
            writer.write(resp_header + resp_pdu)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def physics_loop():
    """Every second: move the temperature by the coils, publish state.json."""
    temp = START_TEMP
    while True:
        compressor = coils[CO_COMPRESSOR]
        defrost = coils[CO_DEFROST]
        setpoint = to_signed(holding[HR_SETPOINT])

        if defrost:
            temp += 10          # defrost heater drives the case up fast
        elif compressor:
            temp -= 5           # compressor pulls it down
        else:
            temp += 2           # warms by leakage when idle

        temp = max(-400, min(600, temp))
        input_regs[IR_CASE_TEMP] = to_reg(temp)

        state = {
            "case_temp_c": round(temp / 10, 1),
            "setpoint_c": round(setpoint / 10, 1),
            "compressor": bool(compressor),
            "defrost": bool(defrost),
            "danger": temp > 40,   # above 4.0 C = food-safety line
        }
        try:
            with open(STATE_FILE, "w") as sf:
                json.dump(state, sf)
        except OSError:
            pass

        await asyncio.sleep(1)


async def main():
    coils[CO_COMPRESSOR] = 1
    holding[HR_SETPOINT] = to_reg(START_SETPOINT)
    input_regs[IR_CASE_TEMP] = to_reg(START_TEMP)

    host, port = PROFILE["plc"].rsplit(":", 1)
    server = await asyncio.start_server(handle_client, host, int(port))
    print(f"Fridge PLC listening on {host}:{port}  (state -> {STATE_FILE})")
    asyncio.create_task(physics_loop())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nFridge PLC stopped.")
