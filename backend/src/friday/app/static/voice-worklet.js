// Turns the (echo-cancelled) microphone into 20 ms frames of 16 kHz 16-bit mono
// for FRIDAY. Runs on the browser's audio thread.
class FridayMic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 16000; // input samples per output sample
    this.pos = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(320);
    this.n = 0;
  }

  process(inputs) {
    // No input (some browsers do this while the microphone is muted or quiet)
    // counts as silence, so FRIDAY still hears that you've stopped talking.
    const input = (inputs[0] && inputs[0][0]) || this.quiet || (this.quiet = new Float32Array(128));
    for (let i = 0; i < input.length; i++) {
      // Averaging the samples that make up one output sample also filters out
      // sounds too high for 16 kHz.
      this.sum += input[i];
      this.count++;
      this.pos += 1;
      if (this.pos >= this.step) {
        this.pos -= this.step;
        const s = Math.max(-1, Math.min(1, this.sum / this.count));
        this.sum = 0;
        this.count = 0;
        this.frame[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.frame.length) {
          this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
          this.frame = new Int16Array(320);
          this.n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("friday-mic", FridayMic);
