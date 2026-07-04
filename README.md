# Verilog Coding Agent

Multi-agent RTL generation workflow for synthesizable Verilog-2001 RTL.

The agent breaks a user RTL request into implementation tasks, creates an architecture contract, plans control logic and datapath, generates RTL, reviews microarchitecture quality, runs lint when available, and optionally creates a smoke testbench.

See [AGENT_USAGE.md](AGENT_USAGE.md) for setup and run examples.

## Run Dashboard

Monitor active and completed `output_*` agent runs with the local dashboard:

```bash
python3 dashboard.py
```

Open `http://localhost:8766` to view the current run status, retry counts,
force-forward flags, recent logs, failed attempts, and generated artifacts.

## RTL DataPath Visualizer

`rtl_datapath_visualizer.py` can read an `rte`-style filelist (`.f`) and generate:

1. The module hierarchy below the inferred top module.
2. Likely datapath connections using name-based heuristics.

Example:

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f 3
```

Depth rules:

- The depth can be passed as a positional number, for example `filelist.f 3`, or as `--depth 3`.
- `--depth 3`: draw modules up to 3 instance levels below TOP.
- `--depth 0`: draw every parsed module reachable from TOP.
- `--top <module>`: override automatic TOP detection when needed.

Outputs:

- `rtl_datapath.dot`: Graphviz DOT source.
- `rtl_datapath.png`: generated automatically when Graphviz `dot` is installed.
- `rtl_datapath.excalidraw`: editable Excalidraw-compatible block diagram.

Output paths can be overridden with `--out`, `--png`, and `--excalidraw`.

Display rules:

- Blue node: top module.
- Orange node/edge: likely datapath module or connection.
- Gray node/edge: general control or other connection.

Supported filelist entries:

- Verilog/SystemVerilog file paths (`.v`, `.sv`, `.vh`, `.svh`).
- `+incdir+...` entries are accepted and ignored during parsing.
- `-v <file>`.
- Comments and blank lines.

Complex macro-heavy or `generate`-heavy structures may not be parsed perfectly.

## VCS/FSDB Failure Explainer

`vcs_failure_explainer.py` creates a first-pass debug report for VCS simulation failures.
It parses VCS/UVM logs, finds the first blocking failure timestamp, builds a failure
window, generates Verdi launch artifacts for the FSDB, and optionally ranks active
signals when a VCD export is available.

Typical VCS/FSDB run:

```bash
python3 vcs_failure_explainer.py \
  --log ./simv.log \
  --fsdb ./dump.fsdb \
  --filelist ./filelist.f \
  --rtl-dir ./rtl \
  --out-dir ./sim_debug_report
```

If you can export a small failure-window VCD from FSDB/Verdi utilities, pass it too:

```bash
python3 vcs_failure_explainer.py \
  --log ./simv.log \
  --fsdb ./dump.fsdb \
  --vcd ./failure_window.vcd \
  --filelist ./filelist.f \
  --rtl-dir ./rtl \
  --before-ns 100 \
  --after-ns 20
```

When a saved DUT output vector and a reference vector are available, pass both
files to anchor debug on the first data mismatch:

```bash
python3 vcs_failure_explainer.py \
  --log ./simv.log \
  --fsdb ./dump.fsdb \
  --vcd ./failure_window.vcd \
  --reference-vector ./reference.vec \
  --actual-vector ./actual.vec \
  --vector-signal out_data \
  --filelist ./filelist.f \
  --rtl-dir ./rtl
```

Supported vector lines can be plain one-value-per-line entries or key/value rows:

```text
cycle=42 time=18420ns out_data=0x35
cycle=42 time=18420ns out_data=0x31
```

Header-based CSV/TSV vectors are also supported:

```text
cycle,time_ns,out_data
42,18420,0x35
42,18420,0x31
```

If the vector file has no explicit time field, use `--vector-cycle-ns` and
`--vector-start-time-ns` to map sample/cycle indices back onto the waveform.

Outputs:

- `debug_report.md`: human-readable failure summary, waveform ranking, and RTL candidates.
- `debug_report.html`: browser-friendly version of the report.
- `debug_summary.json`: machine-readable parsed log, window, signal, and source data.
- `signal_list.txt`: ranked signals to inspect in Verdi.
- `open_verdi.sh`: generated when `--fsdb` is supplied.
- `verdi_open.tcl`: window/signal notes for local Verdi customization.

Try the included fixture:

```bash
python3 vcs_failure_explainer.py \
  --run-dir examples/sim_debug_sample \
  --vector-signal out_data \
  --rtl-dir examples/sim_debug_sample/rtl \
  --out-dir /tmp/vcs_failure_explainer_sample
```

Native FSDB waveform parsing depends on Verdi/Novas utilities and licensing, so the
first version treats FSDB as the canonical waveform database for launch/debug and uses
VCD input for portable signal activity analysis.
