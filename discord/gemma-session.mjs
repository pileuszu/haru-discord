import { EventEmitter } from 'node:events';
import { connect } from 'node:net';
import { performance } from 'node:perf_hooks';
import WebSocket from 'ws';
import { VoiceSession } from './voice-session.mjs';

const FRAME_BYTES = 3840; // 20 ms of 48 kHz stereo signed PCM16.
const MAX_BUFFERED_INPUT = 128 * 1024;

/** Keep speaker identity through recognition; the Discord decoder emits stereo PCM. */
function monoFrame(stereo) {
  const mono = Buffer.allocUnsafe(FRAME_BYTES / 2);
  let energy = 0;
  for (let i = 0; i < FRAME_BYTES; i += 4) {
    const sample = Math.trunc((stereo.readInt16LE(i) + stereo.readInt16LE(i + 2)) / 2);
    mono.writeInt16LE(sample, i / 2);
    energy += sample * sample;
  }
  return { mono, voiced: energy >= (FRAME_BYTES / 4) * 140 ** 2 };
}

export class GemmaBridge extends EventEmitter {
  constructor(socketPath) {
    super();
    this.socketPath = socketPath;
    this.tails = new Map();
    this.closed = false;
    this.ready = false;
  }

  start() {
    const ws = new WebSocket('ws://localhost/voice', {
      createConnection: () => connect({ path: this.socketPath }),
      perMessageDeflate: false, maxPayload: 32 * 1024, handshakeTimeout: 10_000,
    });
    this.ws = ws;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { reject(new Error('음성 엔진 준비 시간 초과')); this.close(); }, 120_000);
      const failed = error => {
        clearTimeout(timer);
        if (!this.ready) reject(error);
        else if (!this.closed) this.emit('error', error);
      };
      ws.on('error', failed);
      ws.on('close', () => {
        failed(new Error('로컬 음성 엔진과 연결이 끊겼습니다.'));
        this.ready = false;
      });
      ws.on('message', (data, binary) => {
        if (this.closed) return;
        try {
          if (binary) {
            if (!this.ready || data.length !== FRAME_BYTES) throw new Error('Unexpected voice PCM frame.');
            this.emit('audio', data);
            return;
          }
          const message = JSON.parse(data.toString());
          switch (message.type) {
            case 'ready':
              clearTimeout(timer);
              this.ready = true;
              resolve();
              break;
            case 'clear':
              this.emit('clear');
              break;
            case 'status':
              this.emit('status', message);
              break;
            case 'error':
              throw new Error(typeof message.message === 'string' ? message.message.slice(0, 200) : '음성 처리에 실패했습니다.');
            default:
              throw new Error('Unknown bridge response.');
          }
        } catch (error) { failed(error); this.close(); }
      });
    });
  }

  send(message) {
    if (this.closed || !this.ready || this.ws.readyState !== WebSocket.OPEN) return false;
    if (this.ws.bufferedAmount > MAX_BUFFERED_INPUT) throw new Error('로컬 음성 인식이 입력을 처리하지 못했습니다.');
    this.ws.send(JSON.stringify(message));
    return true;
  }

  push(speaker, stereo, overlap) {
    if (!Buffer.isBuffer(stereo) || stereo.length % 4) throw new Error('Unaligned Discord PCM.');
    const tail = this.tails.get(speaker);
    const bytes = tail ? Buffer.concat([tail, stereo]) : stereo;
    let offset = 0, heard = false;
    for (; offset + FRAME_BYTES <= bytes.length; offset += FRAME_BYTES) {
      const { mono, voiced } = monoFrame(bytes.subarray(offset, offset + FRAME_BYTES));
      heard ||= voiced;
      this.send({ type: 'pcm', speaker, audio: mono.toString('base64'), overlap });
    }
    if (offset < bytes.length) this.tails.set(speaker, Buffer.from(bytes.subarray(offset)));
    else this.tails.delete(speaker);
    return heard;
  }

  end(speaker) {
    this.tails.delete(speaker);
    this.send({ type: 'end', speaker });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.tails.clear();
    this.ws?.terminate();
  }
}

export class GemmaVoiceSession extends VoiceSession {
  async openModel(fail) {
    this.bridge = new GemmaBridge(this.server.socketPath);
    this.lastAudio = -Infinity;
    this.bridge.on('error', error => fail(error, 'bridge_error'));
    this.bridge.on('clear', () => { this.output.clear(); this.lastAudio = -Infinity; });
    this.bridge.on('status', ({ state }) => {
      if (typeof state === 'string' && /^[a-z_]{1,32}$/.test(state)) {
        if (state === 'searching') this.searching = true;
        else if (['search_finished', 'turn_redirect', 'turn_stop', 'waiting', 'reply_started'].includes(state)) this.searching = false;
        this.phase = state;
        this.phaseAt = performance.now();
      }
    });
    this.bridge.on('audio', pcm => {
      if (this.closed) return;
      this.lastAudio = performance.now();
      this.generatedFrames++;
      if (this.output.push(pcm)) fail(new Error('음성 송신 버퍼가 가득 찼습니다.'), 'output_overflow');
    });
    await this.bridge.start();
  }

  submitText(channel, speaker, text) {
    if (this.closed || !this.startedAt || this.connection?.state.status !== this.voiceApi.VoiceConnectionStatus.Ready) return false;
    return this.bridge.send({ type: 'text', channel, speaker, text, overlap: this.isPlaying() });
  }

  isPlaying() {
    return this.output.pendingBytes > 0 || performance.now() - this.lastAudio < 60;
  }

  inputSink(fail) {
    return {
      add() {},
      remove: id => { if (!this.closed) this.bridge.end(id); },
      push: (id, pcm) => {
        try {
          if (this.bridge.push(id, pcm, this.isPlaying())) this.lastInput = performance.now();
        } catch (error) { fail(error, 'input_error'); }
      },
    };
  }

  // Only send actual received audio. The bridge tracks end-of-speech in wall time.
  startInputClock() {}

  status() {
    const now = performance.now();
    const playing = this.isPlaying();
    const hearing = now - this.lastInput < 320;
    let activity = '대기 중';
    if (this.closed) activity = '종료';
    else if (!this.startedAt) activity = '연결 중';
    else if (playing) activity = hearing ? '말하며 듣는 중' : '말하는 중';
    else if (this.searching) activity = '검색 중';
    else if (hearing) activity = '듣는 중';
    else if (this.phase === 'stt_partial' && now - this.phaseAt < 1200) activity = '음성 인식 중';
    else if (['stt_final', 'turn_start', 'turn_redirect', 'turn_continue', 'search_finished', 'reply_started'].includes(this.phase)) activity = '답변 생성 중';
    return `${activity} · Gemma · 유나 · 수신 패킷 ${this.receivedPackets} · 음성 출력 ${this.generatedFrames}프레임 · 송신 대기 ${Math.round(this.output.pendingBytes / 192)}ms`;
  }

  stop(reason) {
    super.stop(reason);
    this.bridge?.close();
  }
}
