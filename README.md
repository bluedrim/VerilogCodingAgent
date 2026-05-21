# Verilog Coding Agent

Multi-agent RTL generation workflow for Verilog/SystemVerilog.

The agent breaks a user RTL request into implementation tasks, creates an architecture contract, plans control logic and datapath, generates RTL, reviews microarchitecture quality, runs lint when available, and optionally creates a smoke testbench.

See [AGENT_USAGE.md](AGENT_USAGE.md) for setup and run examples.
