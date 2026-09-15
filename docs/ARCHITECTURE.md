# 아키텍처와 데이터 계약

## 책임과 코드 위치

실행법은 [README](../README.md)에 있다.

| 위치 | 책임 |
| --- | --- |
| `src/local_assets_engine/server.py` | 127.0.0.1 HTTP API, 화면·작업 파일 제공, 호스트·출처 검사 |
| `src/local_assets_engine/runner.py` | 한 줄 작업 실행, 단계 기록(`Stage`), 중지·종료 처리 |
| `src/local_assets_engine/measure.py` | 단계 프로세스 실행, 출력 스트리밍, 진행률 해석, `/usr/bin/time -l` 측정 |
| `src/local_assets_engine/jobs.py` | `job.json` 원자적 기록, 재시작 복구, 파일 경로 검증 |
| `src/local_assets_engine/recipes/` | 레시피별 입력 검증(`prepare`)과 단계 조립(`run`) |
| `src/local_assets_engine/tools/` | 엔진 환경에서 도는 단계 프로세스: BiRefNet, Blender |
| `src/local_assets_engine/workers/trellis_runner.py` | TRELLIS 환경에서 generate.py를 감싸 실행 |
| `src/local_assets_engine/imaging.py` | 모델 없는 이미지 후처리: 캔버스 맞춤, 픽셀화, 자동 검사 |
| `presets.py`, `config/presets.json` | 모델, 종류 프리셋, 3D 기본값 |
| `doctor.py`, `bench.py`, `cli.py` | 진단, 측정 요약, 명령줄 |
| `electron-app/main/` | 엔진 수명(`engine.mjs`), 창, IPC, 서비스 조립 |
| `electron-app/renderer/` | 화면. 엔진이 `/`로 제공한다 |
| `electron-app/shared/` | 요청 조립·표시 형식 같은 공통 계산. 엔진이 `/shared`로 제공한다 |

## 프로세스 구조

```text
Electron main ──spawn──▶ 엔진 서버 (.venv, Python 3.13, 127.0.0.1:47831)
BrowserWindow ──HTTP───▶   화면 · API · /files
javis · CLI   ──HTTP───▶        │
                           러너 스레드 (한 번에 한 작업)
                                │  단계마다 /usr/bin/time -l 아래 별도 프로세스
     ┌──────────────┬───────────┴────────┬─────────────────────────┬─────────────────┐
  mflux 이미지   tools.remove_bg     workers/trellis_runner     tools.blender_post   gltfpack
  (.venv)        (.venv, torch MPS)  (trellis-mac/.venv 3.11)   (.venv, bpy)
```

- 앱은 이미 응답하는 엔진이 있으면 그대로 쓰고, 직접 띄운 엔진만 앱이 끝날 때 끈다.
- 엔진이 SIGTERM을 받으면 진행 중인 작업을 중지한다. 단계 프로세스는 자기 세션으로
  실행되므로 그 프로세스 그룹에 SIGTERM을, 3초 뒤에도 남으면 SIGKILL을 보낸다.
- 엔진이 시작할 때 `queued`로 남은 작업은 `cancelled`, `running`으로 남은 작업은
  `failed`로 닫는다. 어느 쪽도 다시 실행하지 않는다.
- 모델은 서버 프로세스에 올리지 않는다. 레시피는 torch·bpy를 import하지 않고, 모델은
  단계 프로세스 안에서만 불러온다.

## 의존성 규칙

- Electron은 `electron-app/main.mjs`에서만 import해 서비스에 주입한다.
- `main`과 `renderer`는 서로의 구현을 import하지 않는다. `shared`는 상위 계층을 참조하지 않는다.
- renderer의 의존성 전체에 Node·Electron이 없어야 한다. 파일 고르기, Finder 열기,
  끌어다 놓은 파일 경로는 preload의 `window.assetsStudio`로만 쓴다.
- 화면의 `../shared/x.mjs` import는 URL에서 `/shared/x.mjs`가 된다. model-viewer는
  `/vendor/model-viewer.min.js`로 제공하며 없으면 3D 미리보기만 빠진다.
- `workers/`는 TRELLIS 환경에서 실행되므로 `local_assets_engine`을 import하지 않는다.
- `npm run check:architecture`가 경로 단절·계층 역참조·순환·화면 의존성을 검사한다.

## HTTP API

| 메서드·경로 | 내용 |
| --- | --- |
| `GET /api/health` | 상태, 버전, 실행 중인 작업, 출력 폴더 |
| `GET /api/doctor?deep=true` | 진단. `deep`은 하위 프로세스 검사(MPS, TRELLIS Metal)를 포함 |
| `GET /api/presets` | `config/presets.json` |
| `GET /api/bench` | 단계·조건별 소요 시간과 최대 메모리 |
| `GET /api/jobs?limit=100` | 최근 작업 |
| `POST /api/jobs` | `{recipe, params}`를 검증해 대기열에 넣는다. 입력 오류는 400 |
| `GET /api/jobs/{id}` | 작업 기록 |
| `POST /api/jobs/{id}/cancel` | 대기 작업은 바로 취소, 진행 작업은 중지 요청 |
| `POST /api/jobs/{id}/assets/{assetId}/review` | `{status}`: `pending`, `approved`, `rejected` |
| `GET /api/assets?review=&kind=` | 작업을 가로지른 에셋 목록 |
| `GET /files/{id}/{path}` | 해당 작업 폴더 안의 파일만 제공 |

Host가 `127.0.0.1`·`localhost`가 아니거나 Origin이 다른 사이트면 403으로 거절한다.

### 레시피 입력

| 레시피 | 입력 | 기본값 |
| --- | --- | --- |
| `image` | `subject`(필수), `preset`, `style`, `count` 1~8, `seed`, `width`·`height` 256~2048(16의 배수로 내림), `removeBackground` | `item-icon`, 4장, 프리셋 크기 |
| `text-to-3d` | `image` 입력과 3D 입력. 자동 검사를 통과한 첫 컨셉 이미지로 3D를 만든다 | `prop-3d`, 1장 |
| `image-to-3d` | `imagePath`(절대 경로) 또는 `source: {jobId, assetId}`, 3D 입력, `removeBackground` | 배경 제거 켬 |
| 3D 입력 | `pipelineType` `512`·`1024`·`1024_cascade`, `textureSize` 512·1024·2048, `targetFaces` 0~1,000,000(0은 줄이지 않음), `sizeMeters`, `meshSeed` | `512`, 1024, 30000, 1.0 |

후보 시드는 시작 시드부터 1씩 늘린다. 시드를 비우면 엔진이 무작위로 정하고 기록한다.

## 작업 기록

`output/jobs/<작업 ID>/job.json`이 작업의 단일 기록이다.

```json
{
  "id": "20260916-013000-ab12",
  "recipe": "image",
  "title": "아이템 아이콘 · red potion",
  "params": { "preset": "item-icon", "subject": "red potion", "prompt": "red potion, game item icon, ...", "seeds": [7, 8, 9, 10] },
  "state": "done",
  "createdAt": "2026-09-16T01:30:00+09:00",
  "startedAt": "2026-09-16T01:30:00+09:00",
  "finishedAt": "2026-09-16T01:31:12+09:00",
  "stages": [
    {
      "name": "generate", "label": "이미지 생성", "state": "done", "progress": 1.0, "detail": null,
      "seconds": 58.4, "peakMemoryBytes": 20000000000,
      "processes": [{ "command": "mflux-generate-z-image-turbo", "seconds": 58.1, "peakMemoryBytes": 20000000000 }]
    }
  ],
  "assets": [
    {
      "id": "a01", "kind": "image", "role": "candidate", "file": "final/seed-7.png",
      "preview": "final/seed-7.png", "meta": { "seed": 7 }, "review": "pending"
    }
  ],
  "error": null,
  "logTail": []
}
```

| 필드 | 값 |
| --- | --- |
| `state` | `queued`, `running`, `cancelling`, `done`, `failed`, `cancelled` |
| `stages[].state` | `running`, `done`, `failed`, `cancelled`, `skipped` |
| `assets[].kind` / `role` | `image`·`mesh` / `candidate`(2D 후보), `concept`(3D용 컨셉), `final`(메시) |
| 이미지 `meta` | `seed`, `preset`, `prompt`, `model`, `width`, `height`, `checks`(`objectFound`, `coverage`, `touchesEdge`), `error`, `raw` |
| 메시 `meta` | `seed`, `pipelineType`, `textureSize`, `targetFaces`, `sizeMeters`, `stats`, `rawFile`, `optimizedFile`, `optimizedBytes`, `source`, `conceptAsset` |

- 러너와 API가 같은 기록을 고치므로 모든 쓰기는 `JobStore.update`로 다시 읽은 뒤 임시
  파일에 쓰고 교체한다.
- 파일 경로는 작업 폴더 기준 상대 경로다. `resolve_file`이 폴더 밖 경로를 막는다.
- 진행률은 최대 0.5초 간격으로 기록한다. 진행 막대 줄은 `job.log`에 남기지 않는다.

## 단계와 측정

| 단계 | 프로세스 | 진행률 출처 |
| --- | --- | --- |
| `generate` | `.venv/bin/<mflux 명령>`, 모든 시드를 한 번에 | 이미지마다 다시 도는 tqdm 막대를 장수로 합산 |
| `cutout` | `python -m local_assets_engine.tools.remove_bg` | `@@progress` JSON 줄 |
| `finish` | 엔진 안(PIL) | 처리한 후보 수 |
| `mesh` | `engines/trellis-mac/.venv/bin/python workers/trellis_runner.py` | generate.py 출력 표식과 샘플러 막대 3개 |
| `post` | `python -m local_assets_engine.tools.blender_post` | `@@progress` JSON 줄 |
| `optimize` | `gltfpack -i asset.glb -o asset.opt.glb` | 없음. gltfpack이 없으면 `skipped` |

- `peakMemoryBytes`는 `/usr/bin/time -l`의 peak memory footprint다. Metal 할당이 포함되는지는
  확인하지 않았다(포함으로 추정). 같은 측정 방식의 기록끼리만 비교한다.
- 모델을 처음 내려받은 실행은 시간 비교에서 빼고 본다.

## 3D 계약

- TRELLIS 입력은 `mesh/input.png`다. 알파가 있으면 그대로 쓰고, 없으면 BiRefNet으로 먼저 지운다.
- `trellis_runner.py`는 `briaai/RMBG-2.0` 로드를 고정 커밋의 `ZhengPeng7/BiRefNet`으로
  바꾼 뒤 generate.py를 실행한다. generate.py가 torch보다 먼저 두는 환경 변수를 같은 값으로 먼저 둔다.
- Blender 정리는 부모 변환을 굽고 메시를 합친 뒤 COLLAPSE 방식으로 면을 줄인다. 바닥 중심을
  원점에 두고 가장 긴 변을 `sizeMeters`로 맞춰 GLB(+Y 위)로 내보낸다.
- 결과 파일은 `mesh/raw.glb`(TRELLIS 원본), `mesh/asset.glb`(정리본),
  `mesh/asset.opt.glb`(gltfpack), `mesh/asset.stats.json`이다.
