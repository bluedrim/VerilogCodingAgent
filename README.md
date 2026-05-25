# Verilog Coding Agent

Multi-agent RTL generation workflow for Verilog/SystemVerilog.

The agent breaks a user RTL request into implementation tasks, creates an architecture contract, plans control logic and datapath, generates RTL, reviews microarchitecture quality, runs lint when available, and optionally creates a smoke testbench.

See [AGENT_USAGE.md](AGENT_USAGE.md) for setup and run examples.

## RTL DataPath Visualizer

`rtl_datapath_visualizer.py` can read an `rte`-style filelist (`.f`) and generate:

1. The overall module hierarchy.
2. Likely datapath connections using name-based heuristics.

Example:

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f --top top
```

Outputs:

- `rtl_datapath.dot`: Graphviz DOT source.
- `rtl_datapath.png`: generated automatically when Graphviz `dot` is installed.

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
