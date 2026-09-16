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
| `src/local_assets_engine/tools/` | 엔진 환경에서 도는 단계 프로세스: BiRefNet, Blender 정리, 프리비즈 렌더 |
| `src/local_assets_engine/workers/trellis_runner.py` | TRELLIS 환경에서 generate.py를 감싸 실행 |
| `src/local_assets_engine/workers/gltf_export.py` | KDTree GLB 내보내기 계약 보정, 이전 원본 복구(모델 적재 없음) |
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
  그래서 레시피를 고친 뒤에는 떠 있던 엔진을 끄고 앱을 다시 열어야 새 코드가 올라온다.
- 화면(HTML·JS·CSS)은 엔진이 저장소에서 바로 내보내며 `Cache-Control: no-store`를 붙이고, 앱도
  화면을 띄울 때마다 창 캐시를 비운다. 작업 결과(`/files/`)와 벤더 스크립트는 캐시를 허용한다.
- 창의 기본 앱 열기는 작업 폴더 안의 `.blend`만 받는다(`assets:open-blend`).
- 엔진이 SIGTERM을 받으면 진행 중인 작업을 중지한다. 단계 프로세스는 자기 세션으로
  실행되므로 그 프로세스 그룹에 SIGTERM을, 3초 뒤에도 남으면 SIGKILL을 보낸다.
- 엔진이 시작할 때 `queued`로 남은 작업은 `cancelled`, `running`으로 남은 작업은
  `failed`로 닫는다. 어느 쪽도 다시 실행하지 않는다.
- 단, 다른 프로세스가 지금 돌리고 있는 작업은 닫지 않는다. 작업을 시작할 때 `owner`에
  프로세스 번호를 적고 기록을 쓸 때마다 `heartbeat`를 갱신하므로, 그 프로세스가 살아
  있고 하트비트가 3분 안쪽이면 남의 작업으로 보고 건너뛴다. 앱과 CLI가 각자 엔진을
  띄울 수 있어서, 이 검사가 없으면 앱을 여는 것만으로 CLI가 돌리던 생성이 끊긴다.
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
| `repair-mesh` | `source: {jobId, assetId}`(완료된 이전 KDTree 메시) | 원본의 시드·해상도·면 수·크기 유지 |
| 3D 입력 | `pipelineType` `512`·`1024`·`1024_cascade`, `textureSize` 512·1024·2048, `targetFaces` 0~1,000,000(0은 줄이지 않음), `sizeMeters`, `meshSeed` | `512`, 1024, 30000, 1.0 |
| `previz` | `preset`(샷 프리셋), `assets` 1~8개 배치, `shots`(앱에서 고친 컷 목록, 없으면 프리셋 그대로), `renderer`, `width`·`height`, `fps` 6~30, `samples`, `aux`, `animatic`, `ground`, `clay` | `game-trailer`, `eevee`, 960×540, 12fps, 16, `keys`, 모두 켬 |
| 컷 항목 | `id`(영문·숫자·-·_ 32자), `label`, `purpose`, `move`, `focus`(`hero`·`scene`·에셋 id), `lens`·`lensEnd` 8~300, `seconds` 0.2~60, `ease`, `framing`(`distance`·`azimuth`·`height`·`targetHeight`·`roll`와 각 `...End`, `targetOffset`) | 프리셋 값 |
| 배치 항목 | `source: {jobId, assetId}`(완성된 메시), `path`(GLB·glTF 절대 경로), `standin`(대역 id) 중 하나, `id`, `position` [x, y, z] 미터, `yaw` 도, `scale`. 대역은 `size` [가로, 깊이, 높이] 미터 0.05~500 | 원점, 0도, 1.0, 카탈로그 치수 |

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
| `owner` | 실행 중인 작업에만 있다. `{pid, heartbeat}`로 시작 복구가 남의 작업을 닫지 않게 한다 |
| `assets[].kind` / `role` | `image`·`mesh`·`shot` / `candidate`(2D 후보·프리비즈 샷), `concept`(3D용 컨셉), `final`(메시) |
| 이미지 `meta` | `seed`, `preset`, `prompt`, `model`, `width`, `height`, `checks`(`objectFound`, `coverage`, `touchesEdge`), `error`, `raw` |
| 메시 `meta` | `seed`, `pipelineType`, `textureSize`, `targetFaces`, `sizeMeters`, `stats`, `rawFile`, `optimizedFile`, `optimizedBytes`, `source`, `conceptAsset` |
| 샷 `meta` | 아래 프리비즈 계약 참고 |

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
| `repair` | `python workers/gltf_export.py --input ... --output ...` | 없음. 기존 KDTree 원본을 새 작업으로 복구 |
| `previz` | `python -m local_assets_engine.tools.previz_render` | `@@progress` JSON 줄, 렌더 호출 수 기준 |

- 이미지 생성과 3D 생성은 GPU 혼잡 신호(`kIOGPUCommandBufferCallbackErrorTimeout`,
  `Command buffer execution failed`, `Insufficient Memory`)로 끊기면 10초 뒤 한 번 다시
  시도한다. 다른 원인의 실패는 바로 멈춘다. 실패한 시도의 측정도 `processes`에 남으므로
  한 단계에 기록이 여러 개일 수 있고, `seconds`와 `peakMemoryBytes`는 단계 전체 기준이다.
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
- KDTree 대체 경로의 GLB 내보내기는 래퍼가 `gltf_export.py`로 교체한다. 굽기에 쓴 V와
  glTF의 이미지 행 좌표를 맞추고, TRELLIS Z-up을 glTF Y-up으로 변환하며 양면 재질로 저장한다.
  원본 메시 extras의 `local_assets_export: {version: 1, backend: "kdtree"}`가 보정 여부를 표시한다.
  Metal 경로에는 이 보정을 적용하지 않으며, 래퍼 보정 로드 실패는 작업 실패로 기록한다.
- 메시 `meta.processingVersion`은 현재 후처리 계약의 버전이다(현재 1). `stats.topology`에는
  `boundaryEdges`, `nonManifoldEdges`, `inconsistentWindingEdges`, `duplicateFaces`, `warnings`를
  기록한다. UV 경계의 동일 위치 정점은 검사할 때만 합쳐 세며 파일을 바꾸지 않는다.
  경고는 에셋 상세에 표시하고 승인·거절은 사람이 정한다.
- `repair-mesh`는 기존 작업 기록으로 이전 KDTree 경로임을 확인하고, 원본 raw의 내보내기만
  새 작업 폴더에서 보정한 뒤 동일한 `post`·`optimize` 단계를 실행한다. 모델을 적재하지 않으며
  각 단계는 동일한 러너와 측정을 거친다. 원본 파일·승인·프리비즈 참조는 바꾸지 않고,
  새 메시의 `meta.source`가 원본 메시를 가리킨다. 복구본은 검토 대기다.

## 프리비즈 계약

프리비즈의 산출물은 그림이 아니라 **샷 값**이다. 그림은 사람이 승인할 수 있게 만든다.

- 레시피가 `previz/plan.json`(배치·샷 규칙)을 쓰고, 도구가 `previz/shots.json`(풀린 카메라
  값과 파일 목록)을 쓴다. 승인 상태는 `job.json`의 에셋에만 있다.
- 앱의 프리비즈 화면은 프리셋을 초안으로 불러와 컷 순서·길이·카메라를 고치고 `shots`로 보낸다. 고친 적이 없으면
  `shots`를 보내지 않아 기록의 `edited`가 거짓이다. 화면의 지도 카메라 계산(`electron-app/shared/previz.mjs`)은
  엔진의 식과 같아야 하며, 같은 에셋·프리셋으로 엔진이 기록한 좌표와 맞는지 테스트가 지킨다.
- 샷 프리셋은 `config/presets.json`의 `previz.shotPresets`다. 장르는 코드가 아니라 이 목록이며
  컷 길이·렌즈·움직임·구도 규칙을 정한다. `focus: "hero"`는 장면의 첫 배치(에셋이나 대역)를 가리킨다.
- 대역은 `previz.standins`다. 부품(`box`·`cylinder`·`sphere`, 원기둥은 `axis`)을 카탈로그 치수 기준
  미터(`at`은 부품 중심, `size`는 부품 경계)로 적고, 부품 전체의 경계가 `size`와 같아야 한다. 설정을
  읽을 때 검사한다. 앱 지도는 `size`로, 엔진은 만든 도형의 경계로 피사체를 재기 때문이다. 정면은 −Y다.
- 레시피는 요청 치수에 맞춰 부품을 축마다 늘여 `params.assets[].parts`에 남기고, 도구는 대역 하나를
  메시 하나로 만든 뒤 GLB와 같이 바닥 중심을 배치 좌표에 맞춘다. 메시를 나누면 돌렸을 때 경계가 지도의
  회전 경계와 달라진다. 대역은 점토 설정과 관계없이 `look.standinColor`(기본 차가운 회색)로 칠한다.
- 프레이밍은 미터가 아니라 **피사체 단위**로 적는다. `distance: 1.0`이 피사체가 화면을 꽉
  채우는 거리이고 `2.0`은 그 두 배다. 그래서 같은 프리셋을 찻잔과 탑에 함께 쓸 수 있다.
  `azimuth` 0도는 피사체 정면(−Y), `height`·`targetHeight`는 피사체 높이의 배수다.
  `...End`가 붙은 값이 있으면 그 사이를 움직이고, `ease`(`inout`·`linear`)로 가감속한다.

| 샷 `meta` 필드 | 값 |
| --- | --- |
| `shot`, `label`, `purpose`, `move`, `order` | 샷 id, 이름, 연출 의도, 움직임 이름, 컷 순서 |
| `focus` | 이 샷이 겨냥한 장면 에셋 id 또는 `scene` |
| `seconds`, `fps`, `frames`, `ease` | 컷 길이와 프리비즈가 실제로 뽑은 프레임 수 |
| `lens`, `lensEnd`, `sensorWidth` | 렌즈(mm)와 센서 폭. 줌이 없으면 두 렌즈 값이 같다 |
| `start`, `end` | `{position, target, lens}`. 미터 단위 월드 좌표(Blender Z 위) |
| `framing` | 프리셋이 준 상대 규칙 원본 |
| `depthRange` | 깊이 맵의 near·far(m). 이 값이 없으면 깊이 그림을 해석할 수 없다 |
| `renderer`, `resolution`, `clay`, `renderSeconds` | 어떤 스케치로 승인했는지와 그 비용 |
| `files` | `animatic`(mp4), `key`(대표 프레임), `color`·`depth`·`line` 목록 |
| `pathFile` | 프레임별 카메라 값이 든 `previz/shots.json` 경로 |

| `sequence` | 컷을 이어 붙인 한 편에서의 자리: `{file, seconds, at, start, end}`. `at`은 초, `start`·`end`는 프레임 |
| `blendFile` | 모든 컷이 한 타임라인에 든 Blender 장면(`previz/scene.blend`) |

- `previz/sequence.mp4`는 컷 애니매틱을 순서대로 이은 한 편이다. Blender 시퀀서로 다시 렌더하지
  않고 잇기만 한다. `shots.json`의 `sequence.cuts`가 컷별 시작 프레임을 담는다.
- `previz/scene.blend`에는 컷마다 `CAM_<샷 id>` 카메라가 있고, 타임라인 마커가 컷 시작 프레임에서
  그 카메라로 바꾼다. 텍스처를 파일 안에 넣어 작업 폴더 밖에서도 열리고, 열면 카메라 시점이다.
  사람이 Blender에서 카메라를 손으로 고치는 출발점이며 엔진은 이 파일을 다시 읽지 않는다.
- 프레임별 카메라 값은 `job.json`을 불리므로 `shots.json`에만 둔다. `job.json`은 시작·끝만 든다.
- 깊이는 `depthRange`로 정규화한 그림이고 샷 안에서 축척이 고정된다. 윤곽선은 그 깊이에
  소벨을 걸어 얻는다. 둘 다 나중 영상 모델의 조건 입력으로 쓸 수 있게 남긴다.
- 파일은 `previz/shots/<샷 id>/`에 모인다. `aux: "keys"`는 처음·중간·끝 프레임만,
  `"all"`은 모든 프레임의 깊이·윤곽을 남긴다.
