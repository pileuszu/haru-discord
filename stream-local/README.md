# Discord 한국어 음성 엔진과 로컬 채팅 실험

Discord에서는 Qwen3-ASR이 참가자별 마이크를 한국어로 전사하고, Gemma 4 E4B가 답변을 생성하며, macOS의 유나 목소리가 통화방에서 답합니다. 등록된 채팅 채널에 쓴 질문도 같은 통화에서 음성으로 답합니다. 브라우저에서는 같은 Gemma 실행기 또는 별도의 실험용 모델을 선택할 수 있습니다.

## Discord 음성 엔진 준비

아래 명령은 `haru-discord` 저장소 루트에서 실행합니다. Apple Silicon Mac, Python 3.12, `uv`, Xcode Command Line Tools의 `clang`, `serve` 하위 명령을 지원하는 llama.cpp의 `llama` 실행 파일이 필요합니다. 음성 합성기는 Apple의 한국어 Yuna 음성(`com.apple.voice.compact.ko-KR.Yuna`)을 사용합니다.

### 1. 브리지와 음성 인식 파이썬 환경

봇은 `stream-local/.venv`에서 음성 브리지를 실행하고, 한국어 인식용 MLX 모델은 `.venv-pipeline`의 별도 프로세스에서 로드합니다.

```sh
uv venv --python 3.12 stream-local/.venv
uv pip install --python stream-local/.venv/bin/python -r stream-local/requirements.txt

uv venv --python 3.12 .venv-pipeline
uv pip install --python .venv-pipeline/bin/python 'mlx==0.32.4' 'mlx-audio==0.6.6' 'transformers==5.23.0'
```

### 2. 모델 파일 준비

다음 명령은 Gemma의 Q4_K_XL GGUF 한 개와 Qwen3-ASR 0.6B 8비트 스냅샷을 코드가 기본으로 찾는 위치에 받습니다. 소스와 모델 파일은 따로 관리합니다. 이미 모델이 있으면 아래 경로 설정으로 기존 파일을 사용할 수 있습니다.

```sh
stream-local/.venv/bin/hf download unsloth/gemma-4-E4B-it-GGUF \
  gemma-4-E4B-it-UD-Q4_K_XL.gguf \
  --revision bfc15c382204943c3a8fff0c750b94ae2364d7a3 --local-dir stream-local/.models

stream-local/.venv/bin/hf download mlx-community/Qwen3-ASR-0.6B-8bit \
  --revision 89e96d92ba34aca20b3e29fb10cc284097d1219f --cache-dir .runtime/huggingface/hub
```

### 3. 경로 확인과 봇 시작

저장소 루트의 `.env`에서 `HARU_VOICE_BACKEND=gemma-yuna`로 설정합니다. 기본 경로를 바꿀 때는 아래 변수에 실제 절대 경로를 입력하세요.

| 환경 변수 | 용도와 기본 위치 |
|---|---|
| `HARU_STREAM_LOCAL_DIR` | 음성 브리지 디렉터리. 기본값은 저장소의 `stream-local` |
| `GEMMA_LLAMA_BIN` | `llama serve --offline`을 실행할 파일. 기본값은 `~/.llama-app/llama` |
| `GEMMA_MODEL` | GGUF 파일. 기본값은 `stream-local/.models/gemma-4-E4B-it-UD-Q4_K_XL.gguf`이며, 실행기는 Hugging Face 캐시도 확인 |
| `HARU_STT_PYTHON` | MLX Audio가 설치된 인터프리터. 기본값은 저장소의 `.venv-pipeline/bin/python` |
| `HARU_STT_MODEL` | 위 Qwen3-ASR 스냅샷 디렉터리. 브리지는 저장소의 `.runtime/huggingface/hub` 아래에서 지정된 리비전을 확인 |

`llama serve --help`로 설치된 실행기를 확인할 수 있습니다. PATH에 있는 `llama`를 사용할 경우 `command -v llama`의 결과를 `GEMMA_LLAMA_BIN`에 입력합니다. 유나 합성기는 첫 사용 시 `yuna_worker.m`을 빌드하고 바이너리를 `stream-local/.runtime/yuna`에 보관합니다.

Discord 앱·토큰과 Node.js 의존성을 준비한 뒤 루트에서 `npm start`를 실행합니다. 시작 로그의 `Gemma · 유나 ready.`는 브리지와 모델이 준비됐다는 뜻입니다. 음성 채널에서 `/haru join`으로 연결하고 `/haru status`로 상태를 확인합니다. 앱 권한과 명령 등록 절차는 [Discord 설정](../docs/discord-setup.md)에 있습니다.

## Discord에서 대화하기

- **마이크**에서 “하루야, 대한민국의 수도는 어디야?”처럼 묻습니다. 호출어는 “하루”, “하루야”, “하루찡”, “하루짱”입니다. 먼저 이름만 부른 경우 같은 사람이 5초 안에 시작한 다음 질문 한 건을 받습니다.
- **채팅**에서는 `/haru chat add`로 일반·공지 채널이나 음성 채널의 채팅을 한 번에 최대 25개 등록합니다. `list`로 조회하고 `remove`로 해제합니다. 등록된 채팅에 `하루야, 7 더하기 5는 얼마야?`라고 쓰면 하루가 현재 연결된 음성 채널에서 말합니다. 채팅에서 이름만 썼다면 같은 사람이 같은 채팅에 후속 질문을 써야 합니다.
- **통화방**의 대화 기록은 마이크와 채팅이 공유합니다. 참가자가 여러 명이면 Gemma는 누가 말했는지 번호로 구분합니다. 일반·공지 채널의 작성자에게는 통화방 보기·입장 권한이 필요합니다. 음성 채널에 딸린 채팅은 하루가 바로 그 방에 들어와 있을 때 사용할 수 있습니다. `/haru leave`로 통화를 종료합니다.

마이크 입력은 약 120ms의 침묵부터 미리 전사하고, 약 320ms의 침묵에서 발화를 확정합니다. 그동안 추가 발화가 들어오면 최신 음성에 맞춰 전사를 갱신합니다. 채팅 호출은 이 전사 과정을 건너뜁니다. 유나는 Gemma가 생성한 첫 구절부터 합성하고 Discord에는 48kHz 스테레오 음성을 20ms 프레임으로 보냅니다. 유나가 말하는 도중에도 마이크를 사용할 수 있습니다.

### 인터넷 검색

Gemma는 질문을 듣고 직접 답하거나 검색을 선택합니다. 검색하는 경우 “잠깐만, 검색해 볼게.”라고 먼저 말하고, 외부 서비스에는 짧은 검색어만 보냅니다. “검색하지 말고”라고 하면 로컬에서 답합니다.

일반 검색은 DuckDuckGo Lite, 최신 기사는 Google 뉴스 RSS를 사용합니다. 일반 검색에 Google 결과를 사용하려면 루트의 `.runtime/serper.key`에 Serper API 키 한 줄을 저장하고 권한을 `600`으로 설정하세요. 음성 브리지를 단독 실행하는 경우에는 `SERPER_API_KEY` 환경 변수도 지원합니다.

## 브라우저 채팅 실험

루트에서 다음 명령을 실행하고 [로컬 채팅 화면](http://127.0.0.1:8767)을 여세요. 서버는 루프백 주소에서 입력을 받습니다. 화면에 이전 답변이 남아 있어도 새 세션은 빈 모델 문맥으로 시작합니다.

```sh
stream-local/.venv/bin/python stream-local/server.py
```

**Gemma 4 E4B**는 위에서 준비한 GGUF와 유나를 사용합니다. 입력칸에 타이핑하면 한글 조합이 끝난 글을 전달합니다. 문장을 마치거나 약 0.6초간 타이핑을 멈추면 새 입력을 반영합니다. 예를 들어 `목적지는 서울이야.`를 쓴 뒤 `아니, 부산으로 바꿀게. 최종 목적지는 어디야?`를 이어 쓰면 같은 추론 세션에 정정이 들어갑니다. 실행기는 최대 8토큰 단위로 새 입력을 확인합니다. **유나 음성** 버튼으로 소리를 켜거나 끌 수 있습니다. [Gemma로 열기](http://127.0.0.1:8767/?model=gemma-local)

### 선택 모델

브라우저의 모델 선택기에는 다음 실험도 있습니다. 쓸 모델에 해당하는 명령만 루트에서 실행하세요.

**MiniCPM-duplex 2.4B**는 PyTorch의 Apple GPU 백엔드(MPS)에서 원본 BF16 가중치를 사용합니다. 입력을 최대 다섯 단어씩 묶어 보내고 모델이 생성한 대기 토큰은 화면에서 숨깁니다. 세션 문맥은 512토큰입니다. 가중치와 전용 파이썬 환경을 다음과 같이 준비합니다. [공개 가중치](https://huggingface.co/xinrongzhang2022/MiniCPM-duplex) · [공식 실행 프로토콜](https://github.com/thunlp/duplex-model/blob/main/model_server_duplex.py)

```sh
stream-local/.venv/bin/hf download xinrongzhang2022/MiniCPM-duplex \
  --revision 3acba884381a93a589df05ab09262406e0b881d7 --local-dir stream-local/.models/minicpm-duplex
uv venv --python 3.12 stream-local/.runtime/minicpm-venv
uv pip install --python stream-local/.runtime/minicpm-venv/bin/python -r stream-local/requirements-minicpm.txt
```

**Stream-Qwen3-8B**는 MLX에서 10개 채널을 계산하며 화면에는 Output 채널을 표시합니다. 입력 단어 앞에 공백 하나를 붙여 토큰화하고, 마지막 단어는 문장부호가 오거나 잠시 타이핑을 멈출 때 전달합니다. 한 세션은 최대 512단계입니다. 화면의 **모델 단계**는 침묵과 입력 처리를 포함하고, **답변 글자 수**는 실제로 표시된 출력 길이를 나타냅니다. [공식 입력 처리 코드](https://huggingface.co/JonasGeiping/stream-qwen3-8b/blob/54c7451bfcccecc233fad91affa68563d1de9d66/stream_inference.py#L115)

```sh
stream-local/.venv/bin/hf download JonasGeiping/stream-qwen3-8b \
  --revision 54c7451bfcccecc233fad91affa68563d1de9d66 --local-dir stream-local/.models/stream-qwen3-8b
```
