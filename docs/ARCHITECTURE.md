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
| `src/local_assets_engine/workers/trellis_infer.py`, `mesh_extract.py` | 감축 전 형상·복셀 PBR 저장, 동일 결과의 벡터화 연결 추출 |
| `src/local_assets_engine/workers/quality_*.py` | 원본 표면 재구성, 형상 오차 제한 감축, 최종 UV·PBR 굽기 |
| `src/local_assets_engine/workers/gltf_export.py` | 좌표·UV·PBR GLB 계약, 이전 KDTree 원본 복구 |
| `src/local_assets_engine/uploads.py` | 20MB·1,677만 픽셀 이하 로컬 이미지 가져오기, 경로 없는 ID |
| `tools/asset_edit.py`, `tools/edit_pixels.mjs` | 측정 프로세스에서 표면 편집 적용, 원본 형상 버퍼 보존 |
| `electron-app/shared/editing.mjs`, `glb.mjs` | 미리보기·최종 저장이 공유하는 레이어 합성(`renderEdit`)·영역 마스크·표면 투영과 GLB 읽기 |
| `electron-app/renderer/editor.js`, `edit-preview.js`, `drafts.js` | 편집 공간(레이어·손잡이·브러시·작업 내역), 미리보기 Worker, 로컬 임시 편집 보관 |
| `src/local_assets_engine/library.py` | 즐겨찾기·태그·컬렉션 검증, 버전 계보, 작업 폴더 파일 분류(보관 용량) |
| `src/local_assets_engine/imaging.py` | 모델 없는 이미지 후처리: 캔버스 맞춤, 픽셀화, 자동 검사 |
| `presets.py`, `config/presets.json` | 모델, 종류 프리셋, 3D 기본값 |
| `doctor.py`, `bench.py`, `cli.py` | 진단, 측정 요약, 명령줄 |
| `estimates.py` | 같은 생성 조건의 성공 기록 중앙값과 대기열 완료 예상 |
| `electron-app/renderer/library-batch.js` | 에셋 다중 선택, 일괄 정리, 작업 단위 휴지통 확인 |
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
- 휴지통 보내기(`assets:trash-intermediate`, `assets:trash-job`)는 main이 엔진의
  `GET /api/jobs/{id}/storage`를 다시 읽어 진행 중이 아닌지, 요청한 경로가 `intermediate`로
  분류됐는지 확인한 뒤 `shell.trashItem`으로 옮긴다. 지우지 않으므로 Finder 휴지통에서 되살린다.
- 창에는 제목 막대가 없다(`hiddenInset`). 맨 위 28px 띠와 화면 머리글이 `-webkit-app-region: drag`이고
  단추·입력·팝업은 `no-drag`다. 끌기 띠는 문서 맨 앞에 있어 뒤에 오는 `no-drag` 요소가 영역을 뚫는다.
- 엔진이 SIGTERM을 받으면 진행 중인 작업을 중지한다. 단계 프로세스는 자기 세션으로
  실행되므로 그 프로세스 그룹에 SIGTERM을, 3초 뒤에도 남으면 SIGKILL을 보낸다.
- 엔진이 시작할 때 `queued`로 남은 작업은 `cancelled`, `running`으로 남은 작업은
  `failed`로 닫는다. 어느 쪽도 다시 실행하지 않는다.
- 단, 다른 프로세스가 지금 돌리고 있는 작업은 닫지 않는다. 작업을 시작할 때 `owner`에
  프로세스 번호를 적고 기록을 쓸 때마다 `heartbeat`를 갱신하므로, 그 프로세스가 살아
  있고 하트비트가 3분 안쪽이면 남의 작업으로 보고 건너뛴다. 앱과 CLI가 각자 엔진을
  띄울 수 있어서, 이 검사가 없으면 앱을 여는 것만으로 CLI가 돌리던 생성이 끊긴다.
- 같은 출력 저장소의 `.engine.lock`을 프로세스 사이에서 공유한다. 독립 CLI와 앱도 생성·후처리를
  동시에 실행하지 않는다. 대기열도 소유자와 하트비트를 유지하고, 다른 엔진의 중지 요청을 읽는다.
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
| `POST /api/uploads` | PNG/JPG/WEBP 바이트를 검증·정규화하고 로컬 이미지 ID 반환 |
| `GET /uploads/{id}` | 해당 이미지 PNG. ID로만 조회, 경로 입력 불가 |
| `GET /api/presets` | `config/presets.json` |
| `GET /api/bench` | 단계·조건별 소요 시간과 최대 메모리 |
| `GET /api/estimates` | 진행·대기 작업별 표본 수·처리 중앙값·남은 시간·대기 시간·완료 예상(초), 추정 불가는 null |
| `GET /api/jobs?limit=100` | 최근 작업 |
| `POST /api/jobs` | `{recipe, params}`를 검증해 대기열에 넣는다. 입력 오류는 400 |
| `GET /api/jobs/{id}` | 작업 기록 |
| `POST /api/jobs/{id}/cancel` | 대기 작업은 바로 취소, 진행 작업은 중지 요청 |
| `POST /api/jobs/{id}/assets/{assetId}/review` | `{status}`: `pending`, `approved`, `rejected`. 앱은 프리비즈 컷에만 쓴다 |
| `POST /api/jobs/{id}/assets/{assetId}/library` | `{favorite?, tags?, addTags?, removeTags?, collection?, note?}`. 태그 교체·추가·제거 중 하나만 받는다. 태그 20개·40자, 컬렉션 60자, 메모 500자. 빈 값은 필드를 지운다 |
| `GET /api/jobs/{id}/assets/{assetId}/versions` | 이 이미지·메시의 원본부터 모든 파생 버전: `{current, root, versions[{key, parent, depth, relation, summary, ...}]}` |
| `GET /api/assets?review=&kind=&favorite=&collection=&tag=` | 작업을 가로지른 에셋 목록 |
| `GET /api/storage` | 분류별 합계와 작업별 크기, 다른 작업이 원본으로 쓰는지(`usedBy`), 중간 파일 목록 |
| `GET /api/jobs/{id}/storage` | 작업 폴더의 파일별 크기·분류와 `active` |
| `GET /files/{id}/{path}` | 해당 작업 폴더 안의 파일만 제공 |

Host가 `127.0.0.1`·`localhost`가 아니거나 Origin이 다른 사이트면 403으로 거절한다.

### 레시피 입력

| 레시피 | 입력 | 기본값 |
| --- | --- | --- |
| `image` | `subject`(필수), `preset`, `style`, `imageModel`(등록된 모델 id), `count` 1~8, `seed`, `width`·`height` 256~2048(16의 배수로 내림), `removeBackground` | `item-icon`, 4장, 프리셋 크기 |
| `text-to-3d` | `image` 입력과 3D 입력. 자동 검사를 통과한 첫 컨셉 이미지로 3D를 만든다 | `prop-3d`, 1장 |
| `image-to-3d` | `imagePath`(절대 경로), `uploadId`, 또는 `source: {jobId, assetId}`, 3D 입력, `removeBackground` | 배경 제거 켬 |
| `repair-mesh` | `source: {jobId, assetId}`(완료된 이전 KDTree 메시) | 원본의 시드·해상도·면 수·크기 유지 |
| `refine-mesh` | `source: {jobId, assetId}`(감축 전 원본이 있는 완료된 메시), 3D 출력 설정 | 원래 시드·형상 해상도, 현재 품질 기본값 |
| 3D 입력 | `pipelineType` `512`·`1024`·`1024_cascade`, `textureSize` 512·1024·2048·4096, `targetFaces` 0~1,000,000(0은 줄이지 않음), `gameFaces` 0~1,000,000(0은 게임용 생략), `gameTextureSize`, `sizeMeters`, `meshSeed` | `512`, 4096, 1,000,000, 게임용 생략(`gameFaces: 0`), 1.0m |
| `import-image` | `uploadId`, `name` | 이미지를 새 에셋으로 등록 |
| `edit-asset` | `source`, `name`, `plan`(아래 편집 계약), `replaceEdits` | 원본 보존, 새 버전 생성. 컬렉션·태그는 원본에서 이어받는다 |
| `previz` | `preset`(샷 프리셋), `assets` 1~8개 배치, `shots`(앱에서 고친 컷 목록, 없으면 프리셋 그대로), `renderer`, `width`·`height`, `fps` 6~30, `samples`, `aux`, `animatic`, `ground`, `clay` | `game-trailer`, `eevee`, 960×540, 12fps, 16, `keys`, 모두 켬 |
| 컷 항목 | `id`(영문·숫자·-·_ 32자), `label`, `purpose`, `move`, `focus`(`hero`·`scene`·에셋 id), `lens`·`lensEnd` 8~300, `seconds` 0.2~60, `ease`, `framing`(`distance`·`azimuth`·`height`·`targetHeight`·`roll`와 각 `...End`, `targetOffset`) | 프리셋 값 |
| 배치 항목 | `source: {jobId, assetId}`(완성된 메시), `path`(GLB·glTF 절대 경로), `standin`(대역 id) 중 하나, `id`, `position` [x, y, z] 미터, `yaw` 도, `scale`. 대역은 `size` [가로, 깊이, 높이] 미터 0.05~500 | 원점, 0도, 1.0, 카탈로그 치수 |

후보 시드는 시작 시드부터 1씩 늘린다. 시드를 비우면 엔진이 무작위로 정하고 기록한다.

2D 카테고리는 `imageCategories`(게임·실사·애니), 종류는 `presets[].category`다. 카테고리 없는 기존 2D
프리셋은 게임으로 표시한다. `imageModel`은 기존 기본 모델 객체, `imageModels`는 추가 선택지 목록이다.
모델 선택 우선순위는 요청의 `imageModel` → 프리셋의 `imageModel` → 공통 기본이다. 요청 준비 단계가
서버 설정에서 `imageModelConfig`를 복사해 저장하고 실행은 그 스냅샷을 쓴다. 클라이언트가 보낸 모델 설정은
받지 않는다. `modelArg`는 mflux의 `--model`에 명시적으로 전달한다. 새 실사·애니 프리셋은 배경 제거를
기본으로 끄며, 기존 게임·3D의 모델·스텝·해상도는 유지한다. 이미지 메타에 `category`도 기록한다.

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
| `owner` | 실행·대기를 맡은 `{pid, heartbeat}`. 시작 복구가 살아 있는 다른 엔진의 작업을 닫지 않게 한다 |
| `assets[].kind` / `role` | `image`·`mesh`·`shot` / `candidate`(2D 후보·프리비즈 샷), `concept`(3D용 컨셉), `final`(메시) |
| `assets[].favorite`·`tags`·`collection`·`note` | 사람이 정리한 값(없으면 필드 자체가 없다). `review`와 따로다. 자동 검사는 이 값을 바꾸지 않는다 |
| 이미지 `meta` | `seed`, `preset`, `prompt`, `model`, `width`, `height`, `checks`(`objectFound`, `coverage`, `touchesEdge`), `error`, `raw` |
| 메시 `meta` | `seed`, `pipelineType`, `textureSize`, `targetFaces`, `sizeMeters`, `stats`, `variant`(`master`·`game`), `label`, `sourceStateFile`, `inspectionFile`, `processingVersion`, `rawFile`, `optimizedFile`, `optimizedBytes`, `source`, `conceptAsset` |
| 샷 `meta` | 아래 프리비즈 계약 참고 |

- 러너와 API가 같은 기록을 고치므로 모든 수정은 `JobStore.update`에서 작업별 파일 잠금과
  스레드 잠금을 잡고 다시 읽은 뒤 임시 파일에 쓰고 교체한다.
- 파일 경로는 작업 폴더 기준 상대 경로다. `resolve_file`이 폴더 밖 경로를 막는다.
- 진행률은 최대 0.5초 간격으로 기록한다. 진행 막대 줄은 `job.log`에 남기지 않는다.

## 단계와 측정

| 단계 | 프로세스 | 진행률 출처 |
| --- | --- | --- |
| `generate` | `.venv/bin/<mflux 명령>`, 모든 시드를 한 번에 | 이미지마다 다시 도는 tqdm 막대를 장수로 합산 |
| `cutout` | `python -m local_assets_engine.tools.remove_bg` | `@@progress` JSON 줄 |
| `finish` | 엔진 안(PIL) | 처리한 후보 수 |
| `mesh` | `engines/trellis-mac/.venv/bin/python workers/trellis_runner.py` | generate.py 출력 표식과 샘플러 막대 3개 |
| `surface` | TRELLIS 환경 `quality_surface.py` | unsigned distance 재구성·형상 보존 감축 |
| `lod` | TRELLIS 환경 `quality_lod.py` + gltfpack | 형상 오차 제한과 위상 회귀 검사, UV 이전 실행 |
| `texture-master`·`texture-game` | TRELLIS 환경 `quality_texture.py` | UV → 원본 표면 질의 → native PBR |
| `post-master`·`post-game` | `python -m local_assets_engine.tools.blender_post` | 크기·원점만 정리, 추가 감축 없음 |
| `optimize-master`·`optimize-game` | `gltfpack ... -noq` | 추가 감축·양자화 없는 전송용 사본 |
| `inspect` | `python -m local_assets_engine.tools.mesh_inspect` | 실제 GLB의 네 측면·위·아래 렌더 |
| `post`·`optimize` | Blender·gltfpack | 이전 `repair-mesh` 전용 |
| `edit`·`import` | `python -m local_assets_engine.tools.asset_edit` + Node.js 픽셀 계산 | 원본을 새 작업에 복사하고 새 버전 저장 |
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

1. `mesh/input.png`를 TRELLIS에 넣는다. 알파가 없으면 먼저 BiRefNet으로 배경을 지운다.
   래퍼는 파이프라인 내부 RMBG-2.0 로드도 고정 커밋의 BiRefNet으로 바꾼다.
2. 고정된 TRELLIS 모델 스냅샷에서 요청한 해상도의 모델만 올린다. 512 기본값과 학습된
   샘플러 설정은 유지한다. `mesh_extract.py`는 면의 순서·연결·분할을 바꾸지 않고 CPU
   정수 키 탐색을 벡터화한다. 원본 엔진 클론은 수정하지 않는다.
3. 감축 전 **전체 메시·복셀 PBR**을 `mesh/source.npz`에 원자적으로 저장하고 모델 프로세스를
   끝낸다. `source.json`에는 모델 리비전·시드·입력 해시·샘플러 설정·적재/추론 시간을 남긴다.
4. CPU에서 원본 표면까지의 unsigned distance를 계산해 좁은 두께의 일관된 표면으로 재구성한다.
   물체 내부를 통째로 채우지 않아 고리 구멍과 열린 얇은 면을 보존한다. 가장 짧은 모서리부터
   위상을 보존하며 품질본 면 수로 감축하고, 뒤집힘을 막으며 원본 표면에 가깝게 투영한다.
5. 게임용은 기본 생성에서 생략한다. API에 `gameFaces > 0`을 명시한 경우에만 **품질본에서 UV를 펴기 전에** 형상 오차를 제한하며 감축한다. 위상이 나빠지면
   오차를 더 엄격히 하여 재시도하고 끝내 통과하지 못하면 품질본 형상을 유지한다. 목표 면 수를
   맞추려고 형상을 망가뜨리지 않으며, 초과한 실제 면 수와 이유를 경고에 기록한다.
6. 각 최종 형상에 UV를 펴고 텍셀 중심을 원본 표면으로 투영하여 원본 복셀의 PBR을 삼선형
   보간한다. 없는 복셀은 검은색과 섞지 않는다. 별도 감마를 추가하지 않고 금속성·거칠기
   계수는 1이다. 패딩을 채워 필터링의 검은 경계도 막는다. Z-up→Y-up과 UV 행 규약을
   위치·부드러운 노멀·텍스처에 함께 적용한다.
7. Blender는 **추가 감축 없이** 바닥 중심과 미터 크기만 맞춘다. 품질본과 게임용은 동일한
   변환을 공유한다. gltfpack 사본도 감축·정밀도 양자화를 하지 않는다.
8. 실제 최종 GLB를 여섯 방향으로 렌더하고 품질본을 `pending` 에셋으로 등록한다.
   게임용을 요청한 경우에도 별도 `pending` 에셋이다.
   카드의 그림은 생성 입력이 아니라 해당 GLB의 렌더다.

파일은 `mesh/asset.glb`(품질본), `asset.game.glb`(게임용), 각각의 `.raw.glb`·`.opt.glb`·
`.stats.json`·`.raw.bake.json`, `surface/*.npz`·진단 JSON, `inspection/*-contact.png`다.
`raw`는 현재 계약에서는 최종 재질 굽기 직후, 크기·원점 정리 전 파일을 뜻한다.
`source.npz`가 유일한 감축 전 모델 원본이다. 텍스처 해상도는 아틀라스 출력 크기이며 모델이
예측한 복셀 이상의 새 디테일을 만들어내는 값은 아니다.

`meta.processingVersion`은 **2**다. `stats.sourceTriangles`는 생성 원본,
`facesIn`·`facesOut`은 정규화 전후의 면 수다. `stats.topology`는 열린 경계·비다양체 모서리·
이웃 면 방향·중복 면을 검사하고, `stats.lod`에는 게임용의 감축 시도와 실제 오차 제한을 남긴다.
UV 경계의 동일 위치 정점은 검사할 때만 합친다. 검사는 경고이며 승인·거절은 사람이 한다.

`refine-mesh`는 완료된 원본 NPZ와 입력 이미지를 **새 작업**으로 복사한 뒤 4~8단계를 실행한다.
원본의 시드·형상 해상도는 유지하고 면 수·텍스처는 현재 요청/기본값을 따른다. 모델 추론을
반복하지 않고 다른 출력 설정을 비교할 수 있다. 기존 작업·승인·프리비즈 참조는 바꾸지 않는다.

이전 계약(버전 1)의 `repair-mesh`는 기존 KDTree raw GLB의 UV·위쪽 축·양면 표시만 복구한다.
이미 손실된 형상은 되살리지 못한다. 이전 `audit: true` 작업의 `mesh/audit/decoded.npz`는
`refine-mesh`의 원본으로 사용할 수 있다. 새 생성은 `audit` 여부와 관계없이 원본을 항상 남긴다.
진단용 `trellis_audit.py`는 이전 경로 재현용이며 일반 생성 기본 경로는 아니다.

## 생성 UI와 표면 편집 계약

디자인 컴포넌트와 사용 흐름은 [DESIGN_SYSTEM](DESIGN_SYSTEM.md)을 따른다.
3D 컨셉 확인과 바로 생성은 UI의 명시적 선택이며 후보 수에 의존하지 않는다. 레거시 CLI의
`text-to-3d`는 계속 자동 생성을 지원한다. 주 화면에는 결과·간결한 상태를 보여 주고 상세 단계는
작업 기록 팝업에 표시한다. `job.json` 계측과 환경의 측정 기록은 그대로 유지한다.

`edit-asset`의 `plan`은 전체 보정과 레이어 목록이다.

- `brightness`, `contrast`, `saturation`: 픽셀 색 조절. `metallic`, `roughness`: 기존 PBR 계수에 곱할 유지 비율. 기본 1.
- `frame`(2D만): `{turns 0..3, flipX, crop, ratio, padding 0..0.5, background, width}`. `crop`은 회전·반전한
  이미지 기준 정규화 사각형 `{x,y,w,h}` 또는 예전 비율 이름(`square`·`portrait`·`landscape`·`wide`), `null`은 원래 크기.
  `padding`은 자른 영역 긴 변의 비율로 네 방향에 더하고 `background`(없으면 투명)로 채운다. `width`는 출력 가로
  픽셀(16~4096, 세로는 비율대로이며 4096을 넘지 않게 함께 줄인다). 줄일 때는 칸 평균, 정수배 확대는 최근접 픽셀이다.
- `layers`(32개 이하, `id`는 영문·숫자·`-`·`_`): 공통 `{id, type, name, visible, locked}`.
  - `color`: `{mode: match|fill, from, to, tolerance, region}`. `match`는 `from`과 가까운 색만, `fill`은 영역 전체를
    바꾸며 둘 다 원래 명암을 유지한다. `region`이 `null`이면 에셋 전체, `{strokes: []}`이면 아무 곳도 바꾸지 않는다.
    브러시 획은 순서대로 칠하기(1)·지우기(0)이고 가장자리 30%가 부드럽다. 2D 획은 `[u, v, 반지름, 모드]`로 **원본
    이미지** 정규화 좌표(반지름은 원본 가로 비율)라 회전·자르기를 바꿔도 같은 부위다. 3D 획은 `[x, y, z, 반지름(m), 모드]`
    glTF 모델 좌표 구이며, 삼각형을 UV에 래스터화해 구 안의 표면 텍셀만 고르고 UV 섬 바깥 여백으로 2텍셀 번진다.
    같은 색이 여러 부위에 있어도 칠한 부위만 바뀐다. 획은 2만 개까지 받는다.
  - `stamp`(로고·문구): `{uploadId, size, rotation, opacity, text?, textColor?, font?}`. 2D는 출력 이미지 기준 `x,y`와
    출력 가로 비율 크기, 3D는 미터 크기와 `position, normal, depth, clip`이다. `clip: connected`(새 레이어 기본)는
    찍은 면과 같은 위치 정점으로 이어진 표면 중 로고 방향을 보고 사각형·깊이 안에 드는 면만 받는다. 자물쇠·손잡이처럼
    튀어나온 부위에 붙인 로고가 옆면에서 멈춰 뒤 판으로 번지지 않는다. `projection`(예전 편집본)은 범위 안 모든 앞면이다.
    로고도 UV 섬 경계 여백으로 번져 필터링 때 끊겨 보이지 않는다.
- 적용 순서: 색 레이어(원본 색에서 고름) → 전체 보정 → 2D 구성 → 로고·문구 레이어(목록 순서). 색 레이어는 로고 위에 오지 않는다.
- 예전 설정(`recolor` 하나, `overlay` 하나, 문자열 `crop`)은 엔진과 화면이 같은 규칙으로 `color-1`·`stamp-1` 레이어로 옮긴다.

미리보기는 Web Worker, 최종 저장은 측정된 Python→Node 프로세스에서 **같은** `renderEdit`을 실행한다. 미리보기
텍스처만 1024px 이내로 표시하며 최종 파일은 원래 텍스처 해상도를 유지한다. 2D 미리보기는 출력 크기 조정을 생략한다
(좌표가 정규화돼 있어 모양은 같다). 형상(`meshContext`: 합친 정점·면·면 법선·경계 상자, 필요할 때 이음매 정점 병합과
UV 덮개)은 로고나 브러시 영역이 있을 때만 푼다. 1백만 면 상자 기준 1024px 미리보기 한 번에 20~120ms, 4K 저장의
브러시 영역 계산은 약 1초였다. CanvasTexture의 V 규약은 화면에 적용할 때만 뒤집는다. 저장 PNG와 glTF UV는 그대로다.

3D 로고는 선택한 면 주변을 평면 투영하여 원래 색 텍스처에 합성한다. 조각·불리언·형상 변경이나 실제 음각 기능은
아니며 곡면을 따라 늘여 감싸지도 않는다. 애니메이션, 스킨, 압축·특수 UV 메시에는 지원 범위를 명확한 오류로 알린다.
이 프로젝트의 정적 품질본이 대상이다. GLB 저장은 이미지 bufferView와 요청한 재질 계수만 바꾸고 모든 형상·UV·노멀·
면 인덱스 바이트를 보존한다.

결과는 `edit/asset.png` 또는 `edit/asset.glb`, `edit/source.*`(편집 기준), 레이어마다 `edit/logo-<레이어 id>.png`,
`edit/request.json`, `edit/stats.json`, 3D 검수 이미지다. 픽셀 계산용 `edit/scratch/`는 저장이 끝나면 지운다.
`meta.source`는 이전 버전을, `editBaseFile`, `editStampFiles`(레이어 id → 그림), `editPlan`은 재편집 기준과 설정을
가리킨다(예전 편집본은 `editStampFile` 하나). UI가 `replaceEdits: true`로 저장하면 기준 파일에 수정된 설정을
재적용하여 로고·보정이 중복으로 구워지지 않는다. 기본 API 호출은 선택한 현재 파일 위에 새 편집을 적용한다.

편집본의 원본 복셀 참조는 그대로 물려주지 않는다. 과거 복셀로 재구성하면 표면 편집이 사라지기 때문이다. 이전 버전을
열어 원본으로 돌아갈 수 있다. 파일·승인·프리비즈 참조는 덮어쓰지 않고 새 에셋을 만든다. 미저장 편집은 브라우저
IndexedDB에 `{version: 2, name, plan, stamps: [[레이어 id, {image, blob, upload, label}]], selected}`로 따로 보관하고
변경 0.4초 뒤, 창을 닫거나 앱이 가려질 때(`pagehide`·`visibilitychange`) 쓴다. 만들기 화면의 작성 중 요청은 localStorage에 둔다.

## 버전 계보와 보관 용량

- 보관함은 현재 보이는 카드만 다중 선택한다. 필터 밖으로 숨겨진 에셋은 선택에서 뺀다. 컬렉션 변경과
  태그 추가·제거·교체는 각 에셋 API를 순서대로 호출하고, 실패한 항목만 선택을 유지한다. 태그 추가·제거는
  서버의 작업 잠금 안에서 최신 목록을 읽어 적용한다. 작업 휴지통은 에셋이 속한 작업 id를 중복 제거한 뒤,
  같은 작업의 미선택 에셋·원본까지 옮긴다는 확인 창을 거쳐 기존 main 검증 통로를 쓴다.
- 예상 시간은 이미지·설명→3D·이미지→3D·재구성의 같은 모델/출력 조건에서 완료된 기록 3개 이상을 쓴다.
  실패·재시도·로그에 다운로드가 확인되는 기록은 제외하고 단계 시간 합계의 중앙값에서 처리 경과를 뺀다.
  대기 작업에는 앞선 작업의 남은 시간도 더한다. 기록 부족·중지·예상 초과 작업이 앞에 있으면 완료 예상은
  `null`이다. 주제별 난이도·다른 앱의 GPU 사용은 예측하지 못하므로 UI에는 분 단위의 대략적인 값만 표시한다.

- 버전의 부모는 `meta.source`(편집·재구성·복구·이미지→3D)이고, 없으면 같은 작업의 `meta.conceptAsset`(설명→3D)이다.
  `lineage`는 현재 에셋에서 뿌리까지 올라간 뒤 뿌리의 모든 자손을 만든 순서로 펼친다. 부모 작업이 지워졌으면
  `missingParent`로 표시한다.
- 작업 폴더의 파일 분류: 에셋 `file`·메타의 기타 경로 → `results`, `sourceStateFile`·`editBaseFile`·`editStamp*`·
  `mesh/input.png`·`mesh/source.json`·`mesh/audit/decoded.npz` → `bases`(다시 만들기에 필요), `rawFile`·`optimizedFile` →
  `copies`, `preview`·`inspection*` → `previews`, `job.json`·`job.log`·`*.stats.json`·`*.bake.json`·요청/통계 JSON →
  `records`, `*/surface/*`·`*/lod-work/*`·`*/scratch/*`·`*/audit/*`(나머지) → `intermediate`, 그 밖은 `other`.
  휴지통으로 보낼 수 있는 것은 `intermediate`와 작업 폴더 전체뿐이다. 다른 작업이 `meta.source`로 가리키는 작업은
  `usedBy`에 나타나고, 폴더 전체를 보내면 그 버전의 계보에서 부모가 빠진다.

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
