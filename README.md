# Cold Guard

An inline Modbus watchdog that guards a legacy PLC. Every command to the PLC
passes through Cold Guard, which inspects it against a safety profile and
forwards only what is safe — with no changes to the PLC itself.

Built for HackGB 2026, Industrial track. It recreates the October 2026
supermarket refrigeration attack (setpoint driven to 50 °C, forced continuous
defrost) against a simulated fridge, and blocks it live.

## The pieces

| File | Role | Runs on (demo) |
|---|---|---|
| `watchdog.py` | Cold Guard: inline proxy, six rules, colored live log | Parrot OS machine |
| `plc.py` | Simulated fridge PLC (plain-socket Modbus TCP) + physics | Moe's Mac |
| `controller.py` | Store controller / HMI, normal traffic | Conner's laptop |
| `attacker.py` | Rogue device replaying the refrigeration attack | Daya's laptop |
| `dashboard.py` | Plant dashboard (Streamlit): temperature + traffic | Moe's Mac |
| `profile.json` | The safety profile: addresses, limits, every rule value | — |

`watchdog.py` and `plc.py` use only the Python standard library.
`controller.py` and `attacker.py` use `pymodbus`; `dashboard.py` uses `streamlit`.

## The six rules (checked in order, first failure blocks)

1. **source** — only an allowed writer may send a write
2. **function** — reads (1–4) pass; writes only via 5 and 6; 15, 16, others blocked
3. **address** — FC5 only to known coils; FC6 only to known holding registers
4. **range** — a register value must stay within its safe min/max
5. **defrost** — the defrost coil can't be turned on faster than its interval
6. **rate** — no more than N writes in a short window from one source

All limits live in `profile.json`, so tuning needs no code changes.

## Register map

| Table | Address | Name | Meaning |
|---|---|---|---|
| Coil | 0 | compressor | 1 = on, 0 = off |
| Coil | 1 | defrost | 1 = on, 0 = off |
| Holding register | 0 | setpoint | °C × 10, signed |
| Input register | 0 | case temperature | °C × 10, signed, read-only |

Negative temperatures wrap: a value over 32767 means subtract 65536.

## Running it

### Quick check (one machine)

    python3 selftest.py && cat /tmp/selftest.out

Starts the PLC and watchdog in one process and exercises all six rules.

### The four-device demo

1. Edit `profile.json`:
   - `listen`: where the watchdog accepts connections, e.g. `0.0.0.0:502`
   - `watchdog`: the address clients dial to reach it, e.g. `192.168.50.2:502`
   - `plc`: the Mac's address, e.g. `192.168.50.10:5020`
   - `allowed_writers`: the controller's IP, e.g. `192.168.50.20`
2. On the **Mac**: `python3 plc.py` then, in another terminal, `streamlit run dashboard.py`
3. On the **Parrot machine**: `sudo python3 watchdog.py` (full-screen terminal shows the live log)
4. On **Conner's laptop**: `python3 controller.py`
5. On **Daya's laptop**: `python3 attacker.py`

### Bypass mode (the before/after)

Set `"mode": "bypass"` in `profile.json`, or create a file named `BYPASS` next
to `watchdog.py`. In bypass, every command is forwarded (marked `BYPASS`), so the
attack gets through and the dashboard shows the case climbing past 4 °C.

## Demo arc

1. Normal operation: case holds about −18 °C, traffic flows green.
2. Daya runs the attack: Cold Guard blocks it, the Pi screen and dashboard go red,
   the log names the rule.
3. Flip to bypass and rerun: the case warms past the 4 °C food-safety line.
4. Swap the profile and the same watchdog protects a different plant.

## AI disclosure

Parts of this project were developed with help from Claude (Anthropic).
AI assistance was used for: scaffolding the watchdog proxy and the plain-socket
PLC simulator, drafting the detection rules and the Streamlit dashboard,
adapting code to the installed pymodbus version, and writing documentation.
All code was reviewed, tested, and integrated by the team during the event.
