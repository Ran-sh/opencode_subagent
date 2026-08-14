<p align="center">
  <img src="./assets/opencode-subagent-flow.svg" alt="Codex → OpenCode Subagent → OpenCode Go" width="100%">
</p>

<p align="center">
  <a href="./README.md">简体中文</a> · <strong>English</strong>
</p>

<!-- README_SYNC: 2026-08-14.1 -->
<!-- README.md and README.en.md are maintained as a synchronized pair. -->

# opencode_subagent

## Overview

A native `OpenCode` subagent that connects Codex to the OpenCode Go API. The project provides 19 models, model-and-reasoning-effort switching, and adapters for Chat Completions, Responses, and Anthropic Messages transports.

The default configuration is:

```text
model=deepseek-v4-flash, effort=max
```

## Key features

- Native invocation: `spawn_agent(agent_type="OpenCode", fork_context=false, ...)`
- Supports `chat_completions`, `responses`, and `anthropic_messages`
- The local compatibility gateway listens only on `127.0.0.1`
- API keys are stored only in the system credential store, never in README files, configuration, logs, or command arguments
- Atomic model/effort switching, rollback on failure, and explicit model-catalog refresh

## Quick start

Requirements: Windows or macOS, Python 3.11+, Node.js/npm, the Codex desktop app, and an OpenCode Go API key.

Install the skill:

```bash
npx skills add Ran-sh/opencode_subagent -g -y
```

Restart Codex, open a new task, and say:

```text
Use $codex-opencode-go-subagent to complete the initial setup.
```

You can also run setup directly:

```powershell
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py setup --api-key-stdin --json
```

On macOS, replace `py -3` with `python3`.

## Default configuration

After setup, the profile should be:

```text
model_provider = opencode-go
# selected profile: model=deepseek-v4-flash, effort=max
model = deepseek-v4-flash
reasoning_effort = max
agent_role = OpenCode
```

The API key is read from stdin and stored in Windows Credential Manager or macOS Keychain. Restart Codex and open a new task after setup.

## Switch models

Model and effort must be supplied together:

```powershell
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile list --json
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile set --model gpt-5.6-luna --effort high --json
```

Static switch without a live probe:

```powershell
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile set --model qwen3.8-max --effort max --skip-live-test --json
```

## Native subagent call

After setup, the primary Agent uses:

```text
spawn_agent(agent_type="OpenCode", fork_context=false, ...)
```

`OpenCode` is a text-and-tools Agent. The primary Agent should inspect images, audio, video, and screenshots first, then pass the required facts as text. Do not impersonate the native subagent with the old `DeepSeek` role, `codex exec`, or the management script.

## Technical reference

For the complete model catalog, protocol conversion, tool calls, reasoning state, security boundaries, management commands, migration/rollback, and acceptance procedures, see:

- [Technical reference](docs/TECHNICAL_REFERENCE.md)
- [Compatibility reference](codex-opencode-go-subagent/references/compatibility.md)
- [Operations guide](codex-opencode-go-subagent/references/operations.md)
- [Gateway protocol](codex-opencode-go-subagent/references/protocol.md)

The Chinese and English READMEs share the same `README_SYNC` revision. CI checks their sections, key commands, model contracts, and paired changes.

## License

This project is licensed under the [MIT License](LICENSE). Legacy references, third-party sources, and attribution are documented in [NOTICE.md](NOTICE.md).

This project is not an official project of, or endorsed by, OpenAI, OpenCode, DeepSeek, or any other model provider.
