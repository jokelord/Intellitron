class AsrPcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const configuredFrameSamples = options.processorOptions?.frameSamples;
    this.frameSamples = Number.isInteger(configuredFrameSamples)
      ? configuredFrameSamples
      : 320;
    const configuredMaxBufferedFrames = options.processorOptions?.maxBufferedFrames;
    if (!Number.isInteger(configuredMaxBufferedFrames) || configuredMaxBufferedFrames <= 0) {
      throw new Error("maxBufferedFrames must be a positive integer");
    }
    this.maxBufferedFrames = configuredMaxBufferedFrames;
    this.frame = new Float32Array(this.frameSamples);
    this.frameOffset = 0;
    this.inFlightFrames = 0;
    this.overflowEmitted = false;
    this.paused = false;
    this.captureEpoch = 0;
    this.port.onmessage = ({ data: message }) => {
      if (message && message.type === "pause") {
        if (Number.isInteger(message.epoch)) this.captureEpoch = message.epoch;
        this.paused = true;
        this.frameOffset = 0;
        this.inFlightFrames = 0;
        this.overflowEmitted = false;
      } else if (message && message.type === "resume") {
        if (Number.isInteger(message.epoch)) this.captureEpoch = message.epoch;
        this.paused = false;
        this.frameOffset = 0;
        this.inFlightFrames = 0;
        this.overflowEmitted = false;
      } else if (message && message.type === "frame_ack") {
        if (message.epoch !== this.captureEpoch) return;
        this.inFlightFrames = Math.max(0, this.inFlightFrames - 1);
      }
    };
  }

  process(inputs, outputs) {
    for (const output of outputs) {
      for (const channel of output) channel.fill(0);
    }
    if (this.paused) return true;

    const input = inputs[0]?.[0];
    if (!input) return true;
    let inputOffset = 0;
    while (inputOffset < input.length) {
      const copyLength = Math.min(
        input.length - inputOffset,
        this.frameSamples - this.frameOffset,
      );
      this.frame.set(
        input.subarray(inputOffset, inputOffset + copyLength),
        this.frameOffset,
      );
      inputOffset += copyLength;
      this.frameOffset += copyLength;

      if (this.frameOffset === this.frameSamples) {
        if (this.inFlightFrames >= this.maxBufferedFrames) {
          this.paused = true;
          this.frameOffset = 0;
          if (!this.overflowEmitted) {
            this.overflowEmitted = true;
            this.port.postMessage({ type: "transport_overflow", epoch: this.captureEpoch });
          }
          return true;
        }
        const frame = this.frame;
        this.port.postMessage({ type: "pcm_frame", epoch: this.captureEpoch, samples: frame }, [frame.buffer]);
        this.inFlightFrames += 1;
        this.frame = new Float32Array(this.frameSamples);
        this.frameOffset = 0;
      }
    }
    return true;
  }
}

registerProcessor("asr-pcm-capture", AsrPcmCaptureProcessor);
