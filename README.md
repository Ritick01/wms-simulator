# WMS Simulator — Live Operation

An internal AutoStore training/verification tool. It drives a **full, realistic
picking operation** against the WMS SDK Task Interface and shows every API call
(request + response) and the decoded Log Publisher stream live.

It runs the way a good WMS should:

- **Buffer feeder** keeps a look-ahead of task groups in the system
  (`create_taskgroup` with `req_time`, priority 1).
- **Per-port pick loop** runs `openport` → `openbin` → (wait for the bin to be
  ready) → `closebin`, one bin at a time, with a fast hand-off.
- **Suspend / resume** — `closeport suspend_taskgroup=1` then `openbin` with the
  task group id.
- **Ad-hoc bin** — `openbin` with an explicit `bin_id`.
- **Log Publisher listener** (port 44001) decodes DS/DC/T/F/BL/BM/PM/PD/CI… into
  a live port/bin view.

Because it speaks the plain Task Interface, the **same tool runs against the SDK
simulation or a live grid** — only the host/port in Site setup changes.

## Two modes (choose in Site setup)

- **Observe (read-only) — safe on a live production grid.** The tool only
  **listens** (Log Publisher) and **queries** (`getportstatus`, `getbinstate`,
  …). Every state-changing method is **blocked on the server**, so it can never
  disrupt the customer's real WMS. It auto-discovers ports from the Log
  Publisher and shows live per-port KPIs — **Waitbin** (seconds a port sits idle
  with no ready bin) and **throughput (bins/hr)**. This is the White Glove /
  diagnostic use.
- **Drive (send commands) — dedicated TEST / commissioning grid only.** Runs the
  full operation (creates task groups, open→pick→close). Requires ticking a
  confirmation that no production WMS is connected. On real hardware use
  **confirm = manual** so a human closes each bin; **auto** is for the SDK sim.

> Safety: never run Drive mode against a grid that has the customer's WMS
> connected. Use Observe there.

## Scenarios (Drive mode)

The run follows a **demand profile**, fed **per category** so every port group
gets its share (no starved ports):

- **Steady (24/7)** — hold each category's target rate continuously.
- **Peak-hours** — base rate with a peak window (×factor over a minute range).
- **Timed shift** — a duration with a ramp up at the start and down at the end.

Set **Targets** as `cat:bins_per_hr` (e.g. `1:200,2:150,3:150`) and a
**look-ahead** (default 30 min). The feeder paces task-group creation to hit
each target, scaled by the scenario. The live header shows the demand factor
and active groups per category.

Correctness fixes baked in: the bin pool is validated against the grid
(non-existent bins ignored, no 1011), and a bin is closed only once it is
actually ready at the port (no 1027).

Endpoints: Task `http://<host>:44000/api/v2/task`, Log Publisher `<host>:44001`.

## Feeding the WMS Monitor (Oct 2026)

The simulator is the monitor's reference WMS and its test bench.

- **Request log on disk.** In Drive mode every call the simulated WMS sends —
  including polls that are not shown in the UI — is written to
  `logs\request-log<YYYYMMDD>_<run id>.txt` in the AutoStore request-log line
  format (`timestamp,<methodcall>…,<response>…,/api/v2/task,ms`). One file per
  run, so a replay never mixes runs. Point the monitor at the `logs` folder
  (Real-time → Request log) or replay a file (Existing case).
- **Grid time.** At sim speed 1000 (10×) the log is stamped in grid time, so a
  10-minute run reads like 100 minutes on site and every bins/h and duration the
  monitor computes is real-world comparable. The live call log carries the same
  `ts_iso`.
- **`GET /api/calllog/since?seq=N`** — every call after N, oldest first (5,000
  call buffer). The monitor uses it so nothing is missed at high speed.
- **`GET /api/run_info`** — a suggested site profile (target = the demand the
  run was asked to deliver), the defects planted, and the log path. The monitor's
  *Load from simulator* button reads it.
- **Inject defects** (Drive mode, Site setup). Plants known integration mistakes
  on purpose — every Nth bin per port, in turn — so the monitor can be checked:
  G01 closeport per bin · G02 no prepare phase · G04 ad-hoc opens · G05 ping-pong
  · G06 category churn · G07 getbinstate polling · A01 long hold · C02 bin
  conflict · E01 early closebin (1027). Planted calls are labelled
  `[inject:Gxx]` in the call list. Then use the monitor's Runs tab → *Check
  against simulator injections*. Leave all off for a clean reference run.
  For G02, start from a fresh SDK simulation so no work from an earlier run is
  still queued.
- **Guideline breaches** (same *Inject defects* box, Oct 23 2026). Checked by the
  monitor's guideline check, not a T2 rule:
  GL_PREP forecasts 30% of the bin pool at the start (limit 25%) · GL_LOOKAHEAD
  keeps only ~2 min of work queued (guideline ~30 min) · GL_HANDOFF waits 3 s
  between closebin and openbin at the first picking port (guideline ~100 ms).
  GL_PREP needs the SDK to accept `forecastbin`; if it refuses, the status line
  says so and nothing is counted as planted. `run_info` reports `total_bins`
  (the pool size) so the monitor can apply the 25% rule.
  Use sim speed 10× or lower for a clean guideline reference: at 60× each call's
  own wait is stretched 60× in grid time and the hand-off reads *watch*.

## Test scenarios for the WMS Monitor (Oct 23 2026)

Use **Site setup → Test scenario**:
1. Pick a scenario and press **Load settings**. This fills in the targets, speed, pick time, staffing and planted defects, and shows what the monitor should find.
2. Tick the test-grid box, press **Configure ▸**, then **Start ▶**. Run for at least the minutes shown.
3. On the monitor, press **Check test scenario**. It shows PASS or FAIL, with one line per expected finding.

There are 13 scenarios in `test_scenarios.py`: clean reference, peak ×3, overload, starved ports, slow operators, operator away (with and without auto port close), ports opened too early, bad integration, guideline breaches, ad-hoc heavy, grid stop and full day.

Grid stop is a manual step: stop the grid in the Service Console during the run. Add a scenario by adding an entry to `SCENARIOS`.

## Realistic WMS behaviour (Oct 23 2026)

Site setup → Drive mode, all values in grid seconds:

- **Staff ports to demand** (on by default). Opens only the ports each category's
  target needs: one port works about 3600 / (pick time + 5 s) bins/h, so 500 bins/h
  at 20 s pick time = 4 ports. The rest show *standby*. Opening 18 ports for
  500 bins/h starves them all after the prepared buffer is gone: 1015 answers,
  60 s gaps between bins, and the monitor's score falls to ~35 within minutes.
- **Pick time** (default = estpicktime, 20 s). With confirm = auto the tool
  plays the operator: it waits this long before closebin. No call is made on a
  port while its bin is open.
- **Bin arrival buffer** (default 300 s). How long the WMS waits for a bin to
  reach the port. If it is late: alarm (`getportstatus`, labelled `[alarm]`), one
  more buffer, then the port is stopped. It never opens the next bin while one
  is open (that was the monitor's G10).
- **Operator away** (inject A01, default 150 s): the operator leaves with a bin
  open; no closebin for that long. The monitor reports A01 and recommends an
  auto port close.
- **Auto port close after** (0 = off; try 120). The WMS suspends the port
  (`closeport suspend_taskgroup=1`) when no closebin comes in time, then
  `openport` + `openbin taskgroup_id` when the operator is back. The monitor
  treats this as the correction: no A01, G01 or G05.
- **Stop** closes a port that still has a bin open with suspend, so ending a run
  no longer shows up as G01.
- Restart the simulator after updating `engine.py` (uvicorn does not reload).

Behaviour changes that make the reference run cleaner:

- Bin arrival is taken from the **Log Publisher** bin-mode events (the monitor's
  G07 correction); `getbinstate` is used once as a fallback, and only polled
  when the LP is not connected.
- The feeder counts a task group as done when its last bin is closed, so the
  buffer is topped up from real consumption (before, the time-based expiry left
  ports polling 'no work' while the feeder thought the buffer was full).
- **Stop** closes the port sessions it opened, so the next run doesn't start
  with `openport` → 1002.
- `/api/op/clear_results` works (it was wired in `app.py` but missing in the engine).

## Important — run it INSIDE the SDK machine

The SDK and Log Publisher listen on `localhost` inside your **Parallels Windows
VM**. Run this tool there (not on the Mac), or point Site setup at the VM's IP.

## Setup (Windows, no admin needed)

```powershell
cd C:\wms-simulator
python -m venv wms
.\wms\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
.\wms\Scripts\Activate.ps1
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

## Use

1. **Load a grid** in the WMS SDK and **start the simulation** (robots can move).
   Make sure the **bin pool** in Site setup lists bin ids that exist in that grid.
2. In the tool: **Site setup** → set host/port, bin pool, buffer size, and add
   your ports (pick the real port ids + types from your grid). **Configure &
   start operation**.
3. Press **Start ▶**. Watch the ports pick bin-by-bin, the API calls stream on
   the right (green 200 / red fault, click to expand request+response), and the
   Log Publisher events at the bottom.
4. Try **Suspend/Resume** and **Ad-hoc bin** on a port to show those features.
5. Use **Test SDK** (top right) to confirm reachability; **Sim ▶/■/Speed** to
   control the simulation.

## Notes

- Internal tool — keep it on a company repo or local, not personal GitHub.
- Bin pool must match the loaded grid, or `create_taskgroup` / `openbin` will
  fault (1011 / 1015).
- Port types and custom/AutoCase tracks are shown for site modelling; the live
  pick loop drives standard picking ports.


