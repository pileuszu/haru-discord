import { ApplicationFlagsBitField, ChannelType, PermissionFlagsBits, REST, Routes } from 'discord.js';

const separator = '[\\s,.!?…~，。！？、:：—–-]';
const possibleWake = new RegExp('^(?:안녕' + separator + '+)?하[루룻]');
// Only the local bridge decides whether this actually arms a follow-up.
const possibleStandalone = new RegExp('^(?:안녕' + separator + '+)?하[루룻](?:[야짱찡징장층]|' + separator + ')*$');

export async function canReadMessageContent(token) {
  try {
    const app = await new REST({ version: '10', timeout: 5000, retries: 0 }).setToken(token).get(Routes.currentApplication());
    const flags = ApplicationFlagsBitField.Flags;
    return Boolean(app.flags & (flags.GatewayMessageContent | flags.GatewayMessageContentLimited));
  } catch {
    return false;
  }
}

export function createTextHandler({ guildId, channels, fullContent, getSession }) {
  const arms = new Map();
  return function handle(message) {
    // Gateway content is guild-wide. Check the selected source and audible destination first.
    if (message.guildId !== guildId || !channels.has(message.channelId) ||
        !message.author || message.author.bot || message.webhookId || message.system) return;
    const session = getSession();
    if (!session?.startedAt || session.closed || session.guild.id !== guildId || !message.member) return;
    if (message.channel.type === ChannelType.GuildVoice && message.channelId !== session.channel.id) return;
    if (!message.channel.permissionsFor(message.guild.members.me)?.has(PermissionFlagsBits.ViewChannel) ||
        !session.channel.permissionsFor(message.member)?.has([PermissionFlagsBits.ViewChannel, PermissionFlagsBits.Connect])) return;
    const channel = channels.key(message.channelId);
    if (!channel) return;

    let text = message.content.normalize('NFC').trim();
    const mention = text.match(/^<@!?(\d+)>\s*/u);
    if (mention && mention[1] === message.client.user.id) text = text.slice(mention[0].length);
    else if (!fullContent) return;
    if (!text) return;

    const speaker = message.author.id;
    const source = channel + ':' + speaker;
    const prior = arms.get(source);
    if (prior) {
      clearTimeout(prior.timer);
      arms.delete(source);
    }
    if (!possibleWake.test(text) && prior?.session !== session) return;
    // Python applies the same authoritative gate as microphone input. An admitted
    // question enters the current conversation and its audio follows normal playback.
    if (session.submitText(channel, speaker, text) && possibleStandalone.test(text)) {
      const timer = setTimeout(() => arms.delete(source), 5000).unref();
      arms.set(source, { session, timer });
    }
  };
}
