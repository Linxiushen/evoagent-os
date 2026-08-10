import assert from "node:assert/strict";

let ProcessorClass;
globalThis.sampleRate = 48000;
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.port = { onmessage: null, postMessage: () => {} };
  }
};
globalThis.registerProcessor = (name, processorClass) => {
  assert.equal(name, "echoweave-mic-processor");
  ProcessorClass = processorClass;
};

await import("../../src/echoweave/web/mic-worklet.js");
assert.ok(ProcessorClass);

const processor = new ProcessorClass();
const frames = [];
processor.port.postMessage = (message) => {
  assert.equal(message.type, "pcm16");
  frames.push(new Int16Array(message.pcm));
};

for (let block = 0; block < 375; block += 1) {
  const input = new Float32Array(128);
  for (let index = 0; index < input.length; index += 1) {
    const sample = block * input.length + index;
    input[index] = 0.5 * Math.sin((2 * Math.PI * 440 * sample) / sampleRate);
  }
  const output = new Float32Array(128);
  assert.equal(processor.process([[input]], [[output]]), true);
  assert.ok(output.every((sample) => sample === 0));
}

assert.equal(frames.length, 50);
assert.ok(frames.every((frame) => frame.length === 320));
assert.ok(frames.some((frame) => frame.some((sample) => sample !== 0)));

processor.port.onmessage({ data: { type: "stop" } });
assert.equal(
  processor.process([[new Float32Array(128)]], [[new Float32Array(128)]]),
  false,
);
