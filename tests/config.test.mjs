import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseConfig } from '../discord/config.mjs';
import { modelEnvironment } from '../discord/worker-env.mjs';
import { createBackend } from '../discord/backend.mjs';

const credentials = { DISCORD_TOKEN: 'test-token', DISCORD_APPLICATION_ID: '12345678901234567', DISCORD_GUILD_ID: '22345678901234567' };

test('Haru is the default and obsolete VOICE_BACKEND cannot select old pipeline', () => {
  const config = parseConfig({ ...credentials, VOICE_BACKEND: 'pipeline' });
  assert.equal(config.language, 'ko');
  assert.equal(config.quantize, 8);
  assert.equal(config.maxSessionSeconds, 0);
  assert.match(config.referencePath, /assets\/voices\/ko\.flac$/);
  assert.equal(parseConfig({ ...credentials, HARU_LANGUAGE: 'ja' }).language, 'ja');
  assert.throws(() => parseConfig({ ...credentials, HARU_LANGUAGE: 'obsolete' }), /HARU_LANGUAGE/);
});

test('precision and duration settings are validated', () => {
  for (const [value, expected] of [['8', 8], ['bf16', null], ['BF16', null], ['none', null]]) {
    assert.equal(parseConfig({ ...credentials, HARU_QUANTIZE: value }).quantize, expected);
  }
  for (const value of ['4', 'int8', 'float16', '-1']) assert.throws(() => parseConfig({ ...credentials, HARU_QUANTIZE: value }), /HARU_QUANTIZE/);
  for (const [duration, expected] of [['0', 0], ['60', 60], ['3600', 3600]]) {
    assert.equal(parseConfig({ ...credentials, HARU_MAX_SESSION_SECONDS: duration }).maxSessionSeconds, expected);
  }
  for (const duration of ['-1', '59', '3601', '90.5', 'Infinity', 'no']) {
    assert.throws(() => parseConfig({ ...credentials, HARU_MAX_SESSION_SECONDS: duration }), /0 or an integer from 60 to 3600/);
  }
});

test('required Discord credentials and numeric IDs are validated without exposing values', () => {
  assert.throws(() => parseConfig({}), /Fill DISCORD_TOKEN/);
  assert.equal(parseConfig({}, { requireCredentials: false }).token, '');
  for (const name of ['DISCORD_APPLICATION_ID', 'DISCORD_GUILD_ID']) {
    assert.throws(() => parseConfig({ ...credentials, [name]: 'username' }), /numeric ID/);
  }
});

test('model child inherits runtime paths but no Discord secrets or identifiers', () => {
  const env = { ...credentials, PATH: '/usr/bin', HF_HUB_OFFLINE: '0' };
  const child = modelEnvironment('/project', env);
  assert.equal(Object.keys(child).some(key => key.startsWith('DISCORD_')), false);
  assert.equal(child.PATH, '/usr/bin');
  assert.equal(child.HF_HOME, '/project/.runtime/huggingface');
  assert.equal(child.HF_HUB_OFFLINE, '1');
  assert.equal(env.DISCORD_TOKEN, 'test-token');
});

test('Haru backend does not spawn the model and validates voice reference', async () => {
  const tmp = mkdtempSync(join(tmpdir(), 'haru-config-'));
  try {
    const voice = join(tmp, 'voice.flac'); writeFileSync(voice, 'test');
    const config = parseConfig({ ...credentials, HARU_REFERENCE_PATH: voice, HARU_QUANTIZE: 'bf16' });
    const backend = await createBackend(config);
    assert.equal(backend.server.child, null);
    assert.equal(backend.server.quantize, null);
    assert.equal(backend.server.language, 'ko');
    assert.equal(backend.Session.name, 'VoiceSession');
    await backend.server.stop();
    await assert.rejects(createBackend({ ...config, referencePath: join(tmp, 'missing') }), /Voice reference missing/);
  } finally { rmSync(tmp, { recursive: true, force: true }); }
});
