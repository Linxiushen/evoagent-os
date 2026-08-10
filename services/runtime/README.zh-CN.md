# EvoAgent Runtime

**一个能够持续运行、可以进化，但不会偷偷改写自己的本地优先 Agent 控制面。**

EvoAgent Runtime 不是一次性对话 Demo。它把 OpenClaw 类系统最关键的工程问题放进一个可安装项目：长期会话、HTTP/WebSocket Gateway、模型与工具循环、风险策略、人工审批、计划任务、记忆、审计事件和受评测约束的 Prompt 演化。

## 核心能力

- SQLite WAL 持久化会话、运行、消息、审批、任务、Prompt 与事件
- 离线确定性 Provider，以及 OpenAI 兼容模型端点
- FTS5 长期记忆与上下文召回
- 带 JSON Schema 的工具注册表和 low/medium/high 风险策略
- 高风险工具暂停、人工批准/拒绝、原运行恢复
- 工作区路径隔离、HTTPS 白名单和 SSRF 防护
- 适用于 24/7 数字员工的周期调度器
- 从反馈提案、基线对比、场景回归到人工发布的完整演化闭环
- 可直接操作运行、审批、事件与候选版本的 Web 控制台

## 快速开始

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
evoagent-runtime demo --state-dir .demo
evoagent-runtime serve --state-dir .demo --port 8811
```

访问 `http://127.0.0.1:8811`。默认离线模式无需 API Key，可以完整演示记忆、审批与演化。

生产环境至少配置：

```powershell
$env:EVOAGENT_PROVIDER="openai"
$env:EVOAGENT_API_KEY="..."
$env:EVOAGENT_GATEWAY_TOKEN="使用强随机值"
$env:EVOAGENT_HTTP_ALLOWLIST="api.example.com"
evoagent-runtime serve
```

## 自进化不是“自动改 Prompt”

每次变化都有父版本、反馈证据、基线分数、候选分数、安全分数和状态。候选必须在独立场景集上达到最小增益且不触发 forbidden 条件，随后仍需人工 promotion。失败候选保留证据但不会影响在线行为。

更多内容见 [架构](docs/architecture.md)、[威胁模型](docs/threat-model.md)、[评测卡](docs/benchmark-card.md) 与 [项目论文](paper/PAPER.md)。

