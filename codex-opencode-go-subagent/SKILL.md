---
name: codex-opencode-go-subagent
description: 仅在用户要求配置、检查、测试、修复、切换模型或思考强度、停用或卸载 Codex 的 OpenCode Go 原生子 Agent 时使用；普通 OpenCode 使用问题与已配置后的日常编码任务不要触发。
license: MIT
---

# Codex OpenCode Go Subagent

本 Skill 只维护 OpenCode Go 原生子 Agent 的配置与验收流程，不承接日常用户任务。确定性操作交给 `scripts/codex_opencode_go.py`；不要手动修改配置文件、模型目录、Agent 文件或系统凭据库。

新安装默认 Profile 是 `deepseek-v4-flash + max`，原生角色是 `OpenCode`。模型矩阵与 wire 映射见 [references/compatibility.md](references/compatibility.md)，网关协议与安全见 [references/protocol.md](references/protocol.md)，命令与排障见 [references/operations.md](references/operations.md)。

## 关键契约

- 只使用桌面应用内置的 Codex 运行时；版本仅用于诊断，兼容性以真实派发验收为准。
- 日常任务只能直接调用 `spawn_agent(agent_type="OpenCode", fork_context=false, ...)`；不要为日常任务运行本 Skill、管理脚本或 `codex exec`。
- 当前工具若不认识 `OpenCode` 角色，只提示用户重启桌面应用或打开新任务；不得改用默认角色、脚本或 `codex exec` 代做当前任务。
- 支持 text/shell/apply_patch/function/MCP；不支持 image/audio/hosted web/imagegen/computer-use。视觉与网页内容由父 Agent 先识别后以文字传入。
- 凭据只通过 `--api-key-stdin` 的标准输入传递并保存到系统凭据库；不写临时文件、命令行或日志。

## 管理命令一览

入口：macOS 用 `python3`，Windows 用 `py -3`；脚本为 `<skill-dir>/scripts/codex_opencode_go.py`，示例一律加 `--json`。完整语法见 [references/operations.md](references/operations.md)。

| 命令 | 作用 |
|---|---|
| status | 只读检查运行时、配置、模型目录、凭据与能力 |
| setup | 写入配置并做 direct 探针 |
| test [--all-models] | 本地网关 direct 探针（默认 Profile 或全部 19 模型） |
| repair | 按当前父模型重应用配置并做 direct 探针 |
| profile list/show/set | 查看或原子切换 Profile |
| models list/refresh | 只读查询或显式刷新模型目录 |
| gateway status/start/stop | 只读查询或启停本地网关 |
| disable/uninstall | 停用角色 / 移除受管配置 |

## 触发后的流程

1. 先运行 `status --json`，根据结构化状态继续，不靠文件名猜测。
2. 配置请求运行 `setup --json`；父模型已变化或配置损坏时运行 `repair --json`。新安装默认 Flash/max，`repair` 保留当前合法 Profile。
3. 缺少凭据时简洁索要 API Key；收到后只通过 `--api-key-stdin` 传入，不复述、不回显。
4. 切换请求先运行 `profile show --json`，再调用 `profile set --model <模型> --effort <档位> --json`；两个参数必须同时提供。相同组合零写入、不产生备份，但默认仍完成 direct 探针。
5. 切换成功后提示用户重启桌面应用并打开新任务；除非用户明确要求稍后手动验收，否则不要传 `--skip-live-test`。
6. `profile set` 失败时确认结果已回滚；不要手改模型目录、Agent 或清单。
7. `models list` 只读；`models refresh` 只按显式请求执行，`setup`/`repair` 不刷新。未知项、协议错误或冲突只报告，不静默采用。
8. `gateway status` 只读；`gateway start|stop` 会启动或停止本地网关进程。
9. `test` 与 `test --all-models` 只执行本地网关 direct 探针（文本 marker 加 function-tool marker），不创建原生会话、不查 SQLite。
10. `disable` 停用角色但保留 Provider、模型目录和凭据；`uninstall` 移除受管配置，只有用户明确要求删除凭据时才传 `--remove-credential`。

## 旧 DeepSeek 子 Agent 迁移

- `setup`/`repair` 不带 `--migrate-deepseek` 时，对旧 DeepSeek 配置零写入，只报告可迁移状态。
- 显式传 `--migrate-deepseek` 才迁移：保留旧 Skill、备份和 DeepSeek 凭据，只接管 Provider/角色标记块与模型目录清单。
- `--skip-live-test` 只跳过 direct 探针，不跳过静态检查，也不影响父 Agent 的原生验收。
- manager 不做原生验收：原生 gate 由父 Agent 另行 `spawn_agent`，并检查 SQLite 元数据与精确口令 `NATIVE_OPENCODE_OK`，见 [references/operations.md](references/operations.md) 的分层发布门。

## 状态处理

- `configured`：受管静态配置完整；status 不记录 direct 探针结果，也不代表原生 gate 通过。
- `credential_missing`：索要 API Key 后继续原流程。
- `operation_in_progress`：已有配置操作正在运行，稍后重试，不并发修改。
- `conflict`：报告不属于本 Skill、已被用户修改或哈希漂移的冲突文件和字段，等待用户决定；不得静默覆盖。
- `unsupported`：报告缺少的系统能力，不按固定版本号猜测兼容性，也不手工绕过。
- `failed`：读取结构化 `errors`；若程序已回滚，明确说明，不再手改配置。

effort 档位只是请求参数，不承诺具体速度、价格或服务端推理资源；各模型允许档位、refresh 规则与能力边界见 compatibility.md 与 protocol.md。
