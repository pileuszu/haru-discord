import { resolve } from 'node:path';

/** Local model processes do not need Discord credentials or account identifiers. */
export function modelEnvironment(root, env = process.env) {
  const inherited = Object.fromEntries(Object.entries(env).filter(([key]) => !key.startsWith('DISCORD_')));
  return { ...inherited, HF_HOME: resolve(root, '.runtime/huggingface'), HF_HUB_OFFLINE: '1', PYTHONUNBUFFERED: '1' };
}

/** Only forward runtime paths/settings needed by the local speech and text engines. */
export function streamEnvironment(root, env = process.env) {
  const keys = ['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'HF_HOME', 'HF_HUB_CACHE', 'XDG_CACHE_HOME',
    'GEMMA_MODEL', 'GEMMA_LLAMA_BIN', 'HARU_STT_PYTHON', 'HARU_STT_MODEL'];
  const paths = Object.fromEntries(keys.filter(key => env[key] !== undefined).map(key => [key, env[key]]));
  return { ...paths, HARU_REPO: root, HF_HUB_OFFLINE: '1', PYTHONUNBUFFERED: '1' };
}
