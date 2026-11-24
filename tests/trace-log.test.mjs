import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { VoiceTraceLog } from '../discord/trace-log.mjs';

function temp() {
  const dir = mkdtempSync(join(tmpdir(), 'haru-voice-trace-'));
  return { dir, path: join(dir, 'private', 'voice.jsonl'), clean() { rmSync(dir, { recursive: true, force: true }); } };
}

test('trace creates 0600 JSONL with stable timestamps and metadata', () => {
  const f = temp();
  try {
    const log = new VoiceTraceLog({ path: f.path, now: () => new Date('2026-01-01T00:00:00Z') });
    assert.equal(log.write('haru_model_text', { session_id: 'session', text: '안녕하세요' }), true);
    assert.equal(log.write('haru_session_stop', { reason: 'command_leave' }), true);
    const entries = readFileSync(f.path, 'utf8').trim().split('\n').map(JSON.parse);
    assert.deepEqual(entries[0], { timestamp: '2026-01-01T00:00:00.000Z', event: 'haru_model_text', session_id: 'session', text: '안녕하세요' });
    assert.equal(entries[1].event, 'haru_session_stop');
    assert.equal(entries[1].reason, 'command_leave');
    assert.equal(statSync(f.path).mode & 0o777, 0o600);
  } finally { f.clean(); }
});

test('size rotation bounds logs and preserves the latest events', () => {
  const f = temp();
  try {
    const log = new VoiceTraceLog({ path: f.path, maxBytes: 110, backups: 2 });
    for (let i = 0; i < 4; i++) assert.equal(log.write('event', { index: i }), true);
    assert.equal(JSON.parse(readFileSync(f.path, 'utf8')).index, 3);
    assert.equal(JSON.parse(readFileSync(f.path + '.1', 'utf8')).index, 2);
    assert.equal(JSON.parse(readFileSync(f.path + '.2', 'utf8')).index, 1);
    assert.equal(statSync(f.path + '.1').mode & 0o777, 0o600);
  } finally { f.clean(); }
});

test('a failed write and an oversized record never break callers or change a symlink target', () => {
  const f = temp();
  try {
    const log = new VoiceTraceLog({ path: f.path, maxBytes: 100 });
    assert.equal(log.write('event', { data: 'x'.repeat(100) }), false);
    const target = join(f.dir, 'target'); writeFileSync(target, 'untouched');
    symlinkSync(target, join(f.dir, 'link'));
    assert.equal(new VoiceTraceLog({ path: join(f.dir, 'link') }).write('event'), false);
    assert.equal(readFileSync(target, 'utf8'), 'untouched');
  } finally { f.clean(); }
});
