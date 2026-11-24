import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { VoiceSession } from '../discord/voice-session.mjs';

function fixture({ sendAudio, startServer, language = 'ko' } = {}) {
  const connection = new EventEmitter();
  connection.receiver = {}; connection.state = { status: 'ready' };
  connection.subscribe = player => { connection.player = player; };
  connection.destroy = () => { connection.state.status = 'destroyed'; };
  const player = new EventEmitter(); player.play = resource => { player.resource = resource; };
  player.stop = () => {};
  const api = {
    VoiceConnectionStatus: { Ready: 'ready', Disconnected: 'disconnected', Destroyed: 'destroyed', Signalling: 'signalling', Connecting: 'connecting' },
    NoSubscriberBehavior: { Play: 'play' }, StreamType: { Raw: 'raw' },
    joinVoiceChannel: options => { assert.equal(options.selfDeaf, false); assert.equal(options.selfMute, false); return connection; },
    entersState: async (_, status) => { if (status === 'ready') return connection; throw new Error('not reconnected'); },
    createAudioPlayer: () => player,
    createAudioResource: stream => ({ stream }),
  };
  const haru = new EventEmitter(); haru.frames = []; haru.pendingAudioBytes = 0; haru.droppedAudioBytes = 0;
  haru.start = async request => { haru.request = request; };
  haru.close = () => { haru.wasClosed = true; };
  haru.sendAudio = frame => {
    haru.frames.push(Buffer.from(frame));
    if (sendAudio && !sendAudio(frame)) { haru.droppedAudioBytes += frame.length; return false; }
    return true;
  };
  let receiver;
  const participants = { active: new Map(), start() {}, stop() { this.stopped = true; }, remove() {} };
  const logs = [];
  const session = new VoiceSession({ guild: { id: 'guild', client: { user: { id: 'bot' } } }, channel: { id: 'channel' }, server: { socketPath: '/tmp/haru.sock', start: startServer ?? (async () => {}) }, referencePath: '/tmp/voice.flac', maxSessionSeconds: 0, language, voiceApi: api, createHaru: () => haru, makeParticipants: options => { receiver = options; return participants; }, trace: { write: (event, fields) => { logs.push({ event, ...fields }); return true; } } });
  const errors = []; session.on('error', e => errors.push(e.message));
  return { session, haru, player, connection, participants, logs, errors, get receiver() { return receiver; } };
}

function stereo(sample) { const frame = Buffer.alloc(3840); for (let i = 0; i < frame.length; i += 2) frame.writeInt16LE(sample, i); return frame; }

test('full duplex forwards live speech and silence while receiving model audio and logs model text separately', async () => {
  const f = fixture(); await f.session.start();
  assert.equal(f.session.expiry, undefined);
  assert.equal(f.haru.request.language, 'ko'); assert.equal(f.haru.request.reference_path, '/tmp/voice.flac');
  assert.deepEqual(f.haru.request.card, {
    situation: '상대방이 궁금한 것을 물어보는 중이다. 질문을 하는 사람은 상대방이고, 당신은 대답하는 사람이다. 당신은 방금 들은 질문의 답을 자연스러운 한국어로 말한다.',
  });
  assert.equal(f.haru.request.system_prompt, undefined);
  f.receiver.sink.add('speaker'); f.receiver.sink.push('speaker', stereo(16384));
  f.haru.emit('audio', Buffer.alloc(25600), { index: 1, audioReadyMs: 76 });
  assert.equal(f.session.output.pendingBytes, 25600);
  assert.equal(f.session.metrics.stepSummary, null);
  f.haru.emit('step', { index: 1, stepMs: 85 });
  f.haru.emit('text', '안녕');
  await delay(70);
  assert.ok(f.haru.frames.length >= 2);
  assert.equal(f.haru.frames[0].readFloatLE(0), 0.5);
  assert.equal(f.haru.frames[1].readFloatLE(0), 0);
  assert.equal(f.session.generatedFrames, 1);
  assert.equal(f.session.metrics.stepSummary.p95, 85);
  assert.equal(f.logs.find(x => x.event === 'haru_model_text').text, '안녕');
  assert.ok(!f.logs.some(x => x.event.includes('stt')));
  f.session.stop('command_leave');
  assert.equal(f.connection.state.status, 'destroyed'); assert.equal(f.participants.stopped, true); assert.equal(f.haru.wasClosed, true);
  assert.equal(f.logs.at(-1).reason, 'command_leave');
});

test('the first dropped model input block ends the session with diagnostic counters', async () => {
  const f = fixture({ sendAudio: () => false }); await f.session.start();
  await Promise.race([new Promise(resolve => f.session.once('closed', resolve)), delay(300).then(() => { throw new Error('session did not stop'); })]);
  assert.equal(f.errors.length, 1); assert.match(f.errors[0], /audio was dropped/);
  const stop = f.logs.find(x => x.event === 'haru_session_stop');
  assert.equal(stop.reason, 'model_input_overflow'); assert.equal(stop.model_input_dropped_bytes, 3840);
  assert.match(f.session.status(), /모델 입력 버림 20ms/);
});

test('output or participant input queue overflow ends the session instead of silently losing audio', async () => {
  const output = fixture(); await output.session.start();
  output.haru.emit('audio', Buffer.alloc(3840 * 26), { index: 1, stepMs: 150 });
  assert.equal(output.session.closed, true); assert.equal(output.logs.at(-1).reason, 'output_overflow');
  assert.ok(output.logs.at(-1).output_dropped_frames > 0);
  const input = fixture(); await input.session.start(); input.receiver.sink.add('speaker');
  input.receiver.sink.push('speaker', Buffer.alloc(3840 * 11));
  assert.equal(input.session.closed, true); assert.equal(input.logs.at(-1).reason, 'input_overflow');
  assert.ok(input.logs.at(-1).input_dropped_frames > 0);
});

test('unrecoverable voice disconnection reports reason and stops cleanly', async () => {
  const f = fixture(); await f.session.start();
  const closed = new Promise(resolve => f.session.once('closed', resolve)); f.connection.emit('disconnected'); await closed;
  assert.equal(f.logs.at(-1).reason, 'connection_disconnected');
  assert.equal(f.connection.state.status, 'destroyed'); assert.equal(f.errors.length, 1);
});


test('model frame limit stops feeding, lets final audio drain and closes with a clear reason', async () => {
  const f = fixture(); await f.session.start();
  f.haru.emit('audio', Buffer.alloc(25600), { index: 26999, stepMs: 80 });
  assert.equal(f.session.state, '모델 대화 한도 도달');
  assert.ok(f.logs.some(x => x.event === 'haru_model_frame_limit'));
  const frames = f.haru.frames.length;
  await delay(60);
  assert.equal(f.haru.frames.length, frames);
  await Promise.race([new Promise(resolve => f.session.once('closed', resolve)), delay(500).then(() => { throw new Error('model limit did not close'); })]);
  assert.equal(f.logs.at(-1).reason, 'model_frame_limit');
  assert.match(f.errors[0], /conversation limit/);
});


test('the same voice session cannot be started again or restarted after it stops', async () => {
  const f = fixture();
  await f.session.start();
  await assert.rejects(f.session.start(), /only be started once/);
  f.session.stop();
  await assert.rejects(f.session.start(), /only be started once/);
});

test('stopping while the model server is starting cancels the connection cleanly', async () => {
  let releaseServer;
  const serverStarted = new Promise(resolve => { releaseServer = resolve; });
  const f = fixture({ startServer: () => serverStarted });
  const starting = f.session.start();
  f.session.stop('command_leave');
  releaseServer();
  await assert.rejects(starting, /cancelled/);
  assert.equal(f.connection.state.status, 'destroyed');
  assert.equal(f.haru.request, undefined);
  assert.equal(f.session.inputClock, undefined);
  assert.equal(f.logs.at(-1).reason, 'command_leave');
});

test('Japanese sessions preserve the configured character card', async () => {
  const f = fixture({ language: 'ja' });
  await f.session.start();
  assert.deepEqual(f.haru.request.card, {
    name: 'ハル', gender: 'female', backchannel: 'mid',
    persona: '日本語で話す、明るく親しみやすい友達。相手の話をよく聞き、自然な日本語で短く分かりやすく答える。趣味は散歩と音楽。',
    relationship: 'ハルと相手は初めて話す友達。',
    situation: '音声通話で友達と日常や好きなことについて自然に雑談している。自己紹介は求められた場合だけする。',
  });
  f.session.stop();
});
