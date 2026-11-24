import { EventEmitter } from 'node:events';
import net from 'node:net';
import { isAbsolute } from 'node:path';

const AUDIO = 0x01;
const START = 0x02;
const HELLO = 0x10;
const FRAME = 0x11;
const TEXT = 0x12;
const ASSETS = 0x13;
const ERROR = 0x14;
const STEP = 0x16;
const SAMPLE_RATE = 48000;
const FRAME_SAMPLES = 6400;
const FRAME_PCM_BYTES = FRAME_SAMPLES * 4;
const MAX_REQUEST_BYTES = 64 * 1024;
// Bound both queued socket bytes and input not yet answered by model frames.
const MAX_PENDING_AUDIO_BYTES = SAMPLE_RATE * 4 / 2;
const PAYLOAD_LIMITS = new Map([
  [ASSETS, 512 * 1024], [HELLO, 16 * 1024], [FRAME, FRAME_PCM_BYTES + 8],
  [TEXT, 64 * 1024], [ERROR, 16 * 1024], [STEP, 8],
]);
const utf8 = new TextDecoder('utf-8', { fatal: true });

function packet(kind, payload) {
  const message = Buffer.allocUnsafe(payload.length + 5);
  message[0] = kind;
  message.writeUInt32LE(payload.length, 1);
  payload.copy(message, 5);
  return message;
}

function jsonObject(payload, label) {
  const value = JSON.parse(utf8.decode(payload));
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must contain a JSON object`);
  }
  return value;
}

function computeTime(payload) {
  const ms = payload.readFloatLE(4);
  if (!Number.isFinite(ms) || ms < 0) throw new Error('Invalid Haru frame compute time');
  return ms;
}

/** One full-duplex session on the local upstream haru_mlx.native Unix socket. */
export class HaruClient extends EventEmitter {
  constructor({ socketPath, timeoutMs = 30000 }) {
    super();
    if (typeof socketPath !== 'string' || !isAbsolute(socketPath) || socketPath.includes('\0')) {
      throw new TypeError('socketPath must be an absolute local Unix socket path');
    }
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647) {
      throw new RangeError('timeoutMs must be a positive timer duration');
    }
    this.socketPath = socketPath;
    this.timeoutMs = timeoutMs;
    this.assets = null;
    this.hello = null;
    this.droppedAudioBytes = 0;
    this._state = 'idle';
    this._buffer = Buffer.alloc(0);
    this._socket = null;
    this._timer = null;
    this._resolveStart = null;
    this._rejectStart = null;
    this._failure = null;
    this._closeEmitted = false;
    this._sentAudioBytes = 0;
    this._lastFrameIndex = -1;
    this._awaitingStep = false;
  }

  get ready() { return this._state === 'ready'; }
  get pendingAudioBytes() {
    return Math.max(0, this._sentAudioBytes - Math.max(0, this._lastFrameIndex) * FRAME_PCM_BYTES);
  }

  async start(request = {}) {
    if (this._state !== 'idle') throw new Error('HaruClient.start() can only be called once');
    if (request === null || typeof request !== 'object' || Array.isArray(request)) {
      throw new TypeError('START request must be an object');
    }
    const startPayload = Buffer.from(JSON.stringify({ audio_first: true, ...request }));
    if (startPayload.length > MAX_REQUEST_BYTES) throw new RangeError('START request is too large');
    this._state = 'awaiting-assets';
    this._startPayload = startPayload;
    return new Promise((resolve, reject) => {
      this._resolveStart = resolve;
      this._rejectStart = reject;
      this._timer = setTimeout(() => this._fail(new Error('Timed out waiting for Haru ASSETS/HELLO')), this.timeoutMs);
      try {
        this._socket = net.createConnection({ path: this.socketPath });
        this._socket.on('data', (data) => this._receive(data));
        this._socket.on('error', (error) => this._fail(error));
        this._socket.on('close', () => this._onClose());
      } catch (error) {
        this._fail(error);
        this._onClose();
      }
    });
  }

  _receive(data) {
    if (this._state === 'closed' || this._failure) return;
    this._buffer = this._buffer.length ? Buffer.concat([this._buffer, data]) : data;
    try {
      while (this._buffer.length >= 5 && this._state !== 'closed' && !this._failure) {
        const kind = this._buffer[0];
        const length = this._buffer.readUInt32LE(1);
        const limit = PAYLOAD_LIMITS.get(kind);
        if (limit === undefined) throw new Error(`Unknown Haru message type: ${kind}`);
        if (length > limit) throw new Error(`Haru payload length ${length} exceeds limit ${limit}`);
        if (kind === FRAME && length !== FRAME_PCM_BYTES + 8) throw new Error('Invalid Haru FRAME payload length');
        if (kind === STEP && length !== 8) throw new Error('Invalid Haru STEP payload length');
        if (this._buffer.length < 5 + length) return;
        const payload = this._buffer.subarray(5, 5 + length);
        this._buffer = this._buffer.subarray(5 + length);
        this._dispatch(kind, payload);
      }
    } catch (error) {
      this._fail(error);
    }
  }

  _dispatch(kind, payload) {
    if (kind === ERROR) {
      const response = jsonObject(payload, 'ERROR');
      throw new Error(`Haru server: ${String(response.message ?? 'unknown error')}`);
    }
    if (kind === ASSETS) {
      if (this._state !== 'awaiting-assets') throw new Error('Unexpected Haru ASSETS message');
      this.assets = jsonObject(payload, 'ASSETS');
      this._state = 'awaiting-hello';
      this._socket.write(packet(START, this._startPayload));
      this._startPayload = null;
      return;
    }
    if (kind === HELLO) {
      if (this._state !== 'awaiting-hello') throw new Error('Unexpected Haru HELLO message');
      const hello = jsonObject(payload, 'HELLO');
      if (hello.sample_rate !== SAMPLE_RATE || hello.frame_samples !== FRAME_SAMPLES) {
        throw new Error(`Unsupported Haru audio format: ${hello.sample_rate} Hz / ${hello.frame_samples} samples`);
      }
      this.hello = hello;
      this._state = 'ready';
      clearTimeout(this._timer);
      this._timer = null;
      const resolve = this._resolveStart;
      this._resolveStart = this._rejectStart = null;
      resolve(hello);
      return;
    }
    if (!this.ready) throw new Error('Haru sent audio/text before HELLO');
    if (kind === STEP) {
      const index = payload.readUInt32LE(0);
      if (!this._awaitingStep || index !== this._lastFrameIndex) throw new Error('Unexpected Haru STEP index or order');
      const stepMs = computeTime(payload);
      this._awaitingStep = false;
      this.emit('step', { index, stepMs });
      return;
    }
    if (this._awaitingStep) throw new Error('Expected Haru STEP for the current FRAME');
    if (kind === TEXT) {
      if (this.hello.audio_first === true && this._lastFrameIndex < 0) throw new Error('Haru sent TEXT before the first FRAME');
      this.emit('text', utf8.decode(payload));
      return;
    }
    if (kind === FRAME) {
      const index = payload.readUInt32LE(0);
      if (index !== this._lastFrameIndex + 1) throw new Error('Haru FRAME index is out of sequence');
      const ms = computeTime(payload);
      this._lastFrameIndex = index;
      const metadata = { index, sampleRate: SAMPLE_RATE, frameSamples: FRAME_SAMPLES };
      if (this.hello.audio_first === true) {
        this._awaitingStep = true;
        this.emit('audio', payload.subarray(8), { ...metadata, audioReadyMs: ms });
      } else {
        // Stock haru_mlx.native publishes audio when the whole step has finished.
        this.emit('audio', payload.subarray(8), { ...metadata, stepMs: ms });
        if (this.ready) this.emit('step', { index, stepMs: ms });
      }
    }
  }

  /** True means accepted. False means discarded to cap latency; no hidden application queue. */
  sendAudio(pcm) {
    if (!Buffer.isBuffer(pcm)) throw new TypeError('AUDIO must be a Buffer');
    if (pcm.length % 4 !== 0) throw new RangeError('AUDIO must contain complete float32 samples');
    if (!this.ready || this._socket.destroyed) throw new Error('Haru session is not ready');
    if (pcm.length === 0) return true;
    if (this.pendingAudioBytes + pcm.length > MAX_PENDING_AUDIO_BYTES ||
        this._socket.writableLength + pcm.length + 5 > MAX_PENDING_AUDIO_BYTES) {
      this.droppedAudioBytes += pcm.length;
      return false;
    }
    // write(false) still accepts data; writableLength limits following calls.
    this._socket.write(packet(AUDIO, pcm));
    this._sentAudioBytes += pcm.length;
    return true;
  }

  _fail(error) {
    if (this._failure || this._state === 'closed') return;
    this._failure = error instanceof Error ? error : new Error(String(error));
    clearTimeout(this._timer);
    this._timer = null;
    if (this._rejectStart) this._rejectStart(this._failure);
    this._resolveStart = this._rejectStart = null;
    this._state = 'closed';
    this._buffer = Buffer.alloc(0);
    this._socket?.destroy();
    // start() rejection remains usable even if a consumer forgot an error listener.
    if (this.listenerCount('error')) this.emit('error', this._failure);
  }

  _onClose() {
    if (this._state !== 'closed') {
      this._fail(new Error(this.ready ? 'Haru socket closed unexpectedly' : 'Haru socket closed before HELLO'));
    }
    if (!this._closeEmitted) {
      this._closeEmitted = true;
      this.emit('close');
    }
  }

  close() {
    if (this._state === 'closed') return;
    if (this._rejectStart) this._rejectStart(new Error('Haru session closed before HELLO'));
    this._resolveStart = this._rejectStart = null;
    clearTimeout(this._timer);
    this._timer = null;
    this._state = 'closed';
    this._buffer = Buffer.alloc(0);
    if (this._socket) this._socket.destroy();
    else this._onClose();
  }
}
