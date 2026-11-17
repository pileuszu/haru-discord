# haru-discord

Haru-4B를 Apple Silicon의 MLX로 실행하고 한국어 음성 대화를 검증하는 프로젝트입니다. 로컬 실행과 Discord 음성 연결 코드를 포함합니다. 이 Mac에서는 Haru Voice 봇 등록과 Haru Lab 서버의 상태 명령 응답까지 확인했습니다. 실제 음성 통화는 후속 검증 대상입니다.

자동 검증 결과와 한계는 [로컬 검증 기록](docs/local-validation.md)에 정리했습니다.

## 로컬 환경

- Apple Silicon Mac, macOS 14 이상(고정된 MLX 0.32.4 wheel 기준). 공식 native 앱은 실용 최소 메모리 16GB를 안내합니다.
- 이 프로젝트의 초기 검증 장비: Mac mini M4 / 통합 메모리 32GB.
- `uv`, Git, Xcode Command Line Tools 또는 Xcode.
- Python 3.12.15, Haru 코드와 모델 revision은 각각 `.python-version`, `runtime.json`에 고정합니다. Python 패키지는 `uv.lock`으로 고정합니다.
- 모델 약 10GB 외에 가상환경·캐시·빌드 공간이 필요합니다. `.runtime`, `.venv`, `artifacts`는 Git에서 제외합니다.

```bash
cd ~/repos/haru-discord
./scripts/haru setup
./scripts/haru download
./scripts/haru smoke-ko
```

`setup`은 프로젝트 안에 공식 Haru 소스와 Python을 준비하고 `uv sync --locked`를 실행합니다. 기본 Python이나 시스템 패키지를 교체하지 않습니다. 다운로드한 모델은 `.runtime/huggingface`에 저장합니다. 모델이 준비된 이후 추론은 로컬에서 실행합니다.

## 한국어 기능 검증

`smoke-ko`는 macOS의 한국어 합성 음성으로 질문 세 개를 만들고, 하나의 Haru 세션에서 입력합니다. 마이크는 사용하지 않습니다. 첫 질문의 사용자 이름은 캐릭터 프롬프트에 넣지 않아 마지막 기억 질문을 따로 확인할 수 있습니다. 질문 뒤에는 답변을 생성할 수 있도록 12초의 무음을 붙입니다.

Haru는 8-bit backbone으로 실행하며 음성 코덱과 음성 헤드는 원래 정밀도를 유지합니다. 첫 실행은 Metal 커널 컴파일과 프롬프트 처리 때문에 오래 걸릴 수 있습니다.

실행마다 `artifacts/ko-날짜-시간/`에 저장합니다.

| 파일 | 내용 |
| --- | --- |
| `input.wav`, `input.json` | 질문 음성 및 질문별 시점·합성 음성 여부 |
| `reference.wav` | 검증용 참조 목소리 |
| `reply.wav` | Haru의 24kHz 응답 음성 |
| `conversation.wav` | 왼쪽 사용자 / 오른쪽 Haru 음성 |
| `transcript.txt`, `transcript.jsonl` | 모델의 출력 텍스트와 프레임별 처리시간. 사용자 음성의 ASR 전사문은 아닙니다. |
| `metrics.json` | 처리시간 p50/p95, MLX peak 메모리, 출력 신호 검사 |
| `card.txt` | 실제 사용한 한국어 캐릭터 카드 |

자동 성공 판정은 **유효한 비무음 음성 및 한글 텍스트 생성**만 확인합니다. 질문 이해, 이름 기억, 발음의 자연스러움, 끼어들기 처리까지 통과했다는 의미가 아닙니다. 응답 텍스트와 음성을 함께 검토해야 합니다. 프레임 처리시간은 모델 계산시간이며 마이크부터 스피커까지의 실제 지연이 아닙니다.

본인의 녹음으로도 같은 경로를 시험할 수 있습니다. 참조 목소리는 사용할 권리가 있는 한 사람의 깨끗한 음성 3~180초를 사용합니다.

```bash
./scripts/haru smoke-ko --input /absolute/path/question.wav --reference /absolute/path/voice.wav
```

WAV/AIFF/FLAC은 기본 의존성으로 처리합니다. 다른 압축 포맷은 upstream에서 FFmpeg를 요구할 수 있습니다.

## 마이크로 직접 대화하기

```bash
./scripts/haru demo-voice  # 선택: macOS Yuna 합성 음성을 로컬 기본 목소리로 준비
./scripts/haru build-app
./scripts/haru app
```

이 프로젝트는 공식 native macOS 앱을 빌드합니다. 프로젝트 전용 Python 래퍼를 통해 고정 모델과 캐시를 사용하고, 기본 언어는 한국어, 기본 정밀도는 int8로 시작합니다. 사용자가 앱 설정에서 정밀도를 바꾸면 그 선택을 전달합니다.

`demo-voice`는 upstream의 Git 제외 경로에 한국어 기본 목소리를 만들며 기존 파일은 덮어쓰지 않습니다.

앱에서 입력·출력 장치를 선택하고, 참조 목소리를 지정한 뒤 대화를 시작합니다. 마이크 권한 요청이 뜨면 직접 허용하세요. 녹음은 시작 버튼을 누른 뒤에 진행됩니다. 자동 검증용 `reference.wav`도 로컬 시험에 사용할 수 있지만 실제 목소리로 바꿔 대화 품질을 확인하는 것이 좋습니다. 헤드폰을 사용하면 스피커 소리가 다시 입력되는 것을 줄일 수 있습니다.

앱을 종료하면 앱이 시작한 모델 서버도 종료합니다. 로그는 공식 앱 기준 `~/Library/Logs/Haru/app.log`에 남습니다. `./scripts/haru app` 전에 모델 다운로드와 빌드를 마쳐야 합니다. 프로젝트 경로를 바꾸면 Python 경로가 앱에 기록되므로 다시 빌드하세요.

## 다른 기기의 Discord에서 대화하기

이 Mac에서 봇과 MLX를 실행하고, 다른 기기에서 Discord 음성 채널에 들어가 `/haru join`으로 대화하는 구조입니다. Mac의 마이크나 공개 웹서버는 필요하지 않습니다.

새 봇 생성, `.env` 설정, 서버 초대, 실행 절차는 [Discord 설정 안내](docs/discord-setup.md)를 참고하세요. 봇 등록·서버 초대·실제 `/haru status` 응답은 확인했습니다. 실서버 음성 수신과 DAVE 음성 연결은 아직 확인하지 않았습니다. 먼저 로컬 연결을 `./scripts/haru bot-smoke`로 검증할 수 있습니다.

## 출처

- [Haru 공식 저장소](https://github.com/higurashi-ai/Haru)
- [MLX 구현](https://github.com/higurashi-ai/Haru/tree/main/haru_mlx)
- [Haru-4B 모델 카드](https://huggingface.co/higurashi-ai/Haru-4B)

Haru 코드와 모델은 Apache-2.0으로 공개되어 있으며 제3자 구성요소 고지는 upstream에 포함되어 있습니다. 모델과 생성한 음성은 이 저장소에 재배포하지 않습니다.
