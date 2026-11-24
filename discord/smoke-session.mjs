// Real VoiceSession + native MLX worker with synthetic speech and Opus on both
// sides. The Discord connection/player are simulated; no Discord network,
// DAVE, microphone or real participant is involved.
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { setTimeout as delay } from 'node:timers/promises';
import { ROOT, readConfig } from './config.mjs';
import { HaruServer } from './server.mjs';
import { VoiceSession } from './voice-session.mjs';

const config = readConfig({ requireCredentials: false });
const question = config.language === 'ja' ? 'こんにちは、好きな食べ物は何ですか？' : '안녕, 대한민국의 수도가 어디야?';
const voice = config.language === 'ja' ? 'Kyoko' : 'Yuna';
const out = join(ROOT, 'artifacts', `session-bridge-${new Date().toISOString().replace(/[:.]/g, '-')}`);
const python = join(ROOT, '.venv/bin/python');
await mkdir(out, { recursive: true });
execFileSync('/usr/bin/say', ['-v', voice, '-r', '170', '-o', join(out, 'question.aiff'), question]);
execFileSync(python, ['-c', `
import sys,numpy as np,soundfile as sf
from pathlib import Path
from haru_mlx.audio import load_audio
p=Path(sys.argv[1]); a=load_audio(p/'question.aiff',sample_rate=48000)
a=np.concatenate([np.zeros(48000,dtype=np.float32),a,np.zeros(10*48000,dtype=np.float32)])
a=np.pad(a,(0,(-len(a))%960)); sf.write(p/'input.wav',a,48000,subtype='PCM_16')
b=(np.clip(a,-1,1)*32767).astype('<i2'); (p/'input.pcm').write_bytes(np.column_stack([b,b]).tobytes())
`, out]);
const input = await readFile(join(out, 'input.pcm'));
const require = createRequire(import.meta.url);
const OpusScript = require('opusscript');
const inputCodec = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
const outputCodec = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
const incoming = [];
for (let offset = 0; offset < input.length; offset += 3840) {
  incoming.push(Buffer.from(inputCodec.decode(inputCodec.encode(input.subarray(offset, offset + 3840), 960))));
}

let failure;
const decoded = [];
let outputPackets = 0;
const connection = new EventEmitter();
connection.receiver = {};
connection.state = { status: 'ready' };
connection.subscribe = player => { connection.player = player; };
connection.destroy = () => { connection.state.status = 'destroyed'; };
const player = new EventEmitter();
player.play = ({ stream }) => {
  player.stream = stream;
  stream.on('data', frame => {
    try {
      // ClockedAudio sends 20ms raw 48kHz stereo frames just as it would to
      // @discordjs/voice; compare after the real Opus encode/decode path.
      const packet = outputCodec.encode(frame, 960);
      decoded.push(Buffer.from(outputCodec.decode(packet)));
      outputPackets++;
    } catch (error) { failure = error; }
  });
  stream.on('error', error => { failure = error; });
  stream.resume();
};
player.stop = () => {};
const voiceApi = {
  VoiceConnectionStatus: { Ready: 'ready', Disconnected: 'disconnected', Destroyed: 'destroyed', Signalling: 'signalling', Connecting: 'connecting' },
  NoSubscriberBehavior: { Play: 'play' }, StreamType: { Raw: 'raw' },
  joinVoiceChannel: options => {
    if (options.selfDeaf || options.selfMute) throw new Error('The full-duplex connection must receive and send audio.');
    return connection;
  },
  entersState: async (_, status) => { if (status !== 'ready') throw new Error('Mock connection cannot reconnect.'); return connection; },
  createAudioPlayer: () => player,
  createAudioResource: stream => ({ stream }),
};
let participantOptions;
const participants = { active: new Map(), start() {}, stop() { this.stopped = true; }, remove() {} };
const traceEvents = [];
const trace = { write(event, fields) { traceEvents.push({ event, ...fields }); return true; } };
const server = new HaruServer({ language: config.language, quantize: config.quantize });
server.on('error', error => { failure = error; });
const session = new VoiceSession({
  guild: { id: 'synthetic-guild', client: { user: { id: 'synthetic-bot' } } },
  channel: { id: 'synthetic-channel' }, server,
  referencePath: config.referencePath, language: config.language, maxSessionSeconds: 0,
  voiceApi, trace, makeParticipants: options => { participantOptions = options; return participants; },
});
session.on('error', error => { failure = error; });
let sentBytes = 0;
let inputPackets = 0;
try {
  console.log('Starting real local Haru model with a simulated Discord connection and clocked Opus.');
  await session.start();
  const sendAudio = session.haru.sendAudio.bind(session.haru);
  session.haru.sendAudio = frame => {
    const accepted = sendAudio(frame);
    if (accepted) sentBytes += frame.length;
    return accepted;
  };
  participantOptions.sink.add('synthetic-speaker');
  participants.active.set('synthetic-speaker', {});
  const started = performance.now();
  // Feed halfway between the VoiceSession's 20ms ticks to avoid repeatedly
  // hitting their timer boundary; the real per-speaker mixer receives PCM.
  await delay(10);
  for (const frame of incoming) {
    if (failure) throw failure;
    if (session.closed) throw new Error('Voice session stopped before the synthetic input completed.');
    participantOptions.onPacket(Buffer.alloc(1), 'synthetic-speaker');
    participantOptions.sink.push('synthetic-speaker', frame);
    inputPackets++;
    await delay(Math.max(0, started + 10 + inputPackets * 20 - performance.now()));
  }
  // Freeze the input clock to make the number of expected model frames stable,
  // while the output clock continues draining playback.
  session.inputClock.stop();
  const expectedFrames = 1 + Math.floor(sentBytes / (6400 * 4));
  const deadline = performance.now() + 5000;
  while (session.generatedFrames < expectedFrames && performance.now() < deadline && !failure && !session.closed) await delay(20);
  await delay(250);
  if (failure) throw failure;
  if (session.closed) throw new Error('Voice session stopped before the output finished.');
  if (session.generatedFrames !== expectedFrames) throw new Error(`Expected ${expectedFrames} model frames, received ${session.generatedFrames}.`);
  if (session.input.droppedFrames || session.output.droppedFrames || session.haru.droppedAudioBytes) throw new Error('Voice audio was dropped.');
  const output = Buffer.concat(decoded);
  let energy = 0;
  for (let i = 0; i < output.length; i += 2) energy += (output.readInt16LE(i) / 32768) ** 2;
  const rms = output.length ? Math.sqrt(energy / (output.length / 2)) : 0;
  const transcript = traceEvents.filter(e => e.event === 'haru_model_text').map(e => e.text).join('');
  const metrics = {
    syntheticInput: true, microphoneUsed: false, liveDiscord: false, daveNetworkVerified: false,
    semanticAccuracyVerified: false, language: config.language, quantize: config.quantize,
    question, inputPackets, sentAudioMs: sentBytes / 192, modelFrames: session.generatedFrames, expectedFrames,
    outputPackets, outputRms: rms, hasModelText: !!transcript.trim(), modelText: transcript,
    droppedInputFrames: session.input.droppedFrames, droppedOutputFrames: session.output.droppedFrames,
    droppedModelInputBytes: session.haru.droppedAudioBytes, playbackUnderflows: session.metrics.playbackUnderflows,
    stepMs: session.metrics.stepSummary,
  };
  await writeFile(join(out, 'reply.pcm'), output);
  execFileSync(python, ['-c', `import sys,numpy as np,soundfile as sf; from pathlib import Path; p=Path(sys.argv[1]); a=np.frombuffer((p/'reply.pcm').read_bytes(),dtype='<i2').reshape(-1,2); sf.write(p/'reply.wav',a,48000,subtype='PCM_16')`, out]);
  await writeFile(join(out, 'metrics.json'), JSON.stringify(metrics, null, 2) + '\n');
  console.log(JSON.stringify(metrics, null, 2));
  console.log(`Artifacts: ${out}`);
  if (!(rms > 1e-4)) throw new Error('Returned Opus-decoded audio is silent; inspect the artifacts.');
  if (!transcript.trim()) throw new Error('Haru produced no model text; inspect the artifacts and listen to the audio.');
} finally {
  session.stop('synthetic_test_finished');
  await server.stop();
  inputCodec.delete();
  outputCodec.delete();
}
