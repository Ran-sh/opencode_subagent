<p align="center">
  <img src="./assets/opencode-subagent-flow.svg" alt="Codex → OpenCode Subagent → OpenCode Go" width="100%">
</p>

<p align="center">
  <a href="./README.md">简体中文</a> · <strong>English</strong>
</p>

<!-- README_SYNC: 2026-08-13.2 -->
<!-- README.md and README.en.md are maintained as a synchronized pair. -->

# opencode_subagent

Maintained by **Ran-sh**.

This project provides a native OpenCode Go subagent for the Codex desktop app. It ships protocol metadata for 19 OpenCode Go models and registers them under one native `OpenCode` role. New installations default to `OpenCode(deepseek-v4-flash_max)`, with `model=deepseek-v4-flash` and `effort=max`. It does not create a second role named `DeepSeek`.

Normal invocation:

```text
spawn_agent(agent_type="OpenCode", fork_context=false, ...)
```

OpenCode configuration uses model names in the `opencode-go/<model-id>` form. The local Codex model registry and Agent Profile keep the plain `<model-id>` without the provider prefix.

## Five-step quick start

1. Install the skill: `npx skills add Ran-sh/opencode_subagent -g -y`.
2. Restart Codex and open a new task.
3. In the new task, say: `Use $codex-opencode-go-subagent to complete the initial setup.`
4. Confirm that the role is `OpenCode` and the default Profile is `model=deepseek-v4-flash, effort=max`.
5. For normal coding work, the primary Agent calls `spawn_agent(agent_type="OpenCode", fork_context=false, ...)`. Do not rerun the setup script.

`spawn_agent(...)` is a Codex Agent interface, not a terminal command. With `fork_context=false`, the subagent does not inherit the main conversation, so every delegated task must be self-contained.

## Features

- One native `OpenCode` role backed by a unified 19-model catalog, defaulting to `model=deepseek-v4-flash, effort=max`;
- Translation between the Codex Responses protocol and OpenCode Go `chat_completions`, `responses`, and `anthropic_messages` transports;
- Multi-turn tool calls and Responses SSE streaming for supported tools;
- Support for Codex developer instructions while filtering hosted web-search declarations that ordinary models cannot consume;
- Atomic model-and-effort switching with rollback of the model catalog, Agent configuration, and management manifest on failure;
- Explicit OpenCode Go model-catalog refreshes; unknown entries and transport conflicts are reported, never silently adopted;
- API keys stored only in Windows Credential Manager or macOS Keychain; the local gateway listens only on `127.0.0.1`;
- Compatibility with legacy managed state containing 18 models: `minimax-m2.5` is added in memory without rewriting user files.

This repository contains only the configuration skill, protocol gateway, tests, and documentation. It does not contain `deepseek-dispatch-governor`, API keys, private logs, or workspace identifiers.

## Architecture

The banner above shows the complete primary path: Codex sees one native `OpenCode` role, the local gateway adapts protocols, and the final request is sent to OpenCode Go.

| Layer | Responsibility | Security boundary |
| --- | --- | --- |
| **Codex** | The primary Agent delegates native `OpenCode` tasks | The primary task's model and authentication remain unchanged |
| **OpenCode Subagent** | Local authentication, Responses SSE, tool round trips, and three transport adapters | Listens only on `127.0.0.1`; reasoning state remains in memory |
| **OpenCode Go** | Provides 19 models and their reasoning-effort options | The API key is read only from the system credential store |

The local gateway handles protocol conversion, tool-call round trips, SSE streaming, and reasoning-handle state. The primary task continues to use the user's existing Codex model and authentication. This skill does not change the top-level `model` or `model_provider`.

This project therefore does not require CC Switch. `opencode-go` serves only the `OpenCode` subagent. If the primary Codex task uses a third-party local proxy as its top-level `model_provider`, only the primary task continues to depend on that proxy. To connect the primary task directly to official Codex, switch its top-level provider back to the built-in `openai` provider while retaining `agents.OpenCode` and `model_providers.opencode-go`.

## Installation

Requirements: Windows or macOS, Python 3.11+, a Node.js/npm installation that provides `npx`, the ChatGPT/Codex desktop app, and a valid OpenCode Go API key.

```bash
npx skills add Ran-sh/opencode_subagent -g -y
```

`-g` installs the skill globally and `-y` accepts the prompt automatically. Restart Codex and open a new task after installation so the skill and `OpenCode` role enter the current tool schema.

## Initial setup

In a new task, say:

```text
Use $codex-opencode-go-subagent to configure OpenCode Go as a native Codex subagent.
```

The skill first performs a read-only status check and asks for an API key only if no credential exists. The key is passed to the manager through standard input and stored in the system credential store. It is never written to configuration files, command arguments, logs, or temporary files.

You can also run the manager directly from a clone of this repository. Global-skill users should prefer the natural-language workflow above. On Windows:

```powershell
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py setup --api-key-stdin --json
```

On macOS:

```bash
python3 codex-opencode-go-subagent/scripts/codex_opencode_go.py setup --api-key-stdin --json
```

After static setup succeeds, the default Profile should be:

```text
model_provider = opencode-go
model = deepseek-v4-flash
reasoning_effort = max
agent_role = OpenCode
```

By default, `setup`, `repair`, and `profile set` run a direct local-gateway probe; `test` always runs it. `--skip-live-test` skips that probe for the first three commands. Probes call real models and may incur cost, latency, or rate limits, especially with `test --all-models`. A direct probe is not a native Codex dispatch acceptance test. Full native acceptance also creates an `OpenCode` subagent, checks task metadata, and verifies the acceptance marker.

## Daily coding delegation

After setup, normal tasks do not need to rerun the skill or manager. The following is an example of the native interface used internally by the primary Agent; it is not a terminal command:

```text
spawn_agent(agent_type="OpenCode", fork_context=false, ...)
```

Do not impersonate the subagent with a `DeepSeek` role, `codex exec`, or the management script. If `OpenCode` is absent from the current tool schema, restart Codex or open a new task.

`OpenCode` is a text-and-tools Agent. The primary Agent must inspect images, audio, video, screenshots, and visual web content first, then pass the required facts as text. See “Protocol and security boundaries” for the complete support matrix.

## Supported models

The frozen catalog currently contains protocol metadata for 19 active models, sourced on 2026-08-13. Catalog inclusion and protocol support do not imply that every account can access every model, or that every model has completed live API validation.

| Transport | Count | Model IDs |
|:---|---:|:---|
| `chat_completions` | 11 | `deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.1`, `glm-5.2`, `grok-4.5`, `hy3`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, `mimo-v2.5`, `mimo-v2.5-pro` |
| `responses` | 1 | `gpt-5.6-luna` |
| `anthropic_messages` | 7 | `minimax-m2.5`, `minimax-m2.7`, `minimax-m3`, `qwen3.6-plus`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.8-max` |

See the [compatibility reference](codex-opencode-go-subagent/references/compatibility.md) for each model's allowed efforts, default effort, and transport mapping. `default` is a pseudo-effort that omits the reasoning-strength field upstream. Effort is only a request parameter; it does not guarantee speed, price, or actual server-side reasoning resources. `profile set --model` uses the model ID without the `opencode-go/` prefix.

## Switching models and reasoning effort

Model and effort must be supplied together. Windows examples:

```powershell
# List every model × effort combination
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile list --json

# Show the active Profile
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile show --json

# Switch atomically and run static checks plus the direct local-gateway probe
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile set --model gpt-5.6-luna --effort high --json

# Make a static-only switch and validate manually later
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py profile set --model qwen3.8-max --effort max --skip-live-test --json
```

On macOS, replace `py -3` with `python3`. Reapplying the active combination does not modify files or create a backup, but it still runs the direct probe by default. After a real switch succeeds, restart Codex and open a new task so the new Agent Profile enters the tool schema.

> **Warning:** Direct probes call the real OpenCode Go API. `test --all-models` requests every catalog model in sequence and may incur cost, latency, rate limits, or model-permission errors.

## Management commands

```text
python3 codex-opencode-go-subagent/scripts/codex_opencode_go.py <command> --json
```

Use `py -3` on Windows. Stable commands include:

| Command | Purpose |
|:---|:---|
| `status` | Read-only checks for configuration, model catalog, credentials, gateway, and managed-file hashes |
| `setup` | Initial configuration followed by a direct probe by default |
| `test [--all-models]` | Test the active Profile or every model with its default effort |
| `repair` | Rebuild managed configuration from the active valid OpenCode Profile and validate it |
| `profile list/show/set` | List, inspect, or atomically switch model and effort |
| `models list/refresh` | List models read-only or explicitly refresh the catalog transactionally |
| `gateway status/start/stop` | Inspect, start, or stop the local gateway |
| `disable` | Disable the role while retaining the provider, catalog, state, and API key |
| `uninstall` | Remove managed configuration; delete the API key only with explicit `--remove-credential` |

See the [operations guide](codex-opencode-go-subagent/references/operations.md) for complete arguments, status codes, transaction order, rollback behavior, and troubleshooting.

## Protocol and security boundaries

| Supported | Unsupported |
|:---|:---|
| Text, shell, apply_patch, ordinary function tools, custom tools, MCP tools | Images, audio, hosted web, image generation, computer use |

- The gateway may bind only to `127.0.0.1`; model requests and shutdown require a local bearer token;
- Request bodies are limited to 32 MiB, concurrency to 8 requests, connection timeout to 30 seconds, and streaming idle timeout to 650 seconds;
- Raw reasoning remains only in gateway memory and is referenced through random handles: 2,048-entry LRU, two-hour TTL, no persistence;
- Logs may contain only model, transport, status, `duration_ms`, and a redacted request ID—not request bodies, response bodies, or credentials;
- The gateway is not registered for system startup or installed as a service. When a token is requested, the auth helper reuses the same startup mechanism to launch the gateway on demand; users can also manage it explicitly with `gateway start|stop`;
- Managed-file hash drift returns `conflict` instead of silently overwriting user changes.

See the [gateway protocol reference](codex-opencode-go-subagent/references/protocol.md) for endpoints, SSE events, tool mappings, and the reasoning lifecycle.

## Verification

Repository automation:

```bash
python -B -m unittest discover -s tests -v
python -B scripts/quick_validate.py codex-opencode-go-subagent --repo-root .
python -B scripts/check_readme_sync.py --repo-root .
```

`README.md` and `README.en.md` share the same `README_SYNC` revision and fixed section, command, and model contracts. CI also compares the change base; changing only one README fails the build.

Interpret evidence by layer:

- Unit tests and static validation prove code contracts and repository structure;
- The manager's direct probe proves that the local gateway can complete text and function-tool round trips;
- The native gate must also use `spawn_agent(agent_type="OpenCode", fork_context=false, ...)` and verify the subtask's provider, model, effort, and `agent_role=OpenCode`;
- Lower-level tests cannot substitute for the real OpenCode Go service, every model, or a real macOS environment.

Verification status:

| Scope | Platform | Evidence | Status |
|:---|:---|:---|:---|
| Repository code and static structure | Windows | 137 unit tests and quick_validate | Passed |
| `model=deepseek-v4-flash, effort=max` | Windows | Native `OpenCode` dispatch through the managed gateway and SQLite metadata | Passed |
| Remaining models and representative Profiles | — | Real OpenCode Go API and native gate | Pending per-model validation |
| macOS | macOS | Real credential-store, gateway, and native-dispatch testing | Pending |

The older release checklist in the reference documentation still marks the native gate as pending. The table above records the actual Windows acceptance completed for this repository on 2026-08-13. Future releases must update the operations guide at the same time to prevent status descriptions from diverging again.

## Data sources

- [OpenCode Go documentation](https://opencode.ai/docs/zh-cn/go/)
- [OpenCode model configuration](https://opencode.ai/docs/config/#models)
- [OpenCode Zen](https://opencode.ai/docs/zh-cn/zen/)
- [models.dev](https://models.dev/)
- [Reference project: Ran-sh/codex_subagent_deepseek](https://github.com/Ran-sh/codex_subagent_deepseek)

## Credits, trademarks, and license

This repository uses [Ran-sh/codex_subagent_deepseek@84458a2](https://github.com/Ran-sh/codex_subagent_deepseek/tree/84458a2ca16522e8685e6ba18468c6608cd8f1d1) as its legacy reference baseline and preserves attribution to oil-oil and Ran-sh. See the [third-party provenance and attribution notice](NOTICE.md) for inherited scope and the new implementation in this repository.

Third-party names and marks are used only to describe compatibility. Their respective owners retain all rights. This project is not an official project of, or endorsed by, OpenAI, Anomaly, DeepSeek, or any other model provider.

This project is licensed under the [MIT License](LICENSE). Any use, modification, or redistribution must preserve the copyright and permission notices required by that license.
