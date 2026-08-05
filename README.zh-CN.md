# EvoAgent Forge

**面向 Agent Skill 的可信软件供应链。**

技能不是一段可以随意复制的 Prompt，而是可能读取文件、访问网络、执行进程的代码制品。Forge 为它提供完整生命周期：严格元数据、能力声明、静态扫描、确定性打包、Ed25519 签名、内容寻址发布、可搜索注册中心、可执行测试用例和评测驱动演化。

## 它解决什么

- 安装前就能看到技能申请的权限，而不是运行后才发现
- 相同源码构建出相同 SHA-256，便于复现和审计
- 名称/版本不可覆盖，发布物按内容寻址保存
- 签名绑定精确字节与发布者密钥，可使用 fingerprint 建立信任
- 反馈必须先转化为回归用例，候选副本通过安全与质量门禁后才能发新版本

## 快速体验

```powershell
pip install -e ".[dev]"
evoagent-forge init examples\demo --name demo-skill
evoagent-forge scan examples\demo
evoagent-forge evaluate examples\demo
evoagent-forge package examples\demo
evoagent-forge serve --port 8822
```

控制台地址是 `http://127.0.0.1:8822`。签名证明“某个密钥签署了这些字节”，不等于代码天然安全；生产安装器仍应固定可信 fingerprint、展示权限并使用沙箱。

