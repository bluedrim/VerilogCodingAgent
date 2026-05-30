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

## OpenAI account run

Use `openai` when you want the agent to call models through your OpenAI account API key.
The Codex app login token is not read by this script; provide an API key through
`OPENAI_API_KEY`, `.env`, or `--llm-api-key`.

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
- `--auto-approve`: Skip the final manual approval prompt.
- `--max-retries`: Maximum retries per coding, microarchitecture review, and verification stage.
- `--max-architecture-retries`: Maximum architecture review retries.
- `--max-supervisor-retries`: Maximum Supervisor review retries.
- `--max-control-datapath-retries`: Maximum Control/Data Path plan review retries.
- `--max-testbench-retries`: Maximum smoke testbench generation retries.
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
- `--llm-temperature`: Model temperature.
- `--llm-api-url`: OpenAI-compatible chat completions URL.
- `--llm-api-key`: API key for OpenAI or the remote endpoint. The saved config redacts it.

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
- `prompts/verilog_coding_repair.md`
- `prompts/microarchitecture_review.md`
- `prompts/verification.md`
- `prompts/testbench.md`

## Outputs

Generated files are written under the artifact directory. By default this is
`output_<project_keyword>_<YYYYMMDD>_<HHMMSS>`; use `--artifact-dir` to choose
a fixed path such as `generated_rtl`.

Important artifacts:

- `<artifact-dir>/user_requirement.txt`
- `<artifact-dir>/llm_config.json`
- `<artifact-dir>/execution_config.json`
- `<artifact-dir>/manager_plan.json`
- `<artifact-dir>/architecture_contract.md`
- `<artifact-dir>/logs/architecture_review_attempt_*.md`
- `<artifact-dir>/logs/*_supervisor_review_attempt_*.md`
- `<artifact-dir>/logs/*_control_datapath_plan.md`
- `<artifact-dir>/logs/*_control_datapath_review_attempt_*.md`
- `<artifact-dir>/logs/*_coding_attempt_*.json`
- `<artifact-dir>/logs/*_microarchitecture_review_attempt_*.md`
- `<artifact-dir>/compile_order.f`
- `<artifact-dir>/file_manifest.json`
- `<artifact-dir>/run_summary.json`
- `<artifact-dir>/logs/`
- `<artifact-dir>/failed_attempts/`

If `verilator` or `iverilog` is installed, syntax lint runs automatically. If neither
tool is available, lint is skipped and the skip is recorded in the logs. Use
`--require-lint` when lint tool availability should be a blocking quality gate.

`<artifact-dir>/run_summary.json` includes `run_id`, `run_status`, `failed_stage`,
`blocking_report`, artifact directory, lint policy, blackbox policy, generated
file count and size limits, prompt context limit, user request limit, Manager
task limit, Manager fallback status, task progress, retry limits, final lint
status, and stage-specific retry counts.
