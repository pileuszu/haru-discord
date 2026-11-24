import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { ChannelType, PermissionFlagsBits } from 'discord.js';
import { command } from '../discord/commands.mjs';
import { ChatChannels, handleChatChannels } from '../discord/chat-channels.mjs';

const first = '10000000000000001', second = '10000000000000002', hidden = '10000000000000003';
function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'haru-test-channels-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, 'channels.json');
  const store = new ChatChannels(path);
  const guild = { members: { me: 'bot' }, channels: { cache: new Map() } };
  for (const id of [first, second, hidden]) {
    guild.channels.cache.set(id, { id, type: ChannelType.GuildText, name: '채널' + id.at(-1),
      permissionsFor: who => ({ has: permission => permission === PermissionFlagsBits.ViewChannel && (id !== hidden || who === 'bot') }) });
  }
  function interaction(action, values) {
    const menu = Boolean(values);
    return {
      member: 'ordinary member', guild, guildId: 'guild', commandName: 'haru',
      options: { getSubcommandGroup: () => 'chat', getSubcommand: () => action },
      values, customId: 'haru-chat:' + action,
      isChatInputCommand: () => !menu, isChannelSelectMenu: () => menu && action === 'add',
      isStringSelectMenu: () => menu && action === 'remove',
      async reply(obj) { this.output = obj; }, async update(obj) { this.output = obj; },
    };
  }
  async function run(action, values) {
    const x = interaction(action, values);
    assert.equal(await handleChatChannels(x, store, 'guild'), true);
    return x.output;
  }
  return { store, path, run, interaction };
}
test('everyone can select multiple channels, list them, remove a subset and restart', async t => {
  const { store, path, run } = fixture(t);
  assert.deepEqual(store.ids(), []);
  const group = command.toJSON().options.find(opt => opt.name === 'chat');
  assert.deepEqual(group.options.map(opt => opt.name), ['add', 'list', 'remove']);
  const add = (await run('add')).components[0].toJSON().components[0];
  assert.equal(add.max_values, 25);
  await run('add', [first, second]);
  const originalKey = store.key(first);
  await run('add', [first]);
  assert.equal(store.key(first), originalKey);
  assert.deepEqual(new ChatChannels(path).ids(), [first, second]);
  const list = await run('list');
  assert.ok(list.content.includes(first) && list.content.includes(second));
  const options = (await run('remove')).components[0].toJSON().components[0].options;
  assert.equal(options.length, 2);
  await run('remove', [originalKey]);
  assert.deepEqual(new ChatChannels(path).ids(), [second]);
  await run('add', [first]);
  assert.notEqual(store.key(first), originalKey);
  await run('remove', [originalKey]);
  assert.equal(store.has(first), true);
});
test('inaccessible channels cannot be selected, listed or removed by guessing', async t => {
  const { store, run, interaction } = fixture(t);
  await run('add', [first, hidden]);
  assert.deepEqual(store.ids(), [first]);
  store.add([hidden]);
  assert.ok(!(await run('list')).content.includes(hidden));
  await run('remove', [store.key(hidden)]);
  assert.equal(store.has(hidden), true);
  const other = interaction('add', [second]);
  other.guildId = 'other';
  await handleChatChannels(other, store, 'guild');
  assert.equal(store.has(second), false);
});
