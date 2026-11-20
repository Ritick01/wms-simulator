"""
test_scenarios.py  —  the test pack for the WMS Monitor.

Each scenario = the simulator settings that create a situation, what the
monitor should find, and the checks the monitor runs after the run
(Real-time tab -> Check test scenario).

setup   form values the simulator page fills in on Load
expect  must        rules the monitor must report (rule ids)
        must_not    rules it must NOT report (a false alarm would)
        no_issues   no problem at all
        min_pct / max_pct   achieved as % of expected (robots / ports)
        guidelines  guideline checks that must read breach or watch
        planted     the simulator's planted defects must all be caught
manual  a step the person does during the run (e.g. stop a robot)
"""

SCENARIOS = {
    "clean": {
        "name": "Clean reference",
        "what": "A good WMS at 500 bins/h, ports staffed to demand. The baseline every other run is read against.",
        "finds": "No problems, about 90% or more of what the ports can deliver.",
        "minutes": 30,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "arrival": 300, "away": 150, "autoclose": 0,
                  "staff": True, "inject": [], "profile": "steady"},
        "expect": {"no_issues": True, "min_pct": 85},
    },
    "peak": {
        "name": "Peak ×3",
        "what": "Demand triples from grid minute 10 to 30. Ports are staffed for the peak from the start.",
        "finds": "Work released too late (short look-ahead) and ports waiting for work outside the peak.",
        "minutes": 40,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": True, "inject": [],
                  "profile": "peak", "peak": {"factor": 3, "from": 10, "to": 30}},
        "expect": {"guidelines": ["lookahead"], "must_not": ["G01", "G05", "G10"]},
    },
    "overload": {
        "name": "Overload",
        "what": "3,000 bins/h — more than the ports and robots can do. The WMS is fine; capacity is the limit.",
        "finds": "No WMS fault; the expected rate names the limit (ports or robots).",
        "minutes": 20,
        "setup": {"targets": "1:3000", "sim_speed": 1000, "pick": 20, "staff": True, "inject": [], "profile": "steady"},
        "expect": {"must_not": ["G01", "G02", "G05", "G10", "E01"]},
    },
    "starve": {
        "name": "Starved ports",
        "what": "Every port open for only 500 bins/h of work — far more ports than the demand needs.",
        "finds": "Busy-polling against 'no work', high bin wait, a low share of what the ports could do.",
        "minutes": 30,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": False, "inject": [], "profile": "steady"},
        "expect": {"must": ["G02"], "max_pct": 70},
    },
    "slow_ops": {
        "name": "Slow operators",
        "what": "60 s per pick. Fewer bins per hour, but the WMS still drives the grid correctly.",
        "finds": "No problems; close to 100% of what slow operators allow.",
        "minutes": 30,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 60, "staff": True, "inject": [], "profile": "steady"},
        "expect": {"no_issues": True, "min_pct": 85},
    },
    "away": {
        "name": "Operator away",
        "what": "Every 5th bin the operator leaves for 150 s with the bin open; no auto port close.",
        "finds": "Bin held at the port too long, with the advice to use an auto port close.",
        "minutes": 20,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "away": 150, "autoclose": 0, "staff": True,
                  "inject": ["A01"], "profile": "steady"},
        "expect": {"must": ["A01"], "planted": True},
    },
    "away_autoclose": {
        "name": "Operator away + auto port close",
        "what": "Same operator away, but the WMS suspends the port after 120 s and resumes when they are back.",
        "finds": "No long holds, no torn-down ports, no ping-pong — the correction works.",
        "minutes": 20,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "away": 150, "autoclose": 120, "staff": True,
                  "inject": ["A01"], "profile": "steady"},
        "expect": {"must_not": ["A01", "G01", "G05"]},
    },
    "polling": {
        "name": "Ports opened too early",
        "what": "Ports open before any work is prepared; the WMS polls openbin while the work trickles in.",
        "finds": "Busy-polling against 'no work' (1015) and a short look-ahead.",
        "minutes": 15,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": True, "inject": ["G02"], "profile": "steady"},
        "expect": {"must": ["G02"], "planted": True},
    },
    "bad_wms": {
        "name": "Bad integration",
        "what": "Planted mistakes: bin released with closeport, ping-pong, polling getbinstate, closebin before the bin arrives.",
        "finds": "Port torn down per bin, ping-pong, polling for arrival, rejected early closebin (1027).",
        "minutes": 20,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": True,
                  "inject": ["G01", "G05", "G07", "E01"], "profile": "steady"},
        "expect": {"planted": True},
    },
    "guidelines": {
        "name": "Guideline breaches",
        "what": "Over-preparation (30% of bins forecast), only ~2 min of work queued, 3 s hand-off at one port.",
        "finds": "Preparation share, look-ahead and hand-off breaches in the guideline check.",
        "minutes": 20,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": True,
                  "inject": ["GL_PREP", "GL_LOOKAHEAD", "GL_HANDOFF"], "profile": "steady"},
        "expect": {"planted": True},
    },
    "adhoc": {
        "name": "Ad-hoc heavy",
        "what": "One bin in five opened by bin id instead of through task groups.",
        "finds": "Ad-hoc bin opens, with how much longer those bins take than task-group bins.",
        "minutes": 20,
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": True, "inject": ["G04"], "profile": "steady"},
        "expect": {"must": ["G04"], "planted": True},
    },
    "grid_stop": {
        "name": "Grid stop",
        "what": "A clean run; part-way through, you stop the grid (put a robot in error or press stop in the Service Console) for a few minutes.",
        "finds": "Grid stopped — reported once, not as a port problem; the simulator waits instead of closing ports.",
        "minutes": 20,
        "manual": "About 5 minutes into the run, stop the grid in the Service Console for 3-5 minutes, then start it again.",
        "setup": {"targets": "1:500", "sim_speed": 1000, "pick": 20, "staff": True, "inject": [], "profile": "steady"},
        "expect": {"must": ["D01"], "must_not": ["D03", "A01"]},
    },
    "full_day": {
        "name": "Full day",
        "what": "24 grid hours at 20×: demand curve from night to the 16:00 peak, ports staffed per hour, breaks, shift changes.",
        "finds": "A day without WMS faults; breaks and unstaffed hours show as planned idle, not as errors.",
        "minutes": 72,
        "setup": {"targets": "1:500", "sim_speed": 2000, "pick": 20, "staff": True, "inject": [], "profile": "day"},
        "expect": {"must_not": ["G01", "G05", "G10", "E01"], "min_pct": 60},
    },
}


def public():
    """What the simulator page and the monitor need, in order."""
    return [dict(v, id=k) for k, v in SCENARIOS.items()]
