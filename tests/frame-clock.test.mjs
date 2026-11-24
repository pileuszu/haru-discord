import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FrameClock } from '../discord/frame-clock.mjs';

function fixture(options = {}) {
  let time = 0; let scheduled; let nextId = 0; const calls = []; const overruns = [];
  const clock = new FrameClock({
    now: () => time,
    setTimer: (fn, ms) => { scheduled = { fn, ms, id: ++nextId }; return nextId; },
    clearTimer: () => { scheduled = null; },
    onTick: () => { calls.push(time); return options.onTick?.(calls.length); },
    onOverrun: lag => overruns.push(lag),
    ...options.config,
  });
  return { clock, calls, overruns, get scheduled() { return scheduled; }, fireAt(t) { time = t; const item = scheduled; scheduled = null; assert.ok(item, 'expected a scheduled callback'); item.fn(); } };
}

test('clock catches timer drift using ideal monotonic deadlines', () => {
  const f = fixture(); f.clock.start();
  f.fireAt(21); assert.equal(f.calls.length, 1); assert.equal(f.scheduled.ms, 19);
  f.fireAt(42); assert.equal(f.calls.length, 2); assert.equal(f.scheduled.ms, 18);
  f.fireAt(105); assert.equal(f.calls.length, 5); assert.equal(f.scheduled.ms, 15);
  f.clock.stop(); assert.equal(f.scheduled, null);
});

test('catch-up is capped per callback without discarding due frames', () => {
  const f = fixture({ config: { maxCatchUpFrames: 4 } }); f.clock.start();
  f.fireAt(190); assert.equal(f.calls.length, 4); assert.equal(f.scheduled.ms, 0);
  f.fireAt(190); assert.equal(f.calls.length, 8); assert.equal(f.scheduled.ms, 0);
  f.fireAt(190); assert.equal(f.calls.length, 9); assert.equal(f.scheduled.ms, 10);
  f.clock.stop();
});

test('extreme stalls stop the clock and report lag instead of emitting a burst', () => {
  const f = fixture(); f.clock.start(); f.fireAt(220);
  assert.deepEqual(f.calls, []); assert.deepEqual(f.overruns, [200]);
  assert.equal(f.scheduled, null); assert.equal(f.clock.running, false);
});

test('backpressured output rebases the deadline rather than emitting stale frames', () => {
  const f = fixture({ onTick: n => n === 1 ? false : true }); f.clock.start();
  f.fireAt(95); assert.equal(f.calls.length, 1); assert.equal(f.scheduled.ms, 20);
  f.fireAt(115); assert.equal(f.calls.length, 2); assert.equal(f.scheduled.ms, 20);
  f.clock.stop();
});

test('stop during a callback prevents later catch-up frames', () => {
  let clock; let calls = 0; let scheduled;
  clock = new FrameClock({ now: () => 100, setTimer: fn => { scheduled = fn; return 1; }, clearTimer: () => {}, onTick: () => { calls++; clock.stop(); }, onOverrun: () => {} });
  clock.start(); clock.deadline = 20; scheduled();
  assert.equal(calls, 1); assert.equal(clock.running, false);
});
