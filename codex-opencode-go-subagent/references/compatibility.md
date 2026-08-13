# 兼容性与安全边界

## 支持范围

- macOS、Windows、Python 3.11+；ChatGPT/Codex 桌面应用至少启动过一次。
- 原生子 Agent 角色 `OpenCode`、Provider `opencode-go`、Codex `wire_api=responses`。
- 新安装默认 `deepseek-v4-flash + max`。
- 无第三方运行时依赖。

## 配置位置与凭据

- 默认 `CODEX_HOME` 为 `~/.codex`。本 Skill 只使用 `$CODEX_HOME` 相对路径或技能目录相对路径，不写死本机绝对路径；受管文件与备份位置以 `status`/`setup` 的 `--json` 输出为准。
- API Key 只通过 `--api-key-stdin` 的标准输入传递，保存到 Windows Credential Manager 或 macOS Keychain。
- 凭据目标名称以脚本 `--json` 输出为准；程序不打印、不回显、不写入临时文件、命令行或日志。
- 程序不修改顶层 `model` 或顶层 `model_provider`，主任务仍使用用户原来的模型与登录方式。

## 模型矩阵（19 个活跃模型，来源日期 2026-08-13）

transport 固定为 `chat_completions`、`responses`、`anthropic_messages`。`effort` 一列是允许档位；`default` 是伪档位（见下）。不列出别名、旧模型或已弃用模型。

OpenCode 配置使用 `opencode-go/<model-id>`；本地注册表保留不带 provider 前缀的 `<model-id>`。

### chat_completions（11）

| 模型 ID | 显示名 | 允许 effort | 默认 |
|---|---|---|---|
| deepseek-v4-flash | DeepSeek V4 Flash | low, high, max | max |
| deepseek-v4-pro | DeepSeek V4 Pro | high, max | max |
| glm-5.1 | GLM-5.1 | default | default |
| glm-5.2 | GLM-5.2 | high, max | max |
| grok-4.5 | Grok 4.5 | low, medium, high | high |
| hy3 | Hy3 | none, low, high | high |
| kimi-k2.6 | Kimi K2.6 | default | default |
| kimi-k2.7-code | Kimi K2.7 Code | default | default |
| kimi-k3 | Kimi K3 | max | max |
| mimo-v2.5 | MiMo V2.5 | default | default |
| mimo-v2.5-pro | MiMo V2.5 Pro | default | default |

### responses（1）

| 模型 ID | 显示名 | 允许 effort | 默认 |
|---|---|---|---|
| gpt-5.6-luna | GPT 5.6 Luna | none, low, medium, high, xhigh, max | max |

### anthropic_messages（7）

| 模型 ID | 显示名 | 允许 effort | 默认 |
|---|---|---|---|
| minimax-m2.5 | MiniMax M2.5 | default | default |
| minimax-m2.7 | MiniMax M2.7 | default | default |
| minimax-m3 | MiniMax M3 | none, high | high |
| qwen3.6-plus | Qwen3.6 Plus | none, high, max | max |
| qwen3.7-max | Qwen3.7 Max | none, high, max | max |
| qwen3.7-plus | Qwen3.7 Plus | none, high, max | max |
| qwen3.8-max | Qwen3.8 Max | none, high, max | max |

## Effort 规则与 wire 映射

- effort 档位只是提交给模型提供方的请求参数，不承诺具体速度、价格或服务端推理资源。
- `default` 是伪档位：manifest 与上游请求都不发送强度；Codex 模型目录兼容显示为 `none`。
- `chat_completions`：非 default 档位发送 `reasoning_effort`。
- `responses`：发送 `reasoning.effort`。
- `anthropic_messages`：
  - MiniMax M3：`none` → disabled，`high` → enabled。
  - Qwen 系列：`none` → disabled，`high` → budget_tokens=16000，`max` → budget_tokens=31999。
  - Messages 请求统一 `max_tokens=32000`。
- 未列出的档位应被拒绝，不得自动降级到近似档位。

## 模型目录 refresh

- 只按显式 `models refresh` 执行；`setup`/`repair` 不刷新模型目录。
- 顺序：先读取 Go `/models`，再读取 models.dev 推理选项。
- 未知模型、协议错误或与现有数据冲突：只报告，不采用。
- 未选中的模型缺失 → 标记 `unavailable`；选中的模型消失 → 整次 refresh 在写入前中止，旧配置保持不变。
- refresh 采用事务：备份、写入、验证，失败恢复；结果以 `--json` 为准。

## 能力范围

支持：text、shell、apply_patch、function、MCP。

拒绝：image、audio、hosted web、imagegen、computer-use。

父 Agent 必须先完成视觉与网页内容识别，再以文字事实派发；子 Agent 不得声称查看过图片、视频或截图。

## 原生验收与 direct 探针

- manager 的 `test`/`setup`/`repair`/`profile set` 只做本地网关 direct 探针：文本 marker 与 function-tool marker。
- 真实原生派发验收由父 Agent 单独执行：`spawn_agent(agent_type="OpenCode", fork_context=false, ...)`，检查 SQLite 子线程元数据（Provider、模型、effort、agent_role=OpenCode），并确认精确口令 `NATIVE_OPENCODE_OK`。
- 两者缺一不可；manager 的 direct 探针不等于原生验收。分层发布门见 operations.md。

## 数据来源与实现引用（2026-08-13）

- https://opencode.ai/docs/zh-cn/go/
- https://opencode.ai/docs/config/#models
- https://opencode.ai/docs/zh-cn/zen/
- https://opencode.ai/zen/go/v1/models
- https://models.dev/api.json
- https://github.com/anomalyco/opencode/tree/3a90639cb57619a21e59f544b3e8d23ffed56f48
- https://github.com/Ran-sh/codex_subagent_deepseek/tree/84458a2ca16522e8685e6ba18468c6608cd8f1d1
- https://github.com/openai/codex
