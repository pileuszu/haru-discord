/** Aggregate levels only: never retain audio samples or model text. */
export class VoiceMetrics {
  constructor() {
    this.input = this._empty();
    this.output = this._empty();
    this.latest = null;
    this.textCharacters = 0;
    this.playbackUnderflows = 0;
    this.stepTimes = [];
  }

  _empty() { return { count: 0, sumSquares: 0, peak: 0 }; }

  observeAudio(direction, pcm) {
    if (direction !== 'input' && direction !== 'output') throw new RangeError('Invalid audio direction');
    if (!Buffer.isBuffer(pcm) || pcm.length % 4 !== 0) throw new TypeError('Expected aligned float32 PCM');
    const bucket = this[direction];
    for (let offset = 0; offset < pcm.length; offset += 4) {
      const sample = pcm.readFloatLE(offset);
      if (!Number.isFinite(sample)) continue;
      const abs = Math.abs(sample);
      bucket.count++;
      bucket.sumSquares += sample * sample;
      if (abs > bucket.peak) bucket.peak = abs;
    }
  }

  observeStep(milliseconds) {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) throw new RangeError('Invalid model step time');
    this.stepTimes.push(milliseconds);
    if (this.stepTimes.length > 256) this.stepTimes.shift();
  }

  get stepSummary() {
    if (!this.stepTimes.length) return null;
    const sorted = [...this.stepTimes].sort((a, b) => a - b);
    const percentile = fraction => sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
    return { p50: percentile(0.5), p95: percentile(0.95), count: sorted.length };
  }

  observeText(text) {
    // Count code points without storing speech text or logging its contents.
    for (const char of text) if (!/\s/u.test(char)) this.textCharacters++;
  }

  popPlayback(queue) {
    if (queue.pendingBytes < queue.frameBytes) this.playbackUnderflows++;
    return queue.popFrame();
  }

  snapshot() {
    const level = bucket => ({
      rms: bucket.count ? Math.sqrt(bucket.sumSquares / bucket.count) : 0,
      peak: bucket.peak,
      samples: bucket.count,
    });
    this.latest = { input: level(this.input), output: level(this.output) };
    this.input = this._empty();
    this.output = this._empty();
    return this.latest;
  }

  status() {
    const data = this.latest;
    const step = this.stepSummary;
    const timing = step ? ` · 최근 모델 프레임 p50/p95 ${step.p50.toFixed(1)}/${step.p95.toFixed(1)}ms` : '';
    if (!data) return `입출력 레벨 측정 중 · 텍스트 글자 ${this.textCharacters} · 재생 부족 ${this.playbackUnderflows}${timing}`;
    const fmt = x => x.toFixed(4);
    return `최근 5초 입력 rms/peak ${fmt(data.input.rms)}/${fmt(data.input.peak)} · 출력 rms/peak ${fmt(data.output.rms)}/${fmt(data.output.peak)} · 텍스트 글자 ${this.textCharacters} · 재생 부족 ${this.playbackUnderflows}${timing}`;
  }
}
