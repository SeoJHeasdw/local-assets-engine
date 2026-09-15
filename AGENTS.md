# Local Assets Engine 작업 지침

## 작업 시작

1. `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/SETUP.md`를 읽는다.
2. `npm run doctor`로 지금 만들 수 있는 것과 빠진 준비물을 확인한다.
3. 작업 트리의 기존 수정을 보존한다. 구조·코드 변경 후 `npm run check`를 실행한다.

이 저장소는 javis의 생산물 모듈이다. M4 Max 36GB에서 유료 API·구독 없이 게임용 2D
에셋과 3D 메시를 만든다. 형제 프로젝트 `edu/local-tts-engine`의 구조와 검사 방식을 따른다.
엔진 Python은 3.13(`.venv`), TRELLIS는 별도 3.11 환경(`engines/trellis-mac/.venv`)이다.

## 생성·검수 정책

- 무거운 작업은 한 줄로 하나씩 실행한다. 통합 메모리 36GB에서 이미지 모델과 TRELLIS를
  동시에 올리지 않는다. 앱·CLI·javis 요청은 모두 같은 엔진 줄을 쓴다.
- 모든 생성 단계는 별도 프로세스로 `/usr/bin/time -l` 아래에서 돌고, 소요 시간과 최대
  메모리를 `job.json`에 남긴다. 측정을 우회하는 실행 경로를 만들지 않는다. 이 기록이
  장비 교체를 판단하는 근거다.
- 자동 검사와 사람의 승인을 구분한다. 자동 검사는 경고만 하고 후보를 지우거나 승인하지 않는다.
- 결과 품질을 속도·자원 절약보다 우선한다. 품질을 바꾸는 기본값(모델, 스텝, 해상도,
  면 수)은 같은 프롬프트·시드로 비교한 결과를 보여 주고 사용자 승인 후 바꾼다.
- 엔진이 다시 시작되면 대기·진행 중이던 작업을 저절로 다시 돌리지 않고 닫는다.

## 라이선스와 비용

- 유료 API·구독·유료 MCP(Higgsfield 등)를 기본 경로로 쓰거나 결제하지 않는다.
- 모델·코드·데이터를 채택하기 전에 상업 이용과 이용 지역(한국) 조건을 원문으로 확인하고
  `docs/DECISIONS.md`에 출처와 날짜를 남긴다. 기존 기록은 최신 재검증을 대신하지 않는다.
- Hunyuan3D 계열(한국 제외), RMBG-2.0(비상업), Roblox Cube(연구 전용)는 쓰지 않는다.
  TRELLIS의 배경 제거를 BiRefNet으로 바꿔 불러오는 계약을 유지한다.
- `trust_remote_code`로 불러오는 모델은 커밋 해시로 고정한다.

## 데이터와 변경 경계

- 모델 가중치, `output/`, `engines/` 클론, `.env`, 토큰은 Git에 커밋하지 않는다.
  Hugging Face 토큰을 요청하거나 문서에 적지 않는다.
- `engines/trellis-mac`의 원본 코드는 고치지 않는다. 필요한 우회는
  `src/local_assets_engine/workers/trellis_runner.py`와 `scripts/setup_trellis.sh`에 둔다.
- 설치·모델 다운로드 전에는 목적과 예상 용량을 알린다.
- 엔진 HTTP는 127.0.0.1 전용이다. 호스트·출처 검사를 약하게 만들지 않는다.
- 앱 계층 규칙은 `docs/ARCHITECTURE.md`를 따르고 `npm run check:architecture`로 검사한다.

## 협업과 문서

사용자에게는 한국어로 결론부터 간결하게 보고한다.
README는 사용법, ARCHITECTURE는 구조·데이터 계약, DECISIONS는 채택·기각 근거와
라이선스, SETUP은 사용자가 직접 할 준비를 소유한다. 같은 설명을 여러 문서에 쌓지 않는다.
