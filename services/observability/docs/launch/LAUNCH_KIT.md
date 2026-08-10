# HarnessLab v0.2 Launch Kit

This kit is designed to earn relevant users and contributors, not vanity traffic. Adapt each post
to the community instead of publishing identical text everywhere.

## Positioning

**One line:** HarnessLab records an agent run as a portable trace contract, then fails CI when the
harness lifecycle, tool path, approval path, or terminal semantics regress.

**Primary audience:** agent runtime and harness authors, MCP integrators, eval infrastructure
engineers, and teams maintaining multiple model/provider adapters.

**Proof points:**

- Deterministic offline quickstart with no API key.
- Interactive GitHub Pages demo with no installation.
- `snapshot` and `verify` produce a real CI exit code.
- Stable protocol fingerprint plus stricter content fingerprint.
- Live trace and regression diff workbench.
- Redaction before events reach SSE, UI, or exported artifacts.
- Explicit non-claim about unpublished DeepSeek Harness compatibility.

## Chinese X post

```text
Agent 的回归不一定表现为“答案变差”。

工具可能在审批前执行、call/result 可能失去关联、终态可能重复出现，而普通日志和答案评测很难把这些问题变成 CI gate。

我把 HarnessLab 升级到了 v0.2：

- snapshot：把一次 Agent 运行固化成 trace contract
- verify：重放并检测生命周期 / 工具 / 审批路径回归
- 双 SHA-256 指纹与结构化 diff
- SSE trace workbench
- 默认 fail-closed，事件发布前脱敏
- 离线 demo，无需 API Key

项目：https://github.com/Linxiushen/harnesslab
在线演示：https://linxiushen.github.io/harnesslab/

想听听做 Agent runtime、MCP 和 eval infra 的同学：你们最想在 CI 里固定哪条 harness invariant？
```

Attach `docs/regression.png`. Do not tag unrelated accounts. Reply to questions with concrete
artifacts or code references instead of reposting the launch text.

## English X post

```text
Agent regressions are not always answer regressions.

A tool can execute before approval, call/result correlation can break, or a run can emit two terminal states while the final answer still looks fine.

HarnessLab v0.2 turns harness behavior into a CI contract:

- snapshot / verify CLI
- stable protocol fingerprints
- structural trace diffs
- fail-closed tool policy
- pre-publication credential redaction
- deterministic offline demo

No model key required:
https://github.com/Linxiushen/harnesslab
Live demo: https://linxiushen.github.io/harnesslab/

Which harness invariant would you gate in CI?
```

## Show HN

**Title**

```text
Show HN: HarnessLab - snapshot and diff agent harness traces in CI
```

**Body**

```text
I built HarnessLab after noticing that answer-level evals miss orchestration regressions.

A run can still produce a plausible answer when a tool executes before approval, a tool result is no longer correlated with its call, or the runtime emits an invalid terminal sequence.

HarnessLab records model/tool/policy lifecycle events as a portable harnesslab.trace/v1 artifact. The snapshot command creates a reviewed baseline; verify reruns the task and exits non-zero on structural drift. A separate content fingerprint can be made strict for deterministic fixtures.

The demo adapter is offline and deterministic, so the quickstart needs no API key. There is also a local trace/diff workbench, an OpenAI-compatible adapter, a public DeepSeek API adapter, and an optional MCP stdio bridge.

I have deliberately not guessed the unpublished DeepSeek Harness protocol. The project keeps that discovery/adapter boundary isolated until a real specification or fixture exists.

Repository: https://github.com/Linxiushen/harnesslab

I would especially value feedback on the Trace Contract invariants and artifact shape.
```

## Reddit / LocalLLaMA

Use a `Project` or `Showcase` flair if the community provides one.

```text
Title: I made a CI regression contract for agent harness traces (offline demo, MCP bridge)

Most agent eval tooling starts from the final answer. I wanted a small tool that tests the control plane itself: lifecycle order, call/result correlation, approval decisions, and terminal semantics.

HarnessLab v0.2 now records a reviewed run with `snapshot`, reruns it with `verify`, and returns a non-zero exit code on structural drift. Protocol fingerprints ignore latency/token/text noise; content fingerprints can be strict for deterministic fixtures. The reference demo is offline and needs no model key.

Repo: https://github.com/Linxiushen/harnesslab
Trace Contract: https://github.com/Linxiushen/harnesslab/blob/main/docs/TRACE_CONTRACT.md

I am looking for real-world invariants and provider fixtures, especially from people maintaining MCP or multi-provider runtimes.
```

## V2EX / Chinese developer community

```text
标题：开源了一个 Agent Harness 的 trace contract / CI 回归工具

最近在做 Agent Harness 的协议与可观测性实验。普通日志能看到“发生了什么”，答案评测能判断输出质量，但很难在 CI 中固定编排层行为：工具是否先审批后执行、call/result 是否对应、终态是否唯一等。

HarnessLab v0.2 增加了 snapshot / verify：把一次运行固化成脱敏的 trace contract，之后重放并对生命周期、工具路径、审批路径和终态做结构化 diff。默认 demo 离线确定，不需要 API Key。

GitHub：https://github.com/Linxiushen/harnesslab

项目还很早期，最希望收到的是实际 runtime 中遇到过的 invariant 和 adapter fixture，而不是泛泛的功能建议。
```

## Seven-day sequence

1. **Day 0:** Publish the GitHub release and one X post with the regression screenshot.
2. **Day 1:** Submit Show HN during US morning hours; answer every technical question directly.
3. **Day 2:** Post a deeper Chinese write-up on V2EX or a relevant developer community.
4. **Day 3:** Share the project in one relevant Reddit community using its required flair.
5. **Day 4:** Turn the best question into a small issue or documentation improvement and post the result.
6. **Day 5:** Ask one known harness/MCP maintainer for technical feedback, not for a star or repost.
7. **Day 7:** Publish metrics and a small follow-up release based on actual feedback.

Do not cross-post all communities on the same day, buy stars, use star-exchange groups, or send
unsolicited bulk DMs. Those tactics damage repository conversion and contributor trust.

## Metrics

Use GitHub traffic and release data to track:

- Unique repository visitors and referrers.
- README-to-clone conversion.
- Release downloads or artifact views.
- Issues opened by new users.
- Stars per unique visitor, not raw stars alone.
- Number of external fixtures or invariants contributed.

The strongest follow-up signal is a user adding a baseline to their CI, not a passive star.
