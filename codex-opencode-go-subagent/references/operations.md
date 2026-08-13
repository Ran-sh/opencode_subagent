# 操作手册（OpenCode Go 子 Agent）

## 环境与入口

- macOS 使用 `python3`，Windows 使用 `py -3`。
- 脚本：`<skill-dir>/scripts/codex_opencode_go.py`（本技能目录内即 `codex-opencode-go-subagent/scripts/` 相对路径）。
- 示例一律加 `--json`；默认使用当前 `CODEX_HOME`，仅显式指定其他 Codex Home 时传 `--codex-home`。

## 命令清单

| 命令 | 选项 | 行为 | 只读 |
|---|---|---|---|
| status | --json | 检查运行时、配置、模型目录、凭据与客户端能力 | 是 |
| setup | --api-key-stdin [--migrate-deepseek] [--skip-live-test] | 写入配置并做 direct 探针 | 否 |
| test | [--all-models] | 本地网关 direct 探针；--all-models 按每个模型的 default_effort 覆盖全部 19 模型 | 否 |
| repair | [--api-key-stdin] [--migrate-deepseek] [--skip-live-test] | 按当前父模型重应用配置并做 direct 探针 | 否 |
| profile list | --json | 模型×effort 组合与默认 Profile | 是 |
| profile show | --json | 当前 Profile、配置来源与迁移需求 | 是 |
| profile set | --model <模型> --effort <档位> [--skip-live-test] | 原子切换 Profile | 否 |
| models list | --json | 19 模型 transport 与 effort | 是 |
| models refresh | --json | 显式刷新模型目录（事务） | 否 |
| gateway status | --json | 查询本地网关状态 | 是 |
| gateway start | --json | 启动本地网关 | 否 |
| gateway stop | --json | 停止本地网关 | 否 |
| disable | --json | 停用角色，保留 provider/catalog/state/API key | 否 |
| uninstall | [--remove-credential] | 移除受管配置 | 否 |

示例：

- `py -3 <skill-dir>/scripts/codex_opencode_go.py status --json`
- `py -3 <skill-dir>/scripts/codex_opencode_go.py profile set --model deepseek-v4-flash --effort max --json`
- `python3 <skill-dir>/scripts/codex_opencode_go.py models refresh --json`

## 凭据

- Windows Credential Manager、macOS Keychain；只通过 `--api-key-stdin` 标准输入传递。
- 缺少凭据返回 `credential_missing`，索要后继续原流程。
- 不打印、不回显、不写临时文件/命令行/日志；凭据目标名以 `--json` 输出为准。

## 事务与回滚

- setup、repair、profile set、models refresh 都按：备份 → 写入 → 验证 → 失败回滚。
- 同值 `profile set` 零写入、不产生备份；默认仍完成 direct 探针。
- 不修改顶层 model/model_provider；主任务登录方式不变。

## 迁移（旧 DeepSeek 子 Agent）

- 不带 `--migrate-deepseek`：旧 DeepSeek 配置零写入，只报告可迁移状态。
- 显式迁移：保留旧 skill、备份与 DeepSeek 凭据；只接管 Provider/角色标记块与模型目录清单。
- 迁移后必须重新验收（direct 探针；原生 gate 由父 Agent 执行）。

## 特殊行为

- profile set 成功后提示重启桌面应用并打开新任务；相同组合返回 changed=false，不要求重启。
- gateway：随机端口持久复用；认证 helper 每 5 秒检查、按需隐藏启动；无系统自启/空闲退出。
- disable：只停用角色，保留 Provider、模型目录、受管 state 与 API key。
- uninstall：总是删除本地 gateway token 与受管 state；只有 `--remove-credential` 才删除 Go API key。
- models refresh：先 Go /models 再 models.dev；未知/协议/冲突只报告不采用；未选缺失标记 unavailable；选中消失整次中止；setup/repair 不刷新。

## 分层发布验收门

- manager 层：direct 探针。当前 Profile 文本 marker（OPENCODE_TEXT_OK）与 function-tool marker（OPENCODE_TOOL_OK）；`test --all-models` 覆盖全部 19 模型。只证明本地网关往返。
- 父 Agent 层（发布前待执行）：三 Profile 原生 gate：
  1. deepseek-v4-flash / max
  2. gpt-5.6-luna / high
  3. qwen3.8-max / max
  每个都要求：`spawn_agent(agent_type="OpenCode", fork_context=false, ...)`；SQLite 子线程元数据（Provider、模型、effort、agent_role=OpenCode）；精确口令 `NATIVE_OPENCODE_OK`（末尾只容忍一个英文或中文句号）。
- 未执行项：19 模型真实 Go API、三 Profile 原生 gate、macOS 实机均为发布前待验证，不得写为已通过。

## 排障

- configured：status 确认受管静态配置完整；它不持久化 direct 探针结果，也不证明原生 gate。
- unconfigured/not_managed/migration_required/credential_missing/conflict/operation_in_progress/gateway_unavailable/model_unavailable/live_test_failed/unsupported/failed：按结构化 status/code 处理；不得从文件名猜测或编造 ready。
- reasoning_state_expired/reasoning_state_transport_mismatch：是网关 409 协议错误；重新发起不依赖旧 reasoning 句柄的会话/回合。
- conflict/drift：报告文件与字段，等待用户决定；不建议手改配置或强制覆盖漂移。
- 角色未知：提示重启桌面应用或打开新任务；不回退到默认角色、脚本或 codex exec。
