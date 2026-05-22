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
- `--max-retries`: Maximum coding retries per Manager task.
- `--no-testbench`: Skip smoke testbench generation and produce RTL only.
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
- `generated_rtl/manager_plan.json`
- `generated_rtl/architecture_contract.md`
- `generated_rtl/logs/architecture_review_attempt_*.md`
- `generated_rtl/logs/*_supervisor_review_attempt_*.md`
- `generated_rtl/logs/*_control_datapath_plan.md`
- `generated_rtl/logs/*_microarchitecture_review_attempt_*.md`
- `generated_rtl/compile_order.f`
- `generated_rtl/file_manifest.json`
- `generated_rtl/run_summary.json`
- `generated_rtl/logs/`
- `generated_rtl/failed_attempts/`

If `verilator` or `iverilog` is installed, syntax lint runs automatically. If neither
tool is available, lint is skipped and the skip is recorded in the logs.
