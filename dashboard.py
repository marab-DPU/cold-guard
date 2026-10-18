"""
Plant dashboard — the view the judges watch. Runs on the Linux plant box
(192.168.50.20), alongside the PLC and controller.

It speaks no Modbus. It just reads files the other programs write:
  state.json       the fridge's current temperature and status (from plc.py)
  events.jsonl     one line per command Cold Guard allowed or blocked (from watchdog.py)
  controller.json  whether the controller is going through Cold Guard or
                   direct to the PLC in fail-open (from controller.py)

What it shows:
  * a thermometer: the case temperature on a scale with the setpoint, the
    hard ceiling and the food-safety line marked. The column is blue while
    the case is safe, amber once it passes the hard ceiling, red once it
    crosses the food-safety line
  * two large indicator lamps that light up while the compressor and the
    defrost heater are on (they only show state; nothing here is clickable)
  * setpoint, hard ceiling and the peak temperature of the current test
  * a FAIL-OPEN banner while the controller is talking to the PLC directly
  * the live traffic log: allowed (green), bypass (amber), blocked (red),
    safety (violet), with a banner when the most recent command was blocked

Run:  streamlit run dashboard.py
"""

import html
import json

import streamlit as st

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
CONTROLLER_FILE = "controller.json"
DANGER_C = 4.0           # food-safety line
SCALE_LO, SCALE_HI = -30.0, 10.0   # thermometer scale, deg C

BLUE, AMBER, RED, GREEN, VIOLET = "#3fa9ff", "#ffb020", "#ff4d4d", "#2ee57a", "#b58cff"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


profile = load_json("profile.json") or {}
CEILING_C = (profile.get("safety_limits") or {}).get("case_temp_max", 0) / 10

st.set_page_config(page_title="Cold Guard", page_icon="🧊", layout="wide")

st.markdown(f"""
<style>
.block-container {{ padding-top: 3.2rem; }}
.cg-title {{ font-size: 2rem; font-weight: 800; letter-spacing: .02em; margin-bottom: .2rem; }}
.cg-sub {{ color: #8b93a7; margin-bottom: 1rem; }}

.cg-card {{ background: #151922; border: 1px solid #262c3a; border-radius: 16px; padding: 16px 18px; }}

.cg-readout {{ font-size: 3.6rem; font-weight: 800; line-height: 1; }}
.cg-readout small {{ font-size: 1.4rem; color: #8b93a7; font-weight: 600; }}
.cg-status {{ display:inline-block; margin-top:8px; padding: 4px 12px; border-radius: 999px;
              font-weight: 700; font-size: .85rem; letter-spacing: .08em; }}

.cg-lamp {{ border-radius: 20px; padding: 26px 10px 22px; text-align: center;
            border: 2px solid #2a3040; background: #11141b; transition: all .35s ease; }}
.cg-lamp .name {{ font-size: .8rem; letter-spacing: .25em; color: #6f7788; font-weight: 700; }}
.cg-lamp .state {{ font-size: 2.4rem; font-weight: 900; color: #3a4152; margin-top: 4px; }}
.cg-lamp .dot {{ width: 22px; height: 22px; border-radius: 50%; margin: 0 auto 12px;
                 background: #2a3040; }}
.cg-lamp.on-green {{ border-color: #7dffb3;
    background: radial-gradient(circle at 50% 20%, #1f8f4e 0%, #0f5a30 55%, #0b3d22 100%);
    box-shadow: 0 0 42px {GREEN}88, inset 0 0 26px #ffffff22; }}
.cg-lamp.on-green .dot {{ background: {GREEN}; box-shadow: 0 0 18px {GREEN}; }}
.cg-lamp.on-red {{ border-color: #ffb0a0;
    background: radial-gradient(circle at 50% 20%, #c0392b 0%, #7a1f16 55%, #4d140e 100%);
    box-shadow: 0 0 42px {RED}88, inset 0 0 26px #ffffff22; }}
.cg-lamp.on-red .dot {{ background: #ff7a5c; box-shadow: 0 0 18px #ff7a5c; }}
.cg-lamp.on-green .name, .cg-lamp.on-red .name {{ color: #e9ecf3; }}
.cg-lamp.on-green .state, .cg-lamp.on-red .state {{ color: #ffffff; text-shadow: 0 0 14px #ffffffaa; }}

.cg-banner {{ border-radius: 12px; padding: 12px 16px; font-weight: 700; margin-bottom: 10px; }}
.cg-banner.red {{ background: #3a1518; border: 1px solid {RED}; color: #ffd4d4; }}
.cg-banner.amber {{ background: #3a2a10; border: 1px solid {AMBER}; color: #ffe6b3; }}
.cg-banner.green {{ background: #10301f; border: 1px solid {GREEN}; color: #c9ffe0; }}

.cg-row {{ font-family: ui-monospace, Menlo, monospace; font-size: .86rem; padding: 7px 10px;
           margin: 4px 0; border-radius: 8px; background: #11141b; border-left: 5px solid #333;
           color: #d5d9e3; word-break: break-word; }}
.cg-row .t {{ color: #6f7788; margin-right: 8px; }}
.cg-row .tag {{ display:inline-block; min-width: 62px; font-weight: 800; margin-right: 8px; }}
.cg-row.allow {{ border-left-color: {GREEN}; }}  .cg-row.allow .tag {{ color: {GREEN}; }}
.cg-row.bypass {{ border-left-color: {AMBER}; }} .cg-row.bypass .tag {{ color: {AMBER}; }}
.cg-row.block {{ border-left-color: {RED}; background: #1d1316; }} .cg-row.block .tag {{ color: {RED}; }}
.cg-row.safety {{ border-left-color: {VIOLET}; background: #191427; }} .cg-row.safety .tag {{ color: {VIOLET}; }}
</style>
""", unsafe_allow_html=True)


def load_events(limit=16):
    rows = []
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
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


def thermometer_svg(temp, setpoint):
    """An SVG thermometer with the setpoint, hard ceiling and food-safety line marked."""
    W, H = 250, 520
    top, bottom = 30, 408
    cx, tube_w = 90, 34
    bulb_cy, bulb_r = 458, 40

    def y_of(t):
        t = max(SCALE_LO, min(SCALE_HI, t))
        return top + (SCALE_HI - t) / (SCALE_HI - SCALE_LO) * (bottom - top)

    fill = RED if temp > DANGER_C else (AMBER if temp > CEILING_C else BLUE)
    y_temp = y_of(temp)

    parts = [f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'''<defs>
      <linearGradient id="glass" x1="0" x2="1"><stop offset="0" stop-color="#2a3040"/>
        <stop offset=".5" stop-color="#1b2029"/><stop offset="1" stop-color="#2a3040"/></linearGradient>
      <filter id="glow"><feGaussianBlur stdDeviation="4" result="b"/>
        <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    </defs>''')

    # ticks + labels every 5 C
    t = SCALE_LO
    while t <= SCALE_HI + 1e-9:
        y = y_of(t)
        major = int(t) % 10 == 0
        parts.append(f'<line x1="{cx + tube_w/2 + 6}" y1="{y:.1f}" x2="{cx + tube_w/2 + (16 if major else 11)}" '
                     f'y2="{y:.1f}" stroke="#4a5262" stroke-width="1.5"/>')
        if major:
            parts.append(f'<text x="{cx + tube_w/2 + 22}" y="{y + 4:.1f}" font-size="12" fill="#8b93a7">{int(t)}°</text>')
        t += 5

    # the glass tube and bulb (outline)
    parts.append(f'<rect x="{cx - tube_w/2}" y="{top - 14}" width="{tube_w}" height="{bottom - top + 50}" '
                 f'rx="{tube_w/2}" fill="url(#glass)" stroke="#3a4252" stroke-width="2"/>')
    parts.append(f'<circle cx="{cx}" cy="{bulb_cy}" r="{bulb_r}" fill="#1b2029" stroke="#3a4252" stroke-width="2"/>')

    # the liquid
    inner = tube_w - 14
    parts.append(f'<rect x="{cx - inner/2}" y="{y_temp:.1f}" width="{inner}" height="{bulb_cy - y_temp:.1f}" '
                 f'rx="{inner/2}" fill="{fill}" filter="url(#glow)"/>')
    parts.append(f'<circle cx="{cx}" cy="{bulb_cy}" r="{bulb_r - 7}" fill="{fill}" filter="url(#glow)"/>')
    parts.append(f'<circle cx="{cx - 9}" cy="{bulb_cy - 10}" r="6" fill="#ffffff55"/>')

    # reference lines: food-safety, hard ceiling, setpoint
    def marker(val, color, label, dash=""):
        y = y_of(val)
        x0, x1 = cx - tube_w/2 - 56, cx + tube_w/2 + 4
        parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{color}" '
                     f'stroke-width="2" {dash}/>')
        parts.append(f'<text x="{x0}" y="{y - 5:.1f}" font-size="10.5" font-weight="700" fill="{color}">{label}</text>')

    marker(DANGER_C, RED, f"FOOD SAFETY {DANGER_C:.0f}°")
    marker(CEILING_C, AMBER, f"CEILING {CEILING_C:.0f}°")
    marker(setpoint, BLUE, f"SET {setpoint:.0f}°", 'stroke-dasharray="5 4"')

    # current-temperature pointer
    parts.append(f'<polygon points="{cx + tube_w/2 + 2},{y_temp:.1f} {cx + tube_w/2 + 12},{y_temp - 6:.1f} '
                 f'{cx + tube_w/2 + 12},{y_temp + 6:.1f}" fill="#ffffff"/>')
    parts.append("</svg>")
    return "".join(parts)


def lamp(name, on, color):
    cls = f"cg-lamp on-{color}" if on else "cg-lamp"
    return (f'<div class="{cls}"><div class="dot"></div>'
            f'<div class="name">{name}</div><div class="state">{"ON" if on else "OFF"}</div></div>')


def event_row(e):
    t, src = e.get("time", ""), html.escape(str(e.get("src", "")))
    d, rule = e.get("decision"), html.escape(str(e.get("rule", "")))
    detail = html.escape(str(e.get("detail", "")))
    addr = f"FC{e.get('fc')} addr {e.get('address')} val {e.get('value')}"
    if d == "SAFETY":
        return f'<div class="cg-row safety"><span class="t">{t}</span><span class="tag">SAFETY</span>{detail}</div>'
    if d == "BLOCK":
        cls = "safety" if rule == "safety" else "block"
        return (f'<div class="cg-row {cls}"><span class="t">{t}</span><span class="tag">BLOCK</span>'
                f'{src} {addr} — <b>{rule}</b>: {detail}</div>')
    if d == "BYPASS":
        return f'<div class="cg-row bypass"><span class="t">{t}</span><span class="tag">BYPASS</span>{src} {addr}</div>'
    return f'<div class="cg-row allow"><span class="t">{t}</span><span class="tag">ALLOW</span>{src} FC{e.get("fc")} {detail}</div>'


st.markdown('<div class="cg-title">🧊 Cold Guard</div>'
            '<div class="cg-sub">inline Modbus watchdog · supermarket refrigeration</div>',
            unsafe_allow_html=True)


@st.fragment(run_every=1)
def render_live_dashboard():
    state = load_json(STATE_FILE)
    events = load_events()
    link = load_json(CONTROLLER_FILE)

    left, right = st.columns([1.05, 1.25], gap="large")

    with left:
        if state is None:
            st.info("Waiting for the fridge… start plc.py")
        else:
            temp, setpoint = state["case_temp_c"], state["setpoint_c"]
            peak = max(st.session_state.get("peak", temp), temp)
            st.session_state["peak"] = peak

            if temp > DANGER_C:
                status, scolor = "FOOD-SAFETY LINE CROSSED", RED
            elif temp > CEILING_C:
                status, scolor = "ABOVE HARD CEILING", AMBER
            else:
                status, scolor = "WITHIN SAFE RANGE", GREEN

            therm, info = st.columns([1, 1])
            with therm:
                st.markdown(thermometer_svg(temp, setpoint), unsafe_allow_html=True)
            with info:
                st.markdown(
                    f'<div class="cg-readout" style="color:{scolor}">{temp:.1f}<small> °C</small></div>'
                    f'<div class="cg-status" style="background:{scolor}22;color:{scolor};'
                    f'border:1px solid {scolor}">{status}</div>',
                    unsafe_allow_html=True)
                st.write("")
                st.markdown(lamp("COMPRESSOR", state["compressor"], "green"), unsafe_allow_html=True)
                st.write("")
                st.markdown(lamp("DEFROST", state["defrost"], "red"), unsafe_allow_html=True)

            c1, c2, c3 = st.columns(3)
            c1.metric("Setpoint", f"{setpoint:.1f} °C")
            c2.metric("Hard ceiling (always on)", f"{CEILING_C:.1f} °C")
            c3.metric("Peak this test", f"{peak:.1f} °C",
                      delta="under ceiling" if peak <= CEILING_C else "OVER ceiling",
                      delta_color="normal" if peak <= CEILING_C else "inverse")
            if st.button("Reset peak"):
                st.session_state["peak"] = temp

        if link:
            if link.get("path") == "direct":
                st.markdown(f'<div class="cg-banner amber">⚠ FAIL-OPEN since {link["since"]}: Cold Guard '
                            f'is down, controller is talking to the PLC directly. Fridge still running.</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="cg-banner green">🛡 Controller → Cold Guard → PLC '
                            f'(since {link["since"]})</div>', unsafe_allow_html=True)

    with right:
        st.subheader("Cold Guard traffic")
        if events and events[0]["decision"] == "BLOCK":
            e = events[0]
            st.markdown(f'<div class="cg-banner red">🚫 BLOCKED — {html.escape(str(e["rule"]))}: '
                        f'{html.escape(str(e["detail"]))}  (from {html.escape(str(e["src"]))})</div>',
                        unsafe_allow_html=True)

        blocked = sum(1 for e in events if e["decision"] == "BLOCK")
        allowed = sum(1 for e in events if e["decision"] in ("ALLOW", "BYPASS"))
        c1, c2 = st.columns(2)
        c1.metric("Allowed (recent)", allowed)
        c2.metric("Blocked (recent)", blocked)

        if not events:
            st.info("No traffic yet… start the watchdog and controller")
        else:
            st.markdown("".join(event_row(e) for e in events), unsafe_allow_html=True)


render_live_dashboard()
