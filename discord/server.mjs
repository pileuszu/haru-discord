import { EventEmitter } from 'node:events';
import { spawn } from 'node:child_process';
import { existsSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ROOT } from './config.mjs';

export class HaruServer extends EventEmitter {
  constructor() {
    super();
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
    const python = join(ROOT, '.venv/bin/python');
    if (!existsSync(python)) throw new Error('MLX environment missing. Run ./scripts/haru setup.');
    this.stopping = false;
    this.child = spawn(python, ['scripts/discord_server.py', this.socketPath], {
      cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, HF_HOME: join(ROOT, '.runtime/huggingface'), HF_HUB_OFFLINE: '1', PYTHONUNBUFFERED: '1' },
    });
    const child = this.child;
    await new Promise((resolve, reject) => {
      let tail = '';
      const timeout = setTimeout(() => {
        child.kill('SIGTERM');
        reject(new Error('Haru startup exceeded 120 seconds. Check the local model cache.'));
      }, 120_000);
      const onData = data => {
        if (this.child !== child || this.stopping) return;
        tail = (tail + data.toString()).slice(-4000);
        if (!this.ready && tail.includes('[native] ready on')) {
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
        if (!wasReady) reject(new Error(`Haru startup failed (${code ?? signal}). ${tail.slice(-1200)}`));
        else if (!this.stopping) this.emit('error', new Error(`Haru server stopped (${code ?? signal}).`));
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
