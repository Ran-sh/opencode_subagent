# 来源与修改说明

本仓库以 [Ran-sh/codex_subagent_deepseek](https://github.com/Ran-sh/codex_subagent_deepseek/tree/84458a2ca16522e8685e6ba18468c6608cd8f1d1) 固定提交 `84458a2ca16522e8685e6ba18468c6608cd8f1d1` 为旧参考基线，保留原作者 oil-oil 与扩展者 Ran-sh 的归属；MIT 许可全文见 [LICENSE](LICENSE)。

## 继承的设计思想

本仓库继承以下设计思想，但未复制参考项目的全部实现：

- 事务式配置写入、备份与精确回滚
- 系统凭据集成（Windows Credential Manager / macOS Keychain）
- 原生子 Agent 验收方法（子线程元数据与精确口令）

## 本仓库新增贡献

- OpenCode Go 三协议兼容本地网关（chat_completions / responses / anthropic_messages）
- 18 个首版活跃模型目录与 effort 强度策略
- reasoning 句柄与工具调用循环
- 跨 Windows/macOS 的 Python 标准库管理脚本与测试

## 实现参考（仅参考，未捆绑外部源码）

- [anomalyco/opencode@3a90639cb57619a21e59f544b3e8d23ffed56f48](https://github.com/anomalyco/opencode/tree/3a90639cb57619a21e59f544b3e8d23ffed56f48)
- [openai/codex@2cc9dbb9846b2dc03948414df6712adb967c70eb](https://github.com/openai/codex/tree/2cc9dbb9846b2dc03948414df6712adb967c70eb)

## 未捆绑内容

本仓库仅包含配置技能、网关、测试与文档，不包含 deepseek-dispatch-governor、API 凭据、workspace 标识、私有日志或任何生成产物。
