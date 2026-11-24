import { Client, GatewayIntentBits, Events, MessageFlags, PermissionFlagsBits, ChannelType, Options } from 'discord.js';
import { readConfig } from './config.mjs';
import { createBackend } from './backend.mjs';
import { canControl } from './permissions.mjs';
import { ChatChannels, handleChatChannels } from './chat-channels.mjs';
import { canReadMessageContent, createTextHandler } from './text-chat.mjs';

const config = readConfig();
const { server, Session } = await createBackend(config);
const chatChannels = new ChatChannels();
const textEnabled = config.backend === 'gemma-yuna';
const fullContent = textEnabled && await canReadMessageContent(config.token);
const intents = [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates];
if (textEnabled) intents.push(GatewayIntentBits.GuildMessages);
if (fullContent) intents.push(GatewayIntentBits.MessageContent);
const client = new Client({
  intents,
  makeCache: Options.cacheWithLimits({ ...Options.DefaultMakeCacheSettings, MessageManager: 0 }),
});
let session = null;
let shuttingDown = false;

server.on('error', error => { console.error(`Model server: ${error.message}`); session?.stop('worker_error'); });
client.on(Events.Error, error => console.error(`Discord: ${error.message}`));
client.once(Events.ClientReady, async () => {
  try {
    if (client.application.id !== config.applicationId) throw new Error('Token belongs to a different application ID.');
    console.log(`Haru bot online. Backend ${config.backend}; all members of the configured server may control it.`);
    if (textEnabled) console.log(`Haru chat to voice in selected channels: ${fullContent ? 'wake word' : 'bot mention + wake word'}.`);
    await server.start();
    if (!shuttingDown) console.log(`${server.name} ready.`);
  } catch (error) { console.error(error.message); await shutdown(1); }
});

if (textEnabled) {
  const handle = createTextHandler({
    guildId: config.guildId, channels: chatChannels, fullContent,
    getSession: () => session,
  });
  client.on(Events.MessageCreate, message => {
    try { handle(message); } catch (error) { console.error(`Discord chat to voice: ${error.message}`); }
  });
}

client.on(Events.ChannelDelete, channel => {
  if (channel.guildId !== config.guildId || !chatChannels.has(channel.id)) return;
  try { chatChannels.remove([channel.id]); }
  catch (error) { console.error(`Chat channel removal: ${error.message}`); }
});

client.on(Events.VoiceStateUpdate, (before, after) => {
  if (!session || before.guild.id !== session.guild.id) return;
  if (after.id === client.user?.id && session.startedAt && after.channelId !== session.channel.id) session.stop('bot_moved');
  else if (before.channelId === session.channel.id && after.channelId !== session.channel.id) session.removeParticipant(after.id);
});

client.on(Events.InteractionCreate, async interaction => {
  try {
    if (await handleChatChannels(interaction, chatChannels, config.guildId)) return;
  } catch (error) {
    console.error(`Chat channel configuration: ${error.message}`);
    const content = '채팅 채널 설정을 확인하지 못했어요. 잠시 뒤 다시 시도해 주세요.';
    if (interaction.deferred && !interaction.replied) await interaction.editReply({ content }).catch(() => {});
    else if (interaction.replied) await interaction.followUp({ content, flags: MessageFlags.Ephemeral }).catch(() => {});
    else await interaction.reply({ content, flags: MessageFlags.Ephemeral }).catch(() => {});
    return;
  }
  if (!interaction.isChatInputCommand() || interaction.commandName !== 'haru') return;
  if (!canControl(interaction.guildId, config.guildId)) {
    await interaction.reply({ content: '하루가 설정된 서버에서 이 명령을 사용해 주세요.', flags: MessageFlags.Ephemeral }).catch(() => {});
    return;
  }
  try {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const action = interaction.options.getSubcommand();
    if (action === 'status') {
      await interaction.editReply(session?.status() ?? `대화 대기 중 · ${server.name} ${server.ready ? '준비 완료' : '첫 연결 시 로드'}`);
      return;
    }
    if (action === 'leave') { session?.stop('command_leave'); await interaction.editReply('음성 대화를 종료했습니다.'); return; }
    const previousSession = session;
    if (session && action !== 'reset') throw new Error('이미 대화 중입니다. /haru leave 또는 /haru reset을 사용하세요.');
    const guild = interaction.guild;
    const member = await guild.members.fetch(interaction.user.id);
    const channel = member.voice.channel;
    if (!channel || channel.type !== ChannelType.GuildVoice) throw new Error('먼저 일반 음성 채널에 들어가세요.');
    const me = await guild.members.fetchMe();
    if (!channel.permissionsFor(me)?.has([PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak])) {
      throw new Error('봇에 이 채널의 보기·연결·말하기 권한이 필요합니다.');
    }
    if (member.voice.channelId !== channel.id) throw new Error('음성 채널이 변경됐습니다. 다시 /haru join을 실행하세요.');
    if (session !== previousSession) throw new Error('음성 연결이 변경됐습니다. /haru status를 확인하세요.');
    if (action === 'reset') previousSession?.stop('command_reset');
    const current = new Session({ guild, channel, server, referencePath: config.referencePath, maxSessionSeconds: config.maxSessionSeconds, language: config.language });
    session = current;
    current.on('error', error => console.error(`Voice session: ${error.message}`));
    current.once('closed', () => { if (session === current) session = null; });
    try { await current.start(); } catch (error) { current.stop('startup_failed'); throw error; }
    const expiry = config.maxSessionSeconds ? ` ${config.maxSessionSeconds / 60}분 후 자동 종료됩니다.` : '';
    if (config.backend === 'gemma-yuna') {
      await interaction.editReply(`연결했습니다. “하루야”, “하루”, “하루찡”, “하루짱” 중 하나로 부르고 질문하면 유나 목소리로 답합니다. 등록된 채팅 채널에서 같은 호출어로 질문해도 이 음성 채널에서 답합니다. 먼저 부른 분은 잠깐 쉬었다가 질문해도 됩니다. 검색이 필요한 질문은 핵심 검색어만 외부 검색 서비스에 보냅니다. 호출어를 인식하려고 이 Mac에서 참가자의 음성을 실시간으로 전사한다는 점을 채널에 알려주세요. /haru leave로 종료합니다.${expiry}`);
      return;
    }
    await interaction.editReply(`연결했습니다. 이 음성 채널의 모든 사람의 말을 듣고 실시간으로 대화합니다. 호출어 없이 말할 수 있습니다. 채널 참가자에게 봇이 듣고 있고 모델의 출력 텍스트가 Mac의 진단 로그에 저장됨을 알려주세요. 원본 음성은 저장하지 않고 입력 음성의 별도 STT 전사는 제공하지 않습니다. /haru leave로 종료합니다.${expiry}`);

  } catch (error) {
    console.error(`Command: ${error.message}`);
    await interaction.editReply(`실행하지 못했습니다: ${error.message}`).catch(() => {});
  }
});

async function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  session?.stop('shutdown');
  client.destroy();
  await server.stop();
  process.exitCode = code;
}
process.once('SIGINT', () => shutdown());
process.once('SIGTERM', () => shutdown());
client.login(config.token).catch(async error => { console.error(`Discord login failed: ${error.message}`); await shutdown(1); });
