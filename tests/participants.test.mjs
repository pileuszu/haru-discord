import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { Participants } from '../discord/participants.mjs';

function fixture() {
  const speaking = new EventEmitter();
  speaking.users = new Map();
  const subscriptions = new Map();
  const receiver = { speaking, subscriptions, subscribe(id, options) {
    assert.equal(options.end.behavior, 0);
    if (subscriptions.has(id)) return subscriptions.get(id);
    const s = new PassThrough(); subscriptions.set(id, s);
    s.once('close', () => subscriptions.delete(id));
    return s;
  } };
  const guild = { voiceStates: { cache: new Map() }, members: { cache: new Map(), fetch: async id => guild.members.cache.get(id) } };
  const sink = { members: new Set(), frames: [], add(id) { this.members.add(id); }, remove(id) { this.members.delete(id); }, push(id, pcm) { this.frames.push([id, Buffer.from(pcm)]); } };
  const packets = []; const errors = [];
  const p = new Participants({ receiver, guild, channelId: 'room', botId: 'self', sink, onPacket: (_, id) => packets.push(id), onError: (error, id) => errors.push([id, error.message]), makeDecoder: () => new PassThrough() });
  const member = (id, bot = false, channel = 'room') => {
    const value = { user: { id, bot } };
    guild.voiceStates.cache.set(id, { channelId: channel, member: value });
    guild.members.cache.set(id, value);
  };
  return { p, receiver, speaking, subscriptions, guild, sink, packets, errors, member };
}

test('existing and new human speakers have independent subscriptions and PCM', async () => {
  const f = fixture(); f.member('one'); f.member('two'); f.member('self', true); f.member('bot', true); f.member('other-room', false, 'elsewhere');
  f.speaking.users.set('one', Date.now()); f.p.start();
  for (const id of ['two', 'self', 'bot', 'other-room', 'two']) f.speaking.emit('start', id);
  assert.deepEqual([...f.sink.members].sort(), ['one', 'two']);
  f.subscriptions.get('one').write(Buffer.from([1, 0, 2, 0]));
  f.subscriptions.get('two').write(Buffer.from([3, 0, 4, 0]));
  assert.deepEqual(f.packets, ['one', 'two']);
  assert.deepEqual(f.sink.frames.map(([id]) => id), ['one', 'two']);
  const s1 = f.subscriptions.get('one'); const s2 = f.subscriptions.get('two');
  f.p.remove('one'); assert.equal(s1.destroyed, true); assert.equal(s2.destroyed, false); assert.deepEqual([...f.sink.members], ['two']);
  f.p.stop(); assert.equal(s2.destroyed, true); assert.equal(f.speaking.listenerCount('start'), 0); assert.equal(f.sink.members.size, 0);
});

test('individual stream error cleans up only that participant', async () => {
  const f = fixture(); f.member('one'); f.member('two'); f.p.start(); f.speaking.emit('start', 'one'); f.speaking.emit('start', 'two');
  const one = f.subscriptions.get('one'); const closed = new Promise(resolve => one.once('close', resolve));
  one.destroy(new Error('decrypt failed')); await closed;
  assert.deepEqual(f.errors, [['one', 'decrypt failed']]);
  assert.equal(f.p.active.has('one'), false); assert.equal(f.p.active.has('two'), true);
  assert.equal(f.sink.members.has('one'), false); f.p.stop();
});

test('uncached participant is fetched once and verified, departed user is ignored', async () => {
  const f = fixture(); let finish; let fetchCount = 0;
  f.guild.voiceStates.cache.set('pending', { channelId: 'room', member: null });
  f.guild.members.fetch = () => { fetchCount++; return new Promise(resolve => { finish = resolve; }); };
  f.p.start(); f.speaking.emit('start', 'pending'); f.speaking.emit('start', 'pending');
  await Promise.resolve(); assert.equal(fetchCount, 1);
  f.guild.voiceStates.cache.set('pending', { channelId: 'elsewhere', member: null });
  finish({ user: { id: 'pending', bot: false } }); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.p.active.size, 0); assert.equal(f.subscriptions.size, 0); f.p.stop();
});

test('stop cancels in-flight member resolution', async () => {
  const f = fixture(); let finish;
  f.guild.voiceStates.cache.set('pending', { channelId: 'room', member: null });
  f.guild.members.fetch = () => new Promise(resolve => { finish = resolve; });
  f.p.start(); f.speaking.emit('start', 'pending'); await Promise.resolve(); f.p.stop();
  finish({ user: { id: 'pending', bot: false } }); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.p.active.size, 0); assert.equal(f.subscriptions.size, 0);
});
