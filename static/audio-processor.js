(function () {
  "use strict";

  function msToSamples(ms) {
    return Math.round(ms * sampleRate / 1000);
  }

  class MoshiProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      console.log("Moshi processor lives", currentFrame, sampleRate);
      console.log(currentTime);
      this.initState();
      this.port.onmessage = (e) => {
        if (e.data.type === "reset") {
          console.log("Reset audio processor state.");
          this.initState();
          return;
        }
        const frame = e.data.frame;
        this.frames.push(frame);
        this.totalBuffered += frame.length;

        // Start playback once we have enough buffered
        if (!this.started && this.totalBuffered >= this.initialBufferSamples) {
          this.started = true;
          console.log("Playback starting, buffered:", (this.totalBuffered / sampleRate * 1000).toFixed(0), "ms");
        }

        // Drop oldest frames if buffer is too large
        while (this.totalBuffered > this.maxBufferSamples && this.frames.length > 1) {
          const dropped = this.frames.shift();
          const dropLen = dropped.length - this.offsetInFirstBuffer;
          this.totalBuffered -= dropLen;
          this.offsetInFirstBuffer = 0;
        }
      };
    }

    initState() {
      this.frames = [];
      this.offsetInFirstBuffer = 0;
      this.totalBuffered = 0;
      this.started = false;
      // Buffer 150ms before starting playback (handles network jitter)
      this.initialBufferSamples = msToSamples(150);
      // Max buffer 500ms before dropping (prevents runaway latency)
      this.maxBufferSamples = msToSamples(500);
    }

    process(inputs, outputs, parameters) {
      const output = outputs[0][0];
      if (!this.started || this.frames.length === 0) {
        // Output silence
        output.fill(0);
        // If we were playing and ran out, reset to re-buffer
        if (this.started && this.frames.length === 0 && this.totalBuffered <= 0) {
          console.log("Buffer underrun, re-buffering...");
          this.started = false;
          this.totalBuffered = 0;
        }
        return true;
      }

      let written = 0;
      while (written < output.length && this.frames.length > 0) {
        const frame = this.frames[0];
        const available = frame.length - this.offsetInFirstBuffer;
        const needed = output.length - written;
        const toCopy = Math.min(available, needed);

        output.set(
          frame.subarray(this.offsetInFirstBuffer, this.offsetInFirstBuffer + toCopy),
          written
        );

        this.offsetInFirstBuffer += toCopy;
        written += toCopy;
        this.totalBuffered -= toCopy;

        if (this.offsetInFirstBuffer >= frame.length) {
          this.frames.shift();
          this.offsetInFirstBuffer = 0;
        }
      }

      // Fill remaining with silence if not enough data
      if (written < output.length) {
        output.fill(0, written);
      }

      return true;
    }
  }

  registerProcessor("moshi-processor", MoshiProcessor);
})();
