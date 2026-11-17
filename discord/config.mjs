import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

export const ROOT = fileURLToPath(new URL('../', import.meta.url));

export function readConfig({ requireCredentials = true } = {}) {
  const envPath = resolve(ROOT, '.env');
  if (existsSync(envPath)) process.loadEnvFile(envPath);
  const token = process.env.DISCORD_TOKEN?.trim() || '';
  const applicationId = process.env.DISCORD_APPLICATION_ID?.trim() || '';
  const guildId = process.env.DISCORD_GUILD_ID?.trim() || '';
  const allowedUserId = process.env.DISCORD_ALLOWED_USER_ID?.trim() || '';
  const maxSessionSeconds = Number(process.env.HARU_MAX_SESSION_SECONDS || '600');
  if (!Number.isInteger(maxSessionSeconds) || maxSessionSeconds < 60 || maxSessionSeconds > 3600) {
    throw new Error('HARU_MAX_SESSION_SECONDS must be an integer from 60 to 3600.');
  }
  for (const [name, value] of Object.entries({ DISCORD_APPLICATION_ID: applicationId, DISCORD_GUILD_ID: guildId, DISCORD_ALLOWED_USER_ID: allowedUserId })) {
    if (value && !/^\d{17,22}$/.test(value)) throw new Error(`${name} must be a Discord numeric ID.`);
  }
  if (requireCredentials && (!token || !applicationId || !guildId)) {
    throw new Error('Fill DISCORD_TOKEN, DISCORD_APPLICATION_ID and DISCORD_GUILD_ID in the local .env first. See docs/discord-setup.md.');
  }
  const referencePath = resolve(ROOT, process.env.HARU_REFERENCE_PATH?.trim() || '.runtime/Haru/haru_mlx/haru_mlx/assets/voices/ko.flac');
  return { token, applicationId, guildId, allowedUserId, referencePath, maxSessionSeconds };
}
