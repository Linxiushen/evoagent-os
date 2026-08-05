# HarnessLab 中文说明

HarnessLab 是一个面向 Agent Harness 的协议兼容性实验室和执行追踪控制台。它把模型回合、
工具调用、审批决策、工具结果和终止状态保存在同一条严格有序的事件流中，并提供可运行的
conformance 测试，而不是依赖 README 中的兼容性声明。

> DeepSeek Harness 当前尚未公开协议。HarnessLab 不声称已经接入内部或预览接口；项目把
> 能力探测和 provider adapter 隔离在明确边界内，等首个公开或内测规范出现后，可以保留
> 运行时、工具层、UI 和测试矩阵，只新增协议翻译层。

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\harnesslab check
.venv\Scripts\harnesslab serve
```

打开 `http://127.0.0.1:4318`。默认 demo 完全离线、结果确定，不需要模型 API Key。

## 当前能力

- 多轮模型与工具循环，设置强制最大回合数。
- 基于 SSE 的实时事件流，每个工具调用都有相关 ID。
- 只读工具可自动审批；有副作用的工具默认拒绝执行。
- 六项可在 CI 中执行的兼容性检查。
- OpenAI-compatible 和当前公开 DeepSeek API adapter。
- 基于官方 Python SDK 的可选 MCP stdio bridge。
- 为未来 DeepSeek Harness 预留的独立 capability probe 与 adapter 边界。

## 接入 DeepSeek Harness 的第一天

1. 保存官方协议、能力文档和消息样例作为不可变 fixture。
2. 在 `src/harnesslab/adapters/` 实现新的 `HarnessAdapter`。
3. 把原生生命周期事件映射到 HarnessLab 的稳定事件词汇。
4. 运行现有六项通用测试，再增加协议专属测试。
5. 只有全部检查通过后，才在 README 标注实际兼容范围。

这种方式可以让“支持”成为可复现的工程结论，而不是抢发布时间的口号。

