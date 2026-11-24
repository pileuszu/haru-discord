import { test } from 'node:test';
import assert from 'node:assert/strict';
import { command } from '../discord/commands.mjs';
import { canControl } from '../discord/permissions.mjs';

test('all members of the configured guild can use /haru without special permissions', () => {
  assert.equal(canControl('server', 'server'), true);
  assert.equal(command.toJSON().default_member_permissions ?? null, null);
});

test('direct messages and other guilds cannot control the session', () => {
  assert.equal(canControl('other', 'server'), false);
  assert.equal(canControl(null, 'server'), false);
  assert.equal(canControl(undefined, undefined), false);
});
