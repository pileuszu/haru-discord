import { EventEmitter } from 'node:events';
import { spawn } from 'node:child_process';
import { existsSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ROOT } from './config.mjs';
import { modelEnvironment, streamEnvironment } from './worker-env.mjs';

export class HaruServer extends EventEmitter {
  constructor({ language = 'ko', quantize = 8, streamDir = null } = {}) {
    super();
    if (!['ko', 'ja'].includes(language) || ![8, null].includes(quantize)) throw new Error('Invalid Haru server configuration.');
    this.streamDir = streamDir;
    this.name = streamDir ? 'Gemma · 유나' : 'Haru MLX';
    this.language = language;
    this.quantize = quantize;
    this.socketPath = join(tmpdir(), `haru-discord-${process.getuid?.() ?? 'user'}-${process.pid}.sock`);
    this.ready = false;
    this.child = null;
    this.stopping = false;
    this.pending = null;
  }
  start() {
    if (this.ready) return Promise.resolve();
    if (this.pending) return this.pending;
    this.pending = this.#start();
    this.pending.catch(() => { if (!this.child) this.pending = null; });
    return this.pending;
  }
  async #start() {
    const cwd = this.streamDir || ROOT;
    const python = join(cwd, '.venv/bin/python');
    if (!existsSync(python)) throw new Error('Model Python environment missing: ' + python);
    const args = this.streamDir
      ? [join(cwd, 'discord_bridge.py'), '--socket', this.socketPath]
      : ['scripts/discord_server.py', this.socketPath, '--language', this.language, '--quantize', this.quantize === null ? 'none' : String(this.quantize)];
    const env = this.streamDir ? streamEnvironment(ROOT) : modelEnvironment(ROOT);
    const readyMarker = this.streamDir ? '[bridge] ready' : '[native] ready on';
    this.stopping = false;
    this.child = spawn(python, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'], env });
    const child = this.child;
    await new Promise((resolve, reject) => {
      let tail = '';
      const timeout = setTimeout(() => {
        child.kill('SIGTERM');
        reject(new Error('Model startup exceeded 120 seconds. Check the local model cache.'));
      }, 120_000);
      const onData = data => {
        if (this.child !== child || this.stopping) return;
        tail = (tail + data.toString()).slice(-4000);
        if (!this.ready && tail.includes(readyMarker)) {
          this.ready = true;
          clearTimeout(timeout);
          resolve();
        }
      };
      child.stdout.on('data', onData);
      child.stderr.on('data', onData);
      child.once('error', error => {
        clearTimeout(timeout);
        if (this.child === child && !child.pid) this.child = null;
        reject(error);
      });
      child.once('exit', (code, signal) => {
        clearTimeout(timeout);
        if (this.child !== child) return;
        this.child = null;
        const wasReady = this.ready;
        this.ready = false;
        this.pending = null;
        if (!wasReady) reject(new Error(`Model startup failed (${code ?? signal}). ${tail.slice(-1200)}`));
        else if (!this.stopping) this.emit('error', new Error(`Model server stopped (${code ?? signal}).`));
      });
    });
  }
  async stop() {
    this.stopping = true;
    const child = this.child;
    if (child?.pid && child.exitCode === null && child.signalCode === null) {
      await new Promise(resolve => {
        const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
        child.once('exit', () => { clearTimeout(timer); resolve(); });
        child.kill('SIGTERM');
      });
    }
    this.ready = false;
    this.pending = null;
    this.child = null;
    try { unlinkSync(this.socketPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}
