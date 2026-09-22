# Local Assets Engine

javis의 생산물 모듈이다. Apple Silicon Mac에서 유료 API나 구독 없이 게임용 2D 에셋,
실사·애니 이미지와 3D 메시를 만든다. 현재 환경은 M4 Max 36GB다. 로그인·승인처럼 사용자가 직접 할 준비와
2D 모델 선택은 [SETUP](docs/SETUP.md)에 있다.

다음 개선 후보와 남은 검증 항목은 [IMPROVEMENTS](docs/IMPROVEMENTS.md)에 정리했다.

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
| `image` | 설명 → 이미지 후보 → 선택한 종류에 따라 배경 제거·캔버스 맞춤·픽셀화 | 배경 포함 또는 투명 PNG 후보 |
| `text-to-3d` | 설명 → 컨셉 이미지 → TRELLIS.2 원본 → 표면 재구성 → PBR | 품질본 GLB, 여섯 방향 검수 |
| `image-to-3d` | 고른 이미지 → TRELLIS.2 원본 → 표면 재구성 → PBR | 품질본 GLB, 여섯 방향 검수 |
| `refine-mesh` | 저장된 생성 원본 → 출력 설정을 바꿔 표면·PBR 재구성 | 모델 추론 없이 새 품질본 |
| `edit-asset` | 색상·재질·로고·문구·2D 구성 편집 → 새 버전 저장 | 원본을 보존한 PNG·GLB |
| `import-image` | 로컬 이미지 가져오기 | 편집하거나 3D로 만들 이미지 |
| `repair-mesh` | 이전 KDTree 원본의 UV·위쪽 축·양면 표시 보정 → Blender 정리 → gltfpack | 새 작업의 복구 GLB |
| `previz` | 완성된 메시·회색 대역 배치 → 샷 프리셋을 카메라 값으로 풀기 → 스케치 렌더 | 샷별 애니매틱·깊이·윤곽과 샷 값 |

이미지 기본 모델은 FLUX.2 klein 4B(선택: Z-Image Turbo), 배경 제거는 BiRefNet, 3D는 TRELLIS.2 Mac 포트, 프리비즈
스케치는 Blender EEVEE다. 종류 프리셋(아이템 아이콘, 캐릭터 스프라이트, 픽셀 아트, UI 요소,
배경, 3D 소품)과 샷 프리셋(게임 트레일러), 프리비즈 대역(사람, 승용차, 벽, 건물), 모델·기본값은
`config/presets.json`에서 고친다.
자동 검사(물체 유무, 면적, 가장자리 닿음)는 참고 신호다. 고르는 것은 사람이 한다(후보 비교·즐겨찾기).

프리비즈는 연출을 먼저 확정하는 단계다. 산출물은 그림이 아니라 카메라 위치·목표·렌즈·
움직임·길이 같은 **샷 값**이고, 그 값이 나중에 만들 영상 생성의 입력이 된다.
자세한 방향은 [PREVIZ](docs/PREVIZ.md)에 있다.

```bash
# 완성된 3D 에셋 하나로 게임 트레일러 6컷 프리비즈 만들기 (약 20초)
.venv/bin/python -m local_assets_engine run previz \
  --params '{"assets": [{"source": {"jobId": "<3D 작업 ID>", "assetId": "a02"}}]}'

# 3D 에셋 없이 회색 대역만으로: 30도 돌아선 사람과 뒤쪽의 8m 벽
.venv/bin/python -m local_assets_engine run previz \
  --params '{"assets": [{"standin": "person", "yaw": 30}, {"standin": "wall", "size": [8, 0.2, 3], "position": [0, 3, 0]}]}'
```

## 앱에서 하는 일

- **만들기**: 3D 소품·2D 이미지를 선택하고 설명을 적는다. 3D는 컨셉을 먼저 골라서 만들거나,
  한 번에 완성하는 방식을 선택한다. 후보 수·스타일·시드는 생성 설정 팝업에 있다. 이미지 파일이나
  보관함의 후보에서 시작할 수도 있다. 기본 생성은 고품질 원본 하나다. 작성 중인 요청은 앱을 다시 열어도 남는다.
  2D는 **게임 에셋 / 실사 / 애니·일러스트** 카테고리를 고른다. 실사는 인물·제품·풍경, 애니는 캐릭터·일러스트·배경을
  제공하며 배경을 기본으로 유지한다. 생성 설정의 모델 선택은 카테고리별로 기억한다.
- **최근 작업**: 결과 이미지와 간결한 진행 상태, 대기 순서와 대기·처리 시간을 본다. 후보가 여럿이면
  "나란히 비교"로 크게 보고 골라 3D로 만든다. 창을 보고 있지 않을 때 끝나면 macOS 알림이 온다.
  같은 조건의 성공 기록 3개 이상이면 대략적인 완료 예상도 표시한다. 단계별 시간·메모리·로그는 작업 기록을 연다.
- **편집**: 에셋을 열면 레이어로 편집한다. "색 바꾸기"는 비슷한 색만 바꾸거나 영역 전체를 칠하고, "칠한 곳만"을
  고르면 브러시로 부위를 정한다(상자 뚜껑만, 자물쇠만). "로고"·"문구"는 여러 개를 쌓아 보이기·잠금·순서·삭제한다.
  2D는 로고를 끌어 옮기고 모서리·손잡이로 크기·회전하며, 구성 탭에서 자유 자르기·여백·배경색·출력 크기를 정한다.
  3D는 표면을 찍어 붙이고 끌어서 옮기며(Alt+휠 크기, Shift+휠 회전), 기본으로 찍은 부위와 이어진 표면에만 붙는다.
  되돌리기(⌘Z)·다시 실행(⇧⌘Z)·작업 내역·원본 비교가 있고 새 버전으로 저장한다. 다른 작업이 돌고 있으면 대기
  상태를 보여 주고 취소할 수 있다. 미저장 편집은 자동으로 임시 보관된다. "버전 이력"에서 원본부터 이어진 버전을
  보고 이전 버전에서 새로 갈라 편집한다. 실제 형상을 깎는 조각 도구는 아니다.
- **프리비즈**: 기본은 **설명으로 시작**이다. 장면을 적거나 `두 사람` 같은 예시를 눌러 설명을 채우고
  내용을 덧붙인다. 예: “두 사람이 마주 보고 서 있다. 뒤에는 벽이 있다. 카메라가 천천히 다가간다. 전체 8초.”
  `설명으로 장면 준비` → **반영한 내용 확인** → `영상 만들기` 순서다. 현재는 정해진 한국어 표현을 읽어
  사람·자동차·벽·건물의 수량·상대 위치, 마주 보기, 카메라 다가가기·멀어지기·돌기·고정과 전체 길이를 적용한다.
  외모·의상·장소·조명은 메모로 남고, 인물·차량 자체의 움직임은 지원하지 않는다. 이해한 내용만 화면에 표시한다.
  장면 준비 후 주인공을 보관함의 3D 에셋으로 바꿀 수 있다. 설명과 초안은 다시 열어도 남는다.
  **상세 편집**에서는 기존 지도 배치, 컷 순서·길이·렌즈·카메라 손잡이, 렌더 설정을 쓴다. 두 화면은 같은
  초안을 공유하며 전환만으로 초기화하지 않는다. 설명을 다시 적용하면 배치·컷을 다시 만들고 바로 되돌릴 수 있다.
  저장된 영상은 상단 기록에서 열며 `편집으로 불러오기`로 배치·컷·설명을 되살린다. 타임라인에서 컷을 보고
  승인·거절하며, `Blender에서 열기`로 컷별 카메라가 묶인 장면을 연다.
- **보관함**: 즐겨찾기·종류·컬렉션·태그로 거르고 이름·태그·메모로 찾는다. 컬렉션·태그·메모는 편집 화면의
  "정보·정리"에서 붙이고, 편집본은 원본의 컬렉션·태그를 이어받는다. 3D 에셋을 열면 "프리비즈에 넣기"로 그 에셋을
  프리비즈 장면에 넣는다. 승인·거절은 프리비즈 컷 검토에만 쓴다.
  카드 체크박스로 여러 개를 선택하면 컬렉션·태그를 한 번에 추가·제거·교체할 수 있다. 앱의 "선택 에셋의 작업 정리"는
  같은 작업의 미선택 에셋까지 포함한 작업 폴더 전체를 휴지통으로 보낸다. 표시된 목록을 확인한 뒤 실행한다.
- **환경**: 준비 상태 진단, 보관 용량(분류별 크기, 중간 파일·작업을 휴지통으로 보내기), 측정 기록을 본다.
- 창은 맨 위 띠나 각 화면 머리글을 잡아 옮긴다.

## javis와 스크립트에서 쓰기

엔진은 `127.0.0.1:47831`에서만 요청을 받는다. 앱 없이 `npm run engine`으로 띄울 수 있고,
앱은 이미 떠 있는 엔진을 그대로 쓴다. 작업은 앱·CLI·javis 요청을 가리지 않고 한 줄로 실행한다.

```bash
curl -s localhost:47831/api/jobs -H 'Content-Type: application/json' \
  -d '{"recipe": "image", "params": {"preset": "item-icon", "subject": "red health potion", "count": 4}}'
curl -s localhost:47831/api/jobs/<작업 ID>

# 애니 캐릭터 (기본 모델)
.venv/bin/python -m local_assets_engine run image \
  --params '{"preset":"anime-character","subject":"an adult traveler in a blue cloak","count":1,"seed":42}'

# 실사 제품 사진, Z-Image Turbo로 비교
.venv/bin/python -m local_assets_engine run image \
  --params '{"preset":"photo-product","imageModel":"z-image-turbo","subject":"a green ceramic teapot","count":1,"seed":42}'
```

`.venv/bin/python -m local_assets_engine run <레시피> --params '<JSON>'`은 엔진이 떠 있으면
엔진에 작업을 넣고, 없으면 그 자리에서 실행한다. API 목록은 [ARCHITECTURE](docs/ARCHITECTURE.md)에 있다.

3D 기본 출력은 **100만 면·4K 품질본 하나**다. 게임용 파일은 기본 생성에 포함하지 않는다. 원본은 따로 저장되며 텍스처를 구운 뒤 다시 감축하지 않는다.
보관함에서 에셋을 열어 회전하고, 정보 → "여섯 방향 보기"로 뒤와 바닥까지 확인한다.
다른 면 수·텍스처가 필요하면 정보 → "고품질 원본 다시 구성" 또는 다음 명령을 쓴다.

```bash
.venv/bin/python -m local_assets_engine run refine-mesh \
  --params '{"source": {"jobId": "<3D 작업 ID>", "assetId": "a01"}, "textureSize": 4096, "targetFaces": 1000000}'
```

예전 에셋 중 원본 NPZ가 없는 것은 입력 이미지로 새로 생성해야 전체 개선이 적용된다.
아래 `repair-mesh`는 예전 표시·UV 오류만 복구하며 손실된 형태를 복원하지 못한다.
원본과 즐겨찾기 등 정리 상태는 보존하고, 보관함에 새 에셋을 만든다. 이미 보정된 결과나
Metal 경로의 결과에는 적용하지 않는다. 서버가 이전 코드를 쓰고 있다면 진행 작업을 마친 뒤
앱과 엔진을 다시 열어 새 레시피를 불러온다.

```bash
.venv/bin/python -m local_assets_engine run repair-mesh \
  --params '{"source": {"jobId": "<기존 3D 작업 ID>", "assetId": "a02"}}'
```

## 폴더 안내

| 폴더 | 책임 |
| --- | --- |
| `src/local_assets_engine/` | 엔진: HTTP 서버, 작업 기록, 러너, 측정, 진단, CLI |
| `src/local_assets_engine/recipes/` | 2D·3D·프리비즈 레시피와 입력 검증 |
| `src/local_assets_engine/tools/` | 단계 프로세스: BiRefNet 배경 제거, Blender 메시 정리·프리비즈 렌더 |
| `src/local_assets_engine/workers/` | TRELLIS 환경(Python 3.11)에서 도는 실행기 |
| `electron-app/` | 앱. `main`은 엔진 수명·창·IPC, `renderer`는 화면, `shared`는 공통 계산 |
| `config/presets.json` | 모델, 종류 프리셋, 3D 기본값, 프리비즈 샷 프리셋·대역 |
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
