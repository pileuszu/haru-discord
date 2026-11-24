import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, copyFileSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'haru-app-python-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, 'scripts'), { recursive: true });
  mkdirSync(join(root, '.venv/bin'), { recursive: true });
  copyFileSync(new URL('../scripts/app-python', import.meta.url), join(root, 'scripts/app-python'));
  copyFileSync(new URL('../scripts/env.sh', import.meta.url), join(root, 'scripts/env.sh'));
  const python = join(root, '.venv/bin/python');
  writeFileSync(python, `#!/bin/bash\nif [[ "\${1:-}" == '-c' ]]; then\n  if [[ "\${FAIL_MODEL_LOOKUP:-0}" == 1 ]]; then exit 43; fi\n  printf '%s\\n' '/fixture/model'\n  exit 0\nfi\nprintf '%s\\n' "$@"\n`);
  chmodSync(python, 0o755);
  const run = (args, env = {}) => execFileSync('bash', [join(root, 'scripts/app-python'), ...args], { encoding: 'utf8', env: { ...process.env, ...env } }).trimEnd().split('\n');
  return { run };
}

test('native app uses pinned model, int8, and Korean by default', t => {
  const { run } = fixture(t);
  assert.deepEqual(run(['-m', 'haru_mlx.native', '--socket', '/tmp/haru.sock']),
    ['-m', 'haru_mlx.native', '--socket', '/tmp/haru.sock', '--model', '/fixture/model', '--quantize', '8', '--language', 'ko']);
});

test('explicit model, precision and language are preserved without looking up the default model', t => {
  const { run } = fixture(t);
  const args = ['-m', 'haru_mlx.native', '--model=/custom', '--quantize', 'none', '--language=ja'];
  assert.deepEqual(run(args, { FAIL_MODEL_LOOKUP: '1' }), args);
});

test('non-native Python invocations are forwarded unchanged', t => {
  const { run } = fixture(t);
  assert.deepEqual(run(['--version']), ['--version']);
});
