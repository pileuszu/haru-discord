import { test } from 'node:test';
import assert from 'node:assert/strict';
import { VoiceMixer } from '../discord/mixer.mjs';

function pcm(left, right = left, bytes = 3840) {
  const b = Buffer.alloc(bytes);
  for (let i = 0; i < bytes; i += 4) { b.writeInt16LE(left, i); b.writeInt16LE(right, i + 2); }
  return b;
}
test('one speaker retains its mono level including stereo fold-down', () => {
  const m = new VoiceMixer(); m.add('one'); m.push('one', pcm(16384, 8192));
  assert.equal(m.frame().readFloatLE(0), .375); assert.equal(m.pendingBytes, 0);
});
test('overlapping speakers are summed without attenuating individual speech', () => {
  const m = new VoiceMixer(); m.add('a'); m.add('b'); m.push('a', pcm(8192)); m.push('b', pcm(4096));
  assert.equal(m.frame().readFloatLE(0), .375);
});
test('overlapping loud speakers are clipped instead of wrapping', () => {
  const m = new VoiceMixer(); m.add('a'); m.add('b'); m.push('a', pcm(30000)); m.push('b', pcm(30000));
  assert.equal(m.frame().readFloatLE(0), 1);
  m.push('a', pcm(-30000)); m.push('b', pcm(-30000)); assert.equal(m.frame().readFloatLE(0), -1);
});
test('removing one speaker drops its pending data and keeps the other', () => {
  const m = new VoiceMixer(); const q = m.add('a'); assert.equal(m.add('a'), q); m.add('b');
  m.push('a', pcm(4096)); m.push('b', pcm(8192)); assert.equal(m.remove('a'), true);
  assert.equal(m.remove('a'), false); assert.equal(m.push('a', pcm(4096)), false);
  assert.equal(m.frame().readFloatLE(0), .25); assert.equal(m.get('a'), undefined);
  m.clear(); assert.equal(m.pendingBytes, 0); assert.equal(m.get('b'), undefined);
});
test('silence is produced without consuming partial speaker frames', () => {
  const m = new VoiceMixer();m.add('a');m.push('a', pcm(8192,8192,1920));
  assert.deepEqual(m.frame(), Buffer.alloc(3840)); assert.equal(m.pendingBytes, 1920);
  m.push('a', pcm(8192,8192,1920)); assert.equal(m.frame().readFloatLE(0), .25);
  assert.deepEqual(m.frame(), Buffer.alloc(3840));
});
test('speaker queues cap latency and preserve aggregate drop counter after removal', () => {
  const m=new VoiceMixer({maxFrames:2});m.add('a');m.push('a',Buffer.concat([pcm(4096),pcm(8192),pcm(12288)]));
  assert.equal(m.droppedFrames,1);assert.equal(m.pendingBytes,7680);
  assert.equal(m.frame().readFloatLE(0),.25);assert.equal(m.frame().readFloatLE(0),.375);
  m.remove('a');assert.equal(m.droppedFrames,1);
});
