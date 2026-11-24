const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static/yuna.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function player(globals = {}) {
  const window = new EventTarget();
  vm.runInNewContext(source, {window, CustomEvent, URL, AbortController, setTimeout, ...globals});
  return new window.YunaReader({baseURL: () => 'http://127.0.0.1:8877'});
}

function chat() {
  const reader = player();
  const spoken = [];
  reader.enabled = reader.ready = true;
  reader.play = async text => { spoken.push(text); };
  const slice = output => reader.row({output, model_state:'slice'});
  return {reader, spoken, slice};
}

test('first stable Korean phrase plays while a split number is held', async () => {
  const {reader, spoken, slice} = chat();
  slice('7 더하기 5는 1');
  await tick();
  assert.deepEqual(spoken, ['7 더하기 5는']);
  slice('7 더하기 5는 12예요.');
  reader.finish();
  await tick();
  assert.deepEqual(spoken, ['7 더하기 5는', '열둘이에요.']);
});

test('decimal and markdown survive model slice boundaries', async () => {
  const {reader, spoken, slice} = chat();
  slice('**결과는** 10.');
  await tick();
  assert.deepEqual(spoken, []);
  slice('**결과는** 10.25입니다.');
  reader.finish();
  await tick();
  assert.deepEqual(spoken, ['결과는 10.25입니다.']);
});

test('a long uninterrupted identifier remains speakable within the POST limit', async () => {
  const {reader, spoken, slice} = chat();
  const text = '가'.repeat(139) + '𝔸' + '나'.repeat(1000);
  slice(text);
  reader.finish();
  await tick();
  assert.equal(spoken.join(''), text);
  assert.ok(spoken.every(item => item.length <= 480 && item.isWellFormed()));
});

test('a backchannel preserves ongoing audio, pending text, and further output', async () => {
  const {reader, spoken, slice} = chat();
  const prefix = '서울에서 시작합니다. 다음';
  slice(prefix);
  await tick();
  const epoch = reader.epoch;
  let stopped = false;
  const playing = {stop() { stopped = true; }, disconnect() {}};
  reader.sources.set(playing, {start:0, end:1});
  reader.interrupt();
  reader.input('여행\n응, 계', '여행');
  reader.row({model_state:'backchannel', speech_action:'continue', input:'여행\n응, 계속해', output:prefix});
  slice(prefix + '은 부산이에요.');
  reader.finish();
  await tick();
  assert.equal(stopped, false);
  assert.equal(reader.epoch, epoch);
  assert.deepEqual(spoken, ['서울에서 시작합니다.', '다음은 부산이에요.']);
});

test('redirect finishes audible phrase even if its HTTP reader is still scheduling PCM', async () => {
  const {reader, spoken, slice} = chat();
  reader.context = {currentTime:0.5};
  let releasePhrase, running;
  reader.play = async (text, signal, phrase) => {
    spoken.push(text);
    if (running) return;
    running = {phrase, signal};
    phrase.start = 0.1;
    phrase.end = 0.7;
    reader.sources.set({stop() { assert.fail('audible phrase was cut'); }}, phrase);
    await new Promise(resolve => { releasePhrase = resolve; });
  };
  const old = '서울에서 시작합니다. 나중에 읽을 내용입니다.';
  slice(old);
  reader.row({speech_action:'redirect', model_state:'interleaved', output:old});
  slice(old+'\n답은 12예요.');
  assert.equal(running.signal.aborted, false);
  assert.deepEqual(spoken, ['서울에서 시작합니다.']);
  const changed = old + '\n답은 12예요.';
  reader.row({model_state:'interleaved', speech_action:'redirect', output:changed});
  slice(changed + '\n다시 계산하면 13이야.');
  releasePhrase();
  await tick();
  assert.deepEqual(spoken, ['서울에서 시작합니다.', '다시 계산하면 열셋이야.']);
});

test('redirect after text EOS removes future sources but lets only the audible phrase finish', () => {
  const {reader} = chat();
  reader.context = {currentTime:1};
  reader.cursor = 5;
  const stopped = [], playing = {start:0.4, end:1.11}, queued = {start:1.11, end:1.5};
  for (const [index, phrase] of [playing, playing, queued].entries()) {
    reader.sources.set({stop() { stopped.push(index); }, disconnect() {}}, phrase);
  }
  reader.finish();
  reader.stateChanged('speaking');
  reader.row({speech_action:'redirect', model_state:'turn', output:'이전 문장'});
  assert.deepEqual(stopped, [2]);
  assert.equal(reader.until, 1.11);
  assert.equal(reader.state, 'speaking');
  reader.row({speech_action:'stop', model_state:'speech_stop', output:'이전 문장'});
  assert.deepEqual(stopped, [2,0,1]);
  reader.row({model_state:'slice',output:'이전 문장. 들리면 안 돼.'});
  assert.equal(reader.pending, '');
  reader.row({model_state:'turn',output:'이전 문장. 들리면 안 돼.'});
  assert.equal(reader.stopped, false);
});

test('Yuna reads ordinary Korean integer predicates with native cardinals and the matching copula', async () => {
  const {reader, spoken, slice} = chat();
  slice('1야. 2야. 20이에요. 21이야. 34예요. 58야. 79입니다. 90야. **99예요**.');
  reader.finish();
  await tick();
  assert.deepEqual(spoken, ['하나야.', '둘이야.', '스물이에요.', '스물하나야.',
    '서른넷이에요.', '쉰여덟이야.', '일흔아홉입니다.', '아흔이야.', '아흔아홉이에요.']);
});

test('emphasized numeric answer is spoken when its copula follows the Markdown delimiter', async () => {
  const {reader, spoken, slice} = chat();
  slice('7 더하기 5는 **12**');
  await tick();
  slice('7 더하기 5는 **12**입니다. __3__야. `12`입니다. 코드가 **12**입니다.');
  reader.finish();
  await tick();
  assert.deepEqual(spoken, ['7 더하기 5는','열둘입니다.', '셋이야.', '12입니다.', '코드가 12입니다.']);
});

test('quantities, decimals, negatives, leading zeros and code identifiers retain their original numerals', async () => {
  const {reader, spoken, slice} = chat();
  slice('A12야. 2.12야. -12야. 012야. 12kg이야. 120야. 코드가 ');
  await tick();
  slice('A12야. 2.12야. -12야. 012야. 12kg이야. 120야. 코드가 12야.');
  reader.finish();
  await tick();
  assert.equal(spoken.join(' '), 'A12야. 2.12야. -12야. 012야. 12kg이야. 120야. 코드가 12야.');
});

test('an earlier suspended activation can be resumed from a new gesture', async () => {
  let resumes = 0;
  let wake;
  const suspended = new Promise(resolve => { wake = resolve; });
  class Context {
    constructor() { this.state = 'suspended'; }
    createGain() { return {connect() {}}; }
    resume() {
      resumes++;
      if (resumes === 2) { this.state = 'running'; wake(); }
      return suspended;
    }
  }
  const reader = player({AudioContext:Context, fetch:async () => ({ok:true})});
  const first = reader.enable();
  assert.equal(reader.enabled, true);
  assert.equal(resumes, 1);
  await reader.enable();
  await first;
  assert.equal(resumes, 2);
  assert.equal(reader.ready, true);
});

test('PCM frames survive an HTTP chunk boundary inside a signed sample', async () => {
  const body = new ReadableStream({
    start(channel) {
      channel.enqueue(Uint8Array.of(0x34));
      channel.enqueue(Uint8Array.of(0x12, 0xff, 0xff, 0x00));
      channel.enqueue(Uint8Array.of(0x80));
      channel.close();
    },
  });
  const reader = player({fetch:async () => new Response(body, {headers:{'X-Audio-Sample-Rate':'22050'}})});
  reader.context = {currentTime:0};
  const values = [];
  reader.schedule = (view, offset, size, rate) => {
    assert.equal(rate, 22050);
    for (let i = 0; i < size; i++) values.push(view.getInt16((i + offset) * 2, true));
  };
  await reader.play('12예요.', new AbortController().signal, {id:1, epoch:0});
  assert.deepEqual(values, [0x1234, -1, -32768]);
});
