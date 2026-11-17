import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { createAudioPlayer, createAudioResource, joinVoiceChannel, entersState, VoiceConnectionStatus, NoSubscriberBehavior, StreamType, EndBehaviorType } from '@discordjs/voice';
import prism from 'prism-media';
import { HaruClient } from './haru-client.mjs';
import { PcmQueue, pcm16StereoToFloat32Mono, float32MonoToPcm16Stereo } from './audio.mjs';

// The Discord player consumes 48 kHz stereo PCM in 20 ms blocks.
class ClockedAudio extends Readable {
  constructor(queue) {
    super({ highWaterMark: 3840 * 3 });
    this.blocked = false;
    this.timer = setInterval(() => {
      if (!this.blocked && !this.destroyed) this.blocked = !this.push(queue.popFrame());
    }, 20);
  }
  _read() { this.blocked = false; }
  _destroy(error, callback) { clearInterval(this.timer); callback(error); }
}

export class VoiceSession extends EventEmitter {
  constructor({ guild, channel, controllerId, server, referencePath, maxSessionSeconds }) {
    super();
    Object.assign(this, { guild, channel, controllerId, server, referencePath, maxSessionSeconds });
    this.state = 'connecting';
    this.closed = false;
    this.receivedPackets = 0;
    this.receivedPcmBytes = 0;
    this.generatedFrames = 0;
    this.input = new PcmQueue(3840, 10);
    this.output = new PcmQueue(3840, 25);
  }
  async start() {
    const fail = error => { if (!this.closed) { this.emit('error', error); this.stop(); } };
    this.connection = joinVoiceChannel({ channelId: this.channel.id, guildId: this.guild.id, adapterCreator: this.guild.voiceAdapterCreator, selfDeaf: false, selfMute: false });
    this.connection.on('error', fail);
    this.connection.on(VoiceConnectionStatus.Disconnected, () => fail(new Error('Discord voice disconnected. Run /haru join again.')));
    await Promise.all([entersState(this.connection, VoiceConnectionStatus.Ready, 30_000), this.server.start()]);
    if (this.closed) throw new Error('Conversation was cancelled.');
    this.haru = new HaruClient({ socketPath: this.server.socketPath });
    this.haru.on('error', fail);
    this.haru.on('close', () => fail(new Error('Haru conversation socket closed.')));
    this.haru.on('audio', pcm => {
      this.generatedFrames += 1;
      this.output.push(float32MonoToPcm16Stereo(pcm));
    });
    await this.haru.start({ language: 'ko', reference_path: this.referencePath, guidance_scale: 1.5, card: {
      name: '하루', gender: 'female', backchannel: 'mid',
      persona: '한국어로 대화하는 친근한 친구. 상대의 이야기를 듣고 편안하게 자기 생각을 나눈다.',
      relationship: '디스코드 음성 채널에서 처음 만난 친구이다.',
      situation: '음성 통화로 서로 자기소개를 하고 일상과 취미 이야기를 나누고 있다.',
    } });
    if (this.closed) throw new Error('Conversation was cancelled.');
    this.opus = this.connection.receiver.subscribe(this.controllerId, { end: { behavior: EndBehaviorType.Manual } });
    this.decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
    this.opus.on('data', () => { this.receivedPackets += 1; });
    this.opus.on('error', fail);
    this.decoder.on('error', fail);
    this.decoder.on('data', pcm => { this.receivedPcmBytes += pcm.length; this.input.push(pcm); });
    this.opus.pipe(this.decoder);
    this.playback = new ClockedAudio(this.output);
    this.playback.on('error', fail);
    this.player = createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Play } });
    this.player.on('error', fail);
    this.connection.subscribe(this.player);
    this.player.play(createAudioResource(this.playback, { inputType: StreamType.Raw }));
    let stalled = 0;
    this.inputTimer = setInterval(() => {
      try {
        const accepted = this.haru.sendAudio(pcm16StereoToFloat32Mono(this.input.popFrame()));
        stalled = accepted ? 0 : stalled + 1;
        if (stalled >= 50) fail(new Error('Haru is falling behind. Conversation stopped to avoid growing delay.'));
      } catch (error) { fail(error); }
    }, 20);
    this.expiry = setTimeout(() => this.stop(), this.maxSessionSeconds * 1000);
    this.startedAt = Date.now();
    this.state = 'listening';
  }
  status() {
    return `${this.state} · 수신 패킷 ${this.receivedPackets} · 모델 프레임 ${this.generatedFrames} · 입력 대기 ${Math.round(this.input.pendingBytes / 192)}ms · 출력 대기 ${Math.round(this.output.pendingBytes / 192)}ms · 버린 프레임 ${this.input.droppedFrames + this.output.droppedFrames}`;
  }
  stop() {
    if (this.closed) return;
    this.closed = true;
    this.state = 'closed';
    clearInterval(this.inputTimer);
    clearTimeout(this.expiry);
    this.opus?.unpipe(this.decoder);
    this.opus?.destroy();
    this.decoder?.destroy();
    this.playback?.destroy();
    this.player?.stop(true);
    this.haru?.close();
    if (this.connection?.state.status !== VoiceConnectionStatus.Destroyed) this.connection?.destroy();
    this.input.clear();
    this.output.clear();
    this.emit('closed');
  }
}
