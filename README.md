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
| `watchdog.py` | Cold Guard: inline proxy, six rules, colored live log | Parrot machine (`192.168.50.2`) |
| `plc.py` | Simulated fridge PLC (plain-socket Modbus TCP) + physics | Conner's laptop (`192.168.50.20`) |
| `controller.py` | Store controller / HMI, normal traffic | Conner's laptop (`192.168.50.20`) |
| `dashboard.py` | Plant dashboard (Streamlit): temperature + traffic | Conner's laptop |
| `attacker.py` | Rogue device replaying the refrigeration attack | Moe's Mac |
| `profile.json` | The safety profile: addresses, limits, every rule value | — |

The **plant** — the fridge PLC, the store controller, and the dashboard — all
run together on Conner's laptop at `192.168.50.20`. The PLC isn't a separate
box; it's a process there that the controller drives, so the controller's
traffic comes from the same address, which is why `192.168.50.20` is the one
allowed writer. **Cold Guard** is the Parrot machine inline in front of the
plant. The **attacker** is a separate rogue device (Moe's Mac) on the same LAN.

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

## Safety limits (always on, even in bypass)

Underneath the six rules is a safety tier that bypass can't switch off and no
network command can change (`safety_limits` in `profile.json`, tenths of °C):

- **Hard ceiling** (`case_temp_max`, 0 °C): a setpoint above it is always refused.
- **Trip** (`trip_at`, −3 °C): Cold Guard reads the case temperature itself every
  half second. Once the case reaches the trip point, it refuses every command
  that adds heat (defrost on, compressor off), from anyone, and holds the
  fridge in its safe state (defrost off, compressor on).
- **Reset** (`reset_at`, −6 °C): the hold releases once the case has cooled.

It trips below the ceiling because the case keeps warming for a moment after
the heat is cut. The only command Cold Guard ever sends on its own is that
cooling-only safe state. Bulk writes (FC 15/16) are also refused in bypass,
since they can't be checked value by value.

## Fail-open and the flat-network model

Cold Guard sits inline, but it is not a single point of failure. Every device
prefers the watchdog's address and only falls back to the PLC's address when
the watchdog doesn't answer — the same connect-with-fallback logic in both
`controller.py` and `attacker.py`. This models a flat plant network behind a
bump-in-the-wire guard: when the guard is up, all traffic crosses it; when it's
gone (crash, reboot, power loss, a hardware bypass relay tripping), devices
reach the PLC's address directly.

- **Controller fail-open:** if Cold Guard stops answering, the controller
  notices within about a second, talks to the PLC directly, and keeps the case
  at its normal temperature. Every 5 seconds it rechecks and routes back through
  Cold Guard when it returns. Cold Guard is never the reason the fridge stops.
- **Attacker, same logic:** the rogue device also prefers the watchdog. While
  the watchdog is up, its commands cross Cold Guard and are blocked (armed) or
  capped by the safety ceiling (bypassed) — being *blocked* does not reroute it,
  because the connection is alive, it's just refused. Only when the watchdog is
  truly down does the attacker reach the PLC directly. That is the "before Cold
  Guard" world: nothing in the path, and the case runs away.

The dashboard shows a FAIL-OPEN banner while the controller is direct. While
fail-open the fridge runs but is unprotected, exactly as it was before Cold
Guard was installed.

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

### The three-machine demo

1. Edit `profile.json`:
   - `listen`: where the watchdog accepts connections, e.g. `0.0.0.0:502`
   - `watchdog`: the address clients dial to reach it, e.g. `192.168.50.2:502`
   - `plc`: Conner's laptop (the plant), e.g. `192.168.50.20:5020`
   - `allowed_writers`: the controller's IP, i.e. Conner's laptop `192.168.50.20`
2. On **Conner's laptop** (the plant), three terminals:
   `python3 plc.py`, then `python3 controller.py`, then `streamlit run dashboard.py`
3. On the **Parrot machine** (Cold Guard): `sudo python3 watchdog.py`
   (full-screen terminal shows the live log)
4. On **Moe's Mac** (the attacker): `python3 attacker.py`

### Bypass mode

Set `"mode": "bypass"` in `profile.json`, or create a file named `BYPASS` in the
folder you run `watchdog.py` from. In bypass the six rules are off and commands
are forwarded (marked `BYPASS`), but the safety limits still apply.

## Demo arc

1. **Test 1, armed:** the attack runs and every command is blocked at the door
   (source rule first); the log names the rule. The case holds about −20 °C.
2. **Test 2, bypassed:** the attacker's compressor-off and defrost-on commands get
   through and the case warms, but the always-on safety limits refuse the 50 °C
   setpoint and stop the case before it reaches 0 °C. The dashboard shows the peak.
3. Re-arm: the attack is blocked again and the controller pulls the case back down.
4. **Watchdog down (the "before Cold Guard" world):** stop the watchdog. The
   attacker's traffic now reaches the PLC directly with nothing in the path and
   the case runs away, while the controller fails open and keeps cooling —
   showing that the protection, not the plant, is what Cold Guard adds.

## AI disclosure

Parts of this project were developed with help from Claude (Anthropic).
AI assistance was used for: scaffolding the watchdog proxy and the plain-socket
PLC simulator, drafting the detection rules and the Streamlit dashboard,
adapting code to the installed pymodbus version, and writing documentation.
All code was reviewed, tested, and integrated by the team during the event.
