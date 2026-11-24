# Haru 전이중 구조

운영 기본값은 한국어 Haru-4B MLX와 8비트 언어 모델 백본이다. BF16은 연구용 비교 설정으로 유지한다. 코덱·음성 생성 헤드는 8비트 설정에서도 원본 정밀도다.

```text
Discord 참가자별 Opus → 48kHz stereo PCM 디코딩 → 사람별 제한 버퍼·혼합
→ 20ms 48kHz mono float 입력(무음 포함) → 공식 Haru MLX Unix socket
→ 133.3ms 모델 프레임의 48kHz mono float 출력 → stereo PCM → Discord Opus
```

- `discord/participants.mjs`: 음성 채널의 사람별 수신·디코딩. 봇의 오디오는 제외.
- `discord/mixer.mjs`, `audio.mjs`: 입력 혼합과 제한된 PCM 버퍼.
- `discord/voice-session.mjs`, `frame-clock.mjs`: 단조 시계로 누적 오차를 보정하는 동시 송수신, 종료·재연결·유실 처리와 로그.
- `discord/haru-client.mjs`, `server.mjs`, `scripts/discord_server.py`: 소켓 메시지 검증과 로컬 모델 프로세스 관리.
- `scripts/discord_audio_session.py`, `scripts/discord_native.py`: 공식 MLX 모델의 음성과 텍스트를 계산하고 Discord 소켓으로 전달하는 순서 제어.
- `discord/voice-metrics.mjs`, `trace-log.mjs`: 입력·출력 레벨, 프레임 처리시간, 재생 부족 및 생성 텍스트 진단. 사용자 STT는 제공하지 않음.

모델 입력은 응답 중에도 20ms마다 진행하고 말이 없는 구간도 무음으로 채운다. 소켓 입력이 유실되거나 음성 큐가 넘치면 대화를 명시적으로 종료한다. 원본 모델은 24kHz 3,200샘플(약 133ms)을 한 프레임으로 처리하며, native 서버가 48kHz 클라이언트 스트림을 변환한다. 음성 출력을 텍스트 유무로 음소거하지 않아 모델의 비언어적 출력도 그대로 전달된다.

Discord 클라이언트는 연결 시 음성 우선 전송을 요청한다. 이 모드에서 Mac 서버는 한 프레임의 음성 파형이 준비되면 소켓에 `FRAME`을 보내고, 봇은 받은 PCM을 즉시 송신 큐에 넣는다. 같은 프레임의 텍스트 토큰은 기존 방식으로 샘플링해 다음 프레임에 입력한다. 계산이 끝나면 서버는 `STEP`과 생성된 텍스트를 보낸다. 봇의 모델 처리시간은 `STEP`을 기준으로 하므로 음성이 먼저 도착해도 이전과 같은 범위를 측정한다. 음성 우선 전송을 지원하지 않는 서버와 연결하면 기존 `FRAME` 경로를 쓴다.

`./scripts/haru test`는 프레임·큐·동시 입출력과 설정·종료 처리를 모의 환경에서 확인한다. `./scripts/haru bot-smoke`는 실제 로컬 모델과 Opus를, `./scripts/haru session-smoke`는 실제 세션 타이머와 Opus를 모의 Discord 연결로 확인한다. 둘 다 실제 Discord 네트워크는 포함하지 않는다.
