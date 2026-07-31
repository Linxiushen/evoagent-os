const EW = {
  ws: null,
  audioContext: null,
  micStream: null,
  micProcessor: null,
  micSource: null,
  micSilentGain: null,
  microphoneRequested: false,
  micRequestEpoch: 0,
  micPending: null,
  playbackSources: new Set(),
  scheduledAt: 0,
  ttsSampleRate: 48000,
  currentAssistant: null,
  started: false,
  startPending: false,
  videoQueue: [],
  playingVideo: false,
  currentVideoUrl: null,
  syncAudioTurns: new Map(),
  playoutEpoch: 0,
  helloReceived: false,
  startSent: false,
  startAttempt: 0,
  textPending: false,
  personaName: "Echo",
};
const MAX_VIDEO_QUEUE = 4;
const MAX_VIDEO_BYTES = 32 * 1024 * 1024;

const $ = (selector) => document.querySelector(selector);
const startButton = $("#startButton");
const cancelButton = $("#cancelButton");
const sendButton = $("#sendButton");
const textInput = $("#textInput");
const transcript = $("#transcript");
const disclosureAck = $("#disclosureAck");
const personaInput = $("#personaInput");
const accessTokenInput = $("#accessTokenInput");
const avatar = $("#syntheticAvatar");
const avatarVideo = $("#avatarVideo");
const stateLabel = $("#stateLabel");
const connectionDot = $("#connectionDot");
const toast = $("#toast");
const fragmentParams = new URLSearchParams(location.hash.slice(1));
accessTokenInput.value = fragmentParams.get("token") || "";
personaInput.value = fragmentParams.get("persona") || "demo";
if (location.hash) {
  history.replaceState(null, "", `${location.pathname}${location.search}`);
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 4200);
}

function addSystemNote(text) {
  const item = document.createElement("div");
  item.className = "system-note";
  item.innerHTML = `<span>系统</span><p></p>`;
  item.querySelector("p").textContent = text;
  transcript.appendChild(item);
  transcript.scrollTop = transcript.scrollHeight;
}

function addMessage(role, text = "", streaming = false) {
  const item = document.createElement("div");
  item.className = `message ${role}${streaming ? " streaming" : ""}`;
  item.innerHTML = `<span class="role"></span><p></p>`;
  item.querySelector(".role").textContent =
    role === "user" ? "YOU" : `${EW.personaName.toUpperCase()} · AI`;
  item.querySelector("p").textContent = text;
  transcript.appendChild(item);
  transcript.scrollTop = transcript.scrollHeight;
  return item;
}

function setState(state) {
  const labels = {
    ready: "准备就绪",
    listening: "正在聆听",
    user_speaking: "检测到语音",
    transcribing: "正在识别",
    thinking: "正在思考",
    speaking: "正在回答",
    closed: "已关闭",
  };
  stateLabel.textContent = labels[state] || state;
  avatar.classList.toggle("listening", state === "listening" || state === "user_speaking");
  avatar.classList.toggle("speaking", state === "speaking");
  if (state === "closed") {
    connectionDot.classList.remove("online");
  }
}

function sendControl(payload) {
  if (EW.ws?.readyState === WebSocket.OPEN) {
    EW.ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function mediaPacket(kind, turnId, ptsMs, payload) {
  const body = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
  const packet = new Uint8Array(12 + body.byteLength);
  packet[0] = 0x45;
  packet[1] = 0x57;
  packet[2] = 1;
  packet[3] = kind;
  const view = new DataView(packet.buffer);
  view.setUint32(4, turnId >>> 0, true);
  view.setUint32(8, ptsMs >>> 0, true);
  packet.set(body, 12);
  return packet.buffer;
}

function parsePacket(buffer) {
  const view = new DataView(buffer);
  if (view.byteLength < 12 || view.getUint8(0) !== 0x45 || view.getUint8(1) !== 0x57) {
    throw new Error("Invalid EchoWeave media packet");
  }
  if (view.getUint8(2) !== 1) {
    throw new Error("Unsupported EchoWeave media protocol version");
  }
  return {
    version: view.getUint8(2),
    kind: view.getUint8(3),
    turnId: view.getUint32(4, true),
    ptsMs: view.getUint32(8, true),
    payload: buffer.slice(12),
  };
}

function resample(input, inputRate, outputRate) {
  if (inputRate === outputRate) return input;
  const ratio = inputRate / outputRate;
  const length = Math.max(1, Math.round(input.length / ratio));
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(input.length - 1, left + 1);
    const mix = position - left;
    output[i] = input[left] * (1 - mix) + input[right] * mix;
  }
  return output;
}

function floatToPCM16(floatSamples) {
  const output = new Int16Array(floatSamples.length);
  for (let i = 0; i < floatSamples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, floatSamples[i]));
    output[i] = value < 0 ? value * 32768 : value * 32767;
  }
  return new Uint8Array(output.buffer);
}

async function ensureAudioContext() {
  if (!EW.audioContext) {
    EW.audioContext = new AudioContext({ latencyHint: "interactive" });
  }
  if (EW.audioContext.state === "suspended") {
    await EW.audioContext.resume();
  }
  return EW.audioContext;
}

async function startMicrophone() {
  if (EW.micStream) return;
  if (EW.micPending) {
    await EW.micPending;
    if (
      EW.micStream ||
      !EW.microphoneRequested ||
      !EW.started ||
      EW.ws?.readyState !== WebSocket.OPEN
    ) {
      return;
    }
  }
  const requestEpoch = EW.micRequestEpoch;
  const pending = (async () => {
    const context = await ensureAudioContext();
    if (
      requestEpoch !== EW.micRequestEpoch ||
      !EW.microphoneRequested ||
      !EW.started ||
      EW.ws?.readyState !== WebSocket.OPEN
    ) {
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    if (
      requestEpoch !== EW.micRequestEpoch ||
      !EW.microphoneRequested ||
      !EW.started ||
      EW.ws?.readyState !== WebSocket.OPEN
    ) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    let source;
    let processor;
    let silentGain;
    try {
      source = context.createMediaStreamSource(stream);
      processor = context.createScriptProcessor(2048, 1, 1);
      silentGain = context.createGain();
      silentGain.gain.value = 0;
      processor.onaudioprocess = (event) => {
        if (EW.ws?.readyState !== WebSocket.OPEN || !EW.started) return;
        const raw = event.inputBuffer.getChannelData(0);
        const downsampled = resample(raw, context.sampleRate, 16000);
        const pcm = floatToPCM16(downsampled);
        const pts = Math.floor(performance.now()) >>> 0;
        EW.ws.send(mediaPacket(1, 0, pts, pcm));
      };
      source.connect(processor);
      processor.connect(silentGain);
      silentGain.connect(context.destination);
      EW.micStream = stream;
      EW.micProcessor = processor;
      EW.micSource = source;
      EW.micSilentGain = silentGain;
    } catch (error) {
      processor?.disconnect();
      source?.disconnect();
      silentGain?.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      throw error;
    }
  })();
  EW.micPending = pending;
  try {
    await pending;
  } finally {
    if (EW.micPending === pending) EW.micPending = null;
  }
}

function stopMicrophone() {
  EW.micRequestEpoch += 1;
  EW.micProcessor?.disconnect();
  EW.micSource?.disconnect();
  EW.micSilentGain?.disconnect();
  EW.micProcessor = null;
  EW.micSource = null;
  EW.micSilentGain = null;
  EW.micStream?.getTracks().forEach((track) => track.stop());
  EW.micStream = null;
}

async function playPCM(payload, sampleRate) {
  const playoutEpoch = EW.playoutEpoch;
  const context = await ensureAudioContext();
  if (playoutEpoch !== EW.playoutEpoch) return;
  const samples = new Int16Array(payload);
  if (!samples.length) return;
  const buffer = context.createBuffer(1, samples.length, sampleRate);
  const channel = buffer.getChannelData(0);
  let energy = 0;
  for (let i = 0; i < samples.length; i += 1) {
    channel[i] = samples[i] / 32768;
    energy += Math.abs(channel[i]);
  }
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  const startAt = Math.max(context.currentTime + 0.025, EW.scheduledAt);
  source.start(startAt);
  EW.scheduledAt = startAt + buffer.duration;
  EW.playbackSources.add(source);
  avatar.classList.add("speaking");
  source.onended = () => {
    EW.playbackSources.delete(source);
    if (!EW.playbackSources.size) avatar.classList.remove("speaking");
  };
  const average = energy / samples.length;
  $("#mouth").style.height = `${Math.min(18, 4 + average * 28)}px`;
}

function clearPlayout() {
  EW.playoutEpoch += 1;
  window.speechSynthesis?.cancel();
  for (const source of EW.playbackSources) {
    try { source.stop(); } catch (_) { /* already stopped */ }
  }
  EW.playbackSources.clear();
  EW.scheduledAt = EW.audioContext?.currentTime || 0;
  EW.videoQueue.length = 0;
  EW.syncAudioTurns.clear();
  avatarVideo.onended = null;
  avatarVideo.onerror = null;
  avatarVideo.onloadeddata = null;
  avatarVideo.pause();
  if (EW.currentVideoUrl) {
    URL.revokeObjectURL(EW.currentVideoUrl);
    EW.currentVideoUrl = null;
  }
  avatarVideo.removeAttribute("src");
  avatarVideo.load();
  EW.playingVideo = false;
  avatarVideo.classList.remove("visible");
  avatar.classList.remove("speaking");
}

function browserSpeak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "zh-CN";
  utterance.rate = 1.04;
  utterance.pitch = 1;
  utterance.onstart = () => avatar.classList.add("speaking");
  utterance.onend = () => {
    if (!window.speechSynthesis.speaking) avatar.classList.remove("speaking");
  };
  window.speechSynthesis.speak(utterance);
}

function queueVideo(data, turnId, ptsMs) {
  if (data.byteLength > MAX_VIDEO_BYTES) {
    showToast("视频分片超过客户端安全上限，已丢弃。");
    return;
  }
  while (EW.videoQueue.length >= MAX_VIDEO_QUEUE) {
    EW.videoQueue.shift();
  }
  const synchronizedAudio = EW.syncAudioTurns.get(turnId) || null;
  if (synchronizedAudio) EW.syncAudioTurns.delete(turnId);
  EW.videoQueue.push({
    blob: new Blob([data], { type: "video/mp4" }),
    synchronizedAudio,
    ptsMs,
  });
  if (!EW.playingVideo) playNextVideo();
}

function playNextVideo() {
  const item = EW.videoQueue.shift();
  if (!item) {
    EW.playingVideo = false;
    avatarVideo.classList.remove("visible");
    return;
  }
  EW.playingVideo = true;
  const playoutEpoch = EW.playoutEpoch;
  const url = URL.createObjectURL(item.blob);
  EW.currentVideoUrl = url;
  avatarVideo.muted = true;
  avatarVideo.classList.add("visible");
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    URL.revokeObjectURL(url);
    if (EW.currentVideoUrl === url) EW.currentVideoUrl = null;
    playNextVideo();
  };
  avatarVideo.onended = finish;
  avatarVideo.onerror = finish;
  avatarVideo.onloadeddata = async () => {
    avatarVideo.onloadeddata = null;
    if (playoutEpoch !== EW.playoutEpoch) return;
    if (item.synchronizedAudio) {
      for (const frame of item.synchronizedAudio.frames) {
        if (playoutEpoch !== EW.playoutEpoch) return;
        await playPCM(frame, item.synchronizedAudio.sampleRate);
      }
    }
    if (playoutEpoch !== EW.playoutEpoch) return;
    avatarVideo.play().catch(finish);
  };
  avatarVideo.src = url;
  avatarVideo.load();
}

function sendStartControl() {
  if (EW.ws?.readyState !== WebSocket.OPEN) return null;
  if (EW.startSent) return EW.startAttempt;
  const personaId = personaInput.value.trim().toLowerCase();
  if (!/^[a-z0-9_-]+$/.test(personaId)) {
    showToast("PERSONA ID 只能包含小写字母、数字、下划线和连字符。");
    return null;
  }
  personaInput.value = personaId;
  EW.startAttempt += 1;
  EW.startSent = true;
  sendControl({
    type: "start",
    persona_id: personaId,
    access_token: accessTokenInput.value,
    ai_disclosure_ack: disclosureAck.checked,
  });
  return EW.startAttempt;
}

function handleEvent(event) {
  switch (event.type) {
    case "session.hello":
      EW.helloReceived = true;
      sendStartControl();
      break;
    case "session.ready":
      EW.started = true;
      EW.personaName = event.persona || "AI";
      connectionDot.classList.add("online");
      startButton.innerHTML = `<span class="mic-icon"></span>实时对话已开启`;
      startButton.disabled = true;
      personaInput.disabled = true;
      accessTokenInput.disabled = true;
      $("#identityName").textContent = EW.personaName.toUpperCase();
      $("#identityType").textContent = event.fictional
        ? "虚构演示角色"
        : "已授权 AI 数字分身";
      addSystemNote(event.disclosure);
      browserSpeak(event.disclosure);
      if (EW.microphoneRequested) {
        startMicrophone().catch((error) => {
          EW.microphoneRequested = false;
          startButton.disabled = false;
          startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
          showToast(`麦克风不可用，已保留文字模式：${error.message}`);
        });
      } else {
        startButton.disabled = false;
        startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
      }
      break;
    case "session.state":
      setState(event.state);
      break;
    case "asr.final":
      addMessage("user", event.text);
      $("#latencyLabel").textContent = `ASR ${event.latency_ms} MS`;
      break;
    case "assistant.delta":
      if (!EW.currentAssistant) EW.currentAssistant = addMessage("assistant", "", true);
      EW.currentAssistant.querySelector("p").textContent += event.text;
      transcript.scrollTop = transcript.scrollHeight;
      break;
    case "assistant.final":
      if (EW.currentAssistant) {
        EW.currentAssistant.classList.remove("streaming");
        EW.currentAssistant.querySelector("p").textContent = event.text;
      }
      EW.currentAssistant = null;
      break;
    case "tts.format":
      EW.ttsSampleRate = event.sample_rate;
      break;
    case "tts.browser":
      browserSpeak(event.text);
      break;
    case "av.sync_begin":
      if (EW.syncAudioTurns.size >= MAX_VIDEO_QUEUE) {
        EW.syncAudioTurns.delete(EW.syncAudioTurns.keys().next().value);
      }
      EW.syncAudioTurns.set(event.turn_id, {
        frames: [],
        sampleRate: event.sample_rate,
      });
      break;
    case "playout.clear":
      clearPlayout();
      if (!event.preserve_transcript) {
        if (EW.currentAssistant) EW.currentAssistant.classList.remove("streaming");
        EW.currentAssistant = null;
      }
      break;
    case "turn.cancelled":
      clearPlayout();
      if (EW.currentAssistant) EW.currentAssistant.classList.remove("streaming");
      EW.currentAssistant = null;
      break;
    case "degraded":
      showToast(`${event.component} 已降级到 ${event.fallback}：${event.reason}`);
      break;
    case "error":
      if (
        ["authentication_failed", "session_start_rejected", "disclosure_not_acknowledged"].includes(
          event.code,
        )
      ) {
        EW.startSent = false;
        EW.startAttempt += 1;
        EW.microphoneRequested = false;
        stopMicrophone();
        startButton.disabled = false;
        personaInput.disabled = false;
        accessTokenInput.disabled = false;
      }
      showToast(`${event.code}：${event.message}`);
      break;
    default:
      break;
  }
}

function openSocket() {
  if (EW.ws?.readyState === WebSocket.OPEN) return Promise.resolve();
  if (
    EW.ws?.readyState === WebSocket.CONNECTING &&
    EW.startPending
  ) {
    return EW.startPending;
  }
  if (EW.ws && EW.ws.readyState !== WebSocket.OPEN) {
    const staleSocket = EW.ws;
    staleSocket.onopen = null;
    staleSocket.onclose = null;
    staleSocket.onerror = null;
    staleSocket.onmessage = null;
    try { staleSocket.close(); } catch (_) { /* already closed */ }
    EW.ws = null;
    EW.startPending = false;
    EW.startSent = false;
    EW.startAttempt += 1;
    EW.started = false;
    EW.helloReceived = false;
    EW.microphoneRequested = false;
    connectionDot.classList.remove("online");
    clearPlayout();
    if (EW.currentAssistant) EW.currentAssistant.classList.remove("streaming");
    EW.currentAssistant = null;
    stopMicrophone();
  }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  EW.startPending = new Promise((resolve, reject) => {
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    EW.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      if (EW.ws !== ws) return;
      connectionDot.classList.add("online");
      resolve();
    };
    ws.onmessage = async (message) => {
      if (EW.ws !== ws) return;
      if (typeof message.data === "string") {
        handleEvent(JSON.parse(message.data));
        return;
      }
      try {
        const packet = parsePacket(message.data);
        if (packet.kind === 2) {
          const sync = EW.syncAudioTurns.get(packet.turnId);
          if (sync) sync.frames.push(packet.payload);
          else await playPCM(packet.payload, EW.ttsSampleRate);
        }
        if (packet.kind === 3) {
          queueVideo(packet.payload, packet.turnId, packet.ptsMs);
        }
      } catch (error) {
        showToast(error.message);
      }
    };
    ws.onerror = () => {
      if (EW.ws === ws) EW.startPending = false;
      reject(new Error("无法连接 EchoWeave 网关"));
    };
    ws.onclose = () => {
      if (EW.ws !== ws) return;
      EW.started = false;
      EW.ws = null;
      EW.startPending = false;
      EW.helloReceived = false;
      EW.startSent = false;
      EW.startAttempt += 1;
      EW.microphoneRequested = false;
      connectionDot.classList.remove("online");
      startButton.disabled = false;
      personaInput.disabled = false;
      accessTokenInput.disabled = false;
      startButton.innerHTML = `<span class="mic-icon"></span>重新连接`;
      setState("closed");
      clearPlayout();
      if (EW.currentAssistant) EW.currentAssistant.classList.remove("streaming");
      EW.currentAssistant = null;
      stopMicrophone();
    };
  });
  return EW.startPending;
}

async function beginConversation() {
  if (!disclosureAck.checked) {
    showToast("请先确认你知道对方是 AI 数字分身。");
    return;
  }
  if (!/^[a-z0-9_-]+$/.test(personaInput.value.trim().toLowerCase())) {
    showToast("请先填写有效的 PERSONA ID。");
    return;
  }
  if (startButton.disabled) return;
  startButton.disabled = true;
  EW.microphoneRequested = true;
  try {
    await ensureAudioContext();
    if (EW.started) {
      await startMicrophone();
      startButton.innerHTML = `<span class="mic-icon"></span>实时对话已开启`;
      return;
    }
    await openSocket();
    sendStartControl();
  } catch (error) {
    EW.microphoneRequested = false;
    showToast(error.message);
    startButton.disabled = false;
    if (EW.started) {
      startButton.innerHTML = `<span class="mic-icon"></span>开启麦克风`;
    }
  }
}

async function sendTextTurn() {
  if (EW.textPending) {
    showToast("上一条消息仍在等待会话启动。");
    return;
  }
  const text = textInput.value.trim();
  if (!text) return;
  if (!disclosureAck.checked) {
    showToast("请先确认 AI 身份提示。");
    return;
  }
  EW.textPending = true;
  try {
    await ensureAudioContext();
    await openSocket();
    const attempt = sendStartControl();
    if (attempt === null) throw new Error("会话尚未建立");
    const waitUntilReady = () => new Promise((resolve, reject) => {
      const deadline = performance.now() + 5000;
      const check = () => {
        if (EW.startAttempt !== attempt) {
          reject(new Error("会话启动已失效，请重试"));
          return;
        }
        if (EW.started) {
          resolve();
          return;
        }
        if (performance.now() >= deadline) {
          reject(new Error("会话启动超时"));
          return;
        }
        setTimeout(check, 20);
      };
      check();
    });
    await waitUntilReady();
    if (!sendControl({ type: "text", text })) {
      throw new Error("连接正在关闭，消息未发送");
    }
    addMessage("user", text);
    if (textInput.value.trim() === text) textInput.value = "";
  } catch (error) {
    showToast(error.message);
  } finally {
    EW.textPending = false;
  }
}

startButton.addEventListener("click", beginConversation);
sendButton.addEventListener("click", sendTextTurn);
textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") sendTextTurn();
});
cancelButton.addEventListener("click", () => {
  clearPlayout();
  sendControl({ type: "cancel" });
});
$("#clearButton").addEventListener("click", () => {
  if (EW.currentAssistant) EW.currentAssistant.classList.remove("streaming");
  EW.currentAssistant = null;
  transcript.innerHTML = "";
  addSystemNote("本地界面记录已清空；服务端默认不持久化音视频。");
});
window.addEventListener("beforeunload", () => {
  sendControl({ type: "stop" });
  EW.ws?.close();
  stopMicrophone();
});
