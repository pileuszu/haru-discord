import test from 'node:test';
import assert from 'node:assert/strict';
import { once, on } from 'node:events';
import http from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { WebSocketServer } from 'ws';
import { parseConfig, ROOT } from '../discord/config.mjs';
import { GemmaVoiceSession } from '../discord/gemma-session.mjs';
import { streamEnvironment } from '../discord/worker-env.mjs';

test('a Gemma voice session forwards separate speakers and clears queued Discord PCM on correction', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'gv-'));
  const socketPath = join(dir, 'v.sock');
  const server = http.createServer();
  const wss = new WebSocketServer({ server, path: '/voice' });
  const session = new GemmaVoiceSession({ server: { socketPath }, trace: { write() {} } });
  t.after(async () => {
    session.stop('test');
    for (const ws of wss.clients) ws.terminate();
    await new Promise(resolve => wss.close(resolve));
    await new Promise(resolve => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(socketPath, resolve); });
  const connected = once(wss, 'connection');
  const fail = error => assert.fail(error);
  const opened = session.openModel(fail);
  const [peer] = await connected;
  const messages = on(peer, 'message');
  peer.send(JSON.stringify({ type: 'ready' }));
  await opened;
  session.startedAt = Date.now();
  for (const [state, activity] of [['searching', '검색 중'], ['wake_waiting', '검색 중'], ['search_finished', '답변 생성 중']]) {
    const status = once(session.bridge, 'status');
    peer.send(JSON.stringify({ type: 'status', state }));
    await status;
    assert.ok(session.status().startsWith(activity));
  }
  const sink = session.inputSink(fail);
  const pcm = Buffer.alloc(3840);
  for (let i = 0; i < pcm.length; i += 4) {
    pcm.writeInt16LE(-1000, i);
    pcm.writeInt16LE(3000, i + 2);
  }
  sink.push('one', pcm.subarray(0, 400));
  sink.push('two', Buffer.alloc(3840));
  sink.push('one', pcm.subarray(400));
  async function read() { return JSON.parse((await messages.next()).value[0].toString()); }
  const first = await read(), second = await read();
  assert.equal(first.speaker, 'two');
  assert.equal(second.speaker, 'one');
  assert.equal(second.overlap, false);
  assert.equal(Buffer.from(second.audio, 'base64').length, 1920);
  assert.equal(Buffer.from(second.audio, 'base64').readInt16LE(0), 1000);
  const frame = once(session.bridge, 'audio');
  peer.send(pcm);
  await frame;
  assert.equal(session.output.pendingBytes, 3840);
  sink.push('one', pcm);
  assert.equal((await read()).overlap, true);
  const cleared = once(session.bridge, 'clear');
  peer.send(JSON.stringify({ type: 'clear' }));
  await cleared;
  assert.equal(session.output.pendingBytes, 0);
  sink.remove('two');
  assert.deepEqual(await read(), { type: 'end', speaker: 'two' });
  await messages.return();
});

test('Gemma uses the bundled or overridden directory and keeps credentials out of its environment', () => {
  const opts = { requireCredentials: false };
  assert.equal(parseConfig({}, opts).backend, 'haru');
  assert.equal(parseConfig({ HARU_VOICE_BACKEND: 'gemma-yuna' }, opts).streamDir, join(ROOT, 'stream-local'));
  assert.throws(() => parseConfig({ HARU_VOICE_BACKEND: 'gemma-yuna', HARU_STREAM_LOCAL_DIR: 'relative' }, opts), /absolute/);
  assert.equal(parseConfig({ HARU_VOICE_BACKEND: 'gemma-yuna', HARU_STREAM_LOCAL_DIR: '/example' }, opts).streamDir, '/example');
  const env = streamEnvironment('/local', { DISCORD_TOKEN: 'secret', AIHUB_APIKEY: 'secret', HOME: '/home', PATH: '/bin' });
  assert.equal(env.HOME, '/home');
  assert.equal(env.HARU_REPO, '/local');
  assert.equal(Object.values(env).includes('secret'), false);
});

test('localized status returns to idle and keeps playback visible when the model is waiting', () => {
  const session = new GemmaVoiceSession({});
  session.startedAt = Date.now();
  session.phase = 'waiting';
  assert.match(session.status(), /^대기 중/);
  session.phase = 'reply_started';
  assert.match(session.status(), /^답변 생성 중/);
  session.phase = 'waiting';
  session.output.push(Buffer.alloc(3840));
  assert.match(session.status(), /^말하는 중/);
  session.lastInput = performance.now();
  assert.match(session.status(), /^말하며 듣는 중/);
  session.output.clear();
  assert.match(session.status(), /^듣는 중/);
  session.lastInput -= 500;
  assert.match(session.status(), /^대기 중/);
});
