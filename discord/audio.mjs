/** Discord PCM: 48 kHz, signed 16-bit stereo. Haru: 48 kHz float32 mono. */
function alignedBuffer(value, alignment, label) {
  if (!Buffer.isBuffer(value)) throw new TypeError(`${label} must be a Buffer`);
  if (value.length % alignment !== 0) throw new RangeError(`${label} must align to ${alignment} bytes`);
}

export function pcm16StereoToFloat32Mono(pcm) {
  alignedBuffer(pcm, 4, 'Stereo PCM');
  const output = Buffer.allocUnsafe(pcm.length);
  for (let offset = 0; offset < pcm.length; offset += 4) {
    output.writeFloatLE((pcm.readInt16LE(offset) + pcm.readInt16LE(offset + 2)) / 65536, offset);
  }
  return output;
}

export function float32MonoToPcm16Stereo(pcm) {
  alignedBuffer(pcm, 4, 'Float32 PCM');
  const output = Buffer.allocUnsafe(pcm.length);
  for (let offset = 0; offset < pcm.length; offset += 4) {
    const sample = pcm.readFloatLE(offset);
    // Corrupt/non-finite model samples must never become loud integer noise.
    const finite = Number.isFinite(sample) ? Math.max(-1, Math.min(1, sample)) : 0;
    const quantized = Math.max(-32768, Math.min(32767, Math.round(finite * 32768)));
    output.writeInt16LE(quantized, offset);
    output.writeInt16LE(quantized, offset + 2);
  }
  return output;
}

/** Bounded stereo PCM FIFO. Underflow produces silence without losing a partial frame. */
export class PcmQueue {
  constructor(frameBytes = 3840, maxFrames = 25) {
    if (!Number.isSafeInteger(frameBytes) || frameBytes <= 0 || frameBytes % 4 !== 0) {
      throw new RangeError('frameBytes must be a positive multiple of 4');
    }
    if (!Number.isSafeInteger(maxFrames) || maxFrames <= 0 || !Number.isSafeInteger(frameBytes * maxFrames)) {
      throw new RangeError('maxFrames must be a positive integer within the safe byte range');
    }
    this.frameBytes = frameBytes;
    this.maxFrames = maxFrames;
    this.droppedFrames = 0;
    this._pending = Buffer.alloc(0);
  }

  get pendingBytes() { return this._pending.length; }
  get dropCount() { return this.droppedFrames; }
  get droppedBytes() { return this.droppedFrames * this.frameBytes; }

  /** Returns how many oldest complete playback frames this push discarded. */
  push(pcm) {
    alignedBuffer(pcm, 4, 'Stereo PCM');
    if (pcm.length === 0) return 0;
    const excess = this._pending.length + pcm.length - this.frameBytes * this.maxFrames;
    const framesToDrop = Math.max(0, Math.ceil(excess / this.frameBytes));
    const bytesToDrop = framesToDrop * this.frameBytes;
    if (bytesToDrop >= this._pending.length) {
      // Copy the retained tail so a small queue cannot retain an enormous input buffer.
      this._pending = Buffer.from(pcm.subarray(bytesToDrop - this._pending.length));
    } else {
      this._pending = Buffer.concat([this._pending.subarray(bytesToDrop), pcm]);
    }
    this.droppedFrames += framesToDrop;
    return framesToDrop;
  }

  popFrame() {
    if (this._pending.length < this.frameBytes) return Buffer.alloc(this.frameBytes);
    const frame = Buffer.from(this._pending.subarray(0, this.frameBytes));
    this._pending = this._pending.subarray(this.frameBytes);
    return frame;
  }

  clear() { this._pending = Buffer.alloc(0); }
}
