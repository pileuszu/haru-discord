/* Local Yuna speech. Buffer only whole words and keep the rendered audio interruptible. */
(() => {
  'use strict';

  function spokenText(raw) {
    return raw
      .replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, '$1')
      .replace(/https?:\/\/\S+/g, '링크')
      .replace(/^\s*[-+#>]+\s+/gm, '')
      .replace(/[*_`#]/g, '')
      .replace(/[\p{Extended_Pictographic}\uFE0F\u200D]/gu, '')
      .replace(/\s+/g, ' ').trim();
  }

  // With compact Yuna, "12야" can sound like "십이야" (10 + 이야).
  // Use native cardinals for ordinary predicates and retain identifiers and quantities.
  function spokenIntegers(raw, previous) {
    const tens = ['', '열', '스물', '서른', '마흔', '쉰', '예순', '일흔', '여든', '아흔'];
    const units = ['', '하나', '둘', '셋', '넷', '다섯', '여섯', '일곱', '여덟', '아홉'];
    return raw.replace(/(^|[\s(“‘"'])(\*{1,2}|_{1,2})?([1-9]\d?)(?:\2)?(이야|야|이에요|예요|입니다)(?=$|[\s.,!?。！？…)"'”’*_])/gu,
      (match, space, emphasis, digits, ending, offset) => {
        const before = previous + raw.slice(0, offset) + space;
        if ((!space && before && !/[\s(“‘"']$/u.test(before)) ||
            /(?:코드|비밀번호|식별자|버전|번호|마이너스|minus|code|pin|version|id)(?:은|는|이|가)?\s*[:=]?\s*$/iu.test(before) ||
            /[-−±]\s*$/u.test(before)) return match;
        const number = Number(digits);
        const word = tens[Math.floor(number / 10)] + units[number % 10];
        const finalConsonant = (word.charCodeAt(word.length - 1) - 0xac00) % 28 !== 0;
        const polite = ending === '예요' || ending === '이에요';
        let copula = ending;
        if (ending !== '입니다') copula = polite ? (finalConsonant ? '이에요' : '예요') : (finalConsonant ? '이야' : '야');
        return space + word + copula;
      });
  }

  function phraseLength(raw, first, finished) {
    const limit = 140;
    let probe = raw.slice(0, limit);
    // Keep each spoken unit short enough to finish naturally on a topic change.
    // Use whole words: the next model slice may complete the last number/word.
    let endOfWords = 0, wordCount = 0;
    for (const word of probe.matchAll(/\S+\s+/g)) {
      endOfWords = word.index + word[0].length;
      wordCount++;
      if (endOfWords >= 16 || wordCount === 4) {
        probe = probe.slice(0, endOfWords);
        break;
      }
    }
    // A decimal point after a digit may still be followed by another digit.
    const pause = /[!?。！？…]|(?<!\d)\.(?=\s|$)|(?<=\d)\.(?=\s)|[,;:](?=\s)|\n/g;
    for (const match of probe.matchAll(pause)) {
      const end = match.index + match[0].length;
      if (spokenText(probe.slice(0, end)).match(/[\p{L}\p{N}]/u)) return end;
    }
    if (finished && raw.length === probe.length) return raw.length;
    if (wordCount) {
      const text = spokenText(raw.slice(0, endOfWords));
      if ((first && (text.length >= 4 || wordCount >= 2)) ||
          text.length >= 14 || probe.length < raw.length || raw.length >= limit) return endOfWords;
    }
    // Keep the unfinished last word, especially numbers split across model slices.
    // A long identifier or text without spaces must still fit a native request.
    if (raw.length >= limit && !wordCount) {
      return /[\uD800-\uDBFF]/.test(raw[limit - 1]) ? limit - 1 : limit;
    }
    return 0;
  }

  class YunaReader {
    constructor({baseURL, onState = () => {}, onError = () => {}}) {
      this.baseURL = baseURL;
      this.onState = onState;
      this.onError = onError;
      this.enabled = false;
      this.ready = false;
      this.context = null;
      this.sources = new Map();
      this.warmRequest = null;
      this.epoch = 0;
      this.activation = 0;
      this.state = 'off';
      this.cursor = 0;
      this.stopped = false;
      this.pending = '';
      this.previous = '';
      this.finished = false;
      this.first = true;
      this.until = 0;
      this.job = null;
      this.nextPhraseId = 0;
    }

    stateChanged(state) {
      if (this.state === state) return;
      this.state = state;
      this.onState(state);
    }

    warm() {
      const url = new URL('/tts/warm', this.baseURL()).href;
      if (this.warmRequest?.url === url) return this.warmRequest.promise;
      const promise = fetch(url, {method:'POST', cache:'no-store'}).then(response => {
        if (!response.ok) throw new Error('이 맥의 유나 음성을 준비할 수 없습니다.');
      }).catch(error => {
        if (this.warmRequest?.promise === promise) this.warmRequest = null;
        throw error;
      });
      this.warmRequest = {url, promise};
      return promise;
    }

    async enable(currentOutput = '') {
      if (this.enabled) {
        const activation = this.activation;
        try {
          if (this.context && this.context.state !== 'running') await this.context.resume();
        } catch (error) {
          if (this.enabled && this.activation === activation) this.fail(error);
        }
        return;
      }
      this.enabled = true;
      this.cursor = currentOutput.length;
      this.stopped = false;
      const activation = ++this.activation;
      this.stateChanged('preparing');
      try {
        if (!this.context) {
          this.context = new AudioContext({latencyHint:'interactive'});
          this.output = this.context.createGain();
          this.output.connect(this.context.destination);
          window.dispatchEvent(new CustomEvent('yuna-audio-ready', {
            detail:{context:this.context, output:this.output},
          }));
        }
        // resume() must be invoked in the user's gesture, before any awaited fetch.
        await Promise.all([this.context.resume(), this.warm()]);
        if (!this.enabled || activation !== this.activation) return;
        this.ready = true;
        this.stateChanged('ready');
        this.pump();
      } catch (error) {
        if (this.enabled && this.activation === activation) this.fail(error);
      }
    }

    disable() {
      this.enabled = this.ready = false;
      this.activation++;
      this.reset();
      if (this.context?.state === 'running') this.context.suspend().catch(() => {});
      this.stateChanged('off');
    }

    fail(error) {
      this.disable();
      this.onError(error.message || '유나 음성을 재생할 수 없습니다.');
    }

    clearSpeech(keep = null) {
      this.epoch++;
      if (this.job && this.job !== keep) {
        this.job.controller.abort();
        this.job = null;
      }
      for (const [source, phrase] of this.sources) {
        if (phrase === keep) continue;
        source.onended = null;
        source.stop();
        source.disconnect();
        this.sources.delete(source);
      }
      this.pending = '';
      this.previous = '';
      this.finished = false;
      this.first = true;
      this.until = keep?.end || 0;
      if (!keep && this.enabled && this.ready) this.stateChanged('ready');
    }

    reset(output = '') {
      this.clearSpeech();
      this.cursor = output.length;
      this.stopped = false;
    }

    // Existing open chat pages may still call these on every edit and IME start.
    // The confirmed row is the first point at which the speaker should act.
    input() {}
    interrupt() {}

    redirect() {
      const now = this.context?.currentTime ?? 0;
      let current = null;
      for (const phrase of this.sources.values()) {
        if (phrase.start <= now && phrase.end > now) { current = phrase; break; }
      }
      this.clearSpeech(current);
      this.stopped = false;
      window.dispatchEvent(new CustomEvent('yuna-redirect', {
        detail:{epoch:this.epoch, retainedPhraseId:current?.id ?? null},
      }));
    }

    row(row) {
      if (!this.enabled || typeof row.output !== 'string') return;
      const boundary = row.model_state === 'turn' || row.model_state === 'interleaved';
      const action = row.speech_action || (boundary ? 'redirect' : null);
      if (action === 'stop') {
        this.clearSpeech();
        this.stopped = true;
        this.cursor = row.output.length;
        window.dispatchEvent(new CustomEvent('yuna-interrupt', {detail:{epoch:this.epoch}}));
        return;
      }
      if (action === 'redirect' || action === 'start') {
        this.redirect();
        this.cursor = row.output.length;
        return;
      }
      const end = row.output.length;
      if (!this.stopped && end > this.cursor) {
        this.pending += row.output.slice(this.cursor);
        this.finished = false;
      }
      this.cursor = end;
      this.pump();
    }

    finish() {
      if (!this.enabled || this.stopped) return;
      this.finished = true;
      this.pump();
    }

    pump() {
      if (!this.ready || this.stopped || this.job) return;
      while (this.pending) {
        const length = phraseLength(this.pending, this.first, this.finished);
        if (!length) break;
        const raw = this.pending.slice(0, length);
        const phrase = spokenText(spokenIntegers(raw, this.previous));
        this.previous = (this.previous + raw).slice(-48);
        this.pending = this.pending.slice(length);
        if (!/[\p{L}\p{N}]/u.test(phrase)) continue;
        const job = {id:++this.nextPhraseId, epoch:this.epoch, start:null, end:0, controller:new AbortController()};
        this.job = job;
        this.first = false;
        this.play(phrase, job.controller.signal, job).catch(error => {
          if (!job.controller.signal.aborted) this.fail(error);
        }).finally(() => {
          if (this.job !== job) return;
          this.job = null;
          this.pump();
        });
        return;
      }
      if (!this.sources.size) this.stateChanged('ready');
    }

    async waitForRoom(signal) {
      while (!signal.aborted && this.until - this.context.currentTime > 0.45) {
        await new Promise(resolve => setTimeout(resolve, 35));
      }
      signal.throwIfAborted();
    }

    async play(text, signal, phrase) {
      await this.waitForRoom(signal);
      signal.throwIfAborted();
      window.dispatchEvent(new CustomEvent('yuna-phrase', {detail:{text, epoch:phrase.epoch, phraseId:phrase.id}}));
      const response = await fetch(new URL('/tts', this.baseURL()), {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text}),
        signal, cache:'no-store',
      });
      if (!response.ok || !response.body) throw new Error('유나 음성을 불러올 수 없습니다.');
      const rate = Number(response.headers.get('X-Audio-Sample-Rate') || 22050);
      const reader = response.body.getReader();
      let odd = null;
      let samples = 0;
      try {
        while (true) {
          const {done, value} = await reader.read();
          signal.throwIfAborted();
          if (done) break;
          let data = value;
          if (odd !== null) {
            data = new Uint8Array(value.byteLength + 1);
            data[0] = odd;
            data.set(value, 1);
          }
          odd = data.length % 2 ? data[data.length - 1] : null;
          const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
          const count = Math.floor(data.length / 2);
          for (let i = 0; i < count;) {
            await this.waitForRoom(signal);
            signal.throwIfAborted();
            const size = Math.min(count - i, Math.ceil(rate * 0.1));
            this.schedule(view, i, size, rate, phrase);
            samples += size;
            i += size;
          }
        }
      } finally {
        reader.releaseLock();
      }
      if (odd !== null || !samples) throw new Error('유나 음성 데이터가 온전히 도착하지 않았습니다.');
      window.dispatchEvent(new CustomEvent('yuna-phrase-end', {
        detail:{epoch:phrase.epoch, phraseId:phrase.id, start:phrase.start, at:phrase.end},
      }));
    }

    schedule(view, start, size, rate, phrase) {
      const buffer = this.context.createBuffer(1, size, rate);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < size; i++) channel[i] = view.getInt16((start+i)*2, true) / 32768;
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.output);
      this.sources.set(source, phrase);
      source.onended = () => {
        source.disconnect();
        this.sources.delete(source);
        if (!this.sources.size && !this.job && this.ready) this.stateChanged('ready');
      };
      const at = Math.max(this.context.currentTime + 0.012, this.until);
      this.until = at + buffer.duration;
      if (phrase.start === null) phrase.start = at;
      phrase.end = this.until;
      source.start(at);
      this.stateChanged('speaking');
    }
  }

  window.YunaReader = YunaReader;
})();
