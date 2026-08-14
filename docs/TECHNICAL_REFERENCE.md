# OpenCode Go Subagent 技术参考

本文档是 `opencode_subagent` 的完整技术参考。GitHub 首页只保留安装和使用入口；协议、状态、模型和验收细节集中维护在这里。

## 1. 目标与边界

项目把 Codex 的原生 Responses 子 Agent 接到 OpenCode Go API。由于 Codex 自定义 provider 使用 Responses wire，而 OpenCode Go 同时提供 Chat Completions、Responses 和 Anthropic Messages，项目使用只监听 `127.0.0.1` 的本地兼容网关完成协议转换。

默认角色固定为 `OpenCode`，默认模型为 `deepseek-v4-flash`，默认 effort 为 `max`。项目不创建旧的 `DeepSeek` 角色，不包含 `deepseek-dispatch-governor`，也不修改主任务的顶层模型和登录方式。

## 2. 请求链路

```text
Codex 主 Agent
    └─ spawn_agent(agent_type="OpenCode", fork_context=false, ...)
       └─ OpenCode 子 Agent
          └─ Codex Responses + 本地 Bearer
             └─ 127.0.0.1 兼容网关
                ├─ chat_completions
                ├─ responses
                └─ anthropic_messages
                   └─ OpenCode Go API
```

网关负责本地鉴权、请求转换、流式 SSE、工具调用往返和 reasoning 句柄管理。系统凭据库中的 OpenCode Go API Key 只在网关内存中使用。

## 3. 模型目录

首版冻结 19 个活跃模型。目录来源和能力状态可以通过 `models refresh` 显式刷新，但 setup/repair 不会自动刷新。

| Transport | 数量 | 模型 |
| --- | ---: | --- |
| `chat_completions` | 11 | `deepseek-v4-flash`、`deepseek-v4-pro`、`glm-5.1`、`glm-5.2`、`grok-4.5`、`hy3`、`kimi-k2.6`、`kimi-k2.7-code`、`kimi-k3`、`mimo-v2.5`、`mimo-v2.5-pro` |
| `responses` | 1 | `gpt-5.6-luna` |
| `anthropic_messages` | 7 | `minimax-m2.5`、`minimax-m2.7`、`minimax-m3`、`qwen3.6-plus`、`qwen3.7-max`、`qwen3.7-plus`、`qwen3.8-max` |

模型与 effort 是一个 Profile。非法组合必须零写入；有效切换采用“备份 → 写入 → 静态验证 → live probe → 失败回滚”。

### Effort 转换

- Chat Completions 使用 `reasoning_effort`；
- Responses 使用 `reasoning.effort`；
- MiniMax M3 使用 thinking 开关；
- Qwen `none` 关闭 thinking，`high/max` 分别使用 `budget_tokens=16000/31999`，并设置 `max_tokens=32000`；
- `default` 是不发送强度字段的伪档位。

## 4. 管理器与文件

稳定命令：

```text
status --json
setup --api-key-stdin [--migrate-deepseek] [--skip-live-test]
test [--all-models]
repair [--skip-live-test]
profile list|show
profile set --model <id> --effort <value> [--skip-live-test]
models list [--json]
models refresh [--json]
gateway status|start|stop
disable
uninstall [--remove-credential]
```

Codex home 中的受管文件包括：

- `config.toml`：`opencode-go` provider 和 `OpenCode` role；
- `agents/OpenCode.toml`：Agent 描述和 Profile；
- `models-opencode-go.json`：模型目录；
- `opencode-go-subagent/manifest.json`：受管文件哈希和当前 Profile；
- `opencode-go-subagent/state.json`：网关状态和模型快照；
- `opencode-go-subagent/backups/`：事务备份。

写入使用临时文件、原子替换、内容哈希和回滚。`config.toml` 使用受管配置投影哈希：只要 provider、`OpenCode` role、`model_catalog_json` 或 `features.multi_agent` 没有变化，Codex 对其他配置的修改不会触发冲突；这些受管字段被改动时仍返回 `conflict`。模型目录、Agent 文件和状态文件继续使用完整字节哈希，不会静默覆盖用户修改。

## 5. 凭据与本地网关

API Key 只允许通过 stdin 输入：

```text
py -3 codex-opencode-go-subagent/scripts/codex_opencode_go.py setup --api-key-stdin --json
```

Windows 使用 Credential Manager，macOS 使用 Keychain。配置、命令参数、日志和状态文件不保存 API Key。

网关默认安全边界：

| 项目 | 值 |
| --- | --- |
| 监听地址 | `127.0.0.1` |
| 请求体上限 | 32 MiB |
| 并发上限 | 8 |
| 建连超时 | 30 秒 |
| 流式空闲超时 | 650 秒 |
| 空闲退出 | 不自动退出 |
| 系统启动 | 不注册 |

网关提供 `/health`、鉴权的 `/v1/responses` 和 `/shutdown`。日志只记录模型、transport、状态、耗时和脱敏 request ID。

## 6. Responses 与工具转换

网关接受 Codex 的完整 Responses 历史、instructions、普通 function/MCP 工具、shell 工具和 custom 工具。

- 普通 JSON Schema 工具保持 function call；
- custom 工具包装为同名 function，参数固定为必填字符串 `{"input":"..."}`；
- 上游流转换为 Responses SSE，包括创建、输出项、文本增量、reasoning、工具参数增量与完成、usage 和 completed/failed；
- 并行工具调用、多回合工具结果、429、Retry-After、请求 ID和流中断都必须保持可观察；
- 图片、音频、hosted web search、图像生成和 computer-use 不在首版承诺范围。

## 7. Reasoning 状态

DeepSeek `reasoning_content` 和 Anthropic thinking/signature 只保存在网关内存。Responses 的 `encrypted_content` 使用：

```text
ocg1:<256-bit 随机句柄>
```

状态表为 2048 项 LRU，TTL 为 2 小时，不落盘、不写日志。网关重启或句柄过期时返回明确的 `reasoning_state_expired`，不伪造推理内容。

## 8. 旧配置迁移

检测到旧 `codex-deepseek-subagent` 托管配置时，未提供 `--migrate-deepseek` 会返回 migration required 且零写入。

显式迁移只移除旧工具拥有的 `DeepSeek` role、provider 和目录选择，保留旧技能、备份和 DeepSeek API 凭据。迁移不会删除用户的无关 provider、模型目录或 Agent。

## 9. 验证层级

本地验证命令：

```bash
python -B -m unittest discover -s tests -v
python -B scripts/quick_validate.py codex-opencode-go-subagent --repo-root .
python -B scripts/check_readme_sync.py --repo-root .
```

证据层级必须区分：

1. `py_compile` 证明 Python 文件可编译；
2. 单元测试证明模块行为契约；
3. manager direct probe 证明本地网关可以完成模型文本和工具往返；
4. 原生 gate 证明 `OpenCode` role、provider、model 和 effort 已被 Codex 实际调用；
5. 真实 OpenCode Go API 逐模型测试证明账户权限和服务端行为。

当前默认 Profile 的原生验收标记为：

```text
NATIVE_OPENCODE_OK
```

## 10. 参考来源

- [OpenCode Go 文档](https://opencode.ai/docs/zh-cn/go/)
- [OpenCode 配置与模型](https://opencode.ai/docs/config/#models)
- [OpenCode Zen](https://opencode.ai/docs/zh-cn/zen/)
- [models.dev](https://models.dev/)
- [参考项目 Ran-sh/codex_subagent_deepseek](https://github.com/Ran-sh/codex_subagent_deepseek)
- [本项目协议说明](../codex-opencode-go-subagent/references/protocol.md)
- [本项目操作手册](../codex-opencode-go-subagent/references/operations.md)

## 11. 维护约定

README 是用户入口，本文档是技术细节的唯一集中参考。新增模型、命令、协议、安全边界或验收状态时：

1. 先更新本文档；
2. 再更新中英文 README 的简短入口说明；
3. 同时递增两份 README 的 `README_SYNC` 修订号；
4. 运行同步检查、完整测试和 CI。
