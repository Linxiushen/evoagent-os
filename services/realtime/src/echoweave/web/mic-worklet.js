class EchoWeaveMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.outputRate = 16000;
    this.frameSamples = 320;
    this.step = sampleRate / this.outputRate;
    this.inputCursor = 0;
    this.nextOutputPosition = 0;
    this.previousSample = 0;
    this.hasPreviousSample = false;
    this.frame = new Int16Array(this.frameSamples);
    this.frameOffset = 0;
    this.active = true;
    this.port.onmessage = (event) => {
      if (event.data?.type === "stop") this.active = false;
    };
  }

  emit(value) {
    const clipped = Math.max(-1, Math.min(1, value));
    this.frame[this.frameOffset] = clipped < 0 ? clipped * 32768 : clipped * 32767;
    this.frameOffset += 1;
    if (this.frameOffset !== this.frameSamples) return;

    const packet = this.frame.buffer;
    this.port.postMessage({ type: "pcm16", pcm: packet }, [packet]);
    this.frame = new Int16Array(this.frameSamples);
    this.frameOffset = 0;
  }

  process(inputs, outputs) {
    for (const output of outputs) {
      for (const channel of output) channel.fill(0);
    }
    if (!this.active) return false;

    const input = inputs[0]?.[0];
    if (!input?.length) return true;
    const blockStart = this.inputCursor;
    const blockEnd = blockStart + input.length;
    if (!this.hasPreviousSample) {
      this.previousSample = input[0];
      this.hasPreviousSample = true;
      this.nextOutputPosition = blockStart;
    }

    while (this.nextOutputPosition <= blockEnd - 1) {
      const localPosition = this.nextOutputPosition - blockStart;
      let left;
      let right;
      let mix;
      if (localPosition < 0) {
        left = this.previousSample;
        right = input[0];
        mix = localPosition + 1;
      } else {
        const leftIndex = Math.floor(localPosition);
        const rightIndex = Math.min(input.length - 1, leftIndex + 1);
        left = input[leftIndex];
        right = input[rightIndex];
        mix = localPosition - leftIndex;
      }
      const value = left * (1 - mix) + right * mix;
      this.emit(value);
      this.nextOutputPosition += this.step;
    }
    this.previousSample = input[input.length - 1];
    this.inputCursor = blockEnd;
    return true;
  }
}

registerProcessor("echoweave-mic-processor", EchoWeaveMicProcessor);
