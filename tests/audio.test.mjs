import test from 'node:test';
import assert from 'node:assert/strict';
import { PcmQueue, pcm16StereoToFloat32Mono, float32MonoToPcm16Stereo } from '../discord/audio.mjs';

function floats(values) {
  const result = Buffer.alloc(values.length * 4);
  values.forEach((value, index) => result.writeFloatLE(value, index * 4));
  return result;
}

test('stereo input mixes channels and preserves PCM endpoints', () => {
  const stereo = Buffer.alloc(16);
  [-32768, -32768, 32767, 32767, -16384, 16384, 8192, 24576]
    .forEach((value, index) => stereo.writeInt16LE(value, index * 2));
  const mono = pcm16StereoToFloat32Mono(stereo);
  assert.deepEqual([0, 4, 8, 12].map((offset) => mono.readFloatLE(offset)), [-1, 32767 / 32768, 0, 0.5]);
});

test('float output duplicates channels, clamps, and silences non-finite samples', () => {
  const pcm = float32MonoToPcm16Stereo(floats([-2, -0.5, 0, 0.5, 2, NaN, Infinity, -Infinity]));
  const expected = [-32768, -16384, 0, 16384, 32767, 0, 0, 0];
  expected.forEach((sample, index) => {
    assert.equal(pcm.readInt16LE(index * 4), sample);
    assert.equal(pcm.readInt16LE(index * 4 + 2), sample);
  });
});

test('converters reject incomplete sample frames', () => {
  assert.throws(() => pcm16StereoToFloat32Mono(Buffer.alloc(2)), /align/);
  assert.throws(() => float32MonoToPcm16Stereo(Buffer.alloc(6)), /align/);
  assert.throws(() => float32MonoToPcm16Stereo(new Uint8Array(4)), /Buffer/);
  assert.equal(pcm16StereoToFloat32Mono(Buffer.alloc(0)).length, 0);
});

test('FIFO joins arbitrary aligned chunks and retains partial frames on underflow', () => {
  const queue = new PcmQueue(12, 3);
  const source = Buffer.from(Array.from({ length: 24 }, (_, i) => i + 1));
  queue.push(source.subarray(0, 4));
  assert.deepEqual(queue.popFrame(), Buffer.alloc(12));
  assert.equal(queue.pendingBytes, 4);
  queue.push(source.subarray(4, 16));
  assert.deepEqual(queue.popFrame(), source.subarray(0, 12));
  queue.push(source.subarray(16));
  assert.deepEqual(queue.popFrame(), source.subarray(12));
  assert.equal(queue.pendingBytes, 0);
});

test('FIFO discards oldest complete frames to bound playback latency', () => {
  const queue = new PcmQueue(8, 2);
  const source = Buffer.from(Array.from({ length: 28 }, (_, i) => i));
  queue.push(source.subarray(0, 12));
  assert.equal(queue.push(source.subarray(12, 20)), 1);
  assert.equal(queue.pendingBytes, 12);
  assert.deepEqual(queue.popFrame(), source.subarray(8, 16));
  queue.push(source.subarray(20));
  assert.deepEqual(queue.popFrame(), source.subarray(16, 24));
  assert.equal(queue.pendingBytes, 4);
  assert.equal(queue.droppedFrames, 1);
  assert.equal(queue.dropCount, 1);
  assert.equal(queue.droppedBytes, 8);
});

test('FIFO owns retained bytes and bounds one oversized push', () => {
  const queue = new PcmQueue(8, 2);
  const huge = Buffer.alloc(804, 7);
  assert.equal(queue.push(huge), 99);
  assert.equal(queue.pendingBytes, 12);
  huge.fill(1);
  assert.deepEqual(queue.popFrame(), Buffer.alloc(8, 7));
  queue.clear();
  assert.equal(queue.pendingBytes, 0);
  assert.throws(() => queue.push(Buffer.alloc(1)), /align/);
  assert.throws(() => new PcmQueue(3), /multiple/);
  assert.throws(() => new PcmQueue(8, 0), /positive/);
});
