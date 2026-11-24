import { performance } from 'node:perf_hooks';

/**
 * Fixed-rate clock corrected against monotonic deadlines, rather than adding
 * event-loop timer delay to every frame. Catch-up is bounded: a badly stalled
 * event loop is reported, not converted into an unbounded burst of audio.
 * Returning false from onTick indicates backpressure and rebases the clock.
 */
export class FrameClock {
  constructor({ onTick, onOverrun, periodMs = 20, maxCatchUpFrames = 4, maxLagMs = 200,
    now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    if (typeof onTick !== 'function' || typeof onOverrun !== 'function' || !(periodMs > 0) ||
        !Number.isInteger(maxCatchUpFrames) || maxCatchUpFrames < 1 || !(maxLagMs >= periodMs)) {
      throw new TypeError('Invalid frame clock options.');
    }
    Object.assign(this, { onTick, onOverrun, periodMs, maxCatchUpFrames, maxLagMs, now, setTimer, clearTimer });
    this.running = false;
    this.timer = null;
  }
  start() {
    if (this.running) return;
    this.running = true;
    this.deadline = this.now() + this.periodMs;
    this.#schedule();
  }
  stop() {
    this.running = false;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }
  #schedule() {
    if (!this.running) return;
    const wait = Math.max(0, Math.ceil(this.deadline - this.now()));
    this.timer = this.setTimer(() => this.#run(), wait);
  }
  #run() {
    this.timer = null;
    if (!this.running) return;
    const current = this.now();
    const lag = current - this.deadline;
    if (lag >= this.maxLagMs) {
      this.stop();
      this.onOverrun(lag);
      return;
    }
    if (lag >= 0) {
      const due = Math.min(this.maxCatchUpFrames, Math.floor(lag / this.periodMs) + 1);
      for (let i = 0; i < due && this.running; i++) {
        const accepted = this.onTick();
        if (!this.running) return;
        if (accepted === false) {
          // A full output Readable has already queued audio; don't try to
          // synthesize a burst of "missed" frames while it is backpressured.
          this.deadline = this.now() + this.periodMs;
          break;
        }
        this.deadline += this.periodMs;
      }
    }
    this.#schedule();
  }
}
