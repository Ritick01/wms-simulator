"""
AutoStore Task Interface client.

Thin wrapper around the AutoStore WMS SDK Task / Bin / Simulation interfaces.
Every method builds the exact XML from the official Postman collection,
POSTs it to the SDK, and returns a parsed result plus the raw request and
response XML so the UI can display both.

All endpoints target the SDK on the local machine (default localhost:44000).
"""

import time
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
import requests

# Error-code table transcribed from the AutoStore interface documentation.
# Shown in the UI so a fault reads "1015 - no tasks/bins ..." not just a number.
ERROR_CODES = {
    1000: "The specified Port is not available.",
    1001: "The specified Port is closing.",
    1002: "The specified Port is already open.",
    1003: "Mandatory parameter port_id is missing.",
    1004: "Parameter port_id is not valid (invalid format or out of range).",
    1006: "Parameter content is not valid (invalid format or out of range).",
    1007: "Parameter category is not valid (invalid format or out of range).",
    1008: "Parameter req_time is not valid (invalid format or out of range).",
    1009: "The specified Port is not open.",
    1010: "Parameter bin_id is not valid (invalid format or out of range).",
    1011: "The specified Bin does not exist.",
    1012: "Parameter task_id is not valid (invalid format or out of range).",
    1013: "Parameter taskgroup_id is not valid (invalid format or out of range).",
    1014: "Parameter shipment is not valid (invalid format or out of range).",
    1015: "There are no tasks or Bins that fulfill the Port select requirements.",
    1016: "There are no Bins ready that fulfill the Port select requirements.",
    1017: "The specified task does not exist.",
    1018: "The specified task group does not exist.",
    1019: "The specified task group is selected by another Port.",
    1020: "The openbin conflicts with a previous openbin call that is not yet closed.",
    1021: "The openbin conflicts with the Port's selected shipment.",
    1022: "The openbin conflicts with the Port's selected task group.",
    1023: "Multiple selection parameters are not allowed.",
    1024: "Mandatory parameter bin_id is missing.",
    1025: "Parameter taskcomplete is not valid (invalid format or out of range).",
    1026: "The Port does not have a selected Bin.",
    1027: "The Bin is not yet ready in the Port.",
    1028: "The Bin and task parameters conflict with the open Bin and task.",
    1029: "The specified Bin is already inserted.",
    1030: "The Port has a selected Bin.",
    1035: "Mandatory parameter req_time is missing.",
    1048: "System is not available.",
    1050: "Too many categories or content codes.",
    1051: "Conflicting category and content codes.",
    1058: "Bin is still outside.",
    1073: "Parameter bin_type is not valid (invalid format or out of range).",
    1077: "Maximum number of Bins already inserted.",
    1083: "No room for given content.",
    1103: "The source port is not open or is not a supported port type.",
    1105: "Parameter target_port_id is not valid; target port not open, "
          "unsupported type, or not on the same workstation.",
    1111: "The compartment id is invalid or not supported by the source port.",
    1113: "The compartment id is invalid or not supported by the target port.",
    9999: "Unsupported command.",
}

# bin_mode letter -> human label (from the interface docs)
BIN_MODE = {
    "O": "OPEN",
    "C": "CLOSED",
    "D": "OUTSIDE/DELIVERING",
}


def pretty_xml(xml_string: str) -> str:
    """Return nicely indented XML for display. Falls back to the raw string."""
    try:
        parsed = minidom.parseString(xml_string)
        out = parsed.toprettyxml(indent="  ")
        # drop blank lines minidom tends to add
        return "\n".join(line for line in out.split("\n") if line.strip())
    except Exception:
        return xml_string


def _esc(value) -> str:
    """Escape a value for safe inclusion in XML text."""
    s = str(value)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_methodcall(name: str, params_xml: str = "") -> str:
    """Wrap a method name and already-built <params> body in a methodcall."""
    body = f"\n  <params>{params_xml}\n  </params>" if params_xml else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<methodcall>\n  <name>{name}</name>{body}\n</methodcall>"
    )


class AutoStoreClient:
    def __init__(self, server: str = "localhost", port: int = 44000, timeout: float = 10.0):
        # On Windows "localhost" resolves to IPv6 ::1 first; the SDK listens on
        # IPv4 only, so every call waits ~2 s for the fallback. Use 127.0.0.1.
        self.server = "127.0.0.1" if str(server).strip().lower() == "localhost" else server
        self.port = port
        self.timeout = timeout
        # optional callback(kind, request_body, response_text, duration_ms):
        # the engine uses it to write an AutoStore-format request log of
        # EVERY call (including polls that are not shown in the UI).
        self.recorder = None

    # --- endpoint helpers -------------------------------------------------
    def _url(self, kind: str) -> str:
        base = f"http://{self.server}:{self.port}"
        if kind == "task":
            return f"{base}/api/v2/task"
        if kind == "bin":
            return f"{base}/api/v2/bin"
        if kind == "simulation":
            return f"{base}/api/v1/simulation"
        raise ValueError(f"unknown endpoint kind: {kind}")

    # --- core POST + parse ------------------------------------------------
    def _post(self, kind: str, xml_body: str) -> dict:
        """
        POST xml_body to the chosen endpoint and return a result dict:
          { ok, status, endpoint, request_xml, response_xml,
            fault_code, fault_text, params }
        Network errors are captured, never raised, so the UI can show them.
        """
        url = self._url(kind)
        result = {
            "ok": False,
            "status": None,
            "endpoint": url,
            "request_xml": pretty_xml(xml_body),
            "response_xml": "",
            "fault_code": None,
            "fault_text": None,
            "params": {},
            "error": None,
        }
        try:
            t0 = time.perf_counter()
            resp = requests.post(
                url,
                data=xml_body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=self.timeout,
            )
            result["duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
            if self.recorder:
                try:
                    self.recorder(kind, xml_body, resp.text, result["duration_ms"])
                except Exception:  # noqa: BLE001 — logging must never break a call
                    pass
            result["status"] = resp.status_code
            result["response_xml"] = pretty_xml(resp.text)
            self._parse_response(resp.text, result)
        except requests.exceptions.ConnectionError:
            result["error"] = (
                f"Could not connect to {url}. Is the WMS SDK running and a "
                f"simulation started?"
            )
        except requests.exceptions.Timeout:
            result["error"] = f"Request to {url} timed out after {self.timeout}s."
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    def _parse_response(self, text: str, result: dict) -> None:
        """Fill result with fault info or returned params from the response XML."""
        try:
            root = ET.fromstring(text.strip())
        except ET.ParseError:
            return
        fault = root.find("fault")
        if fault is not None:
            code_el = fault.find("code")
            if code_el is not None and code_el.text:
                code = int(code_el.text.strip())
                result["fault_code"] = code
                result["fault_text"] = ERROR_CODES.get(code, "Unknown error code.")
            return
        # success: collect any returned params
        params = root.find("params")
        if params is not None:
            for child in params:
                if list(child):  # nested element - keep as raw text
                    result["params"][child.tag] = ET.tostring(
                        child, encoding="unicode"
                    ).strip()
                else:
                    result["params"][child.tag] = (child.text or "").strip()
        result["ok"] = True

    # === TASK INTERFACE - PORT METHODS ===================================
    def openport(self, port_id, category=None, content=None, req_time=None,
                 port_mode=None, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        if req_time:
            p += f"\n    <req_time>{_esc(req_time)}</req_time>"
        if port_mode:
            p += f"\n    <port_mode>{_esc(port_mode)}</port_mode>"
        sel = ""
        if category is not None and str(category) != "":
            for c in str(category).split(","):
                c = c.strip()
                if c:
                    sel += f"\n      <category>{_esc(c)}</category>"
        if content is not None and str(content) != "":
            for c in str(content).split(","):
                c = c.strip()
                if c:
                    sel += f"\n      <content>{_esc(c)}</content>"
        if sel:
            p += f"\n    <select>{sel}\n    </select>"
        return self._post(endpoint, build_methodcall("openport", p))

    def openbin(self, port_id, bin_id=None, task_id=None, taskgroup_id=None,
                shipment=None, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        if bin_id not in (None, ""):
            p += f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        if task_id not in (None, ""):
            p += f"\n    <task_id>{_esc(task_id)}</task_id>"
        if taskgroup_id not in (None, ""):
            p += f"\n    <taskgroup_id>{_esc(taskgroup_id)}</taskgroup_id>"
        if shipment not in (None, ""):
            p += f"\n    <shipment>{_esc(shipment)}</shipment>"
        return self._post(endpoint, build_methodcall("openbin", p))

    def closebin(self, port_id, bin_id, task_id=None, content=None,
                 taskcomplete=None, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        p += f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        if task_id not in (None, ""):
            p += f"\n    <task_id>{_esc(task_id)}</task_id>"
        if content not in (None, ""):
            p += f"\n    <content>{_esc(content)}</content>"
        if taskcomplete not in (None, ""):
            p += f"\n    <taskcomplete>{_esc(taskcomplete)}</taskcomplete>"
        return self._post(endpoint, build_methodcall("closebin", p))

    def closeport(self, port_id, suspend_taskgroup=None, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        if suspend_taskgroup not in (None, ""):
            p += f"\n    <suspend_taskgroup>{_esc(suspend_taskgroup)}</suspend_taskgroup>"
        return self._post(endpoint, build_methodcall("closeport", p))

    def insertbin(self, port_id, bin_id, content=None, bin_type=None, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        p += f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        if content not in (None, ""):
            p += f"\n    <content>{_esc(content)}</content>"
        if bin_type not in (None, ""):
            p += f"\n    <bin_type>{_esc(bin_type)}</bin_type>"
        return self._post(endpoint, build_methodcall("insertbin", p))

    def removebin(self, port_id, bin_id, task_id=None, taskcomplete=None, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        p += f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        if task_id not in (None, ""):
            p += f"\n    <task_id>{_esc(task_id)}</task_id>"
        if taskcomplete not in (None, ""):
            p += f"\n    <taskcomplete>{_esc(taskcomplete)}</taskcomplete>"
        return self._post(endpoint, build_methodcall("removebin", p))

    def showpicktolight(self, source_port_id, source_bin_id, source_compartment,
                        quantity, target_port_id=None, target_bin_id=None,
                        target_compartment=None, endpoint="task"):
        p = f"\n    <source_port_id>{_esc(source_port_id)}</source_port_id>"
        if target_port_id not in (None, ""):
            p += f"\n    <target_port_id>{_esc(target_port_id)}</target_port_id>"
        p += f"\n    <source_bin_id>{_esc(source_bin_id)}</source_bin_id>"
        if target_bin_id not in (None, ""):
            p += f"\n    <target_bin_id>{_esc(target_bin_id)}</target_bin_id>"
        p += f"\n    <source_compartment>{_esc(source_compartment)}</source_compartment>"
        if target_compartment not in (None, ""):
            p += f"\n    <target_compartment>{_esc(target_compartment)}</target_compartment>"
        p += f"\n    <quantity>{_esc(quantity)}</quantity>"
        return self._post(endpoint, build_methodcall("showpicktolight", p))

    # === TASK INTERFACE - TASKGROUP / TASK METHODS =======================
    def create_taskgroup(self, taskgroup_id, category, tasks, req_time=None,
                         priority=None, shipment=None, suspended=None, endpoint="task"):
        """
        tasks: list of dicts, each {task_id, bin_id, item?, order?, estpicktime?}
        """
        p = f"\n    <taskgroup_id>{_esc(taskgroup_id)}</taskgroup_id>"
        if shipment not in (None, ""):
            p += f"\n    <shipment>{_esc(shipment)}</shipment>"
        p += f"\n    <category>{_esc(category)}</category>"
        if req_time not in (None, ""):
            p += f"\n    <req_time>{_esc(req_time)}</req_time>"
        if priority not in (None, ""):
            p += f"\n    <priority>{_esc(priority)}</priority>"
        if suspended not in (None, ""):
            p += f"\n    <suspended>{_esc(suspended)}</suspended>"
        tasks_xml = ""
        for t in tasks:
            tx = f"\n      <task>\n        <task_id>{_esc(t['task_id'])}</task_id>"
            tx += f"\n        <bin_id>{_esc(t['bin_id'])}</bin_id>"
            if t.get("item") not in (None, ""):
                tx += f"\n        <item>{_esc(t['item'])}</item>"
            if t.get("order") not in (None, ""):
                tx += f"\n        <order>{_esc(t['order'])}</order>"
            if t.get("estpicktime") not in (None, ""):
                tx += f"\n        <estpicktime>{_esc(t['estpicktime'])}</estpicktime>"
            tx += "\n      </task>"
            tasks_xml += tx
        p += f"\n    <tasks>{tasks_xml}\n    </tasks>"
        return self._post(endpoint, build_methodcall("create_taskgroup", p))

    def update_taskgroup(self, taskgroup_id, category=None, req_time=None,
                        priority=None, shipment=None, suspended=None, endpoint="task"):
        p = f"\n    <taskgroup_id>{_esc(taskgroup_id)}</taskgroup_id>"
        if shipment not in (None, ""):
            p += f"\n    <shipment>{_esc(shipment)}</shipment>"
        if category not in (None, ""):
            p += f"\n    <category>{_esc(category)}</category>"
        if req_time not in (None, ""):
            p += f"\n    <req_time>{_esc(req_time)}</req_time>"
        if priority not in (None, ""):
            p += f"\n    <priority>{_esc(priority)}</priority>"
        if suspended not in (None, ""):
            p += f"\n    <suspended>{_esc(suspended)}</suspended>"
        return self._post(endpoint, build_methodcall("update_taskgroup", p))

    def cancel_taskgroup(self, taskgroup_id, endpoint="task"):
        p = f"\n    <taskgroup_id>{_esc(taskgroup_id)}</taskgroup_id>"
        return self._post(endpoint, build_methodcall("cancel_taskgroup", p))

    def add_task(self, taskgroup_id, task_id, bin_id, item=None, order=None,
                 estpicktime=None, endpoint="task"):
        p = f"\n    <taskgroup_id>{_esc(taskgroup_id)}</taskgroup_id>"
        tx = f"\n      <task_id>{_esc(task_id)}</task_id>"
        tx += f"\n      <bin_id>{_esc(bin_id)}</bin_id>"
        if item not in (None, ""):
            tx += f"\n      <item>{_esc(item)}</item>"
        if order not in (None, ""):
            tx += f"\n      <order>{_esc(order)}</order>"
        if estpicktime not in (None, ""):
            tx += f"\n      <estpicktime>{_esc(estpicktime)}</estpicktime>"
        p += f"\n    <task>{tx}\n    </task>"
        return self._post(endpoint, build_methodcall("add_task", p))

    def cancel_task(self, task_id, endpoint="task"):
        p = f"\n    <task_id>{_esc(task_id)}</task_id>"
        return self._post(endpoint, build_methodcall("cancel_task", p))

    def setportsequence(self, port_id, taskgroup_ids, endpoint="task"):
        tg = "".join(
            f"\n      <taskgroup_id>{_esc(t)}</taskgroup_id>" for t in taskgroup_ids
        )
        p = f"\n    <port_id>{_esc(port_id)}</port_id>\n    <taskgroups>{tg}\n    </taskgroups>"
        return self._post(endpoint, build_methodcall("setportsequence", p))

    def appendportsequence(self, port_id, taskgroup_ids, endpoint="task"):
        tg = "".join(
            f"\n      <taskgroup_id>{_esc(t)}</taskgroup_id>" for t in taskgroup_ids
        )
        p = f"\n    <port_id>{_esc(port_id)}</port_id>\n    <taskgroups>{tg}\n    </taskgroups>"
        return self._post(endpoint, build_methodcall("appendportsequence", p))

    def flushport(self, port_id, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        return self._post(endpoint, build_methodcall("flushport", p))

    def getbinloc(self, bin_id, endpoint="task"):
        p = f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        return self._post(endpoint, build_methodcall("getbinloc", p))

    # === GENERIC CALLER (training: fire any method from flat params) ======
    def generic_call(self, method, params=None, endpoint="task"):
        """Build <methodcall> from a flat {param: value} dict and POST it.
        Used by the Training tab so every documented method is callable,
        not just the hand-coded ones. Empty values are omitted."""
        p = ""
        for k, v in (params or {}).items():
            if v in (None, ""):
                continue
            p += f"\n    <{_esc(k)}>{_esc(v)}</{_esc(k)}>"
        return self._post(endpoint, build_methodcall(method, p))

    # === TASK INTERFACE - QUERY METHODS ==================================
    def getbinstate(self, bin_id, endpoint="task"):
        p = f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        return self._post(endpoint, build_methodcall("getbinstate", p))

    def getbininfo(self, bin_id, endpoint="task"):
        p = f"\n    <bin_id>{_esc(bin_id)}</bin_id>"
        return self._post(endpoint, build_methodcall("getbininfo", p))

    def getportstatus(self, port_id, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        return self._post(endpoint, build_methodcall("getportstatus", p))

    def getnexttaskgroups(self, port_id, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        return self._post(endpoint, build_methodcall("getnexttaskgroups", p))

    def getdrawerstate(self, port_id, endpoint="task"):
        p = f"\n    <port_id>{_esc(port_id)}</port_id>"
        return self._post(endpoint, build_methodcall("getdrawerstate", p))

    def getsystemstate(self, endpoint="task"):
        return self._post(endpoint, build_methodcall("getsystemstate"))

    def getversion(self, endpoint="task"):
        return self._post(endpoint, build_methodcall("getversion"))

    # === SIMULATION CONTROL ==============================================
    def start_simulation(self):
        return self._post("simulation", build_methodcall("start_simulation"))

    def stop_simulation(self):
        return self._post("simulation", build_methodcall("stop_simulation"))

    def set_simulation_speed(self, speed):
        p = f"\n    <speed>{_esc(speed)}</speed>"
        return self._post("simulation", build_methodcall("set_simulation_speed", p))

    def get_simulation_speed(self):
        return self._post("simulation", build_methodcall("get_simulation_speed"))
