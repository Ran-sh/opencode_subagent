# OpenCode Go 本地网关协议

## 总体路径

- Codex 通过 Responses 协议把请求发给 127.0.0.1 本地网关；网关转换为 OpenCode Go 的 `chat_completions`、`responses` 或 `anthropic_messages` 上游请求。
- 网关绑定 127.0.0.1 随机端口；端口持久复用，不每次重建。
- 认证 helper 每 5 秒检查一次，仅在需要时隐藏启动；其标准输出只提供本地 Bearer 令牌。
- 无系统自启、无空闲自动退出；生命周期由 `gateway start|stop` 管理。

## 端点与安全

- `GET /health`：公开端点，返回网关存活状态。
- `POST /v1/responses`：需要精确匹配本地 Bearer 令牌；承载模型请求。
- `POST /shutdown`：需要相同认证；停止本地网关进程。
- 拒绝非回环 bind；网关监听 host 必须为 127.0.0.1。
- 不支持的方法返回 `method_not_allowed`；错误响应为稳定 code + message，安全错误不暴露内部实现细节。

## 请求转换

- 完整保留 history、instructions、text、reasoning 输入与工具列表。
- 普通 function/MCP 工具保持原 schema 与 function_call 语义。
- custom、shell、apply_patch 工具包装为同名 function，必填 string input；输出时恢复为 custom_tool_call。
- 同一请求内的工具并行调用与多轮工具往返按顺序处理。

## 上游 transport 映射

- `chat_completions`：非 default effort 发送 `reasoning_effort`。
- `responses`：发送 `reasoning.effort`。
- `anthropic_messages`：MiniMax M3 `none`→disabled、`high`→enabled；Qwen `none`→disabled、`high`→budget_tokens=16000、`max`→budget_tokens=31999；`max_tokens=32000`。
- default 伪档位不发送强度。

## SSE 输出

- 事件序列：created/in_progress → item → text/reasoning/tool delta 与 done → usage → completed/failed。
- 上游截断或 650 秒 idle 超时按失败处理（`upstream_idle_timeout`）。
- HTTP 429 透传 `Retry-After`；`request_id` 脱敏后记录。

## Reasoning 生命周期

- `reasoning_content`、`thinking`、signature 与 Responses encrypted payload 只在内存保存。
- 对外只下发 `ocg1:` 前缀的 256-bit 随机句柄。
- 存储为实例内 LRU：上限 2048 条、TTL 2 小时。
- 网关重启、句柄过期或缺失 → `409 reasoning_state_expired`；transport 不匹配 → `409 reasoning_state_transport_mismatch`。
- 句柄不落盘、不外发原始 reasoning 内容。

## 资源与日志

- 请求体上限 32 MiB；并发上限 8；connect 超时 30 秒；stream idle 超时 650 秒。
- 日志只允许 model、transport、status、duration_ms、request_id 字段。
- 不记录请求/响应正文、凭据或 reasoning 内容。

## 明确不支持

- hosted web、image、audio、imagegen、computer-use 等能力在请求中被拒绝。
- 不支持的 transport、模型或 effort 组合返回稳定错误码；不猜测兼容性、不绕过。
