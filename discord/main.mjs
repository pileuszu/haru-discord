import { existsSync } from 'node:fs';
import { Client, GatewayIntentBits, Events, MessageFlags, PermissionFlagsBits, ChannelType } from 'discord.js';
import { readConfig } from './config.mjs';
import { HaruServer } from './server.mjs';
import { VoiceSession } from './voice-session.mjs';

const config = readConfig();
if (!existsSync(config.referencePath)) throw new Error('Korean reference missing. Run ./scripts/haru demo-voice or set HARU_REFERENCE_PATH.');
const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
const server = new HaruServer();
let session = null;
let allowedUserId = config.allowedUserId;
let shuttingDown = false;

server.on('error', error => { console.error(`Model server: ${error.message}`); session?.stop(); });
client.on(Events.Error, error => console.error(`Discord: ${error.message}`));
client.once(Events.ClientReady, async () => {
  try {
    if (client.application.id !== config.applicationId) throw new Error('Token belongs to a different application ID.');
    const app = await client.application.fetch();
    allowedUserId ||= app.owner?.ownerId ?? app.owner?.id;
    if (!allowedUserId) throw new Error('Set DISCORD_ALLOWED_USER_ID for this application.');
    console.log(`Haru bot online. Server ${config.guildId}; controller ${allowedUserId}. Use /haru join in that server.`);
  } catch (error) { console.error(error.message); await shutdown(1); }
});

client.on(Events.VoiceStateUpdate, (before, after) => {
  if (session && (after.id === session.controllerId || after.id === client.user?.id) && after.channelId !== session.channel.id) session.stop();
});

client.on(Events.InteractionCreate, async interaction => {
  if (!interaction.isChatInputCommand() || interaction.commandName !== 'haru') return;
  if (interaction.guildId !== config.guildId || interaction.user.id !== allowedUserId) {
    await interaction.reply({ content: '설정된 서버와 사용자만 Haru를 제어할 수 있습니다.', flags: MessageFlags.Ephemeral }).catch(() => {});
    return;
  }
  try {
    await interaction.deferReply({ flags: MessageFlags.Ephemeral });
    const action = interaction.options.getSubcommand();
    if (action === 'status') {
      await interaction.editReply(session?.status() ?? `대화 대기 중 · MLX ${server.ready ? '준비 완료' : '첫 join 시 로드'}`);
      return;
    }
    if (action === 'leave') { session?.stop(); await interaction.editReply('음성 대화를 종료했습니다.'); return; }
    if (action === 'reset') session?.stop();
    if (session) throw new Error('이미 대화 중입니다. /haru leave 또는 /haru reset을 사용하세요.');
    const guild = interaction.guild;
    const member = await guild.members.fetch(interaction.user.id);
    const channel = member.voice.channel;
    if (!channel || channel.type !== ChannelType.GuildVoice) throw new Error('먼저 일반 음성 채널에 들어가세요.');
    const me = await guild.members.fetchMe();
    if (!channel.permissionsFor(me)?.has([PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect, PermissionFlagsBits.Speak])) {
      throw new Error('봇에 이 채널의 보기·연결·말하기 권한이 필요합니다.');
    }
    if (member.voice.channelId !== channel.id) throw new Error('음성 채널이 변경됐습니다. 다시 /haru join을 실행하세요.');
    if (session) throw new Error('다른 연결 요청이 먼저 시작됐습니다. /haru status를 확인하세요.');
    const current = new VoiceSession({ guild, channel, controllerId: interaction.user.id, server, referencePath: config.referencePath, maxSessionSeconds: config.maxSessionSeconds });
    session = current;
    current.on('error', error => console.error(`Voice session: ${error.message}`));
    current.once('closed', () => { if (session === current) session = null; });
    try { await current.start(); } catch (error) { current.stop(); throw error; }
    await interaction.editReply(`연결했습니다. 지금부터 명령을 실행한 본인의 목소리만 처리합니다. 원본 음성과 대화 내용은 저장하지 않습니다. /haru leave로 종료하며 ${config.maxSessionSeconds / 60}분 후 자동 종료됩니다.`);
  } catch (error) {
    console.error(`Command: ${error.message}`);
    await interaction.editReply(`실행하지 못했습니다: ${error.message}`).catch(() => {});
  }
});

async function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  session?.stop();
  client.destroy();
  await server.stop();
  process.exitCode = code;
}
process.once('SIGINT', () => shutdown());
process.once('SIGTERM', () => shutdown());
client.login(config.token).catch(async error => { console.error(`Discord login failed: ${error.message}`); await shutdown(1); });
