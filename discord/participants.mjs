import { EndBehaviorType } from '@discordjs/voice';
import prism from 'prism-media';

/** Receive each eligible human speaker independently; never include bot audio. */
export class Participants {
  constructor({ receiver, guild, channelId, botId, sink, onPacket = () => {}, onError = () => {}, makeDecoder = () => new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 }) }) {
    Object.assign(this, { receiver, guild, channelId, botId, sink, onPacket, onError, makeDecoder });
    this.active = new Map();
    this.pending = new Map();
    this.running = false;
    this.speakingStart = id => { this.#consider(id); };
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.receiver.speaking.on('start', this.speakingStart);
    for (const id of this.receiver.speaking.users.keys()) this.#consider(id);
  }

  #consider(id) {
    if (!this.running || !id || id === this.botId || this.active.has(id) || this.pending.has(id)) return;
    const state = this.guild.voiceStates.cache.get(id);
    if (!state || state.channelId !== this.channelId) return;
    const member = state.member || this.guild.members.cache.get(id);
    if (member?.user && typeof member.user.bot === 'boolean') {
      if (!member.user.bot) this.#subscribe(id);
      return;
    }
    // A cached voice state can exist without a member. Fetch only that one user;
    // re-check the state after the network call to avoid subscribing on departure.
    const token = {};
    this.pending.set(id, token);
    Promise.resolve().then(() => this.guild.members.fetch(id)).then(fetched => {
      if (!this.running || this.pending.get(id) !== token) return;
      this.pending.delete(id);
      const current = this.guild.voiceStates.cache.get(id);
      if (current?.channelId === this.channelId && fetched?.user?.bot === false && id !== this.botId) this.#subscribe(id);
    }).catch(error => {
      if (this.pending.get(id) === token) {
        this.pending.delete(id);
        if (this.running) this.onError(error, id);
      }
    });
  }

  #subscribe(id) {
    if (!this.running || this.active.has(id) || this.guild.voiceStates.cache.get(id)?.channelId !== this.channelId) return;
    let opus;
    let decoder;
    let added = false;
    try {
      // The installed receiver returns a cached stream until its close event.
      // Do not reuse a stream still closing after a receive/decrypt failure.
      const existing = this.receiver.subscriptions?.get(id);
      if (existing?.destroyed) return;
      // A bounded consumer may decline admission before any decoder is allocated.
      // Only explicit false declines admission; the sink may return nothing on success.
      if (this.sink.add(id) === false) return;
      added = true;
      decoder = this.makeDecoder(id);
      opus = this.receiver.subscribe(id, { end: { behavior: EndBehaviorType.Manual } });
      const entry = { opus, decoder };
      this.active.set(id, entry);
      const current = () => this.running && this.active.get(id) === entry;
      const handleError = error => {
        if (!current()) return;
        this.onError(error, id);
        this.remove(id);
      };
      opus.on('error', handleError);
      decoder.on('error', handleError);
      opus.once('close', () => { if (current()) this.remove(id); });
      decoder.once('close', () => { if (current()) this.remove(id); });
      opus.on('data', packet => { if (current()) this.onPacket(packet, id); });
      decoder.on('data', pcm => {
        if (!current()) return;
        this.sink.push(id, pcm);
      });
      opus.pipe(decoder);
    } catch (error) {
      if (this.active.has(id)) this.remove(id);
      else {
        if (added) this.sink.remove(id);
        opus?.destroy();
        decoder?.destroy();
      }
      if (this.running) this.onError(error, id);
    }
  }

  remove(id) {
    this.pending.delete(id);
    const entry = this.active.get(id);
    if (!entry) return;
    // Delete first: close/error callbacks can fire as a consequence of destroy.
    this.active.delete(id);
    this.sink.remove(id);
    entry.opus.unpipe(entry.decoder);
    entry.opus.destroy();
    entry.decoder.destroy();
  }

  stop() {
    if (!this.running) return;
    this.running = false;
    this.receiver.speaking.off('start', this.speakingStart);
    this.pending.clear();
    for (const id of [...this.active.keys()]) this.remove(id);
  }
}
