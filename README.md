# OpenCode Go 原生子 Agent 技能

本仓库提供单一配置技能 `codex-opencode-go-subagent`：注册原生子 Agent 角色 `OpenCode`，默认 profile 为 `deepseek-v4-flash/max`。本仓库不包含 `deepseek-dispatch-governor`，也不捆绑任何 API 凭据。

## 安装

安装主命令：

```text
npx skills add Ran-sh/opencode_subagent -g -y
```

安装完成后请重启 Codex 或打开新任务，然后调用技能完成首次设置：

```text
$codex-opencode-go-subagent
```

## 首次设置与凭据

API Key 只从标准输入（stdin）读入，并保存到系统凭据库（Windows Credential Manager 或 macOS Keychain）；不会写入配置文件、命令行、日志或临时文件。

Windows：

```text
py -3 <skill-dir>/scripts/codex_opencode_go.py setup --api-key-stdin --json
```

macOS 使用等价的 `python3` 入口：

```text
python3 <skill-dir>/scripts/codex_opencode_go.py setup --api-key-stdin --json
```

## 模型与思考强度

模型与思考强度必须成对设置且同时有效。示例：

```text
py -3 <skill-dir>/scripts/codex_opencode_go.py profile set --model gpt-5.6-luna --effort high --json
```

切换成功后请新建 Codex 任务再使用新 profile。首版包含 18 个活跃模型；模型矩阵、允许的 effort 档位与 transport 映射见 [compatibility.md](codex-opencode-go-subagent/references/compatibility.md)。

## 稳定命令

- `status`、`setup`、`test`、`repair`
- `profile list`、`profile show`、`profile set`
- `models list`、`models refresh`
- `gateway status`、`gateway start`、`gateway stop`
- `disable`、`uninstall`

完整语法、事务与回滚行为、分层发布门槛见 [operations.md](codex-opencode-go-subagent/references/operations.md)。

## 架构

Codex 通过 Responses 协议把请求发送到仅监听 `127.0.0.1` 的本地鉴权网关；网关转换为 OpenCode Go 的 `chat_completions`、`responses` 或 `anthropic_messages` 上游请求。协议细节与安全边界见 [protocol.md](codex-opencode-go-subagent/references/protocol.md)。

## 能力与安全边界

支持：文本、shell、apply_patch、普通 function 工具与 MCP 工具。

拒绝：图片、音频、托管 web search、图像生成、computer-use。

- 请求体上限 32 MiB；并发上限 8；连接超时 30 秒；流式空闲超时 650 秒。
- reasoning 句柄仅保存在内存：2048 条 LRU、2 小时 TTL，不落盘、不外发原始内容。
- API Key 不进入配置、参数、日志或状态。
- 不注册开机启动，也不作为系统服务常驻。

## 测试

```text
python -B -m unittest discover -s tests -v
python -B scripts/quick_validate.py codex-opencode-go-subagent --repo-root .
```

官方 skill-creator 校验器可另行运行；在 Windows 默认代码页环境下需要设置 `PYTHONUTF8=1`，实际命令由各开发环境自行解析，此处不写本机路径。

证据分层说明：manager 的 direct probes（`setup`、`test`、`repair`、`profile set` 中的本地网关往返 marker）只证明本地网关往返，不等同原生 `spawn_agent` gate；单元测试与静态校验也不能替代真实服务或真机验证。

## 发布门槛（当前均待执行）

1. 18 个模型各自的最小流式文本与一次工具调用；
2. `deepseek-v4-flash/max`、`gpt-5.6-luna/high`、`qwen3.8-max/max` 三个原生 `spawn_agent` gate；
3. macOS 真机验证。

在上述门槛全部执行通过之前，本仓库不声明已发布，也不声明 live/native gate 已完成。

## 许可与说明

- [LICENSE](LICENSE)
- [NOTICE.md](NOTICE.md)
