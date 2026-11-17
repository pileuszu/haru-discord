import { REST, Routes, PermissionFlagsBits } from 'discord.js';
import { readConfig } from './config.mjs';
import { command } from './commands.mjs';

try {
  const inviteOnly = process.argv.includes('--invite');
  const config = readConfig({ requireCredentials: !inviteOnly });
  if (!config.applicationId || !config.guildId) throw new Error('Set DISCORD_APPLICATION_ID and DISCORD_GUILD_ID first.');
  const permissions = PermissionFlagsBits.ViewChannel | PermissionFlagsBits.Connect | PermissionFlagsBits.Speak;
  const install = new URL('https://discord.com/oauth2/authorize');
  install.search = new URLSearchParams({ client_id: config.applicationId, scope: 'bot applications.commands', permissions: permissions.toString(), guild_id: config.guildId, disable_guild_select: 'true' });
  console.log(`Install link: ${install}`);
  if (!inviteOnly) {
    const rest = new REST({ version: '10' }).setToken(config.token);
    await rest.post(Routes.applicationGuildCommands(config.applicationId, config.guildId), { body: command.toJSON() });
    console.log('Registered /haru for the configured server.');
  }
} catch (error) {
  console.error(`Command registration failed: ${error.message}`);
  process.exitCode = 1;
}
