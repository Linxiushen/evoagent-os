# EchoWeave-RTC

EchoWeave-RTC 是一个“经本人授权、持续标识为 AI”的实时语音与数字头像
Agent 编排框架。它把端点检测、语音识别、流式回答、语音合成、口型视频、
打断和降级统一在一条可测试的会话链路里。

> 这个项目不支持秘密冒充真人。真人 persona 只能从带授权范围、有效期、
> 素材哈希和服务端签名的清单加载；界面持续显示“AI 数字分身”。

## 当前实现

- 自研 `EW` v1 二进制媒体包协议（PCM16 / MP4 segment）。
- VAD 端点状态机、短 pre-roll、550ms 尾静音。
- generation epoch 打断：取消 LLM/TTS/头像，清空浏览器播放队列。
- DeepSeek V4 Flash SSE 流式客户端与中文语义分句。
- Qwen3-ASR 本地适配器和 vLLM OpenAI transcription 适配器。
- VoxCPM2 本地 `generate_streaming()` 与 vLLM-Omni 适配器。
- Nuwa 离线准备 CLI、`SKILL.md` 加载、素材哈希与人工复核工作流。
- 带 worker 鉴权、大小限制和服务端永久水印的 SoulX Lite bridge。
- 完全无需 GPU/API key 的安全演示模式，便于验证端到端协议。

## 六项组件的准确来源

| 作用 | 官方来源 |
|---|---|
| 用户说完检测 | [snakers4/silero-vad v5.1.2](https://github.com/snakers4/silero-vad/tree/v5.1.2) |
| ASR | [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) |
| 回答 | [DeepSeek API `deepseek-v4-flash`](https://api-docs.deepseek.com/) |
| TTS / 授权音色 | [openbmb/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) |
| 思维方法论提炼 | [alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill) |
| 口型和表情 | [Soul-AILab/SoulX-FlashHead-1_3B](https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B) |

Silero v5 在 Hugging Face 上没有官方同名仓库，第三方转换不作为默认来源；
SoulX 的准确仓库名使用 `1_3B`；Nuwa 是离线 Agent Skill，不是模型服务。

## 3 分钟运行

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\echoweave serve
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，勾选 AI 身份提示。
默认 demo 使用虚构角色、浏览器 TTS 和合成头像。文字输入可立即跑完整状态机；
允许麦克风后也能验证 VAD、端点检测和 ASR 适配器边界。

运行测试：

```powershell
.\.venv\Scripts\python -m pytest -q
```

仓库包含可直接复制到 `.github/workflows/` 的
[`ci/github-actions.yml`](ci/github-actions.yml) CI 模板。

Docker Compose 会把网关监听在容器的所有接口，因此必须先生成访问令牌：

```powershell
Copy-Item .env.example .env
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
(Get-Content .env) -replace '^ECHOWEAVE_ACCESS_TOKEN=.*$', "ECHOWEAVE_ACCESS_TOKEN=$token" |
  Set-Content .env -Encoding utf8
docker compose up --build
Write-Host "打开 http://127.0.0.1:8765/#token=$token"
```

`.dockerignore` 使用构建上下文白名单，不会把 `.env`、真人素材、模型缓存或
本地虚拟环境发送给 Docker builder。当前开发机未安装 Docker，因此本仓库已
完成 Dockerfile/Compose 静态检查，但没有在这台机器上执行镜像构建。

## 完整模型模式

复制 `.env.example` 为 `.env`，按
[部署文档](docs/DEPLOYMENT.md) 将 Qwen、VoxCPM2、SoulX 分别启动为 GPU
worker，再选择：

```text
ECHOWEAVE_VAD_BACKEND=silero_v5
ECHOWEAVE_ASR_BACKEND=qwen_http
ECHOWEAVE_LLM_BACKEND=deepseek
ECHOWEAVE_TTS_BACKEND=voxcpm_http
ECHOWEAVE_AVATAR_BACKEND=soulx_http
```

DeepSeek key 只从服务端 `DEEPSEEK_API_KEY` 读取。不要把聊天里出现过的 key
继续使用，也不要写进 `.env` 后提交。

启用真人 persona 时还要设置至少 32 字节的
`ECHOWEAVE_CONSENT_SIGNING_KEY`；授权 revision 与永久撤回墓碑会写入
`ECHOWEAVE_CONSENT_STATE_PATH`，该路径应放在持久、受备份保护的私有卷上。

对外绑定时还必须设置 `ECHOWEAVE_ACCESS_TOKEN`；网页支持
`#token=<value>&persona=<id>`，读取后会立即从地址栏移除片段。真人 persona
必须显式加入 `ECHOWEAVE_ALLOWED_PERSONAS`。

## 文档

- [架构与打断](docs/ARCHITECTURE.md)
- [媒体协议](docs/PROTOCOL.md)
- [模型、许可证与硬件](docs/MODELS.md)
- [完整部署](docs/DEPLOYMENT.md)
- [授权与安全边界](docs/SECURITY.md)
- [Nuwa 离线蒸馏与人工复核](docs/NUWA_WORKFLOW.md)
- [真人 persona 注册](personas/README.md)

## 硬件

当前开发机的 4 GB GPU 可以运行网关、测试和演示，但不能同时常驻
Qwen3-ASR-1.7B、VoxCPM2 和 SoulX。完整本地实时链路建议让 ASR/TTS 使用
一张较大显存 GPU，并让 SoulX Lite 独占一张 RTX 4090 24 GB 级 GPU。

## License

EchoWeave-RTC code is Apache-2.0. Third-party models keep their own licenses and
acceptable-use requirements. A permissive software license never grants rights
to a person's voice, face, writings or identity.
