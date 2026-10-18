"""
Plant dashboard — the view the judges watch. Runs on Moe's Mac.

It speaks no Modbus. It just reads two files the other programs write:
  state.json    the fridge's current temperature and status (from plc.py)
  events.jsonl  one line per command Cold Guard allowed or blocked (from watchdog.py)

A big temperature gauge turns red when the case crosses the food-safety line,
and a live traffic log shows allowed (green) vs blocked (red) commands with a
banner that flashes when the most recent command was blocked.

Run:  streamlit run dashboard.py
"""

import json

import streamlit as st

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
DANGER_C = 4.0  # food-safety line

st.set_page_config(page_title="Cold Guard", layout="wide")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_events(limit=18):
    rows = []
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("fc") in (1, 2, 3, 4):
                        continue  # skip routine reads; show writes and blocks
                    rows.append(row)
    except OSError:
        pass
    return rows[-limit:][::-1]  # newest first


st.title("🧊 Cold Guard — refrigeration watchdog")


@st.fragment(run_every=1)
def render_live_dashboard():
    state = load_state()
    events = load_events()

    left, right = st.columns([1, 1.4])

    with left:
        st.subheader("Fridge")
        if state is None:
            st.info("Waiting for the fridge… start plc.py")
        else:
            temp = state["case_temp_c"]
            danger = temp > DANGER_C
            st.metric(
                "Case temperature",
                f"{temp:.1f} °C",
                delta=f"setpoint {state['setpoint_c']:.1f} °C",
                delta_color="off",
            )
            if danger:
                st.error(f"⚠ ABOVE {DANGER_C:.0f} °C — food-safety line crossed")
            else:
                st.success("Within safe range")
            st.write(
                f"Compressor: {'🟢 on' if state['compressor'] else '⚪ off'}  |  "
                f"Defrost: {'🔴 on' if state['defrost'] else '⚪ off'}"
            )

    with right:
        st.subheader("Cold Guard traffic")
        last_blocked = events and events[0]["decision"] == "BLOCK"
        if last_blocked:
            e = events[0]
            st.error(f"🚫 BLOCKED — {e['rule']}: {e['detail']}  (from {e['src']})")

        blocked = sum(1 for e in events if e["decision"] == "BLOCK")
        allowed = sum(1 for e in events if e["decision"] in ("ALLOW", "BYPASS"))
        c1, c2 = st.columns(2)
        c1.metric("Allowed (recent)", allowed)
        c2.metric("Blocked (recent)", blocked)

        if not events:
            st.info("No traffic yet… start the watchdog and controller")
        for e in events:
            if e["decision"] == "BLOCK":
                st.markdown(
                    f":red[**BLOCK**] `{e['time']}` {e['src']} "
                    f"FC{e['fc']} addr {e['address']} val {e['value']} "
                    f"— **{e['rule']}**: {e['detail']}"
                )
            elif e["decision"] == "BYPASS":
                st.markdown(
                    f":orange[BYPASS] `{e['time']}` {e['src']} "
                    f"FC{e['fc']} addr {e['address']} val {e['value']}"
                )
            else:
                st.markdown(
                    f":green[ALLOW] `{e['time']}` {e['src']} "
                    f"FC{e['fc']} {e['detail']}"
                )


render_live_dashboard()
