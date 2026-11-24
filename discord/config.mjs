import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { isAbsolute, resolve } from 'node:path';

export const ROOT = fileURLToPath(new URL('../', import.meta.url));

export function readConfig(options = {}) {
  const envPath = resolve(ROOT, '.env');
  if (existsSync(envPath)) process.loadEnvFile(envPath);
  return parseConfig(process.env, options);
}

/** Validate configuration without reading credentials or mutating the process environment. */
export function parseConfig(env, { requireCredentials = true } = {}) {
  const value = (name, fallback = '') => env[name]?.trim() || fallback;
  const token = value('DISCORD_TOKEN');
  const applicationId = value('DISCORD_APPLICATION_ID');
  const guildId = value('DISCORD_GUILD_ID');
  const backend = value('HARU_VOICE_BACKEND', 'haru');
  if (!['haru', 'gemma-yuna'].includes(backend)) throw new Error('HARU_VOICE_BACKEND must be haru or gemma-yuna.');
  const streamDir = value('HARU_STREAM_LOCAL_DIR', backend === 'gemma-yuna' ? resolve(ROOT, 'stream-local') : '');
  if (backend === 'gemma-yuna' && !isAbsolute(streamDir)) throw new Error('Set HARU_STREAM_LOCAL_DIR to the absolute stream-local directory.');
  const language = value('HARU_LANGUAGE', 'ko');
  if (!['ko', 'ja'].includes(language)) throw new Error('HARU_LANGUAGE must be ko or ja.');
  const precision = value('HARU_QUANTIZE', '8').toLowerCase();
  if (!['8', 'bf16', 'none'].includes(precision)) throw new Error('HARU_QUANTIZE must be 8, bf16 or none.');
  const quantize = precision === '8' ? 8 : null;
  const maxSessionSeconds = Number(value('HARU_MAX_SESSION_SECONDS', '0'));
  if (!Number.isInteger(maxSessionSeconds) || (maxSessionSeconds !== 0 && (maxSessionSeconds < 60 || maxSessionSeconds > 3600))) {
    throw new Error('HARU_MAX_SESSION_SECONDS must be 0 or an integer from 60 to 3600.');
  }
  for (const [name, id] of Object.entries({ DISCORD_APPLICATION_ID: applicationId, DISCORD_GUILD_ID: guildId })) {
    if (id && !/^\d{17,22}$/.test(id)) throw new Error(`${name} must be a Discord numeric ID.`);
  }
  if (requireCredentials && (!token || !applicationId || !guildId)) {
    throw new Error('Fill DISCORD_TOKEN, DISCORD_APPLICATION_ID and DISCORD_GUILD_ID in the local .env first. See docs/discord-setup.md.');
  }
  const referencePath = resolve(ROOT, value('HARU_REFERENCE_PATH', `.runtime/Haru/haru_mlx/haru_mlx/assets/voices/${language}.flac`));
  return { backend, streamDir, token, applicationId, guildId, maxSessionSeconds, language, referencePath, quantize };
}
