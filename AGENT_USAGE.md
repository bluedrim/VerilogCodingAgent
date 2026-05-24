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
- `--max-context-chars`: Maximum RTL/context characters sent to each LLM prompt.
- `--artifact-dir`: Directory for generated artifacts and logs.
- `--llm-provider`: `ollama` or `gpt-oss`.
- `--llm-model`: Model name such as `gpt-oss`, `gpt-oss:20b`, `gpt-oss:120b`, or another Ollama model.
- `--llm-temperature`: Model temperature.
- `--llm-api-url`: OpenAI-compatible chat completions URL.
- `--llm-api-key`: API key for the remote endpoint. The saved config redacts it.

## Outputs

Generated files are written under `generated_rtl/`.

Important artifacts:

- `generated_rtl/user_requirement.txt`
- `generated_rtl/llm_config.json`
- `generated_rtl/execution_config.json`
- `generated_rtl/manager_plan.json`
- `generated_rtl/architecture_contract.md`
- `generated_rtl/logs/architecture_review_attempt_*.md`
- `generated_rtl/logs/*_supervisor_review_attempt_*.md`
- `generated_rtl/logs/*_control_datapath_plan.md`
- `generated_rtl/logs/*_control_datapath_review_attempt_*.md`
- `generated_rtl/logs/*_microarchitecture_review_attempt_*.md`
- `generated_rtl/compile_order.f`
- `generated_rtl/file_manifest.json`
- `generated_rtl/run_summary.json`
- `generated_rtl/logs/`
- `generated_rtl/failed_attempts/`

If `verilator` or `iverilog` is installed, syntax lint runs automatically. If neither
tool is available, lint is skipped and the skip is recorded in the logs. Use
`--require-lint` when lint tool availability should be a blocking quality gate.

`generated_rtl/run_summary.json` includes `run_status`, `failed_stage`,
`blocking_report`, artifact directory, lint policy, blackbox policy, generated
file size limit, prompt context limit, retry limits, final lint status, and
stage-specific retry counts.
