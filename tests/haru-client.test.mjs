import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { HaruClient } from '../discord/haru-client.mjs';

function message(kind, content) {
  const payload = Buffer.isBuffer(content) ? content : Buffer.from(JSON.stringify(content));
  const header = Buffer.alloc(5);
  header[0] = kind;
  header.writeUInt32LE(payload.length, 1);
  return Buffer.concat([header, payload]);
}

function audioFrame(index = 0) {
  const payload = Buffer.alloc(8 + 6400 * 4);
  payload.writeUInt32LE(index);
  payload.writeFloatLE(82, 4);
  payload.writeFloatLE(0.25, 8);
  return message(0x11, payload);
}

function readMessages(socket, callback) {
  let pending = Buffer.alloc(0);
  socket.on('data', (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    while (pending.length >= 5) {
      const size = pending.readUInt32LE(1);
      if (pending.length < size + 5) break;
      const kind = pending[0];
      const payload = pending.subarray(5, size + 5);
      pending = pending.subarray(size + 5);
      callback(kind, payload);
    }
  });
}

async function fixture(t, handler) {
  // /tmp keeps Unix socket paths short, including on macOS with a long TMPDIR.
  const directory = await mkdtemp('/tmp/haru-node-test-');
  const socketPath = join(directory, 'h.sock');
  const sockets = new Set();
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.on('error', () => {});
    socket.on('close', () => sockets.delete(socket));
    handler(socket);
  });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  server.listen(socketPath);
  await once(server, 'listening');
  const client = new HaruClient({ socketPath, timeoutMs: 1000 });
  t.after(() => client.close());
  return client;
}

async function fragmentedWrite(socket, bytes) {
  const sizes = [1, 3, 2, 7, 11, 4093];
  for (let offset = 0, index = 0; offset < bytes.length; index += 1) {
    const end = Math.min(bytes.length, offset + sizes[index % sizes.length]);
    if (socket.destroyed) return;
    socket.write(bytes.subarray(offset, end));
    offset = end;
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const hello = { sample_rate: 48000, frame_samples: 6400, step_ms: 82 };

test('fragmented ASSETS/HELLO/frame/text handshake and client AUDIO framing', async (t) => {
  let resolveRequest;
  const requestSeen = new Promise((resolve) => { resolveRequest = resolve; });
  let resolveAudio;
  const sentAudio = new Promise((resolve) => { resolveAudio = resolve; });
  const client = await fixture(t, (socket) => {
    readMessages(socket, (kind, payload) => {
      if (kind === 2) {
        resolveRequest(JSON.parse(payload));
        void fragmentedWrite(socket, Buffer.concat([
          message(0x10, hello), audioFrame(), message(0x12, Buffer.from('안녕하세요')),
        ]));
      } else if (kind === 1) resolveAudio(Buffer.from(payload));
    });
    void fragmentedWrite(socket, message(0x13, { default_language: 'ko' }));
  });
  const firstAudio = once(client, 'audio');
  const firstText = once(client, 'text');
  const firstStep = once(client, 'step');
  assert.deepEqual(await client.start({ language: 'ko', speak_first: false }), hello);
  assert.deepEqual(await requestSeen, { language: 'ko', speak_first: false, audio_first: true });
  assert.equal(client.assets.default_language, 'ko');
  const [pcm, metadata] = await firstAudio;
  assert.equal(pcm.length, 25600);
  assert.equal(pcm.readFloatLE(0), 0.25);
  assert.equal(metadata.index, 0);
  assert.equal(metadata.stepMs, 82);
  assert.deepEqual(await firstStep, [{ index: 0, stepMs: 82 }]);
  assert.deepEqual(await firstText, ['안녕하세요']);
  const input = Buffer.alloc(3840);
  input.writeFloatLE(0.5);
  assert.equal(client.sendAudio(input), true);
  assert.deepEqual(await sentAudio, input);
  const closed = once(client, 'close');
  client.close();
  await closed;
  assert.equal(client.ready, false);
});

test('input without model responses stays bounded and reports dropped bytes', async (t) => {
  const client = await fixture(t, (socket) => {
    readMessages(socket, (kind) => { if (kind === 2) socket.write(message(0x10, hello)); });
    socket.write(message(0x13, {}));
  });
  await client.start();
  const pcm = Buffer.alloc(3840);
  for (let i = 0; i < 25; i += 1) assert.equal(client.sendAudio(pcm), true);
  assert.equal(client.sendAudio(pcm), false);
  assert.equal(client.pendingAudioBytes, 96000);
  assert.equal(client.droppedAudioBytes, 3840);
  assert.throws(() => client.sendAudio(Buffer.alloc(3)), /float32/);
});

test('early socket close rejects start instead of hanging', async (t) => {
  const client = await fixture(t, (socket) => socket.end());
  await assert.rejects(client.start(), /closed before HELLO/);
});

test('handshake timeout rejects start and closes its socket', async (t) => {
  const client = await fixture(t, () => {});
  client.timeoutMs = 25;
  const closed = new Promise((resolve) => client.once('close', resolve));
  await assert.rejects(client.start(), /Timed out/);
  await closed;
});

test('oversized payload header is rejected before buffering its body', async (t) => {
  const client = await fixture(t, (socket) => {
    const header = Buffer.alloc(5);
    header[0] = 0x13;
    header.writeUInt32LE(0xffffffff, 1);
    socket.write(header);
  });
  await assert.rejects(client.start(), /payload length/);
});

test('invalid HELLO audio format is rejected', async (t) => {
  const client = await fixture(t, (socket) => {
    readMessages(socket, (kind) => {
      if (kind === 2) socket.write(message(0x10, { sample_rate: 24000, frame_samples: 3200 }));
    });
    socket.write(message(0x13, {}));
  });
  await assert.rejects(client.start(), /Unsupported Haru audio format/);
});

test('server ERROR becomes a descriptive error event and start rejection', async (t) => {
  const client = await fixture(t, (socket) => socket.write(message(0x14, { message: 'voice too short' })));
  const errors = [];
  client.on('error', (error) => errors.push(error));
  await assert.rejects(client.start(), /voice too short/);
  assert.equal(errors.length, 1);
});

test('malformed FRAME length terminates an active session', async (t) => {
  const client = await fixture(t, (socket) => {
    readMessages(socket, (kind) => {
      if (kind === 2) {
        socket.write(message(0x10, hello));
        setImmediate(() => socket.write(message(0x11, Buffer.alloc(9))));
      }
    });
    socket.write(message(0x13, {}));
  });
  const failure = new Promise((resolve) => client.once('error', resolve));
  await client.start();
  assert.match((await failure).message, /FRAME payload length/);
  assert.equal(client.ready, false);
});

test('only local absolute Unix socket paths are accepted', () => {
  assert.throws(() => new HaruClient({ socketPath: 'tcp://localhost:8000' }), /local Unix/);
  assert.throws(() => new HaruClient({ socketPath: '/tmp/x', timeoutMs: 0 }), /timeoutMs/);
});


function completedStep(index, stepMs) {
  const payload = Buffer.alloc(8);
  payload.writeUInt32LE(index);
  payload.writeFloatLE(stepMs, 4);
  return message(0x16, payload);
}

test('negotiated audio-first delivers PCM while the token is pending', { timeout: 3000 }, async (t) => {
  let peer;
  const client = await fixture(t, (socket) => {
    peer = socket;
    readMessages(socket, (kind) => {
      if (kind === 2) socket.write(message(0x10, { ...hello, audio_first: true }));
    });
    socket.write(message(0x13, {}));
  });
  const events = [];
  for (const event of ['audio', 'step', 'text']) client.on(event, () => events.push(event));
  await client.start();
  const pcmArrived = once(client, 'audio');
  peer.write(audioFrame());
  const [pcm, frame] = await pcmArrived;
  assert.equal(pcm.readFloatLE(0), .25);
  assert.equal(frame.index, 0);
  assert.equal(frame.audioReadyMs, 82);
  assert.equal(frame.stepMs, undefined);
  assert.deepEqual(events, ['audio']);

  const step = once(client, 'step');
  const text = once(client, 'text');
  await fragmentedWrite(peer, Buffer.concat([completedStep(0, 91), message(0x12, Buffer.from('하루'))]));
  assert.deepEqual(await step, [{ index: 0, stepMs: 91 }]);
  assert.deepEqual(await text, ['하루']);
  assert.deepEqual(events, ['audio', 'step', 'text']);
});

test('audio-first rejects mismatched or malformed completion messages', { timeout: 3000 }, async (t) => {
  for (const [description, sequence] of [
    ['next frame arrived before completion', [audioFrame(0), audioFrame(1)]],
    ['late completion for wrong frame', [audioFrame(0), completedStep(1, 92)]],
    ['completion before first PCM', [completedStep(0, 92)]],
    ['text before completion', [audioFrame(0), message(0x12, Buffer.from('하루'))]],
    ['malformed completion header', [audioFrame(0), message(0x16, Buffer.alloc(7))]],
    ['invalid completed time', [audioFrame(0), completedStep(0, NaN)]],
  ]) {
    await t.test(description, async t => {
      let peer;
      const client = await fixture(t, socket => {
        peer = socket;
        readMessages(socket, kind => {
          if (kind === 2) socket.write(message(0x10, { ...hello, audio_first: true }));
        });
        socket.write(message(0x13, {}));
      });
      await client.start();
      const error = once(client, 'error');
      peer.write(Buffer.concat(sequence));
      const [failure] = await error;
      assert.ok(failure instanceof Error);
      assert.equal(client.ready, false);
    });
  }
});
