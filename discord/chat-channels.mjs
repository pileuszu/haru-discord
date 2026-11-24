import { randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import {
  ActionRowBuilder, ChannelSelectMenuBuilder, ChannelType, MessageFlags, PermissionFlagsBits, StringSelectMenuBuilder,
} from 'discord.js';
import { ROOT } from './config.mjs';

const { ViewChannel } = PermissionFlagsBits;
const channelTypes = [ChannelType.GuildText, ChannelType.GuildAnnouncement, ChannelType.GuildVoice];
const addMenuId = 'haru-chat:add';
const removeMenuId = 'haru-chat:remove';
const ephemeral = { flags: MessageFlags.Ephemeral, allowedMentions: { parse: [] } };

export class ChatChannels {
  constructor(path = join(ROOT, '.runtime', 'chat-channels.json')) {
    this.path = path;
    this.entries = new Map();
    try {
      const rows = JSON.parse(readFileSync(path, 'utf8'));
      if (!Array.isArray(rows) || !rows.every(row => Array.isArray(row) && row.length === 2 &&
          /^\d{17,22}$/.test(row[0]) && typeof row[1] === 'string' && row[1].length === 36)) {
        throw new Error('Invalid channel settings');
      }
      this.entries = new Map(rows);
    } catch (error) {
      if (error.code !== 'ENOENT') {
        this.readFailed = true;
        console.error('하루의 채팅 채널 설정 파일을 읽지 못했습니다.');
      }
    }
  }

  has(id) { return this.entries.has(id); }
  key(id) { return this.entries.get(id); }
  ids() {
    if (this.readFailed) throw new Error('저장된 채팅 채널 설정을 읽지 못했습니다.');
    return [...this.entries.keys()];
  }

  add(ids) {
    const next = new Map(this.entries);
    for (const id of ids) if (!next.has(id)) next.set(id, randomUUID());
    this.save(next);
  }

  remove(ids) {
    const next = new Map(this.entries);
    for (const id of ids) next.delete(id);
    this.save(next);
  }

  save(next) {
    if (this.readFailed) throw new Error('채팅 채널 설정 파일을 확인해 주세요.');
    if (this.entries.size === next.size) return;
    mkdirSync(dirname(this.path), { recursive: true });
    const temporary = this.path + '.' + randomUUID() + '.tmp';
    try {
      writeFileSync(temporary, JSON.stringify([...next]) + '\n', { mode: 0o600, flag: 'wx' });
      renameSync(temporary, this.path);
      this.entries = next;
    } finally {
      rmSync(temporary, { force: true });
    }
  }
}

function visibleChannels(interaction, store) {
  return store.ids().map(id => interaction.guild.channels.cache.get(id))
    .filter(channel => channel?.permissionsFor(interaction.member)?.has(ViewChannel));
}

function batches(items, size) {
  const result = [];
  for (let i = 0; i < items.length; i += size) result.push(items.slice(i, i + size));
  return result;
}

async function showSelections(interaction, store, remove) {
  const channels = visibleChannels(interaction, store);
  if (!channels.length) {
    return interaction.reply({ ...ephemeral, content: '볼 수 있는 채널 중 등록된 곳이 없어요. /haru chat add로 선택해 주세요.' });
  }
  // A select supports 25 options. Send each page privately so every registered channel remains reachable.
  const pages = batches(channels, remove ? 25 : 65);
  for (const [index, page] of pages.entries()) {
    const payload = { ...ephemeral };
    if (remove) {
      payload.content = '해제할 채널을 선택해 주세요.';
      const menu = new StringSelectMenuBuilder().setCustomId(removeMenuId)
        .setPlaceholder('호출어 채팅을 끌 채널').setMinValues(1).setMaxValues(page.length);
      menu.addOptions(page.map(channel => ({ label: channel.name, value: store.key(channel.id) })));
      payload.components = [new ActionRowBuilder().addComponents(menu)];
    } else {
      payload.content = '하루가 호출을 기다리는 채널\n' + page.map(channel => '<#' + channel.id + '>').join('\n');
    }
    if (index === 0) await interaction.reply(payload);
    else await interaction.followUp(payload);
  }
}

export async function handleChatChannels(interaction, store, guildId) {
  const command = interaction.isChatInputCommand() && interaction.commandName === 'haru' &&
    interaction.options.getSubcommandGroup(false) === 'chat';
  const adding = interaction.isChannelSelectMenu() && interaction.customId === addMenuId;
  const removing = interaction.isStringSelectMenu() && interaction.customId === removeMenuId;
  if (!command && !adding && !removing) return false;
  if (!interaction.guild || interaction.guildId !== guildId) {
    await interaction.reply({ ...ephemeral, content: '하루가 설정된 서버에서 사용해 주세요.' });
    return true;
  }
  if (command) {
    const action = interaction.options.getSubcommand();
    if (action === 'add') {
      const menu = new ChannelSelectMenuBuilder().setCustomId(addMenuId).setChannelTypes(channelTypes)
        .setMinValues(1).setMaxValues(25).setPlaceholder('하루를 글로 부를 채널');
      await interaction.reply({ ...ephemeral, content: '글로 하루를 부를 채널을 최대 25개 선택해 주세요. 하루는 /haru join으로 연결된 음성 채널에서 말해요.',
        components: [new ActionRowBuilder().addComponents(menu)] });
    } else {
      await showSelections(interaction, store, action === 'remove');
    }
    return true;
  }

  // Resolve selections against current guild permissions, including changes while a menu was open.
  let content;
  if (adding) {
    const accepted = [];
    for (const id of interaction.values) {
      const channel = interaction.guild.channels.cache.get(id);
      if (channelTypes.includes(channel?.type) && channel.permissionsFor(interaction.member)?.has(ViewChannel) &&
          channel.permissionsFor(interaction.guild.members.me)?.has(ViewChannel)) accepted.push(id);
    }
    store.add(accepted);
    content = accepted.length ? '호출어 채팅이 켜진 채널\n' + accepted.map(id => '<#' + id + '>').join('\n') : '';
    if (accepted.length < interaction.values.length) {
      content += (content ? '\n' : '') + '등록하지 못한 채널이 있어요. 채널 종류와 하루의 채널 보기 권한을 확인해 주세요.';
    }
  } else {
    const selected = new Set(interaction.values);
    const ids = visibleChannels(interaction, store).filter(channel => selected.has(store.key(channel.id))).map(channel => channel.id);
    store.remove(ids);
    content = ids.length ? '호출어 채팅을 끈 채널\n' + ids.map(id => '<#' + id + '>').join('\n') : '이미 해제됐거나 목록이 바뀌었어요.';
  }
  await interaction.update({ content, components: [], allowedMentions: { parse: [] } });
  return true;
}
