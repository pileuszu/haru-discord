import assert from 'node:assert/strict';
import test from 'node:test';
import { ChannelType, PermissionFlagsBits } from 'discord.js';
import { createTextHandler } from '../discord/text-chat.mjs';
import { GemmaVoiceSession } from '../discord/gemma-session.mjs';

function fixture({ fullContent = true } = {}) {
  const calls = [];
  const channels = new Map([['10', 'registration-1'], ['20', 'registration-2'], ['30', 'registration-3']]);
  const guild = { id: 'guild', members: { me: { id: 'bot' } } };
  const session = {
    guild, startedAt: 1, closed: false,
    channel: { id: '20', permissionsFor: member => ({ has: bits => member.canListen && bits.includes(PermissionFlagsBits.Connect) }) },
    submitText(...args) { calls.push(args); return true; },
  };
  const state = { session };
  function message(text, override = {}) {
    return {
      guild, guildId: guild.id, channelId: '10', author: { id: '1' }, member: { canListen: true }, content: text,
      channel: { type: ChannelType.GuildText,
        permissionsFor: who => ({ has: flag => who === guild.members.me && flag === PermissionFlagsBits.ViewChannel }),
        send: () => assert.fail('no answer should be posted to a text channel') },
      reply: () => assert.fail('no answer should be posted to the original message'),
      client: { user: { id: '999' } }, ...override,
    };
  }
  const handle = createTextHandler({
    guildId: guild.id, channels: { has: id => channels.has(id), key: id => channels.get(id) },
    fullContent, getSession: () => state.session,
  });
  return { calls, channels, state, session, handle, message };
}

test('addressed messages go to the current voice session, never to Discord text', () => {
  const t = fixture();
  for (const text of ['오늘 하루야?', '다들 안녕', '내일 뭐 해?']) t.handle(t.message(text));
  assert.deepEqual(t.calls, []);
  for (const text of ['하루야 수도는?', '안녕, 하루 뭘 먹을까?', '하루짱 밥 먹었어?', '하루찡, 안녕']) {
    t.handle(t.message(text));
    assert.deepEqual(t.calls.at(-1), ['registration-1', '1', text]);
  }
  assert.equal(t.calls.length, 4);
});

test('unselected channels, other guilds, bots, webhooks and sessions without a destination never inspect text', () => {
  const t = fixture();
  function inspect(override) {
    const msg = t.message('', override);
    Object.defineProperty(msg, 'content', { get: () => assert.fail('read disallowed text') });
    t.handle(msg);
  }
  for (const override of [{ guildId: 'other' }, { guildId: null }, { channelId: '99' },
    { author: { id: 'bot', bot: true } }, { webhookId: 'webhook' }, { system: true }, { member: null }]) inspect(override);
  inspect({ member: { canListen: false } });
  inspect({ channel: { type: ChannelType.GuildText, permissionsFor: () => ({ has: () => false }) } });
  for (const session of [null, { ...t.session, startedAt: 0 }, { ...t.session, closed: true },
    { ...t.session, guild: { id: 'other' } }]) {
    t.state.session = session;
    inspect({});
  }
  assert.deepEqual(t.calls, []);
});

test('a voice channel text chat can only be heard in its own voice channel', () => {
  const t = fixture();
  const otherVoiceText = t.message('하루야 안녕', { channelId: '30' });
  otherVoiceText.channel.type = ChannelType.GuildVoice;
  t.handle(otherVoiceText);
  assert.deepEqual(t.calls, []);
  const sameVoiceText = t.message('하루야 안녕', { channelId: '20' });
  sameVoiceText.channel.type = ChannelType.GuildVoice;
  t.handle(sameVoiceText);
  assert.deepEqual(t.calls, [['registration-2', '1', '하루야 안녕']]);
});

test('mention fallback still requires addressing Haru', () => {
  const t = fixture({ fullContent: false });
  for (const text of ['하루야 수도는?', '<@999> 수도는?', '<@123> 하루야 수도는?', '']) t.handle(t.message(text));
  assert.equal(t.calls.length, 0);
  for (const prefix of ['<@999> ', '<@!999>']) t.handle(t.message(prefix + '하루야 수도는?'));
  assert.deepEqual(t.calls.map(args => args[2]), ['하루야 수도는?', '하루야 수도는?']);
});

test('a bare candidate permits one follow-up to the authoritative gate of the same room, speaker and session', () => {
  const t = fixture();
  t.handle(t.message('하루짱!'));
  t.handle(t.message('어디야?', { author: { id: '2' } }));
  t.handle(t.message('거리야?', { channelId: '20' }));
  t.handle(t.message('대한민국의 수도는?'));
  t.handle(t.message('내일은?'));
  assert.deepEqual(t.calls.map(args => args[2]), ['하루짱!', '대한민국의 수도는?']);
  t.handle(t.message('하루야'));
  t.state.session = { ...t.session };
  t.handle(t.message('지난 통화에서 부른 질문'));
  t.handle(t.message('하루야'));
  t.channels.delete('10');
  t.handle(t.message('삭제된 채널'));
  t.channels.set('10', 'new-registration');
  t.handle(t.message('이전에 부른 질문'));
  assert.deepEqual(t.calls.slice(2).map(args => args[2]), ['하루야', '하루야']);
});

test('typed input uses the live voice socket with playback overlap and is ignored before readiness or after disconnect', () => {
  const records = [];
  const session = new GemmaVoiceSession({ guild: {}, channel: {}, server: {}, trace: {} });
  session.bridge = { send: message => { records.push(message); return true; } };
  session.connection = { state: { status: session.voiceApi.VoiceConnectionStatus.Ready } };
  assert.equal(session.submitText('room', 'member', '하루야 질문'), false);
  session.startedAt = 1;
  session.lastAudio = -Infinity;
  assert.ok(session.submitText('room', 'member', '하루야 질문'));
  session.output.push(Buffer.alloc(3840));
  assert.ok(session.submitText('room', 'member', '하루야 정정'));
  session.connection.state.status = session.voiceApi.VoiceConnectionStatus.Disconnected;
  assert.equal(session.submitText('room', 'member', '하루야 지금은?'), false);
  session.closed = true;
  assert.equal(session.submitText('room', 'member', '하루야 지금은?'), false);
  assert.deepEqual(records, [
    { type: 'text', channel: 'room', speaker: 'member', text: '하루야 질문', overlap: false },
    { type: 'text', channel: 'room', speaker: 'member', text: '하루야 정정', overlap: true },
  ]);
});
