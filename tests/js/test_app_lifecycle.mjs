import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

function classList() {
  return {
    add() {},
    remove() {},
    toggle() {},
  };
}

function element() {
  const listeners = new Map();
  const node = {
    checked: false,
    children: [],
    classList: classList(),
    dataset: {},
    disabled: false,
    hidden: false,
    innerHTML: "",
    style: {},
    textContent: "",
    value: "",
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    append(...children) {
      this.children.push(...children);
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    focus() {},
    load() {},
    pause() {},
    prepend(child) {
      this.children.unshift(child);
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    removeAttribute() {},
    replaceChildren(...children) {
      this.children = [...children];
    },
    setAttribute() {},
  };
  Object.defineProperty(node, "lastElementChild", {
    get() {
      return this.children.at(-1) || null;
    },
  });
  return node;
}

const ids = [
  "startButton",
  "cancelButton",
  "endButton",
  "sendButton",
  "textInput",
  "transcript",
  "disclosureAck",
  "personaInput",
  "accessTokenInput",
  "syntheticAvatar",
  "avatarVideo",
  "stateLabel",
  "connectionDot",
  "toast",
  "networkQuality",
  "networkRtt",
  "turnLatency",
  "firstTokenLatency",
  "pipelineStages",
  "capabilityList",
  "sessionSequence",
  "recoveryAction",
  "activityLog",
  "clearButton",
  "mouth",
  "identityName",
  "identityType",
  "latencyLabel",
];
const elements = new Map(ids.map((id) => [id, element()]));
elements.get("personaInput").value = "demo";
const recoveryLabel = element();
const recoveryDetail = element();
elements.get("recoveryAction").querySelector = (selector) =>
  selector.includes("strong") ? recoveryLabel : recoveryDetail;

let resolveWorkletLoad;
let markWorkletLoadStarted;
const workletLoadGate = new Promise((resolve) => {
  resolveWorkletLoad = resolve;
});
const workletLoadStarted = new Promise((resolve) => {
  markWorkletLoadStarted = resolve;
});
let workletDisconnects = 0;
let workletStops = 0;
let sourceDisconnects = 0;
let gainCreations = 0;
let trackStops = 0;
let fakeNow = 1_000;
let latestIntervalCallback = null;
let intervalId = 0;

class FakeDate extends Date {
  static now() {
    return fakeNow;
  }
}

class MockAudioWorkletNode {
  constructor() {
    this.port = {
      onmessage: null,
      postMessage(message) {
        if (message?.type === "stop") workletStops += 1;
      },
    };
  }

  connect() {}

  disconnect() {
    workletDisconnects += 1;
  }
}

const audioContext = {
  audioWorklet: {
    addModule() {
      markWorkletLoadStarted();
      return workletLoadGate;
    },
  },
  createGain() {
    gainCreations += 1;
    return { connect() {}, disconnect() {}, gain: { value: 0 } };
  },
  createMediaStreamSource() {
    return {
      connect() {},
      disconnect() {
        sourceDisconnects += 1;
      },
    };
  },
  destination: {},
  state: "running",
};
const stream = {
  getTracks() {
    return [
      {
        stop() {
          trackStops += 1;
        },
      },
    ];
  },
};

const documentMock = {
  baseURI: "http://127.0.0.1:8765/",
  createElement() {
    return element();
  },
  querySelector(selector) {
    return selector.startsWith("#") ? elements.get(selector.slice(1)) || null : null;
  },
};
const windowMock = {
  addEventListener() {},
  clearInterval() {},
  clearTimeout,
  setInterval(callback) {
    latestIntervalCallback = callback;
    intervalId += 1;
    return intervalId;
  },
  setTimeout,
  speechSynthesis: { cancel() {}, speak() {}, speaking: false },
};
const sandbox = {
  ArrayBuffer,
  AudioContext: class {},
  AudioWorkletNode: MockAudioWorkletNode,
  Blob,
  DataView,
  Date: FakeDate,
  Error,
  Float32Array,
  Int16Array,
  JSON,
  Map,
  Math,
  Number,
  Promise,
  Set,
  SpeechSynthesisUtterance: class {},
  URL,
  URLSearchParams,
  Uint8Array,
  WebSocket: { CONNECTING: 0, OPEN: 1 },
  console,
  document: documentMock,
  history: { replaceState() {} },
  location: { hash: "", host: "127.0.0.1:8765", pathname: "/", protocol: "http:", search: "" },
  navigator: { mediaDevices: { getUserMedia: async () => stream } },
  performance: { now: () => 1 },
  setInterval,
  setTimeout,
  window: windowMock,
};
vm.createContext(sandbox);
const source = await readFile(new URL("../../src/echoweave/web/app.js", import.meta.url), "utf8");
vm.runInContext(
  `${source}\n;globalThis.__appTest = { EW, startHeartbeat, startMicrophone, stopHeartbeat, stopMicrophone };`,
  sandbox,
  { filename: "app.js" },
);

const { EW, startHeartbeat, startMicrophone, stopHeartbeat, stopMicrophone } = sandbox.__appTest;
EW.audioContext = audioContext;
EW.microphoneRequested = true;
EW.started = true;
EW.ws = { readyState: 1 };

const pendingStart = startMicrophone();
await workletLoadStarted;
stopMicrophone();
resolveWorkletLoad();
await pendingStart;

assert.equal(EW.micStream, null, "a stale request must not publish its MediaStream");
assert.equal(EW.micProcessor, null, "a stale request must not publish its processor");
assert.equal(gainCreations, 0, "a stale request must stop before connecting an output graph");
assert.equal(trackStops, 1, "the locally-held microphone track must be stopped");
assert.equal(sourceDisconnects, 1, "the locally-held media source must be disconnected");
assert.equal(workletStops, 1, "the stale worklet must receive its stop control");
assert.equal(workletDisconnects, 1, "the stale worklet must be disconnected");

const socketA = {
  closes: [],
  readyState: 1,
  sends: [],
  close(code, reason) {
    this.closes.push([code, reason]);
  },
  send(message) {
    this.sends.push(JSON.parse(message));
  },
};
EW.micStream = stream;
EW.microphoneRequested = true;
EW.started = true;
EW.ws = socketA;
startHeartbeat(socketA);
const socketAHeartbeat = latestIntervalCallback;
assert.equal(socketA.sends.length, 1, "heartbeat must ping immediately");
fakeNow += 30_001;
socketAHeartbeat();
assert.deepEqual(socketA.closes, [[4000, "heartbeat_timeout"]]);
assert.equal(EW.started, false, "heartbeat timeout must mark the session stopped");
assert.equal(EW.microphoneRequested, false, "heartbeat timeout must revoke microphone intent");
assert.equal(trackStops, 2, "heartbeat timeout must stop the active microphone track");
assert.equal(EW.micStream, null, "heartbeat timeout must release the active stream");

const socketB = {
  closes: [],
  readyState: 1,
  sends: [],
  close(code, reason) {
    this.closes.push([code, reason]);
  },
  send(message) {
    this.sends.push(JSON.parse(message));
  },
};
fakeNow += 100;
EW.ws = socketB;
EW.started = true;
EW.microphoneRequested = false;
startHeartbeat(socketB);
assert.equal(socketB.sends.length, 1);
socketAHeartbeat();
assert.equal(socketB.closes.length, 0, "a stale socket epoch must not close the replacement");
stopHeartbeat(socketB);
