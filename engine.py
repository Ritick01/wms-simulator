"""
Live-operation engine for the WMS Simulator.

Two background pieces:

1. LogPublisherListener - a TCP client that connects to the AutoStore Log
   Publisher (default 127.0.0.1:44001), decodes the segment stream
   (DS/DC/T/F/BL/BM/BD/PM/PD/CM/CD/CC/CI ...) and keeps a live view of
   ports and bins, plus a rolling buffer of the raw decoded lines.

2. OperationEngine - drives a realistic AutoStore picking operation against
   the Task Interface, exactly the way a good WMS should:
     * keeps a ~buffer of task groups in the system (look-ahead),
     * for each picking port, runs the open -> pick -> close cycle one bin
       at a time (closebin before the next openbin, fast hand-off),
     * supports suspend / resume (closeport suspend=1 -> openbin taskgroup),
     * supports an ad-hoc bin request,
   recording every API call (request + response) so the UI can show it.

It talks to the SDK through AutoStoreClient, so the SAME engine runs against
the SDK simulation or a live grid - only the host/port changes.
"""

import os
import random
import socket
import threading
import time
import re
from collections import Counter, deque
from datetime import datetime, timezone

from autostore_client import AutoStoreClient, BIN_MODE  # noqa: F401


# ---------------------------------------------------------------------------
# value-code lookups (Log Publisher)  -- single char -> label
# ---------------------------------------------------------------------------
LP_BIN_MODE = {"G": "GRID", "F": "FORECAST", "T": "PREPARED", "P": "PORT",
               "R": "TOGRID", "O": "OPEN", "C": "CLOSED", "X": "OUTSIDE",
               "D": "DELETED"}
LP_PORT_MODE = {"O": "OPEN", "I": "INSERT", "A": "AUTO", "C": "CLOSED",
                "S": "SEQUENCE", "R": "RUNOUT", "M": "MULTIPORT_PUPPET",
                "D": "DISABLED"}
LP_PORT_TYPE = {"CO": "ConveyorPort", "ECO": "ConveyorPort Ext", "CA": "CarouselPort",
                "VE": "VersaPort", "VEAI": "VersaAI", "SW": "SwingPort",
                "RE": "RelayPort", "TC": "Transfer/Custom", "FU": "FusionPort",
                "MU": "MultiPort", "PI": "PickUpPort", "AC": "AutoCase",
                "GL": "Grid Lift"}
LP_ORIENT = {"N": "North", "S": "South", "E": "East", "W": "West", "U": "Unknown"}


def now_iso() -> str:
    """UTC ISO-8601 with Z, the format create/update_taskgroup require."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmmss() -> str:
    return datetime.now().strftime("%H:%M:%S")


_BIN_MODE_RE = re.compile(r"<bin_mode>\s*([A-Za-z]+)\s*</bin_mode>")

# getportstatus field extractors (fields come back nested in <port>…)
def _xf(name, text):
    m = re.search(rf"<{name}>\s*([^<]+?)\s*</{name}>", text or "")
    return m.group(1).strip() if m else None

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")

# Defects the simulator can plant on purpose (the SDK half of the monitor's
# golden test set). Key = the monitor rule it should trigger.
INJECTIONS = {
    "G01": "release a bin with closeport instead of closebin (port torn down per bin)",
    "G02": "skip the prepare phase: open ports first, feed work slowly (openbin polls 'no work')",
    "G04": "open some bins ad-hoc by bin id instead of through task groups",
    "G05": "put a just-released bin into the next task group (ping-pong)",
    "G06": "re-open ports with a different category during the shift",
    "G07": "poll getbinstate for bin arrival instead of using Log Publisher events",
    "A01": "operator away: no closebin for 'Operator away' seconds (default 150 s grid time); "
           "with 'Auto port close' on, the WMS suspends the port instead",
    "C02": "put a bin that an active task group still needs into a new group",
    "E01": "send closebin before the bin has arrived (1027)",
    # guideline breaches (the monitor's guideline check, not a T2 rule)
    "GL_PREP": "forecast 30% of the bin pool at the start (guideline: at most 25% of bins requested for preparation)",
    "GL_LOOKAHEAD": "keep only ~2 min of work queued (guideline: ~30 min, at least 10-15 min per category)",
    "GL_HANDOFF": "wait 3 s (grid time) after closebin before the next openbin at the first picking port (guideline: ~100 ms)",
}
PORT_INJECTIONS = ("G01", "G04", "G06", "G07", "A01", "E01")
GL_PREP_SHARE = 0.30          # share of the bin pool forecast by GL_PREP
GL_LOOKAHEAD_MIN = 2          # minutes of work kept queued under GL_LOOKAHEAD
GL_HANDOFF_SEC = 3            # grid seconds between closebin and openbin under GL_HANDOFF
GRID_STOPPED = {"STP", "STOP", "STOPPED", "ERR", "ERROR", "ESTOP"}   # Log Publisher SM values
BIN_EXCHANGE_S = 5            # grid seconds to swap bins at a port (port sizing for staff-to-demand)
# Full-day demand, × the category target, hour 0..23: quiet night, morning
# ramp, lunch dip, afternoon peak, evening wind-down.
DAY_CURVE = [0.2, 0.2, 0.2, 0.2, 0.3, 0.5, 0.8, 1.2, 1.5, 1.4, 1.2, 1.1,
             0.6, 0.9, 1.3, 1.8, 2.0, 1.7, 1.2, 0.9, 0.6, 0.4, 0.3, 0.2]
# Operator rota for the full day (grid minutes), overridable per scenario
DAY_ROTA = {"break_every_min": 120, "break_min": 15, "shift_hours": 8, "handover_min": 5}

# Methods that CHANGE the grid. Blocked in observe mode so the tool can
# never disrupt a live production WMS — it only reads and listens.
WRITE_METHODS = {
    "openport", "openbin", "closebin", "closeport", "insertbin", "removebin",
    "create_taskgroup", "update_taskgroup", "cancel_taskgroup", "add_task",
    "cancel_task", "setportsequence", "appendportsequence", "flushport",
    "showpicktolight", "hidepicktolight", "enablemultiport", "disablemultiport",
    "flushportbin",
    "forecastbin", "cancelforecastbin", "updatebin", "createbin", "deletebin",
    "setconfiguration", "configureport", "forcecharge",
    "portcellallocate", "portlocationready", "portbinready", "portbinmode",
    "case_insert", "case_extract", "case_cancel", "case_recvready",
    "start_simulation", "stop_simulation", "set_simulation_speed",
}
READ_METHODS = {
    "getportstatus", "getbinstate", "getbininfo", "getbinloc",
    "getnexttaskgroups", "getdrawerstate", "getportconfig", "getportweight",
    "getsystemstate", "getsystemmode", "getversion", "gettaskinfo",
    "getportsequence", "getcellcontent", "gettimefactor",
}


# ===========================================================================
# Log Publisher listener
# ===========================================================================
class LogPublisherListener:
    def __init__(self, host="127.0.0.1", port=44001):
        self.host = host
        self.port = port
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.connected = False
        self.error = None
        self.sim_time = None        # unix time from T segment
        self.time_factor = None     # from F segment (100 = normal)
        self.ports = {}             # port_id -> {mode,type,orientation,cells}
        self.bins = {}              # bin_id  -> {mode,grid,x,y,depth,content,height,port}
        self.robots = set()         # robot ids seen in RE segments
        self.system_mode = None     # SM segment: RUN / STP ...
        self.on_system_mode = None  # callback(mode) on change
        self.on_port_mode = None    # callback(port, mode) on change
        self.on_robot = None        # callback(robot, code) on change
        self.robot_codes = {}
        self.lines = deque(maxlen=400)   # decoded, human-readable recent events

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.connected = False

    # -- main loop ---------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=5) as sock:
                    self.connected = True
                    self.error = None
                    self._note("-- connected to Log Publisher --")
                    buf = b""
                    sock.settimeout(1.0)
                    while not self._stop.is_set():
                        try:
                            chunk = sock.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._handle(line.decode("ascii", "replace").strip())
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self.connected = False
            if not self._stop.is_set():
                time.sleep(2)  # retry

    # -- decoding ----------------------------------------------------------
    def _note(self, text):
        with self._lock:
            self.lines.append(f"{_hhmmss()}  {text}")

    def _handle(self, line):
        if not line:
            return
        parts = line.split(",")
        tag = parts[0]
        try:
            if tag == "T":
                if len(parts) > 1:
                    self.sim_time = parts[1]
                return  # heartbeat, don't log every second
            if tag == "F" and len(parts) > 1:
                self.time_factor = parts[1]
                self._note(f"F  time factor = {parts[1]} ({int(parts[1])/100:g}x)")
                return
            if tag == "DS":
                self._note("DS  initial download start"); return
            if tag == "DC":
                self._note("DC  initial download complete"); return
            if tag == "SM" and len(parts) > 1:
                changed = parts[1] != self.system_mode
                self.system_mode = parts[1]
                if changed and self.on_system_mode:
                    self.on_system_mode(parts[1])
                self._note(f"SM  system mode = {parts[1]}"); return
            if tag == "PM" and len(parts) > 2:
                pid, mode = parts[1], parts[2]
                with self._lock:
                    self.ports.setdefault(pid, {})["mode"] = LP_PORT_MODE.get(mode, mode)
                self._note(f"PM  port {pid} mode -> {LP_PORT_MODE.get(mode, mode)}")
                if self.on_port_mode:
                    self.on_port_mode(pid, LP_PORT_MODE.get(mode, mode))
                return
            if tag == "PD" and len(parts) >= 5:
                pid, ptype = parts[1], parts[2]
                orient = parts[-1]
                with self._lock:
                    p = self.ports.setdefault(pid, {})
                    p["type"] = LP_PORT_TYPE.get(ptype, ptype)
                    p["orientation"] = LP_ORIENT.get(orient, orient)
                return
            if tag == "BL":
                self._bl(parts); return
            if tag == "BM" and len(parts) > 2:
                bid, mode = parts[1], parts[2]
                with self._lock:
                    self.bins.setdefault(bid, {})["mode"] = LP_BIN_MODE.get(mode, mode)
                self._note(f"BM  bin {bid} mode -> {LP_BIN_MODE.get(mode, mode)}")
                return
            if tag == "CI" and len(parts) > 3:
                kind = "delivery (outbound)" if parts[3] == "1" else "pickup (inbound)"
                self._note(f"CI  cell {parts[1]},{parts[2]} allocated for {kind}")
                return
            if tag == "RE":
                # robot segment RE,<robot>,<code>: count the fleet; the code is read as its error (0 = none)
                rid = parts[1] if len(parts) > 1 else str(len(self.robots))
                code = parts[2] if len(parts) > 2 else "0"
                with self._lock:
                    self.robots.add(rid)
                    changed = self.robot_codes.get(rid) != code
                    self.robot_codes[rid] = code
                if changed and self.on_robot:
                    self.on_robot(rid, code)
                return
            if tag in ("LF", "PE", "AL", "CL", "LCL"):
                self._note(f"{tag}  {line}")   # surface errors/locks verbatim
                return
        except Exception:
            pass  # never let a malformed segment kill the listener

    def _bl(self, parts):
        # BL,bin_id,grid[,x,y,depth[,mode[,content,height]]]
        bid = parts[1]
        rec = {}
        if len(parts) >= 3:
            rec["grid"] = parts[2]
        if len(parts) >= 6:
            rec["x"], rec["y"], rec["depth"] = parts[3], parts[4], parts[5]
        if len(parts) >= 7:
            rec["mode"] = LP_BIN_MODE.get(parts[6], parts[6])
        if len(parts) >= 9:
            rec["content"], rec["height"] = parts[7], parts[8]
        with self._lock:
            self.bins.setdefault(bid, {}).update(rec)

    # -- snapshot for the UI ----------------------------------------------
    def snapshot(self):
        with self._lock:
            return {
                "connected": self.connected,
                "error": self.error,
                "sim_time": self.sim_time,
                "time_factor": self.time_factor,
                "ports": dict(self.ports),
                "bin_count": len(self.bins),
                "lines": list(self.lines)[-120:],
            }


# ===========================================================================
# Operation engine
# ===========================================================================
class OperationEngine:
    def __init__(self):
        self.client = None
        self.lp = None
        self.cfg = {}
        self.running = False
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()
        self._tg_counter = 0
        self.groups = {}          # tg_id -> {category, bins:[...], state}
        self.ports = {}           # port_id -> live per-port op state
        self.calllog = deque(maxlen=5000)   # newest first; the monitor fetches by seq
        self._seq = 0
        self._clock = (time.time(), 1.0)    # (wall anchor, grid-time factor)
        self._clock_grid0 = time.time()
        self._log_lock = threading.Lock()
        self.request_log = None             # path of the current request-log file
        self.inject = set()
        self._work = {}                     # category -> Event set when work is released
        self._tokens = {}                   # category -> released bins not yet presented
        self._tok_cv = threading.Condition()
        self.inject_every = 5
        self.injected = Counter()
        self.run_id = None
        self._suspend = {}        # port_id -> Event (set = should suspend)
        self._adhoc = {}          # port_id -> bin_id requested ad-hoc
        self._confirm = {}        # port_id -> Event (set = operator confirmed pick)
        self.mode = "observe"     # observe (read-only) | drive (test grid only)
        self.monitor = {}         # port_id -> live read-only KPIs (observe mode)
        self._mon_stop = threading.Event()
        self.status_line = "idle"
        self.phase = "idle"       # idle | preparing | running | stopped
        self.prep_total = 0       # task groups to build in the prep phase
        self.prep_done = 0        # task groups actually created (prep)
        self.prep_bins = 0        # bins of look-ahead being prepared
        self._phase_started = None

    # -- safety: is a write allowed right now? -----------------------------
    def _write_blocked(self, method):
        if self.mode == "observe" and method in WRITE_METHODS:
            return (f"OBSERVE mode is read-only — '{method}' is blocked so the "
                    f"tool cannot disrupt the live grid's WMS.")
        return None

    # -- grid clock --------------------------------------------------------
    # The SDK can run faster than real time (sim speed 1000 = 10×). Logs are
    # stamped in GRID time so a 10× run reads like a real site's log: every
    # duration and bins/h the monitor computes is then real-world comparable.
    def _set_clock(self, speed):
        now = time.time()
        grid_now = self.grid_time(now)
        factor = max(0.01, float(speed or 100) / 100.0)
        self._clock_grid0, self._clock = grid_now, (now, factor)

    def grid_time(self, wall=None):
        wall = time.time() if wall is None else wall
        w0, f = self._clock
        return self._clock_grid0 + (wall - w0) * f

    def grid_sleep(self, grid_seconds, min_wall=0.05):
        # min_wall keeps polling loops from spinning; short deliberate pauses
        # (the hand-off) pass 0 so they stay short in grid time at high speed
        time.sleep(max(min_wall, grid_seconds / self._clock[1]))

    def _grid_iso(self, wall=None):
        t = datetime.fromtimestamp(self.grid_time(wall)).astimezone()
        off = t.strftime("%z")
        return t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + off[:3] + ":" + off[3:]

    # -- request log (AutoStore format, one call per line) --------------------
    def _record(self, kind, req, resp, dur_ms):
        """Client hook: every call the simulated WMS makes, in the same line
        format as an AutoStore request log, so the monitor can follow or replay
        it like a real site's log."""
        if kind == "simulation" or self.mode != "drive":
            return
        one = lambda x: re.sub(r"\s*[\r\n]+\s*", "", re.sub(r"<\?xml[^>]*\?>", "", (x or "")).strip())
        ts = self._grid_iso()
        day = ts[:10].replace("-", "")
        # one file per run (AutoStore-style name + run id) so a replay never
        # mixes runs; calls outside a run go to the plain daily file
        suffix = f"_{self.run_id}" if self.run_id else ""
        path = os.path.join(LOG_DIR, f"request-log{day}{suffix}.txt")
        line = f"{ts},{one(req)},{one(resp)},/api/v2/{kind},{dur_ms:.2f}\n"
        with self._log_lock:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self.request_log = path
            except OSError:
                pass

    # -- call wrapper: run a client method and record it -------------------
    def _call(self, label, fn, *args, **kwargs):
        res = fn(*args, **kwargs)
        entry = {
            "seq": None,
            "ts_iso": self._grid_iso(),
            "time": _hhmmss(),
            "label": label,
            "ok": res.get("ok"),
            "status": res.get("status"),
            "fault_code": res.get("fault_code"),
            "fault_text": res.get("fault_text"),
            "error": res.get("error"),
            "duration_ms": res.get("duration_ms"),
            "request_xml": res.get("request_xml"),
            "response_xml": res.get("response_xml"),
            "params": res.get("params", {}),
        }
        # number and store in one step, so a reader polling by seq never
        # sees seq N+1 before seq N (that would skip a call)
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.calllog.appendleft(entry)
        return res

    # -- setup / teardown --------------------------------------------------
    def configure(self, cfg):
        """cfg: {host, port, lp_host, lp_port, ports:[{port_id,type,role,category}],
                 bin_pool:[...], buffer_target, tasks_per_group, estpicktime}"""
        self.stop()
        self._mon_stop.set()        # stop any previous monitor
        self.cfg = cfg
        self.mode = cfg.get("mode", "observe")
        self.client = AutoStoreClient(cfg.get("host", "localhost"),
                                      int(cfg.get("port", 44000)))
        self.client.recorder = self._record
        with self._lock:
            self.calllog.clear()        # a new configuration starts a clean call log
        self.run_id = None
        self.inject = {k for k in (cfg.get("inject") or []) if k in INJECTIONS}
        self.inject_every = max(2, int(cfg.get("inject_every", 5)))
        self.injected = Counter()
        self._set_clock(100)
        self.lp = LogPublisherListener(cfg.get("lp_host", "127.0.0.1"),
                                       int(cfg.get("lp_port", 44001)))
        self.lp.on_system_mode = self._mark_system_mode
        self.lp.on_port_mode = lambda pid, mode: self._grid_event(
            f"lp_port_mode {pid} {mode} (Log Publisher: port mode)", {"port_id": pid, "mode": mode})
        self.lp.on_robot = lambda rid, code: self._grid_event(
            f"lp_robot {rid} code {code} (Log Publisher: robot)", {"robot": rid, "code": code})
        self.lp.start()
        self.groups = {}
        self.ports = {}
        # seed a fresh task-group id range each configure (avoid 1032 collisions)
        self._tg_base = 100000 + (int(time.time()) % 800000)
        self._tg_counter = 0
        self._bin_offset = random.randrange(1_000_000)
        for p in cfg["ports"]:
            pid = str(p["port_id"])
            self.ports[pid] = {
                "port_id": pid, "type": p.get("type", "Generic"),
                "role": p.get("role", "picking"),
                "category": p.get("category", "1"),
                "confirm": p.get("confirm", "auto"),   # auto | manual (human presses confirm)
                "fill_content": p.get("fill_content", "11"),  # putaway: content after drop
                "state": "ready", "open_bin": None, "open_task": None,
                "picked": 0, "active_tg": None, "last": "",
                "n": 0, "last_closed_bin": None,
            }
            self._suspend[pid] = threading.Event()
            self._confirm[pid] = threading.Event()

        # --- scenario + demand model ------------------------------------
        # categories that have at least one picking port (so we feed each)
        self.pick_categories = sorted({p["category"] for p in self.ports.values()
                                       if p["role"] == "picking"})
        self._work = {c: threading.Event() for c in {p["category"] for p in self.ports.values()}}
        self._tokens = {c: 0 for c in self._work}
        # per-category target throughput (bins/hour). default if not given.
        default_t = int(cfg.get("default_target", 150))
        self.targets = {str(k): int(v) for k, v in (cfg.get("targets") or {}).items()}
        for c in self.pick_categories:
            self.targets.setdefault(c, default_t)
        # scenario profile: steady | peak | shift
        self.scenario = cfg.get("scenario", {"profile": "steady"})
        self.lookahead_min = int(cfg.get("lookahead_min", 30))
        # operator + WMS timing, all in grid seconds
        self.pick_time_s = float(cfg.get("pick_time_s") or cfg.get("estpicktime") or 20)
        self.bin_arrival_timeout_s = float(cfg.get("bin_arrival_timeout_s") or 300)
        self.operator_away_s = float(cfg.get("operator_away_s") or 150)
        self.auto_port_close_s = float(cfg.get("auto_port_close_s") or 0)   # 0 = off
        self.auto_closed = 0
        if "GL_LOOKAHEAD" in self.inject:
            self.lookahead_min = min(self.lookahead_min, GL_LOOKAHEAD_MIN)
        # GL_HANDOFF slows one port only, so the monitor can name it
        picking = sorted((p for p in self.ports.values() if p["role"] == "picking"),
                         key=lambda p: p["port_id"])
        self.slow_handoff_port = picking[0]["port_id"] if picking and "GL_HANDOFF" in self.inject else None
        self.adhoc_pct = float(cfg.get("adhoc_pct", 0))
        if "G04" in self.inject and not self.adhoc_pct:
            self.adhoc_pct = 20.0
        # how long to let the prepared buffer settle before opening ports.
        # a real WMS waits the full look-ahead; for the sim the bins come up
        # fast, so we open once bins are prepared or this many seconds pass.
        self.prep_settle_sec = int(cfg.get("prep_settle_sec", 20))
        # SDK simulation speed (100 = real time / 1×, 1000 = 10×, …). Pushed to
        # the SDK when the operation starts so a 30-min shift compresses into a
        # few minutes of wall-clock without changing any of the logic.
        self.sim_speed = int(cfg.get("sim_speed", 100))
        self._run_start = None
        self._run_start_grid = None
        self.phase = "idle"
        self.prep_total = self.prep_done = self.prep_bins = 0
        # bin pool. "auto" (or empty) => discover the REAL bins from the grid
        # via the Log Publisher initial download (BL segments list every bin).
        # This is what stops the "only 5 bins, reused all day" problem: a real
        # grid has thousands of bins, so a 24 h run spreads across all of them.
        if self.mode == "drive":
            pool = cfg.get("bin_pool") or []
            auto = (not pool) or (len(pool) == 1 and str(pool[0]).lower() == "auto")
            if auto:
                # wait briefly for the LP initial download (DS..DC) to arrive
                for _ in range(40):
                    if len(self.lp.bins) > 0:
                        break
                    time.sleep(0.2)
                time.sleep(0.8)  # let the rest of the BL burst land
                self.cfg["bin_pool"] = self.grid_bins()
                n = len(self.cfg["bin_pool"])
                self.status_line = (f"using {n} real bins discovered from the grid"
                                    if n else "no bins seen on the Log Publisher yet "
                                    "— is the grid loaded & LP connected?")
            else:
                self._validate_bins()

        # observe mode: start the read-only monitor immediately
        self.monitor = {}
        if self.mode == "observe":
            self._mon_stop = threading.Event()
            threading.Thread(target=self._monitor_loop, args=(self._mon_stop,),
                             daemon=True).start()
            self.status_line = f"OBSERVE (read-only) · {len(self.ports)} port(s)"
        else:
            self.status_line = f"DRIVE (test grid) · {len(self.ports)} port(s)"
        return self.state()

    # -- observe mode: read-only KPI monitor -------------------------------
    def _monitor_loop(self, stop):
        """Poll getportstatus (read-only) and compute live KPIs per port.
        Waitbin = seconds a port has sat open with no ready bin."""
        hist = {}  # port_id -> {last_bin, wait_start, deliveries: deque}
        while not stop.is_set():
            # ports: discovered from Log Publisher PD segments, else configured
            pids = list(self.lp.ports.keys()) if self.lp else []
            if not pids:
                pids = list(self.ports.keys())
            now = time.time()
            for pid in pids:
                r = self.client.getportstatus(pid)   # read-only query
                txt = r.get("response_xml", "") or ""
                mode = _xf("mode", txt)
                sel_bin = _xf("selected_bin", txt) or "0"
                ready = _xf("is_ready", txt)
                err = _xf("error", txt)
                h = hist.setdefault(pid, {"last_bin": None, "wait_start": None,
                                          "deliveries": deque(maxlen=400)})
                is_ready = ready in ("1", "true", "True")
                open_mode = (mode == "OPEN")
                # count a delivery when a new bin becomes ready at the port
                if is_ready and sel_bin not in ("0", None) and sel_bin != h["last_bin"]:
                    h["last_bin"] = sel_bin
                    h["deliveries"].append(now)
                    h["wait_start"] = None
                # waitbin: port open but nothing ready -> accumulate idle time
                waitbin = 0
                if open_mode and not is_ready:
                    if h["wait_start"] is None:
                        h["wait_start"] = now
                    waitbin = int(now - h["wait_start"])
                else:
                    h["wait_start"] = None
                # throughput: deliveries in the last 5 min -> bins/hour
                recent = [t for t in h["deliveries"] if now - t <= 300]
                tph = int(len(recent) * 12) if recent else 0
                self.monitor[pid] = {
                    "port_id": pid, "mode": mode or "–",
                    "selected_bin": sel_bin, "ready": is_ready,
                    "error": err not in (None, "0", "false", "False"),
                    "waitbin": waitbin, "throughput_hr": tph,
                    "reachable": r.get("ok") or r.get("fault_code") is not None,
                }
            time.sleep(2)

    def _next_tg(self):
        self._tg_counter += 1
        # base is seeded per configure so reruns don't collide with task
        # groups the SDK still remembers (which caused fault 1032).
        return self._tg_base + self._tg_counter

    def _bins_for_group(self):
        pool = [str(b) for b in self.cfg.get("bin_pool", [])]
        n = int(self.cfg.get("tasks_per_group", 3))
        if not pool:
            return []
        # each run starts at its own point in the pool, so bins that an earlier
        # run left queued in the SDK don't come back as ping-pong (G05)
        off = getattr(self, "_bin_offset", 0)
        bins = [pool[(off + self._tg_counter * n + i) % len(pool)] for i in range(n)]
        if self.phase == "running" and self._tg_counter % self.inject_every == 0:
            if "G05" in self.inject:
                recent = [p["last_closed_bin"] for p in self.ports.values() if p.get("last_closed_bin")]
                if recent and recent[0] not in bins:
                    bins[0] = recent[0]
                    self.injected["G05"] += 1
            elif "C02" in self.inject:
                active = [g for g in self.groups.values() if g["state"] == "active" and g["bins"]]
                if active and active[-1]["bins"][0] not in bins:
                    bins[-1] = active[-1]["bins"][0]
                    self.injected["C02"] += 1
        return bins

    def grid_bins(self):
        """Every bin id the Log Publisher broadcast for the loaded grid
        (from BL segments in the initial download). Sorted numerically."""
        if not self.lp:
            return []
        with self.lp._lock:
            ids = list(self.lp.bins.keys())
        def _k(b):
            try:
                return (0, int(b))
            except ValueError:
                return (1, b)
        return sorted(ids, key=_k)

    def grid_ports(self):
        """Ports the Log Publisher broadcast for the loaded grid (PD/PM
        segments), as [{port_id, type, orientation}] sorted by id."""
        if not self.lp:
            return []
        with self.lp._lock:
            items = list(self.lp.ports.items())
        out = []
        for pid, p in items:
            out.append({"port_id": pid, "type": p.get("type", "ConveyorPort"),
                        "orientation": p.get("orientation", "")})
        def _k(r):
            try:
                return (0, int(r["port_id"]))
            except ValueError:
                return (1, r["port_id"])
        return sorted(out, key=_k)

    def discover(self, lp_host="127.0.0.1", lp_port=44001, wait=4.0):
        """Connect to the Log Publisher and read the REAL grid: its ports
        (id + type) and how many bins exist. Lets the UI load the actual grid
        instead of a hand-typed guess. Read-only — never sends a command."""
        lp = LogPublisherListener(lp_host, int(lp_port))
        lp.start()
        deadline = time.time() + wait
        while time.time() < deadline:
            if lp.ports and lp.bins:
                time.sleep(0.6)   # let the rest of the initial download land
                break
            time.sleep(0.2)
        with lp._lock:
            ports = list(lp.ports.items())
            bin_ids = list(lp.bins.keys())
            connected, error = lp.connected, lp.error
        lp.stop()
        def _pk(kv):
            try:
                return (0, int(kv[0]))
            except ValueError:
                return (1, kv[0])
        ports_out = [{"port_id": pid, "type": p.get("type", "ConveyorPort"),
                      "orientation": p.get("orientation", "")}
                     for pid, p in sorted(ports, key=_pk)]
        return {"connected": connected, "error": error,
                "ports": ports_out, "bin_count": len(bin_ids)}

    def _validate_bins(self):
        """Drop pool bins that don't exist in the grid (would fault 1011)."""
        pool = [str(b) for b in self.cfg.get("bin_pool", [])]
        good, dropped = [], []
        for b in pool:
            r = self.client.getbinstate(b)
            if r.get("fault_code") == 1011:
                dropped.append(b)
            else:
                good.append(b)
        self.cfg["bin_pool"] = good
        if dropped:
            self.status_line = f"ignored non-existent bins: {', '.join(dropped)}"

    def _grid_elapsed_min(self):
        g0 = getattr(self, "_run_start_grid", None)
        if g0 is None:
            return (time.time() - self._run_start) / 60.0
        return (self.grid_time() - g0) / 60.0

    def _day_factor(self, elapsed_min):
        """Full day: demand follows the hourly curve, starting at start_hour."""
        curve = self.scenario.get("hourly") or DAY_CURVE
        hour = int(self.scenario.get("start_hour", 6)) + int(elapsed_min // 60)
        return float(curve[hour % len(curve)])

    def demand_factor(self):
        """Scenario demand multiplier at the current moment."""
        prof = self.scenario.get("profile", "steady")
        if prof == "steady" or not self._run_start:
            return 1.0
        elapsed = self._grid_elapsed_min()   # grid minutes, so windows match the log
        if prof == "day":
            return self._day_factor(elapsed)
        if prof == "shift":
            ramp = float(self.scenario.get("ramp_min", 30))
            dur = float(self.scenario.get("duration_min", 480))
            if elapsed < ramp:
                return max(0.1, elapsed / ramp)
            if elapsed > dur - ramp:
                return max(0.0, (dur - elapsed) / ramp)
            return 1.0
        if prof == "peak":
            for w in self.scenario.get("peak_windows", []):
                if float(w.get("start_min", 0)) <= elapsed < float(w.get("end_min", 0)):
                    return float(w.get("factor", 2.0))
            return float(self.scenario.get("base_factor", 1.0))
        return 1.0

    # -- the operation -----------------------------------------------------
    def start(self):
        if self.mode == "observe":
            self.status_line = ("OBSERVE mode is read-only — switch to DRIVE "
                                "(test grid only) to run the operation.")
            return self.state()
        if self.running:
            return self.state()
        if not self.client:
            self.status_line = "not configured"
            return self.state()
        # a fresh stop event for THIS run; old workers hold the previous
        # (already-set) event and exit, so one run = one worker per port.
        self._stop = threading.Event()
        self._run_start = time.time()
        self.running = True
        # the whole operation runs in one supervisor thread: first PREPARE the
        # look-ahead buffer (ports stay closed), then OPEN the ports and run.
        t = threading.Thread(target=self._operation, args=(self._stop,), daemon=True)
        t.start(); self._threads.append(t)
        return self.state()

    def _operation(self, stop):
        """Two phases, the way a real WMS runs a shift:
        1. PREPARE — build a look-ahead buffer of task groups while ports are
           still closed, so bins are already coming up when we open.
        2. RUN — open the ports, start the per-port pick loop, and keep the
           feeder topping the buffer up to target."""
        # push the SDK simulation speed so a 30-min shift compresses in
        # wall-clock (100 = 1×, 1000 = 10×). Logic is unchanged — only the
        # grid's clock runs faster.
        if self.sim_speed and self.sim_speed != 100:
            self._call(f"set_simulation_speed {self.sim_speed}",
                       self.client.set_simulation_speed, self.sim_speed)
        self._set_clock(self.sim_speed or 100)
        self._run_start_grid = self.grid_time()
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._run_started_iso = self._grid_iso()
        if "GL_LOOKAHEAD" in self.inject:
            self.injected["GL_LOOKAHEAD"] += 1     # run-wide: the whole run keeps a short buffer
        if "G02" in self.inject:
            # planted defect: no look-ahead — ports open on an empty buffer and
            # the feeder trickles work in, so openbin keeps answering 'no work'
            self.injected["G02"] += 1
            self.phase = "running"
            self._phase_started = time.time()
            self.status_line = "running · INJECT G02: ports opened before any work was prepared"
            staffed = self._staff_ports()
            for pid, p in self.ports.items():
                if p["role"] in ("picking", "putaway") and pid in staffed:
                    w = threading.Thread(target=self._port_worker, args=(pid, stop), daemon=True)
                    w.start(); self._threads.append(w)
            self.grid_sleep(60)
            f = threading.Thread(target=self._feeder, args=(stop,), daemon=True)
            f.start(); self._threads.append(f)
            return
        # ---- PHASE 1: prepare the buffer -------------------------------
        self.phase = "preparing"
        self._phase_started = time.time()
        if "GL_PREP" in self.inject:
            self._over_forecast()
        self._prepare_buffer(stop)
        if stop.is_set():
            return
        # let the prepared bins settle (a real WMS waits the full look-ahead;
        # in the sim the bins come up fast, so we wait prep_settle_sec).
        settle_until = time.time() + self.prep_settle_sec
        while time.time() < settle_until and not stop.is_set():
            self.status_line = (f"buffer ready ({self.prep_done} groups) — "
                                f"opening ports in {int(settle_until - time.time())}s")
            time.sleep(1)
        if stop.is_set():
            return
        # ---- PHASE 2: open ports and run -------------------------------
        self.phase = "running"
        self._phase_started = time.time()
        prof = self.scenario.get("profile", "steady")
        staffed = self._staff_ports()
        self.status_line = (f"running · scenario={prof} · targets={self.targets} · "
                            f"{len(staffed)} of {len(self.ports)} ports staffed")
        f = threading.Thread(target=self._feeder, args=(stop,), daemon=True)
        f.start(); self._threads.append(f)
        for pid, p in self.ports.items():
            if p["role"] in ("picking", "putaway") and pid in staffed:
                w = threading.Thread(target=self._port_worker, args=(pid, stop), daemon=True)
                w.start(); self._threads.append(w)

    def _staff_ports(self):
        """Open only as many picking ports per category as its demand needs, like
        a site staffing ports to order volume. Opening every port for a small
        demand starves them all (1015 answers, idle ports, a falling score).
        One port works ~3600 / (pick time + bin exchange) bins/h."""
        roles = ("picking", "putaway")
        everyone = {pid for pid, p in self.ports.items() if p["role"] in roles}
        if not self.cfg.get("staff_to_demand", True):
            return everyone
        per_port = 3600.0 / max(1.0, self.pick_time_s + BIN_EXCHANGE_S)
        prof = self.scenario.get("profile", "steady")
        peak = 1.0
        if prof == "peak":
            peak = max([float(self.scenario.get("base_factor", 1.0))] +
                       [float(w.get("factor", 1.0)) for w in self.scenario.get("peak_windows", [])])
        elif prof == "day":
            # workers for the busiest hour; _wanted() decides who works each hour
            peak = max(float(x) for x in (self.scenario.get("hourly") or DAY_CURVE))

        def _k(pid):
            try:
                return (0, int(pid))
            except ValueError:
                return (1, pid)
        staffed = {pid for pid, p in self.ports.items() if p["role"] == "putaway"}
        self._cat_order = {}
        for cat in self.pick_categories:
            ports = sorted((pid for pid, p in self.ports.items()
                            if p["role"] == "picking" and p["category"] == cat), key=_k)
            self._cat_order[cat] = ports
            need = max(1, -(-int(self.targets.get(cat, 150) * peak) // int(per_port)))
            staffed |= set(ports[:need])
        for pid in everyone - staffed:
            self.ports[pid]["state"] = "standby"
            self.ports[pid]["last"] = (f"not staffed: demand needs fewer ports "
                                       f"(~{per_port:.0f} bins/h per port)")
        self.staffed = sorted(staffed, key=_k)
        if getattr(self, "slow_handoff_port", None) not in staffed and self.slow_handoff_port:
            self.slow_handoff_port = self.staffed[0] if self.staffed else None
        return staffed

    def _prepare_buffer(self, stop):
        """Create look-ahead worth of task groups up front, per category, with
        ports CLOSED — e.g. 1000 bins/hr × 30 min look-ahead = 500 bins of
        work queued before a single port opens. Progress is visible via
        prep_done / prep_total so the UI can show the buffer filling."""
        n = int(self.cfg.get("tasks_per_group", 3))
        est = int(self.cfg.get("estpicktime", 20))
        # groups to prepare per category = (target bins/hr × look-ahead) / group
        prep_groups = {}
        for cat in self.pick_categories:
            bins = self.targets.get(cat, 150) * self.lookahead_min / 60.0
            prep_groups[cat] = max(1, int(round(bins / max(1, n))))
        self.prep_total = sum(prep_groups.values())
        self.prep_bins = self.prep_total * n
        self.prep_done = 0
        # INTERLEAVE categories (round-robin) instead of filling cat 1 fully
        # before cat 2 — so every category has work from the very first minute,
        # exactly like a real WMS feeding all its port groups at once.
        remaining = dict(prep_groups)
        cats = list(prep_groups.keys())
        while any(remaining.values()) and not stop.is_set():
            for cat in cats:
                if remaining[cat] <= 0:
                    continue
                remaining[cat] -= 1
                tg = self._next_tg()
                bins = self._bins_for_group()
                if not bins:
                    continue
                tasks = [{"task_id": tg * 10 + i, "bin_id": b, "item": 1,
                          "estpicktime": est} for i, b in enumerate(bins)]
                res = self._call(f"create_taskgroup {tg} (cat {cat}) [prep]",
                                 self.client.create_taskgroup, tg, cat, tasks,
                                 req_time=now_iso(), priority=1)
                if res.get("ok"):
                    self._release_work(cat, len(bins))
                    self.groups[tg] = {"category": cat, "bins": bins,
                                       "state": "active", "created": time.time()}
                    self.prep_done += 1
                self.status_line = (f"preparing buffer: {self.prep_done}/"
                                    f"{self.prep_total} groups "
                                    f"(~{self.prep_bins} bins, {self.lookahead_min} min "
                                    f"look-ahead) — ports still closed")
                if stop.is_set():
                    return
                time.sleep(0.12)

    def _over_forecast(self):
        """Planted guideline breach: forecast a block of bins worth more than
        25% of the pool (forecastbin min..max), on top of the task groups."""
        try:
            pool = sorted(int(b) for b in self.cfg.get("bin_pool") or [])
        except ValueError:
            self.status_line = "GL_PREP skipped: bin ids are not numeric"
            return
        if not pool:
            return
        k = max(1, int(len(pool) * GL_PREP_SHARE + 0.999))
        r = self._call(f"[inject:GL_PREP] forecastbin {pool[0]}..{pool[k - 1]} ({k} of {len(pool)} bins)",
                       self.client.generic_call, "forecastbin",
                       {"min_bin_id": pool[0], "max_bin_id": pool[k - 1]})
        if r.get("ok"):
            self.injected["GL_PREP"] += 1
        else:
            self.status_line = f"GL_PREP: forecastbin refused ({r.get('fault_text') or r.get('error')})"

    def stop(self):
        was_running = self.running
        self._stop.set()
        self.running = False
        # a WMS ends its shift by closing its port sessions; otherwise the next
        # run's openport gets 1002 'already open' and starts in a messy state
        if was_running and self.mode == "drive" and self.client:
            time.sleep(0.8)   # let each port worker finish the call it is in
            for pid, p in self.ports.items():
                if p.get("role") in ("picking", "putaway") and p.get("state") not in ("unavailable", "ready", "stalled", "standby",
                                                                              "break", "shift change"):
                    if p.get("open_bin"):
                        # a bin is still open: suspend so its work returns to the queue
                        # (a plain closeport here would read as G01 in the monitor)
                        self._call(f"closeport {pid} suspend (end of run, bin {p['open_bin']} open)",
                                   self.client.closeport, pid, suspend_taskgroup=1)
                    else:
                        self._call(f"closeport {pid} (end of run)", self.client.closeport, pid)
                    p["state"] = "closed"
            # cancel the work this run left queued: otherwise the robots keep
            # digging it during the next run, and the next run's groups reuse
            # its bins (the monitor's C02, and bins arriving minutes late)
            left = [tg for tg, g in self.groups.items() if g.get("state") == "active"]
            for tg in left:
                self._call(f"cancel_taskgroup {tg} (end of run)", self.client.cancel_taskgroup, tg)
                self.groups[tg]["state"] = "cancelled"
        self._threads = []
        self.phase = "stopped"
        self.status_line = "stopped"

    def _feeder(self, stop):
        """Scenario-paced, PER CATEGORY: keep ~look-ahead minutes of work in
        each picking category, scaled by the scenario demand factor. This is
        what stops the Ports-2/3/4 starvation and makes the run follow a
        real demand profile instead of 'fill one buffer'."""
        est = int(self.cfg.get("estpicktime", 20))
        n = int(self.cfg.get("tasks_per_group", 3))
        # demand pacing: release work at the target rate (in grid time), on top
        # of the look-ahead built in the prepare phase — so a 300 bins/h demand
        # gives ~300 bins/h, not whatever the ports can pick
        released = {c: 0.0 for c in self.pick_categories}
        last_t = self.grid_time()
        # fallback only: groups are normally marked done when their last bin is
        # closed (see _task_closed). The timer catches groups the SDK dropped.
        lifetime = max(300, 10 * n * est)
        while not stop.is_set():
            if not self.cfg.get("bin_pool"):
                self.status_line = ("no valid bins — set a bin pool that exists "
                                    "in the loaded grid")
                time.sleep(2); continue
            now = time.time()
            # expire groups that have had time to be picked, so the feeder keeps
            # topping up at roughly the consumption / target rate.
            for g in self.groups.values():
                if g["state"] == "active" and now - g.get("created", now) > lifetime:
                    g["state"] = "done"
            factor = self.demand_factor()
            g_now = self.grid_time()
            dt, last_t = max(0.0, g_now - last_t), g_now
            for cat in self.pick_categories:
                target_bph = self.targets.get(cat, 150) * factor
                target_bins = target_bph * self.lookahead_min / 60.0
                target_groups = max(1, int(round(target_bins / max(1, n))))
                released[cat] = min(released[cat] + target_bph * dt / 3600.0 / max(1, n), 3.0)
                active = sum(1 for g in self.groups.values()
                             if g["state"] == "active" and g["category"] == cat)
                if active < target_groups and released[cat] >= 1.0:
                    released[cat] -= 1.0
                    tg = self._next_tg()
                    bins = self._bins_for_group()
                    if not bins:
                        continue
                    tasks = [{"task_id": tg * 10 + i, "bin_id": b, "item": 1,
                              "estpicktime": est} for i, b in enumerate(bins)]
                    res = self._call(f"create_taskgroup {tg} (cat {cat})",
                                     self.client.create_taskgroup, tg, cat, tasks,
                                     req_time=now_iso(), priority=1)
                    if res.get("ok"):
                        self._release_work(cat, len(bins))
                        self.groups[tg] = {"category": cat, "bins": bins,
                                           "state": "active", "created": now}
            time.sleep(0.6)

    def _at_this_port(self, pid, bin_id):
        """LP bin modes don't say WHICH port: confirm once with getportstatus
        that this port has the bin selected and ready."""
        if pid is None:
            return True
        r = self.client.getportstatus(pid)
        txt = r.get("response_xml", "") or ""
        sel = _xf("selected_bin", txt)
        ready = _xf("is_ready", txt)
        if sel is None and ready is None:
            return True                      # fields not reported: trust the LP
        return (sel in (None, str(bin_id))) and ready in (None, "1", "true", "True")

    def _bin_ready(self, bin_id, stop, delay=0.5, force_poll=False, pid=None):
        """Wait until the bin is at the port, so we never closebin early (1027).
        Good practice (and the monitor's G07 correction): use the Log Publisher
        bin-mode events. Only when the LP is not connected — or when G07 is
        injected — fall back to polling getbinstate. Waits up to the bin-arrival
        buffer (grid time): a busy grid can take minutes to dig a bin out."""
        deadline = time.time() + self.bin_arrival_timeout_s / self._clock[1]
        if self.lp and self.lp.connected and not force_poll:
            while time.time() < deadline and not stop.is_set():
                with self.lp._lock:
                    mode = (self.lp.bins.get(str(bin_id)) or {}).get("mode")
                if mode in ("OPEN", "PORT") and self._at_this_port(pid, bin_id):
                    return True
                time.sleep(0.1 if mode not in ("OPEN", "PORT") else 0.5)
            if stop.is_set():
                return False
            # LP silent for this bin: confirm once with a single query
            res = self.client.getbinstate(bin_id)
            m = _BIN_MODE_RE.search(res.get("response_xml", "") or "")
            return bool(m and m.group(1) in ("O", "P"))
        while time.time() < deadline:
            if stop.is_set():
                return False
            res = self.client.getbinstate(bin_id)   # not logged (noisy poll)
            m = _BIN_MODE_RE.search(res.get("response_xml", "") or "")
            if m and m.group(1) in ("O", "P"):
                return True
            time.sleep(delay)
        return False

    # -- full day: who works this hour, breaks, shift changes ---------------
    def _wanted(self, pid):
        """Full day: the first N picking ports of a category work this hour,
        N from this hour's demand (a site re-staffs ports as volume changes)."""
        p = self.ports[pid]
        if self.scenario.get("profile") != "day" or p["role"] != "picking":
            return True
        per_port = 3600.0 / max(1.0, self.pick_time_s + BIN_EXCHANGE_S)
        demand = self.targets.get(p["category"], 150) * self.demand_factor()
        need = max(1, -(-int(demand) // int(per_port)))
        return pid in (getattr(self, "_cat_order", {}).get(p["category"]) or [pid])[:need]

    def _await_staffing(self, pid, stop):
        """Port stays closed (standby) until this hour's demand needs it."""
        p = self.ports[pid]
        while not stop.is_set() and not self._wanted(pid):
            p["state"] = "standby"
            p["last"] = f"not staffed at demand ×{self.demand_factor():g}"
            self.grid_sleep(60)
        p.pop("next_break", None)     # breaks count from when the operator starts
        return not stop.is_set()

    def _rota_pause(self, pid):
        """Full day: (reason, grid seconds) when this port's operator takes a
        break or hands over at shift change; staggered so ports don't all stop."""
        rota = {**DAY_ROTA, **(self.scenario.get("rota") or {})}
        p = self.ports[pid]
        now = self.grid_time()
        every = rota["break_every_min"] * 60
        if "next_break" not in p:
            order = getattr(self, "staffed", None) or [pid]
            idx = order.index(pid) if pid in order else 0
            p["next_break"] = now + every * (0.5 + idx / max(1, len(order)))
        if "next_shift" not in p:
            g0 = self._run_start_grid or now
            p["next_shift"] = g0 + rota["shift_hours"] * 3600
        if now >= p["next_shift"]:
            while p["next_shift"] <= now:
                p["next_shift"] += rota["shift_hours"] * 3600
            p["next_break"] = max(p["next_break"], now + every / 2)
            return "shift change", rota["handover_min"] * 60
        if now >= p["next_break"]:
            p["next_break"] = now + every
            return "break", rota["break_min"] * 60
        return None

    def _day_rota(self, pid, cat, stop):
        """Between bins (none open): close the port cleanly for a break, a shift
        change or the end of its staffed hours, then reopen. Returns False on stop."""
        p = self.ports[pid]
        if not self._wanted(pid):
            self._call(f"closeport {pid} (off: not staffed this hour)", self.client.closeport, pid)
            if not self._await_staffing(pid, stop):
                return False
            self._call(f"openport {pid} (staffed again)", self.client.openport, pid, category=cat)
            p["state"] = "open"
            return True
        pause = self._rota_pause(pid)
        if not pause:
            return True
        why, grid_s = pause
        self._call(f"closeport {pid} ({why})", self.client.closeport, pid)
        p["state"] = why
        p["last"] = f"{why}: back in {grid_s / 60:g} min"
        until = self.grid_time() + grid_s
        while self.grid_time() < until and not stop.is_set():
            self.grid_sleep(min(30, until - self.grid_time()))
        if stop.is_set():
            return False
        self._call(f"openport {pid} (after {why})", self.client.openport, pid, category=cat)
        p["state"] = "open"
        return True

    def _port_worker(self, pid, stop):
        p = self.ports[pid]
        cat = p["category"]
        if self.scenario.get("profile") == "day" and not self._await_staffing(pid, stop):
            return
        # open the port once (1002 = already open is fine)
        p["state"] = "opening"
        op = self._call(f"openport {pid}", self.client.openport, pid, category=cat)
        # fault 1000 = the port doesn't exist in this grid (e.g. configured 10
        # ports but the grid only has 4). Don't hammer it — mark & stop.
        if op.get("fault_code") == 1000:
            p["state"] = "unavailable"
            p["last"] = f"port {pid} not in this grid — use ⟳ Load ports from grid"
            return
        p["state"] = "open"
        while not stop.is_set():
            # suspend requested?
            if self._suspend[pid].is_set():
                tg = p.get("active_tg")
                self._call(f"closeport {pid} (suspend)", self.client.closeport,
                           pid, suspend_taskgroup=1)
                p["state"] = "suspended"; p["last"] = f"suspended (tg {tg})"
                while self._suspend[pid].is_set() and not stop.is_set():
                    time.sleep(0.4)
                if stop.is_set():
                    break
                self._call(f"openport {pid} (resume)", self.client.openport, pid, category=cat)
                if tg:
                    r = self._call(f"openbin {pid} resume tg {tg}", self.client.openbin,
                                   pid, taskgroup_id=tg)
                    self._after_open(pid, r)
                    self._complete_open_bin(pid, stop)
                p["state"] = "open"
                continue
            # full day: hourly staffing, breaks, shift changes (between bins)
            if self.scenario.get("profile") == "day" and not self._day_rota(pid, cat, stop):
                break
            # ad-hoc requested?
            adhoc = self._adhoc.pop(pid, None)
            if adhoc:
                r = self._call(f"openbin {pid} ad-hoc bin {adhoc}", self.client.openbin,
                               pid, bin_id=adhoc)
                p["last"] = f"ad-hoc bin {adhoc}"
                if r.get("ok"):
                    self._after_open(pid, r)
                    self._complete_open_bin(pid, stop)
                continue
            # planted defects (simulator half of the golden test set)
            # while G02 is starving the ports, plant nothing else (keeps G02 clean)
            starving = "G02" in self.inject and not self.groups
            inj = None if starving else self._port_injection(p)
            if inj == "G06" and len(self.pick_categories) > 1:
                other = next(c for c in self.pick_categories if c != cat)
                self._call(f"[inject:G06] closeport {pid}", self.client.closeport, pid)
                self._call(f"[inject:G06] openport {pid} cat {other}", self.client.openport, pid, category=other)
                self.grid_sleep(30)
                self._call(f"[inject:G06] closeport {pid}", self.client.closeport, pid)
                self._call(f"[inject:G06] openport {pid} cat {cat}", self.client.openport, pid, category=cat)
                self.injected["G06"] += 1
                continue
            if not starving and (inj == "G04" or (self.adhoc_pct and random.random() * 100 < self.adhoc_pct
                                                  and "G04" in self.inject)):
                pool = self.cfg.get("bin_pool") or []
                if pool:
                    b = str(random.choice(pool))
                    r = self._call(f"[inject:G04] openbin {pid} ad-hoc bin {b}", self.client.openbin,
                                   pid, bin_id=b)
                    if r.get("ok"):
                        self.injected["G04"] += 1
                        self._after_open(pid, r)
                        self._complete_open_bin(pid, stop)
                        continue
            if p.get("stalled"):
                return
            # normal pick: request the next bin only when work has been released
            busy_poll = "G02" in self.inject and self.phase == "running" and not self.prep_done
            token = True if busy_poll else self._take_work(cat, stop)
            if stop.is_set():
                break
            r = self._call(f"openbin {pid}", self.client.openbin, pid)
            if not r.get("ok") and token and not busy_poll:
                # the router gave that work to another port (or it isn't ready):
                # hand the token back and back off before asking again
                self._release_work(cat, 1)
                self.grid_sleep(5)
            if not r.get("ok"):
                if r.get("fault_code") == 1000:
                    # port vanished / not available — stop this worker
                    p["state"] = "unavailable"
                    p["last"] = f"port {pid} not available — stopped"
                    return
                # nothing ready (1015/1016): a good WMS doesn't hammer openbin —
                # it waits until it releases work for this category (event),
                # with a back-off as a safety net (the monitor's G02 correction)
                p["state"] = "waiting"; p["last"] = r.get("fault_text") or "no work"
                if "G02" in self.inject and self.phase == "running" and not self.prep_done:
                    self.grid_sleep(1)         # planted defect: busy-poll every ~1 s (grid time)
                    continue
                continue
            self._after_open(pid, r)
            p["inject_now"] = inj if inj in ("G01", "G07", "A01", "E01") else None
            self._complete_open_bin(pid, stop)

    # -- work tokens: a port asks for a bin only when released work exists ----
    def _release_work(self, cat, n):
        with self._tok_cv:
            self._tokens[cat] = self._tokens.get(cat, 0) + n
            self._tok_cv.notify(n)          # wake as many ports as bins released

    def _take_work(self, cat, stop, max_grid_wait=60):
        """Wait for released work in this category (no openbin while there is
        none). Returns True with a token, or False after the safety timeout
        (then the port asks once anyway, e.g. for work left in the SDK)."""
        deadline = time.time() + max_grid_wait / self._clock[1]
        with self._tok_cv:
            while self._tokens.get(cat, 0) <= 0 and not stop.is_set():
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._tok_cv.wait(timeout=min(left, 0.5))
            if stop.is_set():
                return False
            self._tokens[cat] -= 1
            return True

    def _task_closed(self, task_id):
        """Count a finished task against its group (task_id = tg*10 + i) so the
        feeder sees real consumption and tops the buffer up in time."""
        try:
            tg = int(task_id) // 10
        except (TypeError, ValueError):
            return
        g = self.groups.get(tg)
        if not g:
            return
        g["done"] = g.get("done", 0) + 1
        if g["done"] >= len(g["bins"]):
            g["state"] = "done"

    def _port_injection(self, p):
        """Which port-level defect to plant on this bin (every Nth bin, in turn)."""
        p["n"] += 1
        active = [k for k in PORT_INJECTIONS if k in self.inject]
        if not active or p["n"] % self.inject_every:
            return None
        return active[(p["n"] // self.inject_every) % len(active)]

    def _test_scenario(self):
        """The test-pack scenario this run was loaded from, with what the
        monitor should find (test_scenarios.py) — None for a free run."""
        sid = (self.cfg or {}).get("test_scenario")
        if not sid:
            return None
        import test_scenarios
        sc = test_scenarios.SCENARIOS.get(sid)
        return dict(sc, id=sid) if sc else None

    def grid_stopped(self):
        return bool(self.lp and str(self.lp.system_mode or "").upper() in GRID_STOPPED)

    def _grid_event(self, label, params):
        """A Log Publisher grid event for the monitor (live call log only)."""
        entry = {"seq": None, "ts_iso": self._grid_iso(), "time": _hhmmss(), "label": label,
                 "ok": True, "status": None, "fault_code": None, "fault_text": None, "error": None,
                 "duration_ms": None, "request_xml": "", "response_xml": "", "params": params}
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.calllog.appendleft(entry)

    def _mark_system_mode(self, mode):
        """Grid event for the monitor: the grid's system mode changed (Log Publisher SM)."""
        entry = {"seq": None, "ts_iso": self._grid_iso(), "time": _hhmmss(),
                 "label": f"lp_system_mode {mode} (Log Publisher: grid {'stopped' if str(mode).upper() in GRID_STOPPED else 'running'})",
                 "ok": True, "status": None, "fault_code": None, "fault_text": None, "error": None,
                 "duration_ms": None, "request_xml": "", "response_xml": "", "params": {"mode": mode}}
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.calllog.appendleft(entry)
        if str(mode).upper() in GRID_STOPPED:
            self.status_line = f"GRID STOPPED (system mode {mode}) — clear the robot errors in the Service Console; waiting"

    def _mark_arrival(self, pid, bin_id):
        """Grid event for the monitor, not a WMS call: the bin reached the port
        (Log Publisher). It lets the monitor split robot delivery from operator
        hold time. Live call log only — the request log stays WMS calls."""
        entry = {"seq": None, "ts_iso": self._grid_iso(), "time": _hhmmss(),
                 "label": f"lp_bin_arrival {pid} bin {bin_id} (Log Publisher: bin at the port)",
                 "ok": True, "status": None, "fault_code": None, "fault_text": None, "error": None,
                 "duration_ms": None, "request_xml": "", "response_xml": "",
                 "params": {"port_id": pid, "bin_id": bin_id}}
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.calllog.appendleft(entry)

    def _after_open(self, pid, res):
        p = self.ports[pid]
        prm = res.get("params", {})
        p["open_bin"] = prm.get("bin_id")
        p["open_task"] = prm.get("task_id")
        p["presented_at"] = self.grid_time()
        p["state"] = "bin requested"

    def _complete_open_bin(self, pid, stop):
        p = self.ports[pid]
        bin_id, task_id = p.get("open_bin"), p.get("open_task")
        if not bin_id:
            return
        p["state"] = "waiting for bin"
        inj = p.pop("inject_now", None)
        if inj == "E01":
            # planted defect: close before the bin is at the port -> 1027
            self._call(f"[inject:E01] closebin {pid} bin {bin_id} (too early)", self.client.closebin,
                       pid, bin_id, task_id=task_id, taskcomplete="true")
            self.injected["E01"] += 1
        if inj == "G07":
            self.injected["G07"] += 1
            # poll a few times on purpose, then wait for it like a polling WMS
            for _ in range(3):
                self._call(f"[inject:G07] getbinstate {bin_id}", self.client.getbinstate, bin_id)
                time.sleep(0.2)
        ready = self._bin_ready(bin_id, stop, force_poll=(inj == "G07"), pid=pid)
        if stop.is_set():
            return
        verb = "drop" if p["role"] == "putaway" else "pick"
        if not ready:
            # the monitor's G10 correction: never request the next bin while this
            # one is open. Alarm + check the port, then give it one more buffer.
            buf = self.bin_arrival_timeout_s
            p["state"] = "alarm: bin late"
            p["last"] = f"bin {bin_id} not at port {pid} after {buf:g} s — checking the port"
            self._call(f"[alarm] getportstatus {pid} (bin {bin_id} not here after {buf:g} s)",
                       self.client.getportstatus, pid)
            ready = self._bin_ready(bin_id, stop, force_poll=(inj == "G07"), pid=pid)
            if stop.is_set():
                return
        while not ready and self.grid_stopped() and not stop.is_set():
            # the grid itself is stopped (robot error, e-stop): no port can get a bin.
            # A good WMS raises the alarm once and waits; it doesn't blame the port.
            p["state"] = "grid stopped"
            p["last"] = f"waiting for bin {bin_id}: the grid is stopped (system mode {self.lp.system_mode})"
            ready = self._bin_ready(bin_id, stop, force_poll=(inj == "G07"), pid=pid)
        if stop.is_set():
            return
        if not ready:
            # still not here: stop this port instead of abandoning the bin
            # (check the port's type / mode in the SDK)
            p["state"] = "stalled"
            p["last"] = (f"stopped: bin {bin_id} never arrived at port {pid} in {2 * self.bin_arrival_timeout_s:g} s — "
                         "check this port's type/mode in the SDK")
            self._call(f"closeport {pid} (stalled: bin never arrived)", self.client.closeport, pid)
            p["open_bin"] = None; p["open_task"] = None
            p["stalled"] = True
            return
        self._mark_arrival(pid, bin_id)
        # how the bin gets closed: auto (tool plays the operator) or
        # manual (a human at the port presses confirm -> confirm_pick).
        if p["confirm"] == "manual":
            p["state"] = f"awaiting confirm ({verb})"
            self._confirm[pid].clear()
            while not self._confirm[pid].is_set() and not stop.is_set():
                if self._suspend[pid].is_set():
                    return  # suspend takes priority; bin stays open
                time.sleep(0.2)
            if stop.is_set():
                return
        else:
            p["state"] = "picking" if verb == "pick" else "putaway"
            # the operator works the bin; no call until they confirm (closebin)
            if inj == "A01":
                self.injected["A01"] += 1
                got = self._operator_away(pid, bin_id, task_id, stop)
                if not got:
                    return
                bin_id, task_id = got
            else:
                self.grid_sleep(self.pick_time_s)
        if stop.is_set():
            return   # run stopped mid-pick: stop() suspends the port, no stray closebin (1026)
        # putaway updates the bin content on close; picking just completes
        content = p["fill_content"] if p["role"] == "putaway" else None
        if inj == "G01":
            # planted defect: release the bin by closing the port, then reopen
            self._call(f"[inject:G01] closeport {pid} (bin {bin_id} released by closeport)",
                       self.client.closeport, pid)
            self._call(f"[inject:G01] openport {pid}", self.client.openport, pid, category=p["category"])
            self.injected["G01"] += 1
        elif str(task_id or "0") == "0":
            # bin presented without a task (ad-hoc, or left in the SDK by an
            # earlier run): release it without task_id — task_id 0 is 1012
            r = self._call(f"closebin {pid} bin {bin_id} (no task: release)", self.client.closebin,
                           pid, bin_id, content=content)
            if not r.get("ok"):
                # can't release it: stop this port instead of looping on the same bin
                p["state"] = "stalled"; p["stalled"] = True
                p["last"] = f"bin {bin_id} (no task) could not be released: {r.get('fault_text')}"
                self._call(f"closeport {pid} (stalled: bin {bin_id} could not be released)",
                           self.client.closeport, pid)
                p["open_bin"] = None; p["open_task"] = None
                return
        else:
            self._call(f"closebin {pid} bin {bin_id} ({verb})", self.client.closebin,
                       pid, bin_id, task_id=task_id, content=content, taskcomplete="true")
        p["last_closed_bin"] = str(bin_id)
        self._task_closed(task_id)
        p["picked"] += 1
        p["open_bin"] = None; p["open_task"] = None
        p["state"] = "open"; p["last"] = f"{verb}ed bin {bin_id}"
        if pid == getattr(self, "slow_handoff_port", None) and inj != "G01":
            # planted guideline breach: slow hand-off at this port
            self.injected["GL_HANDOFF"] += 1
            self.grid_sleep(GL_HANDOFF_SEC)
            return
        self.grid_sleep(0.1, min_wall=0)  # fast hand-off: closebin -> next openbin ~100 ms of grid time (logs are grid time)

    def _operator_away(self, pid, bin_id, task_id, stop):
        """The operator leaves with a bin open (no closebin). Without auto port
        close the bin just sits there (the monitor's A01). With it, the WMS
        suspends the port after auto_port_close_s, returning the work to the
        queue, and resumes the task group when the operator is back.
        Returns the (bin, task) to close, or None if the bin was not re-presented."""
        p = self.ports[pid]
        away, ac = self.operator_away_s, self.auto_port_close_s
        p["state"] = f"operator away ({away:g} s)"
        if not ac or ac >= away:
            self.grid_sleep(away)
            self.grid_sleep(self.pick_time_s)       # back, picks, then confirms
            return bin_id, task_id
        # the auto-close timer runs from when the bin was presented (openbin),
        # the same clock the monitor uses for the hold time
        held = self.grid_time() - p.get("presented_at", self.grid_time())
        self.grid_sleep(max(0.0, ac - held), min_wall=0)
        if stop.is_set():
            return None
        self._call(f"[auto-close] closeport {pid} suspend (no closebin for {ac:g} s)",
                   self.client.closeport, pid, suspend_taskgroup=1)
        self.auto_closed += 1
        p["state"] = "auto-closed (operator away)"; p["open_bin"] = None; p["open_task"] = None
        self.grid_sleep(away - ac)
        if stop.is_set():
            return None
        self._call(f"openport {pid} (operator back)", self.client.openport, pid, category=p["category"])
        p["state"] = "open"
        try:
            tg = int(task_id) // 10              # task_id = tg*10 + i
        except (TypeError, ValueError):
            return None
        r = self._call(f"openbin {pid} resume tg {tg}", self.client.openbin, pid, taskgroup_id=tg)
        if not r.get("ok"):
            return None
        self._after_open(pid, r)
        bin_id, task_id = p.get("open_bin") or bin_id, p.get("open_task") or task_id
        if not self._bin_ready(bin_id, stop, pid=pid):
            return None
        self.grid_sleep(self.pick_time_s)
        return bin_id, task_id

    # -- controls ----------------------------------------------------------
    def suspend(self, pid):
        pid = str(pid)
        if pid in self._suspend:
            self._suspend[pid].set()
        return self.state()

    def resume(self, pid):
        pid = str(pid)
        if pid in self._suspend:
            self._suspend[pid].clear()
        return self.state()

    def adhoc(self, pid, bin_id):
        self._adhoc[str(pid)] = str(bin_id)
        return self.state()

    def confirm_pick(self, pid):
        """Operator at the port pressed 'confirm' -> let the bin close."""
        pid = str(pid)
        if pid in self._confirm:
            self._confirm[pid].set()
        return self.state()

    def clear_results(self):
        """Reset counters and the call log (keeps the configuration)."""
        with self._lock:
            self.calllog.clear()
        for p in self.ports.values():
            p.update(picked=0, n=0, last="")
        self.injected = Counter()
        return self.state()

    def step_call(self, method, params):
        """Fire one interface method (step/training mode) and log it."""
        if method == "set_simulation_speed":
            try:
                self._set_clock(int((params or {}).get("speed", 100)))
            except (TypeError, ValueError):
                pass
        if not self.client:
            self.client = AutoStoreClient()
        blocked = self._write_blocked(method)
        if blocked:
            entry = {"time": _hhmmss(), "label": f"[blocked] {method}", "ok": False,
                     "fault_text": blocked, "error": blocked, "request_xml": "",
                     "response_xml": "", "params": {}}
            with self._lock:
                self._seq += 1
                entry["seq"] = self._seq
                self.calllog.appendleft(entry)
            return {"ok": False, "error": blocked}
        fn = getattr(self.client, method, None)
        try:
            if fn and method not in ("generic_call",):
                return self._call(f"[train] {method}", fn, **(params or {}))
            # any method not hand-coded -> generic flat-XML caller
            return self._call(f"[train] {method}", self.client.generic_call,
                              method, params or {})
        except TypeError as exc:
            return {"ok": False, "error": f"bad params: {exc}"}

    # -- state for the UI --------------------------------------------------
    def state(self):
        active_by_cat = {}
        for g in self.groups.values():
            if g["state"] == "active":
                active_by_cat[g["category"]] = active_by_cat.get(g["category"], 0) + 1
        prep_pct = int(100 * self.prep_done / self.prep_total) if self.prep_total else 0
        return {
            "mode": self.mode,
            "running": self.running,
            "phase": getattr(self, "phase", "idle"),
            "prep_total": self.prep_total,
            "prep_done": self.prep_done,
            "prep_bins": self.prep_bins,
            "prep_pct": prep_pct,
            "lookahead_min": getattr(self, "lookahead_min", 30),
            "bin_pool_size": len(self.cfg.get("bin_pool") or []),
            "status_line": self.status_line,
            "ports": list(self.ports.values()),
            "monitor": list(self.monitor.values()),
            "groups_active": sum(1 for g in self.groups.values() if g["state"] == "active"),
            "groups_total": len(self.groups),
            "buffer_target": self.cfg.get("buffer_target"),
            "scenario": getattr(self, "scenario", {"profile": "steady"}),
            "targets": getattr(self, "targets", {}),
            "demand_factor": round(self.demand_factor(), 2) if self.running else 1.0,
            "active_by_cat": active_by_cat,
            "inject": sorted(self.inject),
            "injected": dict(self.injected),
            "auto_port_closes": getattr(self, "auto_closed", 0),
            "request_log": self.request_log,
            "grid_time_factor": self._clock[1],
        }

    def call_log(self):
        with self._lock:
            return list(self.calllog)[:60]

    def calllog_since(self, seq=0):
        """Every call after seq, oldest first — lets the monitor miss nothing
        even at high sim speed (up to the 5,000-call buffer)."""
        with self._lock:
            items = [e for e in self.calllog if e.get("seq", 0) > seq]
            oldest = self.calllog[-1]["seq"] if self.calllog else 0
            last = self._seq
        dropped = max(0, oldest - seq - 1) if seq and oldest else 0
        return {"seq": last, "entries": items[::-1], "dropped": dropped}

    def run_info(self):
        """What this run is, for the monitor: a suggested site profile (the
        baseline), the defects planted on purpose, and where the log is."""
        targets = getattr(self, "targets", {}) or {}
        cats = set(getattr(self, "pick_categories", []) or targets)
        served = {c: t for c, t in targets.items() if c in cats}
        unserved = {c: t for c, t in targets.items() if c not in cats}
        ports = [{"port_id": p["port_id"], "type": p["type"], "role": p["role"],
                  "category": p["category"]} for p in self.ports.values()]
        return {
            "run_id": self.run_id, "started": getattr(self, "_run_started_iso", None),
            "mode": self.mode, "phase": self.phase, "sim_speed": getattr(self, "sim_speed", 100),
            "grid_time_factor": self._clock[1],
            "request_log": self.request_log, "log_dir": LOG_DIR,
            "inject": sorted(self.inject), "inject_every": self.inject_every,
            "injected": dict(self.injected),
            "injections_available": INJECTIONS,
            "test_scenario": self._test_scenario(),
            "total_bins": len(self.cfg.get("bin_pool") or []) or None,   # for the monitor's 25% rule
            "timing": {"pick_time_s": getattr(self, "pick_time_s", None),
                       "bin_arrival_timeout_s": getattr(self, "bin_arrival_timeout_s", None),
                       "operator_away_s": getattr(self, "operator_away_s", None),
                       "auto_port_close_s": getattr(self, "auto_port_close_s", None)},
            "auto_port_closes": getattr(self, "auto_closed", 0),
            "staffed_ports": getattr(self, "staffed", None),
            "picked": sum(p["picked"] for p in self.ports.values()),
            "ports": ports, "targets": targets,
            "scenario": getattr(self, "scenario", {"profile": "steady"}),
            "site_profile": {
                "name": self.cfg.get("site_name") or "SDK run",
                # only categories with a picking port are fed, so only they count
                "contracted_bph": float(sum(served.values())) if served else None,
                # for the monitor's expected-throughput estimate
                "robots": (len(self.lp.robots) or None) if self.lp else None,
                "pick_time_s": getattr(self, "pick_time_s", None),
                "bin_exchange_s": BIN_EXCHANGE_S,
                "time_factor": 1.0,   # logs are written in grid time already
                "notes": ("Target = the demand the simulator was asked to deliver "
                          f"({served}); log timestamps are grid time."
                          + (f" Not counted (no picking port): {unserved}." if unserved else "")),
            },
        }

    def lp_snapshot(self):
        return self.lp.snapshot() if self.lp else {"connected": False, "lines": []}