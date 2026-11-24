import { closeSync, constants, fchmodSync, fstatSync, mkdirSync, openSync, renameSync, unlinkSync, writeSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { ROOT } from './config.mjs';

/** Local-only voice lifecycle diagnostics; callers supply explicit metadata only. */
export class VoiceTraceLog {
  constructor({ path = resolve(ROOT, '.runtime/voice-debug.jsonl'), maxBytes = 10 * 1024 * 1024, backups = 3, now = () => new Date() } = {}) {
    Object.assign(this, { path, maxBytes, backups, now });
  }
  write(event, fields = {}) {
    let fd;
    try {
      const record = Buffer.from(JSON.stringify({ timestamp: this.now().toISOString(), event, ...fields }) + '\n');
      if (record.length > this.maxBytes) return false;
      mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
      fd = openSync(this.path, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | (constants.O_NOFOLLOW || 0), 0o600);
      fchmodSync(fd, 0o600);
      if (fstatSync(fd).size + record.length > this.maxBytes) {
        closeSync(fd); fd = undefined;
        if (this.backups > 0) {
          try { unlinkSync(`${this.path}.${this.backups}`); } catch (e) { if (e.code !== 'ENOENT') throw e; }
          for (let i = this.backups - 1; i >= 1; i--) {
            try { renameSync(`${this.path}.${i}`, `${this.path}.${i + 1}`); } catch (e) { if (e.code !== 'ENOENT') throw e; }
          }
          try { renameSync(this.path, `${this.path}.1`); } catch (e) { if (e.code !== 'ENOENT') throw e; }
        } else {
          unlinkSync(this.path);
        }
        fd = openSync(this.path, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | (constants.O_NOFOLLOW || 0), 0o600);
        fchmodSync(fd, 0o600);
      }
      writeSync(fd, record);
      return true;
    } catch {
      // Diagnostics must never interrupt voice processing or echo private content.
      return false;
    } finally {
      if (fd !== undefined) { try { closeSync(fd); } catch {} }
    }
  }
}
