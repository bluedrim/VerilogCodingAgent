# Verilog Coding Agent Usage

## Interactive run

```bash
python3 main.py
```

The agent asks for an RTL requirement. You can type the requirement directly or pass a
requirement file path such as `@spec.txt`.

## Non-interactive run

```bash
python3 main.py --spec spec.txt --auto-approve --max-retries 3
```

## Live dashboard

Start the dashboard in a second terminal while the agent is running:

```bash
python3 dashboard.py
```

Then open:

```text
http://localhost:8766
```

The dashboard watches `output_*` artifact directories and shows:

- Current run status and latest heartbeat.
- Stage pass/fail/force-forward state.
- Retry counts and retry limits.
- Latest reports from architecture, Supervisor, Control/Data Path, coding, microarchitecture, verification, and final lint.
- Live `main.py` console output, including `print()` messages and tracebacks.
- Recent files under `logs/`.
- Files under `failed_attempts/`.
- Quick preview for any artifact file.

You can also start a new run directly from the dashboard:

- Type the RTL requirement into the text area.
- Or choose a `.md`, `.markdown`, or `.txt` file; the browser loads its content
  into the requirement box before starting.
- Select only the LLM provider in the dashboard.
- Click `Start new task`.

To continue an interrupted or failed run, select its output directory and click
`Continue task`. The button is enabled only when a saved checkpoint has pending work
and no process is already running for that directory. The agent restores the
Manager plan, current task, review feedback, RTL candidates, accepted files, and
retry counters, then enters the stage following the last completed node.

Select any running or stale project and click `Stop task` to terminate its
dashboard-launched `main.py` process when present and persist the project status
as `stopped`. The checkpoint is preserved, so a stopped project can later use
`Continue task`. Runs whose process has already exited are automatically shown
as stopped instead of remaining incorrectly marked as running.

The dashboard saves the submitted Markdown/text as `dashboard_requirement.md`
inside the new `output_*` artifact directory and starts `main.py` in the
background with final write approval enabled so the non-interactive run does
not stop at the terminal approval prompt. Retry limits, lint policy, testbench
policy, model names, API URLs, and API keys are read from `.env` or environment
variables, not from visible dashboard fields.

Dashboard `.env` controls:

```bash
DASHBOARD_AUTO_APPROVE=true
DASHBOARD_NO_TESTBENCH=false
DASHBOARD_REQUIRE_LINT=false
MAX_RETRIES=3
# DASHBOARD_MAX_MANAGER_RETRIES=3
# DASHBOARD_MAX_RETRIES=3
# DASHBOARD_MAX_ARCHITECTURE_RETRIES=3
# DASHBOARD_MAX_SUPERVISOR_RETRIES=3
# DASHBOARD_MAX_CONTROL_DATAPATH_RETRIES=3
# DASHBOARD_MAX_TESTBENCH_RETRIES=3
DASHBOARD_MAX_MANAGER_TASKS=32
```

Use a different port or artifact root when needed:

```bash
python3 dashboard.py --port 8766 --root .
```

## OpenAI account run

Use `openai` when you want the agent to call models through your OpenAI account API key.
Provide an API key through `OPENAI_API_KEY`, `.env`, or `--llm-api-key`.

```bash
python3 main.py \
  --llm-provider openai \
  --llm-model gpt-4.1 \
  --llm-api-key YOUR_OPENAI_API_KEY \
  --spec spec.txt \
  --auto-approve
```

`.env` example:

```bash
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-4.1
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=8192
```

## gpt-oss run

The `gpt-oss` option can use Ollama locally or an OpenAI-compatible
`/chat/completions` endpoint.

```bash
ollama pull gpt-oss:20b
python3 main.py --llm-provider gpt-oss --llm-model gpt-oss:20b --spec spec.txt --auto-approve
```

Remote OpenAI-compatible endpoint:

```bash
python3 main.py \
  --llm-provider gpt-oss \
  --llm-model gpt-oss \
  --llm-api-url http://abc.net:30001/chat/completions \
  --llm-api-key YOUR_API_KEY \
  --spec spec.txt \
  --auto-approve
```

You can also configure it through `.env`:

```bash
LLM_PROVIDER=gpt-oss
GPT_OSS_MODEL=gpt-oss:20b
LLM_TEMPERATURE=0.1
```

For the remote endpoint:

```bash
LLM_PROVIDER=gpt-oss
GPT_OSS_MODEL=gpt-oss
GPT_OSS_API_URL=http://abc.net:30001/chat/completions
GPT_OSS_API_KEY=YOUR_API_KEY
LLM_TEMPERATURE=0.1
```

Options:

- `--spec`: RTL requirement text or a path to a requirement file.
- `--continue`: Continue from `<artifact-dir>/run_state_checkpoint.json` instead of starting a new run.
- `--auto-approve`: Skip the final manual approval prompt.
- `--max-retries`: Maximum retries per coding, microarchitecture review, and verification stage. Defaults to `MAX_RETRIES` or `3`.
- `--max-manager-retries`: Maximum Manager semantic review/repair attempts. Override with `MAX_MANAGER_RETRIES`.
- `--max-architecture-retries`: Maximum architecture review retries. Override with `MAX_ARCHITECTURE_RETRIES`.
- `--max-supervisor-retries`: Maximum Supervisor review retries. Override with `MAX_SUPERVISOR_RETRIES`.
- `--max-control-datapath-retries`: Maximum Control/Data Path plan review retries. Override with `MAX_CONTROL_DATAPATH_RETRIES`.
- `--max-testbench-retries`: Maximum smoke testbench generation retries. Override with `MAX_TESTBENCH_RETRIES`.
- `--no-testbench`: Skip smoke testbench generation and produce RTL only.
- `--require-lint`: Fail the run if neither `verilator` nor `iverilog` is installed.
- `--lint-timeout`: Syntax lint timeout in seconds.
- `--allow-blackboxes`: Allow unresolved module instantiations in static sanity checks.
- `--max-generated-file-bytes`: Maximum allowed bytes per generated RTL/testbench file.
- `--max-generated-files`: Maximum number of files accepted from RTL/testbench generation.
- `--max-context-chars`: Maximum RTL/context characters sent to each LLM prompt.
- `--max-user-request-chars`: Maximum accepted characters for the user requirement.
- `--max-manager-tasks`: Maximum number of Manager tasks accepted from planning.
- `--fail-on-manager-fallback`: Fail instead of using the single-task fallback when Manager planning output is invalid.
- `--artifact-dir`: Directory for generated artifacts and logs.
- `--llm-provider`: `ollama`, `gpt-oss`, or `openai`.
- `--llm-model`: Model name such as `gpt-4.1`, `gpt-oss`, `gpt-oss:20b`, `gpt-oss:120b`, or another Ollama model.
- `--llm-max-tokens`: Maximum output tokens per response. Override with `LLM_MAX_TOKENS`; the default is `8192`.
- `--run-simulation`: Execute the generated self-checking testbench with `iverilog/vvp`. Disabled by default; enable with `RUN_SIMULATION=true` or `DASHBOARD_RUN_SIMULATION=true` for dashboard runs.
- `--llm-temperature`: Model temperature.
- `--llm-api-url`: OpenAI-compatible chat completions URL.
- `--llm-api-key`: API key for OpenAI or the remote endpoint. The saved config redacts it.

LLM provider definitions, CLI argument registration, environment-variable
resolution, URL normalization, API-key redaction, and backend construction are
centralized in `verilog_agent/llm_config.py`. This keeps provider changes out of
the pipeline orchestration in `main.py`.

## Agent prompts

Team system prompts are stored as Markdown files under `prompts/`.
Edit these files to tune each agent without changing Python code.

Current prompt files:

- `prompts/manager.md`
- `prompts/manager_json_repair.md`
- `prompts/architecture.md`
- `prompts/architecture_review.md`
- `prompts/supervisor.md`
- `prompts/supervisor_review.md`
- `prompts/control_datapath_planner.md`
- `prompts/control_datapath_review.md`
- `prompts/verilog_coding.md`
- `prompts/verilog_coding_action_plan.md`
- `prompts/verilog_implementation_repair.md`
- `prompts/verilog_coding_repair.md`
- `prompts/verilog_review_gate_repair.md`
- `prompts/verilog_coding_closure_review.md`
- `prompts/verilog_coding_quality_review.md`
- `prompts/microarchitecture_review.md`
- `prompts/verification.md`
- `prompts/testbench.md`

## Outputs

Generated files are written under the artifact directory. By default this is
`output_<project_keyword>_<YYYYMMDD>_<HHMMSS>`; use `--artifact-dir` to choose
a fixed path such as `generated_rtl`.

Continue a saved run from the command line:

```bash
python3 main.py --continue --artifact-dir output_my_project_20260713_120000 --auto-approve
```

Every pipeline node writes an atomic checkpoint before it starts and after it
finishes. If a process stops during a node, continuation retries that node. If
the node completed, continuation starts at its graph successor. Completed runs
with no pending or failed stage are intentionally not restartable.

Important artifacts:

- `<artifact-dir>/user_requirement.txt`
- `<artifact-dir>/llm_config.json`
- `<artifact-dir>/execution_config.json`
- `<artifact-dir>/dashboard_heartbeat.json`
- `<artifact-dir>/run_state_checkpoint.json`
- `<artifact-dir>/manager_plan.json`
- `<artifact-dir>/architecture_contract.md`
- `<artifact-dir>/logs/agent_messages/*_attempt_*.md`
- `<artifact-dir>/logs/architecture_review_attempt_*.md`
- `<artifact-dir>/logs/*_supervisor_review_attempt_*.md`
- `<artifact-dir>/logs/*_control_datapath_plan.md`
- `<artifact-dir>/logs/*_control_datapath_review_attempt_*.md`
- `<artifact-dir>/logs/*_coding_attempt_*.json`
- `<artifact-dir>/logs/*_coding_closure_audit_*_attempt_*.md`
- `<artifact-dir>/logs/*_coding_quality_audit_*_attempt_*.md`
- `<artifact-dir>/logs/*_microarchitecture_review_attempt_*.md`
- `<artifact-dir>/compile_order.f`
- `<artifact-dir>/file_manifest.json`
- `<artifact-dir>/run_summary.json`
- `<artifact-dir>/logs/`
- `<artifact-dir>/failed_attempts/`

Agent message snapshots under `logs/agent_messages/` show the rendered
system/human input sent to each agent, payload sizes, and links to externalized
RTL/code payloads. This keeps prompt text reviewable without burying it inside
large code bodies. Set `AGENT_MESSAGE_LOG_MAX_CHARS` to cap the rendered
message preview length; code payload artifacts are still written separately.

On review-driven coding retries, the Coding Closure Auditor checks whether the
current RTL visibly closes the active repair backlog. A failed closure audit is
fed into the focused review-gate repair pass, and the repaired RTL is audited
once more before it can proceed to Microarchitecture Review.

Every Coding Team attempt also builds a cycle-accurate RTL action plan and runs
the generated files through a Coding Quality Auditor. The auditor blocks only
objective functional, timing, reset, protocol, width, synthesizability, or plan
coverage defects. Its actionable report is sent to the focused repair pass, and
the repaired RTL is audited again before leaving the Coding Team.

If `verilator` or `iverilog` is installed, syntax lint runs automatically. If neither
tool is available, lint is skipped and the skip is recorded in the logs. Use
`--require-lint` when lint tool availability should be a blocking quality gate.

`<artifact-dir>/run_summary.json` includes `run_id`, `run_status`, `failed_stage`,
`blocking_report`, artifact directory, lint policy, blackbox policy, generated
file count and size limits, prompt context limit, user request limit, Manager
task limit, Manager fallback status, task progress, retry limits, final lint
status, and stage-specific retry counts.

## VCS/FSDB failure debug helper

For post-simulation debug with VCS and FSDB, use the standalone helper:

```bash
python3 vcs_failure_explainer.py \
  --log ./simv.log \
  --fsdb ./dump.fsdb \
  --filelist ./filelist.f \
  --rtl-dir ./rtl \
  --out-dir ./sim_debug_report
```

When you also have a VCD export for the failure window, add `--vcd` to enable
portable waveform activity ranking:

```bash
python3 vcs_failure_explainer.py \
  --log ./simv.log \
  --fsdb ./dump.fsdb \
  --vcd ./failure_window.vcd \
  --filelist ./filelist.f \
  --rtl-dir ./rtl
```

When saved DUT output and reference vectors are available, add them to identify
the first output mismatch before waveform/source ranking:

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

The helper produces `debug_report.md`, `debug_report.html`, `debug_summary.json`,
`signal_list.txt`, `verdi_open.tcl`, and, when `--fsdb` is supplied, `open_verdi.sh`.
