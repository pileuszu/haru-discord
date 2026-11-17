// Real MLX socket + Opus test, without a Discord account or microphone.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { performance } from 'node:perf_hooks';
import { ROOT, readConfig } from './config.mjs';
import { HaruServer } from './server.mjs';
import { HaruClient } from './haru-client.mjs';
import { PcmQueue, pcm16StereoToFloat32Mono, float32MonoToPcm16Stereo } from './audio.mjs';

const require = createRequire(import.meta.url);
const OpusScript = require('opusscript');
const inputCodec = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
const outputCodec = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
const config = readConfig({ requireCredentials: false });
const out = join(ROOT, 'artifacts', `bridge-${new Date().toISOString().replace(/[:.]/g, '-')}`);
await mkdir(out, { recursive: true });
const python = join(ROOT, '.venv/bin/python');
execFileSync('/usr/bin/say', ['-v', 'Yuna', '-r', '170', '-o', join(out, 'question.aiff'), '안녕, 나는 민수야. 만나서 반가워. 너는 이름이 뭐야?']);
execFileSync(python, ['-c', `
import sys,numpy as np,soundfile as sf
from pathlib import Path
from haru_mlx.audio import load_audio
p=Path(sys.argv[1]); a=load_audio(p/'question.aiff',sample_rate=48000)
a=np.concatenate([np.zeros(48000),a,np.zeros(12*48000)])
a=np.pad(a,(0,(-len(a))%960)); sf.write(p/'input.wav',a,48000)
b=(np.clip(a,-1,1)*32767).astype('<i2'); (p/'input.pcm').write_bytes(np.column_stack([b,b]).tobytes())
`, out]);
const input = await readFile(join(out, 'input.pcm'));
const server = new HaruServer();
const haru = new HaruClient({ socketPath: server.socketPath });
const decodedOutput = [];
const outputQueue = new PcmQueue(3840, 25);
let frames = 0;
let text = '';
let failure;
const steps = [];
server.on('error', error => { failure = error; });
haru.on('error', error => { failure = error; });
haru.on('audio', (audio, meta) => {
  frames += 1;
  if (meta.index) steps.push(meta.stepMs);
  outputQueue.push(float32MonoToPcm16Stereo(audio));
  while (outputQueue.pendingBytes >= 3840) {
    const encoded = outputCodec.encode(outputQueue.popFrame(), 960);
    decodedOutput.push(Buffer.from(outputCodec.decode(encoded)));
  }
});
haru.on('text', value => { text += value; process.stdout.write(value); });
try {
  console.log('Starting local MLX server and testing the Discord audio formats through Opus.');
  await server.start();
  await haru.start({ language: 'ko', reference_path: config.referencePath });
  const started = performance.now();
  let packets = 0;
  for (let offset = 0; offset < input.length; offset += 3840) {
    if (failure) throw failure;
    const opus = inputCodec.encode(input.subarray(offset, offset + 3840), 960);
    const pcm = inputCodec.decode(opus);
    if (!haru.sendAudio(pcm16StereoToFloat32Mono(pcm))) throw new Error('MLX input backlog exceeded the bounded budget.');
    packets += 1;
    await delay(Math.max(0, started + packets * 20 - performance.now()));
  }
  const expectedFrames = 1 + Math.floor(input.length / 25600);
  const deadline = performance.now() + 5000;
  while (frames < expectedFrames && performance.now() < deadline && !failure) await delay(20);
  if (failure) throw failure;
  if (frames !== expectedFrames) throw new Error(`Expected ${expectedFrames} model frames, received ${frames}.`);
  if (!text.match(/[가-힣]/)) throw new Error('No Hangul output was generated.');
  const result = Buffer.concat(decodedOutput);
  let energy = 0;
  for (let i = 0; i < result.length; i += 2) energy += (result.readInt16LE(i) / 32768) ** 2;
  const rms = Math.sqrt(energy / (result.length / 2));
  if (!(rms > 1e-4)) throw new Error('Returned Opus-decoded audio is silent.');
  await writeFile(join(out, 'reply.pcm'), result);
  execFileSync(python, ['-c', `import sys,numpy as np,soundfile as sf; from pathlib import Path; p=Path(sys.argv[1]); a=np.frombuffer((p/'reply.pcm').read_bytes(),dtype='<i2').reshape(-1,2); sf.write(p/'reply.wav',a,48000,subtype='PCM_16')`, out]);
  steps.sort((a,b) => a-b);
  const metrics = { liveDiscord: false, microphoneUsed: false, daveNetworkVerified: false, inputPackets: packets, modelFrames: frames, expectedFrames, opusDecodedOutputBytes: result.length, outputRms: rms, droppedInputBytes: haru.droppedAudioBytes, droppedOutputFrames: outputQueue.droppedFrames, stepMsP50: steps[Math.floor(steps.length * .5)], stepMsP95: steps[Math.floor(steps.length * .95)], transcript: text };
  await writeFile(join(out, 'metrics.json'), JSON.stringify(metrics, null, 2) + '\n');
  console.log('\n' + JSON.stringify(metrics, null, 2));
  console.log(`Artifacts: ${out}`);
} finally {
  haru.close();
  await server.stop();
  inputCodec.delete();
  outputCodec.delete();
}
