# Local Assets Engine

javis의 생산물 모듈이다. Apple Silicon Mac에서 유료 API나 구독 없이 게임용 2D 에셋과
3D 메시를 만든다. 현재 환경은 M4 Max 36GB다. 로그인·승인처럼 사용자가 직접 할 준비와
2D 모델 선택은 [SETUP](docs/SETUP.md)에 있다.

## 실행과 검사

```bash
./app.sh          # 앱을 열고 엔진을 함께 띄운다
npm run doctor    # 지금 만들 수 있는 것과 빠진 준비물
npm run check     # 앱 구조 검사 + 앱·Python 테스트
npm run bench     # 단계별 소요 시간과 최대 메모리 요약
```

## 만드는 것

| 레시피 | 흐름 | 결과 |
| --- | --- | --- |
| `image` | 설명 → 이미지 후보 → 배경 제거 → 캔버스 맞춤 또는 픽셀화 | 투명 PNG 후보 |
| `text-to-3d` | 설명 → 컨셉 이미지 1장 → TRELLIS.2 → Blender 정리 → gltfpack | GLB |
| `image-to-3d` | 고른 후보나 이미지 파일 → TRELLIS.2 → Blender 정리 → gltfpack | GLB |
| `previz` | 완성된 메시 배치 → 샷 프리셋을 카메라 값으로 풀기 → 스케치 렌더 | 샷별 애니매틱·깊이·윤곽과 샷 값 |

이미지 모델은 FLUX.2 klein 4B, 배경 제거는 BiRefNet, 3D는 TRELLIS.2 Mac 포트, 프리비즈
스케치는 Blender EEVEE다. 종류 프리셋(아이템 아이콘, 캐릭터 스프라이트, 픽셀 아트, UI 요소,
배경, 3D 소품)과 샷 프리셋(게임 트레일러), 모델·기본값은 `config/presets.json`에서 고친다.
자동 검사(물체 유무, 면적, 가장자리 닿음)는 참고 신호다. 채택 여부는 보관함에서 사람이 승인한다.

프리비즈는 연출을 먼저 확정하는 단계다. 산출물은 그림이 아니라 카메라 위치·목표·렌즈·
움직임·길이 같은 **샷 값**이고, 그 값이 나중에 만들 영상 생성의 입력이 된다.
자세한 방향은 [PREVIZ](docs/PREVIZ.md)에 있다.

```bash
# 완성된 3D 에셋 하나로 게임 트레일러 6컷 프리비즈 만들기 (약 20초)
.venv/bin/python -m local_assets_engine run previz \
  --params '{"assets": [{"source": {"jobId": "<3D 작업 ID>", "assetId": "a02"}}]}'
```

## 앱에서 하는 일

- **만들기**: 2D·3D, 종류, 설명, 후보 수를 고른다. 3D를 설명으로 만들 때 후보가 1장이면
  곧바로 3D까지 가고, 여러 장이면 컨셉 후보를 연 뒤 "3D로 만들기"를 누른다.
  이미지 파일을 끌어다 놓아 3D로 만들 수도 있다.
- **작업**: 단계별 진행률·소요 시간·최대 메모리를 보고, 진행 중인 작업을 중지한다.
- **프리비즈**: 3D 에셋을 장면에 놓고(위치·회전·크기, 위에서 본 배치 지도) 샷 프리셋과
  렌더 옵션을 고른 뒤 "샷 만들기"를 누른다. 오른쪽에 컷이 순서대로 쌓이고, 컷마다 승인·거절을
  하거나 열어서 애니매틱과 카메라 값·깊이·윤곽을 본다.
- **보관함**: 검토 대기·승인·거절로 거르고, 이미지와 3D(model-viewer), 프리비즈 샷을
  한자리에서 본다. 3D 에셋을 열면 "프리비즈 만들기"로 그 에셋을 프리비즈 장면에 넣는다.
- **환경**: 준비 상태 진단과 측정 기록을 본다.

## javis와 스크립트에서 쓰기

엔진은 `127.0.0.1:47831`에서만 요청을 받는다. 앱 없이 `npm run engine`으로 띄울 수 있고,
앱은 이미 떠 있는 엔진을 그대로 쓴다. 작업은 앱·CLI·javis 요청을 가리지 않고 한 줄로 실행한다.

```bash
curl -s localhost:47831/api/jobs -H 'Content-Type: application/json' \
  -d '{"recipe": "image", "params": {"preset": "item-icon", "subject": "red health potion", "count": 4}}'
curl -s localhost:47831/api/jobs/<작업 ID>
```

`.venv/bin/python -m local_assets_engine run <레시피> --params '<JSON>'`은 엔진이 떠 있으면
엔진에 작업을 넣고, 없으면 그 자리에서 실행한다. API 목록은 [ARCHITECTURE](docs/ARCHITECTURE.md)에 있다.

## 폴더 안내

| 폴더 | 책임 |
| --- | --- |
| `src/local_assets_engine/` | 엔진: HTTP 서버, 작업 기록, 러너, 측정, 진단, CLI |
| `src/local_assets_engine/recipes/` | 2D·3D·프리비즈 레시피와 입력 검증 |
| `src/local_assets_engine/tools/` | 단계 프로세스: BiRefNet 배경 제거, Blender 메시 정리·프리비즈 렌더 |
| `src/local_assets_engine/workers/` | TRELLIS 환경(Python 3.11)에서 도는 실행기 |
| `electron-app/` | 앱. `main`은 엔진 수명·창·IPC, `renderer`는 화면, `shared`는 공통 계산 |
| `config/presets.json` | 모델, 종류 프리셋, 3D 기본값, 프리비즈 샷 프리셋 |
| `scripts/` | TRELLIS 설치, 앱 구조 검사 |
| `tests/` | Python 테스트와 `tests/electron/` 앱 테스트 |
| `docs/` | 구조·데이터 계약, 채택 근거, 수동 준비 |
| `engines/` | 외부 엔진 클론. Git에서 제외하고 `scripts/setup_trellis.sh`가 만든다 |

## 로컬 데이터

| 경로 | 내용 |
| --- | --- |
| `output/jobs/<작업 ID>/job.json` | 작업의 단일 기록: 입력, 단계별 측정, 에셋, 승인 상태 |
| `output/jobs/<작업 ID>/` | `raw/` 생성 원본, `cutout/` 배경 제거, `final/` 후보, `mesh/` 3D, `previz/` 샷, `job.log` |
| `output/jobs/<작업 ID>/previz/shots.json` | 프레임별 카메라 값까지 담긴 샷 기록 |
| `~/.cache/huggingface/hub/` | 모델 가중치 |
