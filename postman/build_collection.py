"""
Build a Postman collection of every AutoStore interface call, in XML and JSON.

Source: autostore_client.py (signatures: positional = required) and the
Training tab catalog in static/index.html. Optional parameters are written
commented out, so a request sends only what it needs:
  XML  ->  <!-- <content>0</content> -->
  JSON ->  // , "content": 0      (leading comma: just delete the //)

The JSON layout mirrors the XML <methodcall> one to one — it is NOT taken from
AutoStore documentation. Check it against one working JSON call before use.

Run:  python postman\\build_collection.py
"""

import json
import os

# A parameter: (name, value, required, note). value may be a list of child
# parameters (nested element) — a repeated element is listed once per value.
R, O = True, False

TG = "{{taskgroup_id}}"
GROUPS = [
    ("Port session", "task", [
        ("openport", "Open a port for a category; AutoStore starts preparing bins.", [
            ("port_id", "{{port_id}}", R, ""),
            ("req_time", "{{req_time}}", O, ""),
            ("port_mode", "", O, "e.g. SEQUENCE"),
            ("select", [("category", "{{category}}", R, "repeat for more categories"),
                        ("content", "", O, "select by content code instead")], R, ""),
        ]),
        ("openbin", "Request the next bin at the port. Use at most ONE selector (else 1023).", [
            ("port_id", "{{port_id}}", R, ""),
            ("bin_id", "{{bin_id}}", O, "ad-hoc bin"),
            ("task_id", "{{task_id}}", O, ""),
            ("taskgroup_id", TG, O, "resume a suspended group"),
            ("shipment", "{{shipment}}", O, ""),
        ]),
        ("closebin", "Close the bin after the pick.", [
            ("port_id", "{{port_id}}", R, ""),
            ("bin_id", "{{bin_id}}", R, ""),
            ("task_id", "{{task_id}}", O, "recommended: without it bin mode can loop"),
            ("content", "", O, ""),
            ("taskcomplete", "true", O, ""),
        ]),
        ("closeport", "End the port session.", [
            ("port_id", "{{port_id}}", R, ""),
            ("suspend_taskgroup", "1", O, "1 = lock the group for a later resume"),
        ]),
    ]),
    ("Bin exchange", "task", [
        ("insertbin", "Register a bin inserted at the port.", [
            ("port_id", "{{port_id}}", R, ""),
            ("bin_id", "{{bin_id}}", R, ""),
            ("content", "", O, ""),
            ("bin_type", "", O, "required the first time a bin is inserted"),
        ]),
        ("removebin", "Register the open bin removed from the port.", [
            ("port_id", "{{port_id}}", R, ""),
            ("bin_id", "{{bin_id}}", R, ""),
            ("task_id", "{{task_id}}", O, ""),
            ("taskcomplete", "true", O, ""),
        ]),
    ]),
    ("Task groups", "task", [
        ("create_taskgroup", "Create a task group and its tasks (copy <task> for more).", [
            ("taskgroup_id", TG, R, ""),
            ("shipment", "{{shipment}}", O, ""),
            ("category", "{{category}}", R, ""),
            ("req_time", "{{req_time}}", R, "SDK faults 1035 without it"),
            ("priority", "20", O, ""),
            ("suspended", "0", O, ""),
            ("tasks", [
                ("task", [("task_id", "{{task_id}}", R, ""), ("bin_id", "{{bin_id}}", R, ""),
                          ("item", "1", O, ""), ("order", "", O, ""),
                          ("estpicktime", "20", O, "seconds")], R, ""),
            ], R, ""),
        ]),
        ("update_taskgroup", "Update group-level properties only (not its tasks).", [
            ("taskgroup_id", TG, R, ""),
            ("req_time", "{{req_time}}", R, ""),
            ("shipment", "{{shipment}}", O, ""),
            ("category", "{{category}}", O, ""),
            ("priority", "20", O, ""),
            ("suspended", "0", O, ""),
        ]),
        ("add_task", "Add one task to an existing group.", [
            ("taskgroup_id", TG, R, ""),
            ("task", [("task_id", "{{task_id}}", R, ""), ("bin_id", "{{bin_id}}", R, ""),
                      ("item", "1", O, ""), ("order", "", O, ""),
                      ("estpicktime", "20", O, "")], R, ""),
        ]),
        ("cancel_task", "Cancel a single task (not while it is selected).", [
            ("task_id", "{{task_id}}", R, "")]),
        ("cancel_taskgroup", "Cancel a whole task group.", [("taskgroup_id", TG, R, "")]),
    ]),
    ("Port sequence", "task", [
        ("setportsequence", "Replace the port's task-group sequence (SEQUENCE mode).", [
            ("port_id", "{{port_id}}", R, ""),
            ("taskgroups", [("taskgroup_id", TG, R, "repeat for each group")], R, ""),
        ]),
        ("appendportsequence", "Append task groups to the port sequence.", [
            ("port_id", "{{port_id}}", R, ""),
            ("taskgroups", [("taskgroup_id", TG, R, "repeat for each group")], R, ""),
        ]),
        ("getportsequence", "Read the port's task-group sequence.", [("port_id", "{{port_id}}", R, "")]),
    ]),
    ("Pick-to-light (AS Router)", "task", [
        ("showpicktolight", "Light a pick: source + optional target compartment.", [
            ("source_port_id", "{{port_id}}", R, ""),
            ("source_bin_id", "{{bin_id}}", R, ""),
            ("source_compartment", "1", R, ""),
            ("quantity", "1", R, ""),
            ("target_port_id", "", O, ""),
            ("target_bin_id", "", O, ""),
            ("target_compartment", "", O, ""),
        ]),
        ("hidepicktolight", "Cancel a pick-to-light.", [
            ("source_port_id", "{{port_id}}", R, ""),
            ("target_port_id", "", O, ""),
        ]),
    ]),
    ("MultiPort / flush (AS Router)", "task", [
        ("enablemultiport", "Enable a MultiPort (ports must be empty).", [("port_id", "{{port_id}}", R, "")]),
        ("disablemultiport", "Disable a MultiPort.", [("port_id", "{{port_id}}", R, "")]),
        ("flushport", "Send all bins at a port back to the grid.", [("port_id", "{{port_id}}", R, "")]),
        ("flushportbin", "Send one bin back to the grid.", [
            ("port_id", "{{port_id}}", R, ""), ("bin_id", "{{bin_id}}", R, "")]),
    ]),
    ("Forecast / bin admin", "task", [
        ("forecastbin", "Set bin(s) to FORECAST. One bin_id OR a min/max range.", [
            ("bin_id", "{{bin_id}}", R, "or use the range below instead"),
            ("min_bin_id", "", O, ""), ("max_bin_id", "", O, "")]),
        ("cancelforecastbin", "Take bin(s) out of FORECAST. One bin_id OR a range.", [
            ("bin_id", "{{bin_id}}", R, "or use the range below instead"),
            ("min_bin_id", "", O, ""), ("max_bin_id", "", O, "")]),
        ("updatebin", "Update a bin's content.", [
            ("bin_id", "{{bin_id}}", R, ""), ("content", "0", R, "")]),
        ("createbin", "Define a new bin (mode OUTSIDE).", [
            ("bin_id", "{{bin_id}}", R, ""), ("bin_type", "1", R, "")]),
        ("deletebin", "Delete a bin (must be OUTSIDE, no task group).", [("bin_id", "{{bin_id}}", R, "")]),
    ]),
    ("Config / system", "task", [
        ("configureport", "Update a port setting (AS Router).", [
            ("port_id", "{{port_id}}", R, ""),
            ("configname", "robotqueue", R, "or weight_config / nexttaskgroupbinlookahead"),
            ("configvalue", "2", R, "")]),
        ("setconfiguration", "Update AS Planner/Router config.", [
            ("configname", "preparationspan", R, ""), ("configvalue", "10", R, "")]),
        ("forcecharge", "Force a % of R5 robots to charge (0/50/100).", [("charge_percent", "50", R, "")]),
    ]),
    ("Port Interface (custom ports)", "task", [
        ("portcellallocate", "Announce a cell for delivery/fetch.", [
            ("port_id", "{{port_id}}", R, ""), ("x", "", R, ""), ("y", "", R, ""),
            ("getbin", "true", R, "true = fetch")]),
        ("portlocationready", "Permit a delivery to the cell.", [
            ("port_id", "{{port_id}}", R, ""), ("x", "", R, ""), ("y", "", R, ""),
            ("getbin", "false", R, "")]),
        ("portbinready", "Confirm a bin is ready in the cell to be fetched.", [
            ("port_id", "{{port_id}}", R, ""), ("x", "", R, ""), ("y", "", R, ""),
            ("bin_id", "{{bin_id}}", R, ""), ("content", "", O, ""), ("bin_type", "", O, "")]),
        ("portbinmode", "Set custom-port bin mode: OPEN / CLOSED / OUTSIDE.", [
            ("port_id", "{{port_id}}", R, ""), ("bin_id", "{{bin_id}}", R, ""),
            ("bin_mode", "OPEN", R, ""), ("content", "", O, "")]),
    ]),
    ("AutoCase", "task", [
        (m, d, [("port_id", "{{port_id}}", R, ""), ("bin_id", "{{bin_id}}", R, "")])
        for m, d in [("case_insert", "Next AutoCase op is an insert."),
                     ("case_extract", "Next AutoCase op is an extract."),
                     ("case_cancel", "Cancel the AutoCase insert/extract."),
                     ("case_recvready", "Next AutoCase op is ready.")]
    ] + [("case_getstate", "Read AutoCase state for a port.", [("port_id", "{{port_id}}", R, "")])]),
    ("Queries (read-only)", "task", [
        ("getportstatus", "Port mode, selected bin/task, ready, errors.", [("port_id", "{{port_id}}", R, "")]),
        ("getbinstate", "Bin mode, content, position, port.", [("bin_id", "{{bin_id}}", R, "")]),
        ("getbininfo", "Bin content + target grid.", [("bin_id", "{{bin_id}}", R, "")]),
        ("getbinloc", "Bin location + bins above.", [("bin_id", "{{bin_id}}", R, "")]),
        ("getnexttaskgroups", "Planned next task groups for a port.", [("port_id", "{{port_id}}", R, "")]),
        ("getdrawerstate", "Drawer position + snap lock (AS Router).", [("port_id", "{{port_id}}", R, "")]),
        ("getportconfig", "Port weight equipment + config.", [("port_id", "{{port_id}}", R, "")]),
        ("getportweight", "Port weight reading.", [
            ("port_id", "{{port_id}}", R, ""), ("timeout_mode", "", O, ""), ("timeout_value", "", O, "")]),
        ("gettaskinfo", "Task group / task details.", [("taskgroup_id", TG, R, "")]),
        ("getcellcontent", "Content code of a grid cell.", [("x", "", R, ""), ("y", "", R, "")]),
        ("getsystemstate", "System mode + forcecharge.", []),
        ("getsystemmode", "System mode.", []),
        ("getversion", "Interface version.", []),
        ("gettimefactor", "Simulation time factor.", []),
    ]),
    ("Simulation control (SDK only)", "simulation", [
        ("start_simulation", "Start the simulation.", []),
        ("stop_simulation", "Stop the simulation.", []),
        ("set_simulation_speed", "Set sim speed (100 = normal).", [("speed", "100", R, "")]),
        ("get_simulation_speed", "Read sim speed.", []),
    ]),
]

PATHS = {"task": "api/v2/task", "simulation": "api/v1/simulation"}


# ---- XML -----------------------------------------------------------------
def xml_lines(params, ind):
    out = []
    for name, val, req, note in params:
        if isinstance(val, list):
            block = [f"{ind}<{name}>"] + xml_lines(val, ind + "  ") + [f"{ind}</{name}>"]
            out += block if req else [f"{ind}<!--"] + block + [f"{ind}-->"]
            continue
        line = f"<{name}>{val}</{name}>"
        tip = f" <!-- {note} -->" if note and req else ""
        out.append(f"{ind}{line}{tip}" if req else
                   f"{ind}<!-- {line}{'  ' + note if note else ''} -->")
    return out


def xml_body(method, params):
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<methodcall>", f"  <name>{method}</name>"]
    if params:
        lines += ["  <params>"] + xml_lines(params, "    ") + ["  </params>"]
    return "\n".join(lines + ["</methodcall>"])


# ---- JSON ----------------------------------------------------------------
# Repeated XML elements (several <task>, <category>, <taskgroup_id>) become
# arrays. Required members come first; optional ones are commented with a
# leading comma, so removing "// " always leaves valid JSON.
ARRAYS = {"task": "tasks", "category": "select", "taskgroup_id": "taskgroups"}


def jval(v):
    if v.startswith("{{"):
        return f'"{v}"'                     # Postman variable, kept as a string
    if v in ("true", "false"):
        return v
    return v if v.lstrip("-").isdigit() else json.dumps(v)


def json_members(params, ind, parent=None):
    req = [p for p in params if p[2]]
    opt = [p for p in params if not p[2]]
    out = []
    for i, (name, val, _, note) in enumerate(req):
        comma = "," if i < len(req) - 1 else ""
        out += json_member(name, val, ind, comma, note, parent)
    for name, val, _, note in opt:
        member = json_member(name, val, ind, "", note, parent)
        member[0] = member[0].replace(ind, f"{ind}// , ", 1)
        out += [member[0]] + [l.replace(ind, f"{ind}// ", 1) for l in member[1:]]
    return out


def json_member(name, val, ind, comma, note, parent):
    tip = f"  // {note}" if note else ""
    if isinstance(val, list):
        inner = val[0][0] if len(val) == 1 and ARRAYS.get(val[0][0]) == name else None
        if inner:                           # <tasks><task>…</task></tasks> -> "tasks": [ {…} ]
            child = val[0][1]
            if isinstance(child, list):
                body = [f"{ind}  {{"] + json_members(child, ind + "    ") + [f"{ind}  }}"]
            else:
                body = [f"{ind}  {jval(child)}{'  // ' + val[0][3] if val[0][3] else ''}"]
            return [f'{ind}"{name}": ['] + body + [f"{ind}]{comma}{tip}"]
        body = json_members(val, ind + "  ", name)
        return [f'{ind}"{name}": {{'] + body + [f"{ind}}}{comma}{tip}"]
    if parent and ARRAYS.get(name) == parent:        # <select><category>1</category> -> "category": [1]
        return [f'{ind}"{name}": [{jval(val)}]{comma}{tip}']
    return [f'{ind}"{name}": {jval(val)}{comma}{tip}']


def json_body(method, params):
    if not params:
        return "{\n  \"name\": \"%s\"\n}" % method
    return "\n".join([f'{{\n  "name": "{method}",\n  "params": {{']
                     + json_members(params, "    ") + ["  }", "}"])


# ---- collection ----------------------------------------------------------
def request(method, desc, params, kind, fmt):
    raw = xml_body(method, params) if fmt == "xml" else json_body(method, params)
    path = PATHS[kind]
    return {
        "name": method,
        "request": {
            "method": "POST",
            "description": desc,
            "header": [{"key": "Content-Type", "value": f"application/{fmt}"}],
            "body": {"mode": "raw", "raw": raw, "options": {"raw": {"language": fmt}}},
            "url": {"raw": "http://{{host}}:{{port}}/" + path, "protocol": "http",
                    "host": ["{{host}}"], "port": "{{port}}", "path": path.split("/")},
        },
    }


def folder(fmt):
    return {"name": fmt.upper(), "item": [
        {"name": g, "item": [request(m, d, p, kind, fmt) for m, d, p in calls]}
        for g, kind, calls in GROUPS]}


PRE_REQUEST = [
    "// req_time = now in UTC, without milliseconds (the SDK format)",
    "pm.collectionVariables.set('req_time', new Date().toISOString().replace(/\\.\\d+Z$/, 'Z'));",
]

VARS = {"host": "127.0.0.1", "port": "44000", "port_id": "1", "bin_id": "10304",
        "category": "1", "taskgroup_id": "123004", "task_id": "420192",
        "shipment": "20961", "req_time": ""}

COLLECTION = {
    "info": {
        "name": "AutoStore Task Interface (XML + JSON)",
        "description": ("Every Task Interface call. Optional parameters are commented out: "
                        "XML <!-- -->, JSON // with a leading comma. The JSON layout mirrors "
                        "the XML and is not confirmed against AutoStore documentation. "
                        "Generated by postman/build_collection.py."),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": PRE_REQUEST}}],
    "variable": [{"key": k, "value": v} for k, v in VARS.items()],
    "item": [folder("xml"), folder("json")],
}

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "AutoStore_Task_Interface.postman_collection.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(COLLECTION, f, indent=2)
    n = sum(len(c) for _, _, c in GROUPS)
    print(f"{n} calls x 2 formats -> {out}")
