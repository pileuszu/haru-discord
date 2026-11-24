import test from 'node:test';
import assert from 'node:assert/strict';
import { VoiceMetrics } from '../discord/voice-metrics.mjs';
import { PcmQueue } from '../discord/audio.mjs';

const pcm = values => {
  const data = Buffer.alloc(values.length * 4);
  values.forEach((n, i) => data.writeFloatLE(n, i * 4));
  return data;
};

test('five-second windows report separately weighted input/output levels and reset', () => {
  const m = new VoiceMetrics();
  m.observeAudio('input', pcm([1, 0]));
  m.observeAudio('input', pcm([-1, NaN]));
  m.observeAudio('output', pcm([0.5, -0.5]));
  const first = m.snapshot();
  assert.ok(Math.abs(first.input.rms - Math.sqrt(2 / 3)) < 1e-7);
  assert.equal(first.input.peak, 1);
  assert.equal(first.input.samples, 3);
  assert.equal(first.output.rms, 0.5);
  assert.equal(first.output.peak, 0.5);
  m.observeAudio('input', pcm([0]));
  const second = m.snapshot();
  assert.equal(second.input.rms, 0);
  assert.equal(second.output.samples, 0);
});

test('counts actual playback queue underruns separately from overflow drops', () => {
  const m = new VoiceMetrics();
  const q = new PcmQueue(8, 1);
  q.push(Buffer.alloc(16));
  assert.equal(q.droppedFrames, 1);
  m.popPlayback(q);
  assert.equal(m.playbackUnderflows, 0);
  assert.deepEqual(m.popPlayback(q), Buffer.alloc(8));
  assert.equal(m.playbackUnderflows, 1);
  assert.equal(q.droppedFrames, 1);
});

test('counts model text characters without retaining or exposing content', () => {
  const m = new VoiceMetrics();
  m.observeText('안녕 \n🌞');
  assert.equal(m.textCharacters, 3);
  assert.ok(!JSON.stringify(m).includes('안녕'));
  assert.ok(!m.status().includes('안녕'));
});

test('frame processing timings use a bounded recent history', () => {
  const metrics = new VoiceMetrics();
  metrics.observeStep(80); metrics.observeStep(150);
  assert.equal(metrics.stepSummary.p95, 150);
  for (let i = 0; i < 300; i++) metrics.observeStep(i);
  assert.equal(metrics.stepSummary.count, 256);
  assert.equal(metrics.stepSummary.p50, 171);
  assert.equal(metrics.stepSummary.p95, 287);
  assert.match(metrics.status(), /프레임 p50\/p95 171\.0\/287\.0ms/);
  assert.throws(() => metrics.observeStep(NaN), /Invalid/);
});
