import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { Readable } from 'node:stream';
import * as discordVoice from '@discordjs/voice';
import { HaruClient } from './haru-client.mjs';
import { PcmQueue, float32MonoToPcm16Stereo } from './audio.mjs';
import { VoiceMixer } from './mixer.mjs';
import { Participants } from './participants.mjs';
import { VoiceMetrics } from './voice-metrics.mjs';
import { VoiceTraceLog } from './trace-log.mjs';
import { FrameClock } from './frame-clock.mjs';
import { sessionPrompt } from './character-cards.mjs';

// The pinned native DuplexConfig ends after 27,000 frames (about 60 minutes).
const MAX_MODEL_FRAMES = 27_000;

// The Discord player consumes 48 kHz stereo PCM in 20 ms blocks.
class ClockedAudio extends Readable {
  constructor(queue, metrics) {
    super({ highWaterMark: 3840 * 3 });
    this.blocked = false;
    this.clock = new FrameClock({
      onTick: () => {
        if (this.blocked || this.destroyed) return false;
        this.blocked = !this.push(metrics.popPlayback(queue));
        return !this.blocked;
      },
      onOverrun: () => this.destroy(new Error('Playback clock stalled. Restart the voice session.')),
    });
    this.clock.start();
  }
  _read() { this.blocked = false; }
  _destroy(error, callback) { this.clock.stop(); callback(error); }
}

export class VoiceSession extends EventEmitter {
  constructor({ guild, channel, server, referencePath, maxSessionSeconds = 0, language = 'ko', voiceApi = discordVoice, createHaru = options => new HaruClient(options), makeParticipants = options => new Participants(options), trace = new VoiceTraceLog() }) {
    super();
    Object.assign(this, { guild, channel, server, referencePath, maxSessionSeconds, language, voiceApi, createHaru, makeParticipants, trace });
    this.sessionId = randomUUID();
    this.traceWarned = false;
    this.state = '연결 중';
    this.closed = false;
    this.startRequested = false;
    this.receivedPackets = 0;
    this.generatedFrames = 0;
    this.input = new VoiceMixer();
    this.output = new PcmQueue(3840, 25);
    this.metrics = new VoiceMetrics();
  }
  #trace(event, fields = {}) {
    try {
      if (this.trace.write(event, { session_id: this.sessionId, ...fields }) === false && !this.traceWarned) {
        this.traceWarned = true; console.error('Voice debug log unavailable.');
      }
    } catch {
      if (!this.traceWarned) { this.traceWarned = true; console.error('Voice debug log unavailable.'); }
    }
  }
  async start() {
    if (this.startRequested || this.closed) throw new Error('Voice session can only be started once.');
    this.startRequested = true;
    const api = this.voiceApi;
    const fail = (error, reason = 'session_error') => {
      if (!this.closed) { this.#trace('haru_error', { reason, name: ['Error','TypeError','RangeError'].includes(error?.name) ? error.name : 'other' }); this.emit('error', error); this.stop(reason); }
    };
    this.connection = api.joinVoiceChannel({ channelId: this.channel.id, guildId: this.guild.id, adapterCreator: this.guild.voiceAdapterCreator, selfDeaf: false, selfMute: false });
    this.connection.on('error', error => fail(error, 'connection_error'));
    this.connection.on(api.VoiceConnectionStatus.Disconnected, async () => {
      // Give transient voice gateway reconnects the opportunity to recover.
      try {
        await Promise.race([
          api.entersState(this.connection, api.VoiceConnectionStatus.Signalling, 5000),
          api.entersState(this.connection, api.VoiceConnectionStatus.Connecting, 5000),
        ]);
      } catch { fail(new Error('Discord voice disconnected. Run /haru join again.'), 'connection_disconnected'); }
    });
    await Promise.all([api.entersState(this.connection, api.VoiceConnectionStatus.Ready, 30_000), this.server.start()]);
    if (this.closed) throw new Error('Conversation was cancelled.');
    await this.openModel(fail);
    if (this.closed) throw new Error('Conversation was cancelled.');
    const sink = this.inputSink(fail);
    this.participants = this.makeParticipants({
      receiver: this.connection.receiver, guild: this.guild, channelId: this.channel.id,
      botId: this.guild.client.user.id, sink,
      onPacket: () => { this.receivedPackets += 1; },
      onError: (error, userId) => {
        this.#trace('participant_error', { user_id: userId, name: ['Error','TypeError','RangeError'].includes(error?.name) ? error.name : 'other' });
        console.error('Participant audio stream failed.');
      },
    });
    this.participants.start();
    this.playback = new ClockedAudio(this.output, this.metrics);
    this.playback.on('error', error => fail(error, 'playback_error'));
    this.player = api.createAudioPlayer({ behaviors: { noSubscriber: api.NoSubscriberBehavior.Play } });
    this.player.on('error', error => fail(error, 'player_error'));
    this.connection.subscribe(this.player);
    this.player.play(api.createAudioResource(this.playback, { inputType: api.StreamType.Raw }));
    this.startInputClock(fail);
    if (this.maxSessionSeconds > 0) this.expiry = setTimeout(() => this.stop('session_limit'), this.maxSessionSeconds * 1000);
    this.startedAt = Date.now();
    this.state = '대화 중';
    this.#trace('haru_session_start', { guild_id: this.guild.id, channel_id: this.channel.id, language: this.language, max_session_seconds: this.maxSessionSeconds });
    console.log(`Voice session started: ${this.status()}`);
    this.diagnosticsTimer = setInterval(() => {
      this.metrics.snapshot();
      console.log(`Voice status: ${this.status()}`);
    }, 5000);
  }
  async openModel(fail) {
    this.haru = this.createHaru({ socketPath: this.server.socketPath });
    this.haru.on('error', error => fail(error, 'haru_error'));
    this.haru.on('close', () => fail(new Error('Haru conversation socket closed.'), 'haru_closed'));
    this.haru.on('audio', (pcm, frame) => {
      if (this.closed) return;
      this.generatedFrames += 1;
      if (this.output.push(float32MonoToPcm16Stereo(pcm)) > 0) {
        fail(new Error('Haru output buffer overflow. Restart the voice session.'), 'output_overflow');
        return;
      }
      this.metrics.observeAudio('output', pcm);
      if (frame?.index >= MAX_MODEL_FRAMES - 1 && !this.modelLimitTimer) {
        // Native sends no more model frames after this one but continues reading
        // input. Stop feeding audio, let the bounded last output drain, then close.
        this.inputClock?.stop();
        this.state = '모델 대화 한도 도달';
        this.#trace('haru_model_frame_limit', { model_frame_index: frame.index });
        const drainMs = Math.ceil(this.output.pendingBytes / 192) + 120;
        this.modelLimitTimer = setTimeout(() => fail(new Error('Haru reached its model conversation limit. Run /haru join again.'), 'model_frame_limit'), drainMs);
      }
    });
    this.haru.on('step', ({ index, stepMs }) => {
      if (!this.closed && index > 0) this.metrics.observeStep(stepMs);
    });
    this.haru.on('text', text => {
      this.metrics.observeText(text);
      // This is Haru-generated text, not a transcript of participants.
      if (text) this.#trace('haru_model_text', { text });
    });
    await this.haru.start({ language: this.language, reference_path: this.referencePath, guidance_scale: 1.5, ...sessionPrompt(this.language) });
  }
  inputSink(fail) {
    return {
      add: id => this.input.add(id),
      remove: id => this.input.remove(id),
      push: (id, pcm) => {
        const previousDrops = this.input.droppedFrames;
        this.input.push(id, pcm);
        if (this.input.droppedFrames > previousDrops) fail(new Error('Participant audio buffer overflow. Restart the voice session.'), 'input_overflow');
      },
    };
  }
  startInputClock(fail) {
    this.inputClock = new FrameClock({
      onTick: () => {
        try {
          const frame = this.input.frame();
          this.metrics.observeAudio('input', frame);
          if (!this.haru.sendAudio(frame)) {
            // A discarded block compresses the model's input time axis. Stop
            // instead of producing misleading, distorted replies.
            fail(new Error('Haru is falling behind and audio was dropped. Restart the voice session.'), 'model_input_overflow');
          }
        } catch (error) { fail(error, 'input_error'); }
      },
      onOverrun: () => fail(new Error('Input audio clock stalled. Restart the voice session.'), 'input_clock_overrun'),
    });
    this.inputClock.start();
  }
  status() {
    const socketDropped = this.haru?.droppedAudioBytes || 0;
    const modelPendingMs = Math.round((this.haru?.pendingAudioBytes || 0) / 192);
    return `${this.state} · 수신 패킷 ${this.receivedPackets} · 모델 프레임 ${this.generatedFrames} · 입력 대기 ${Math.round(this.input.pendingBytes / 192)}ms · 모델 대기 ${modelPendingMs}ms · 출력 대기 ${Math.round(this.output.pendingBytes / 192)}ms · 입출력 버린 프레임 ${this.input.droppedFrames + this.output.droppedFrames} · 모델 입력 버림 ${Math.round(socketDropped / 192)}ms · ${this.metrics.status()}`;
  }
  removeParticipant(userId) { this.participants?.remove(userId); }
  stop(reason = 'unspecified') {
    if (this.closed) return;
    this.closed = true;
    this.state = '종료';
    this.inputClock?.stop();
    clearTimeout(this.expiry);
    clearTimeout(this.modelLimitTimer);
    clearInterval(this.diagnosticsTimer);
    this.#trace('haru_session_stop', { reason, received_packets: this.receivedPackets, generated_frames: this.generatedFrames, input_dropped_frames: this.input.droppedFrames, output_dropped_frames: this.output.droppedFrames, model_input_dropped_bytes: this.haru?.droppedAudioBytes || 0, playback_underflows: this.metrics.playbackUnderflows, model_steps: this.metrics.stepSummary });
    console.log(`Voice session stopped: ${this.status()}`);
    this.participants?.stop();
    this.playback?.destroy();
    this.player?.stop(true);
    this.haru?.close();
    if (this.connection?.state.status !== this.voiceApi.VoiceConnectionStatus.Destroyed) this.connection?.destroy();
    this.input.clear();
    this.output.clear();
    this.emit('closed');
  }
}
