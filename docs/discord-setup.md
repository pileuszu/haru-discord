# 이 Mac에서 실행하고 다른 기기의 Discord로 대화하기

```text
다른 기기의 마이크 → Discord 음성 채널 → 이 Mac의 Discord 봇
                                         ↕ 로컬 Unix socket
                                      Haru MLX int8
```

사용자는 어디서든 Discord에 접속하고, 모델 추론은 이 Mac에서 수행합니다. Mac의 마이크는 사용하지 않습니다. Discord 봇이 외부로 Gateway와 음성 연결을 만들기 때문에 공개 웹서버, 도메인, 공유기 포트 포워딩이 필요하지 않습니다. Mac이 켜져 있고 인터넷에 연결되어 있어야 합니다.

현재 구현은 등록한 서버 한 곳, 제어 사용자 한 명, 동시에 한 대화만 지원합니다. `/haru join` 실행자의 음성만 수신합니다. 다른 사람이 말해도 구독하지 않으며, Haru의 답변은 채널의 다른 참가자에게도 들립니다. 실제 Discord DAVE 음성 연결은 계정 등록 후 확인해야 합니다.

## 1. 새 Discord 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications)에 로그인합니다.
2. **New Application**에서 `Haru Voice`라는 앱을 만듭니다. Discord는 앱 이름에 `Discord` 단어를 허용하지 않습니다. 약관 동의가 표시되면 본인이 검토하고 진행합니다.
3. **General Information**의 **Application ID**를 복사합니다.
4. **Bot**에서 봇 토큰을 생성합니다. 토큰 생성·재설정은 본인이 진행하며, 토큰은 채팅이나 Git에 올리지 않습니다.
5. **Installation**에서 **Guild Install**을 사용합니다. 봇을 초대할 Discord 서버에 대한 관리 권한이 필요합니다.
6. **Privileged Gateway Intents**는 켤 필요가 없습니다. 이 봇은 `Guilds`, `GuildVoiceStates`만 사용합니다.
7. Discord 앱의 개발자 모드에서 시험할 서버를 우클릭해 **서버 ID 복사**를 선택합니다.

## 2. 이 Mac의 로컬 설정

`.env.example`을 참고해 프로젝트 루트의 `.env`에 다음 값을 채웁니다. 이 Mac에는 빈 `.env`가 준비되어 있습니다. 다른 PC에서 새로 clone했다면 먼저 복사합니다.

```bash
cd ~/repos/haru-discord
# .env가 없는 경우에만:
cp -n .env.example .env
chmod 600 .env
```

```dotenv
DISCORD_TOKEN=실제_봇_토큰
DISCORD_APPLICATION_ID=앱_ID
DISCORD_GUILD_ID=시험할_서버_ID
DISCORD_ALLOWED_USER_ID=
HARU_MAX_SESSION_SECONDS=600
HARU_REFERENCE_PATH=
```

- `DISCORD_ALLOWED_USER_ID`: 비워 두면 앱 소유자(팀 앱은 팀 소유자)만 제어할 수 있습니다. 다른 Discord 계정으로 테스트하려면 그 계정의 사용자 ID를 넣습니다.
- `HARU_REFERENCE_PATH`: 사용할 권리가 있는 3~180초 음성 파일의 절대 경로입니다. 비워 두면 `./scripts/haru demo-voice`로 만든 한국어 합성 기본 목소리를 사용합니다.
- `HARU_MAX_SESSION_SECONDS`: 60~3600초, 기본 10분 후 해당 대화를 종료합니다.

## 3. 설치·초대·명령 등록

이 프로젝트의 Discord 패키지는 Node **22.12.0 이상**이 필요합니다. 이 Mac에서 Node 22.22.3으로 검증했습니다. 모델 환경과 체크포인트가 없다면 README의 로컬 설치를 먼저 완료합니다.

```bash
./scripts/haru bot-setup
./scripts/haru demo-voice
./scripts/haru bot-doctor
./scripts/haru bot-invite
```

`bot-invite`가 출력한 URL을 열어 시험할 서버에 봇을 초대합니다. 요청 권한은 **채널 보기, 연결, 말하기**이며 Administrator는 사용하지 않습니다. 그다음 서버 전용 slash command를 등록합니다.

```bash
./scripts/haru bot-register
```

초대 전 등록하면 `Missing Access`가 날 수 있습니다. `bot-register`는 `/haru` 명령만 생성·갱신하며 다른 명령을 지우지 않습니다.

## 4. Mac에서 실행하고 다른 기기에서 시험

```bash
./scripts/haru bot
```

실행 후 다른 기기의 Discord에서:

1. 설정한 서버의 일반 **음성 채널**에 들어갑니다.
2. 같은 서버에서 **`/haru join`**을 실행합니다. 첫 join에서 MLX가 로드되므로 잠시 기다립니다.
3. 연결 완료 안내를 받은 뒤 마이크로 한국어로 말합니다.
4. **`/haru status`**로 수신 패킷과 모델 출력 프레임이 늘어나는지 확인합니다.
5. **`/haru reset`**은 새 대화, **`/haru leave`**는 종료입니다.

명령 실행자가 채널을 나가거나 옮기면 대화도 종료합니다. 서버당 한 세션이 아니라 이 프로세스 전체에서 한 세션만 허용합니다. 음성·전사문을 저장하지 않으며, 연결/오류 상태만 터미널에 기록합니다. 터미널에서 `Ctrl+C`를 누르면 봇과 그 봇이 시작한 Python 서버를 함께 종료합니다.

Mac 절전으로 연결이 끊기는 것을 피하려면 시험 중 다음처럼 실행할 수 있습니다. 실행하는 동안만 절전을 막고 종료 시 해제합니다.

```bash
caffeinate -i ./scripts/haru bot
```

이 버전은 로그인 시 자동 실행하는 서비스가 아닙니다. 전원·인터넷·실행 프로세스 유지가 필요합니다. GUI Haru 앱은 별도 모델을 적재하므로 봇으로 테스트할 때는 앱을 종료하면 메모리를 절약할 수 있습니다.

## 계정 없이 확인할 수 있는 것

```bash
npm test
./scripts/haru bot-doctor
./scripts/haru bot-smoke
```

- 단위 테스트: PCM 변환, 제한된 오디오 버퍼, Unix socket 메시지 분할·오류·종료 처리.
- doctor: Opus 왕복, DAVE 모듈·Python·참조 음성·설정 값의 존재 여부. 토큰 값은 출력하지 않습니다.
- bot-smoke: 한국어 합성 질문 → Opus encode/decode → 실제 MLX Unix socket → Opus encode/decode → WAV. 마이크나 Discord 계정은 사용하지 않습니다.

이 테스트의 성공만으로 Discord 접속·초대·DAVE 네트워크 수신이 검증되는 것은 아닙니다. 실서버 검증에서 수신 패킷, 실제 답변 재생, 끊김 후 재접속을 확인해야 합니다. 모델의 한국어 응답 품질 한계는 기존 로컬 검증 결과와 동일하게 남습니다.

## 연결이 안 될 때

| 증상 | 확인할 사항 |
| --- | --- |
| 봇이 오프라인 | Mac에서 bot 프로세스 실행 여부, 토큰, 인터넷·절전 상태 |
| `/haru`가 없음 | 설정한 서버에 초대했는지와 bot-register 성공 여부 |
| 제어할 수 없다는 응답 | 명령을 실행한 계정이 앱 소유자/설정한 사용자 ID인지 |
| 음성 채널 입장 실패 | 채널 권한의 보기·연결·말하기 허용 여부, Stage가 아닌 일반 음성 채널인지 |
| 수신 패킷이 0 | 명령 실행자 마이크 음소거, 다른 계정으로 말하는지, DAVE 연결 오류 |
| 출력 프레임은 늘지만 응답이 없음 | 모델 침묵 또는 입력 이해 문제. 상태만으로 모델 품질을 확정할 수 없음 |
| Missing Access | 봇 초대 완료 여부, 서버 ID, 명령 등록 계정과 Application ID |

## 의존성

`discord.js 14.28.1`, `@discordjs/voice 0.19.2`, `prism-media 1.3.5`, `opusscript 0.0.8`을 고정했습니다. DAVE 구현은 voice의 `@snazzah/davey` 의존성으로 설치되며 lockfile로 고정합니다. PCM/Opus 스트리밍에는 FFmpeg가 필요하지 않습니다.

- [Discord 봇 시작 안내](https://docs.discord.com/developers/quick-start/getting-started)
- [Voice 0.19.2](https://discord.js.org/docs/packages/voice/0.19.2)

Discord가 음성 수신 API를 공식적으로 문서화하지 않아 라이브러리도 장기적인 수신 호환성을 보장하지는 않습니다. 실제 접속 검증과 버전 고정을 함께 유지합니다.

## 이번 로컬 연결 검증 (2026-11-17 KST)

`bot-smoke`에서 Opus 입력 870패킷을 실제 MLX 서버에 보내 예상 출력 131프레임을 모두 수신했습니다. 버린 입력 바이트와 출력 프레임은 0이었고 한국어 응답 및 비무음 WAV를 생성했습니다. 프레임 계산시간은 p50 75.77ms, p95 78.17ms였습니다. 결과는 로컬 `artifacts/bridge-2026-11-17T01-21-29-833Z/`에 있습니다.

테스트 15개 통과, 설치된 의존성 `npm audit` 결과 취약점 0개를 확인했습니다. 이 결과는 **실제 Discord 연결·DAVE 네트워크 복호화 검증을 포함하지 않습니다.** 이후 봇 등록·서버 초대·토큰 설정과 Gateway/명령 응답까지 확인했습니다. 실제 음성 연결 확인은 남아 있습니다.

종료 처리 보완 후 재실행에서도 870패킷/131프레임과 누락 0을 확인했습니다(`artifacts/bridge-2026-11-17T01-25-15-126Z/`, p50 75.33ms, p95 77.88ms). 이 실행의 답변에는 이름 혼동이 있어, 연결 성공과 한국어 대화 품질을 별도로 평가해야 합니다.


## 실제 등록·운영 상태 (2026-11-17 KST)

- 앱/봇 이름: **Haru Voice**. 개발자 포털에서 생성 완료.
- 테스트 서버: **Haru Lab**. 사용자 요청으로 생성하고 봇을 초대함.
- 초대 권한: 채널 보기, 연결, 말하기. Privileged Intents와 Administrator는 사용하지 않음.
- 앱 ID·서버 ID·토큰은 Git에서 제외된 로컬 `.env`에 설정됨. 토큰 인증과 앱 ID 일치를 확인함.
- 서버 전용 `/haru join`, `/haru leave`, `/haru reset`, `/haru status` 등록 완료.
- 이 Mac에서 봇을 백그라운드 실행하고, Discord에서 `/haru status` → `대화 대기 중 · MLX 첫 join 시 로드` 응답을 확인함.
- 봇의 Node 프로세스가 살아 있는 동안 `caffeinate -i -w`로 유휴 절전을 방지함. 재부팅 후 자동 시작하는 서비스는 아님.
- 로그: `.runtime/bot.log`, 현재 Node PID: `.runtime/bot.pid`. 이 파일들은 커밋하지 않음.
- 사용자가 실제 음성 테스트를 나중에 진행하기로 했으므로 음성 채널 연결·DAVE 송수신·마이크 대화 품질은 아직 검증하지 않음.

다른 기기에서 같은 Discord 계정으로 **Haru Lab → 일반 음성 채널**에 들어간 뒤 채팅에서 `/haru join`을 실행하면 된다. 첫 join은 모델 로드 시간이 추가되며 `/haru leave`로 종료할 수 있다. 다른 계정으로 시험하려면 서버 참가와 `DISCORD_ALLOWED_USER_ID` 설정을 별도로 준비해야 한다.
