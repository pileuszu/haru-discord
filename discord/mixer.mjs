import { PcmQueue } from './audio.mjs';

const FRAME_BYTES = 3840; // 20 ms of signed 16-bit stereo at 48 kHz.

/** Mix separately buffered Discord speakers into a single Haru float32 mono stream. */
export class VoiceMixer {
  constructor({ maxFrames = 10 } = {}) {
    this.queues = new Map();
    this.maxFrames = maxFrames;
    this.droppedFrames = 0;
  }

  add(userId) {
    if (typeof userId !== 'string' || !userId) throw new TypeError('userId must be a nonempty string');
    if (!this.queues.has(userId)) this.queues.set(userId, new PcmQueue(FRAME_BYTES, this.maxFrames));
    return this.queues.get(userId);
  }

  get(userId) { return this.queues.get(userId); }

  remove(userId) {
    const queue = this.queues.get(userId);
    if (!queue) return false;
    queue.clear();
    return this.queues.delete(userId);
  }

  clear() {
    for (const queue of this.queues.values()) queue.clear();
    this.queues.clear();
  }

  push(userId, pcm) {
    const queue = this.queues.get(userId);
    if (!queue) return false;
    this.droppedFrames += queue.push(pcm);
    return true;
  }

  get pendingBytes() {
    let total = 0;
    for (const queue of this.queues.values()) total += queue.pendingBytes;
    return total;
  }

  /** A 20 ms mono float32 block, summing only complete speaker frames. */
  frame() {
    const inputs = [];
    for (const queue of this.queues.values()) {
      if (queue.pendingBytes >= FRAME_BYTES) inputs.push(queue.popFrame());
    }
    const output = Buffer.alloc(FRAME_BYTES);
    for (let offset = 0; offset < FRAME_BYTES && inputs.length; offset += 4) {
      let sum = 0;
      for (const input of inputs) sum += (input.readInt16LE(offset) + input.readInt16LE(offset + 2)) / 65536;
      output.writeFloatLE(Math.max(-1, Math.min(1, sum)), offset);
    }
    return output;
  }
}
