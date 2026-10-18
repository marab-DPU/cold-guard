"""
Plant dashboard — the view the judges watch.

It speaks no Modbus. It just reads files the other programs write:
  state.json       the fridge's current temperature and status (from plc.py)
  events.jsonl     one line per command Cold Guard allowed or blocked (from watchdog.py)
  controller.json  whether the controller is going through Cold Guard or
                   direct to the PLC in fail-open (from controller.py)

A big temperature readout turns red when the case crosses the food-safety line,
the hard ceiling and the peak temperature of the current test are shown under
it, and a live traffic log shows allowed (green), blocked (red) and safety
(violet) events, with a banner when the most recent command was blocked.

Run:  streamlit run dashboard.py
"""

import json

import streamlit as st

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
CONTROLLER_FILE = "controller.json"
DANGER_C = 4.0  # food-safety line


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


profile = load_json("profile.json") or {}
CEILING_C = (profile.get("safety_limits") or {}).get("case_temp_max", 0) / 10

st.set_page_config(page_title="Cold Guard", layout="wide")


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
    state = load_json(STATE_FILE)
    events = load_events()
    link = load_json(CONTROLLER_FILE)

    left, right = st.columns([1, 1.4])

    with left:
        st.subheader("Fridge")
        if state is None:
            st.info("Waiting for the fridge… start plc.py")
        else:
            temp = state["case_temp_c"]
            peak = max(st.session_state.get("peak", temp), temp)
            st.session_state["peak"] = peak

            st.metric(
                "Case temperature",
                f"{temp:.1f} °C",
                delta=f"setpoint {state['setpoint_c']:.1f} °C",
                delta_color="off",
            )
            if temp > DANGER_C:
                st.error(f"⚠ ABOVE {DANGER_C:.0f} °C — food-safety line crossed")
            else:
                st.success("Within safe range")
            st.write(
                f"Compressor: {'🟢 on' if state['compressor'] else '⚪ off'}  |  "
                f"Defrost: {'🔴 on' if state['defrost'] else '⚪ off'}"
            )

            c1, c2 = st.columns(2)
            c1.metric("Hard ceiling (always on)", f"{CEILING_C:.1f} °C")
            c2.metric("Peak this test", f"{peak:.1f} °C",
                      delta="under ceiling" if peak <= CEILING_C else "OVER ceiling",
                      delta_color="normal" if peak <= CEILING_C else "inverse")
            if st.button("Reset peak"):
                st.session_state["peak"] = temp

        if link:
            if link.get("path") == "direct":
                st.warning(f"FAIL-OPEN since {link['since']}: Cold Guard is down, "
                           "controller is talking to the PLC directly. Fridge still running.")
            else:
                st.caption(f"Controller → Cold Guard → PLC (since {link['since']})")

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
            if e["decision"] == "SAFETY":
                st.markdown(f":violet[**SAFETY**] `{e['time']}` {e['detail']}")
            elif e["decision"] == "BLOCK":
                color = "violet" if e["rule"] == "safety" else "red"
                st.markdown(
                    f":{color}[**BLOCK**] `{e['time']}` {e['src']} "
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
