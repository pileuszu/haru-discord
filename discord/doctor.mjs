import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { generateDependencyReport } from '@discordjs/voice';
import { readConfig, ROOT } from './config.mjs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);
const config = readConfig({ requireCredentials: false });
console.log(generateDependencyReport());
const OpusScript = require('opusscript');
const encoder = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
const encoded = encoder.encode(Buffer.alloc(3840), 960);
const decoded = encoder.decode(encoded);
if (decoded.length !== 3840) throw new Error('Opus 20 ms round trip failed.');
encoder.delete();
console.log('Opus encode/decode: OK');
console.log(`Voice backend: ${config.backend}`);
if (config.backend === 'gemma-yuna') {
  for (const file of ['.venv/bin/python', 'discord_bridge.py']) {
    console.log(`${file}: ${existsSync(join(config.streamDir, file)) ? 'ready' : 'missing'}`);
  }
} else {
  console.log(`Python environment: ${existsSync(join(ROOT, '.venv/bin/python')) ? 'ready' : 'missing'}`);
  console.log(`Voice reference: ${existsSync(config.referencePath) ? 'ready' : 'missing'}`);
  console.log(`Language: ${config.language}`);
  console.log(`Backbone precision: ${config.quantize === null ? 'BF16' : 'int8'}`);
}
console.log(`Session time limit: ${config.maxSessionSeconds ? `${config.maxSessionSeconds}s` : 'disabled'}`);
console.log(`Discord token: ${config.token ? 'configured (hidden)' : 'missing'}`);
console.log(`Application ID: ${config.applicationId ? 'configured' : 'missing'}`);
console.log(`Server ID: ${config.guildId ? 'configured' : 'missing'}`);
console.log('This checks local dependencies, not a live Discord voice connection.');
