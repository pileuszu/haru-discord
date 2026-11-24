import { existsSync } from 'node:fs';
import { HaruServer } from './server.mjs';
import { GemmaVoiceSession } from './gemma-session.mjs';
import { VoiceSession } from './voice-session.mjs';

export async function createBackend(config) {
  if (config.backend === 'gemma-yuna') {
    return { server: new HaruServer({ streamDir: config.streamDir }), Session: GemmaVoiceSession };
  }
  if (!existsSync(config.referencePath)) throw new Error('Voice reference missing. Run ./scripts/haru demo-voice or set HARU_REFERENCE_PATH.');
  return { server: new HaruServer({ quantize: config.quantize, language: config.language }), Session: VoiceSession };
}
