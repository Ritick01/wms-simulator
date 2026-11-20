"""
WMS Simulator - live operation backend (FastAPI).

Serves the single-page UI and exposes the operation engine:
  POST /api/setup       configure the site (ports + types, bin pool, buffer)
  POST /api/op/start    start the live operation
  POST /api/op/stop     stop it
  POST /api/op/suspend  suspend one port
  POST /api/op/resume   resume one port
  POST /api/op/adhoc    request an ad-hoc bin at a port
  POST /api/sim/*       start / stop / speed of the SDK simulation
  POST /api/call        fire a single interface method (step mode)
  GET  /api/state       engine + per-port state
  GET  /api/calllog     recent API calls (request + response)
  GET  /api/calllog/since?seq=N  every call after N, oldest first (for the monitor)
  GET  /api/run_info    site profile suggestion, planted defects, request-log path
  GET  /api/lp          decoded Log Publisher stream + live ports/bins
  GET  /api/test        quick reachability check of the SDK

Run inside the machine where the WMS SDK is running (localhost:44000).
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os

from autostore_client import AutoStoreClient
from engine import OperationEngine

app = FastAPI(title="WMS Simulator - Live Operation")
ENGINE = OperationEngine()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---- setup & operation ----------------------------------------------------
@app.post("/api/setup")
async def setup(req: Request):
    cfg = await req.json()
    return JSONResponse(ENGINE.configure(cfg))


@app.post("/api/op/start")
def op_start():
    return JSONResponse(ENGINE.start())


@app.post("/api/op/stop")
def op_stop():
    ENGINE.stop()
    return JSONResponse(ENGINE.state())


@app.post("/api/op/suspend")
async def op_suspend(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.suspend(d["port_id"]))


@app.post("/api/op/resume")
async def op_resume(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.resume(d["port_id"]))


@app.post("/api/op/adhoc")
async def op_adhoc(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.adhoc(d["port_id"], d["bin_id"]))


@app.post("/api/op/confirm")
async def op_confirm(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.confirm_pick(d["port_id"]))


@app.post("/api/op/clear_results")
def op_clear_results():
    return JSONResponse(ENGINE.clear_results())


# ---- live views -----------------------------------------------------------
@app.get("/api/state")
def state():
    return JSONResponse(ENGINE.state())


@app.get("/api/calllog")
def calllog():
    return JSONResponse(ENGINE.call_log())


@app.get("/api/calllog/since")
def calllog_since(seq: int = 0):
    return JSONResponse(ENGINE.calllog_since(seq))


@app.get("/api/run_info")
def run_info():
    return JSONResponse(ENGINE.run_info())


@app.get("/api/scenarios")
def scenarios():
    import test_scenarios
    return JSONResponse(test_scenarios.public())


@app.get("/api/injections")
def injections():
    from engine import INJECTIONS
    return JSONResponse(INJECTIONS)


@app.get("/api/lp")
def lp():
    return JSONResponse(ENGINE.lp_snapshot())


# ---- simulation control ---------------------------------------------------
def _client():
    if ENGINE.client:
        return ENGINE.client
    return AutoStoreClient()


@app.post("/api/discover")
async def discover(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.discover(d.get("lp_host", "127.0.0.1"),
                                        d.get("lp_port", 44001)))


@app.post("/api/sim/start")
def sim_start():
    return JSONResponse(ENGINE.step_call("start_simulation", {}))


@app.post("/api/sim/stop")
def sim_stop():
    return JSONResponse(ENGINE.step_call("stop_simulation", {}))


@app.post("/api/sim/speed")
async def sim_speed(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.step_call("set_simulation_speed", {"speed": d.get("speed", 100)}))


# ---- single call (step mode) ---------------------------------------------
@app.post("/api/call")
async def call(req: Request):
    d = await req.json()
    return JSONResponse(ENGINE.step_call(d.get("method"), d.get("params", {})))


@app.get("/api/test")
def test():
    return JSONResponse(_client().getversion())


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")