# EvoAgent Fleet

**面向专业化 Agent 团队的持久化控制面。**

Fleet 不是让多个 Agent 在一个群聊里互相发言。它把真实分布式任务需要的机制做成可运行系统：DAG 校验、能力匹配、并发限制、带心跳的租约、失败重试、Token/成本预算、人工审批、内容寻址制品、事件账本，以及由成功率、质量、成本和时延驱动的路由评分。

## 为什么有用

- Worker 崩溃后租约过期，任务可以由其他 Worker 接管
- 迟到的 Worker 无法使用失效 token 提交结果
- 依赖没有完成的节点不会被调度
- 高风险发布节点可以在执行前等待人工批准
- 每个产物都有 SHA-256，工作流每次状态变化都能追溯
- 路由演化基于可见指标，而不是模型自己选择“最喜欢”的 Agent

## 运行

```powershell
pip install -e ".[dev]"
evoagent-fleet demo --state-dir .demo-fleet
evoagent-fleet serve --state-dir .demo-fleet --port 8833
```

访问 `http://127.0.0.1:8833` 查看控制台，访问 `/docs` 查看远程 Worker API。

