# 직접 해야 하는 준비

에이전트가 대신할 수 없는 로그인·승인·설치와, 모델 선택 참고 자료를
모았다. 단계마다 끝나면 `npm run doctor`로 확인한다. 기준일은 2026-09-22이다.

## 지금 상태

| 항목 | 상태 |
| --- | --- |
| 엔진 Python 환경(bpy 5.2.2, mflux 0.19.1, BiRefNet 의존성), gltfpack, Electron | 설치됨 |
| TRELLIS.2 Mac 엔진 `engines/trellis-mac` | 설치됨. Metal 텍스처 가속은 빠짐 (Xcode 없음) |
| FLUX.2 klein 4B, Z-Image Turbo, TRELLIS.2, BiRefNet 가중치 | 내려받기 완료 (2026-09-16) |
| CPU 표면·PBR 도구(scikit-image 0.26.0, point-cloud-utils 0.34.0, xatlas) | 설치됨 (2026-09-17). `scripts/setup_trellis.sh`에 포함 |
| Hugging Face 로그인, DINOv3 승인 | 완료 (2026-09-16). `npm run doctor`에서 3D 생성 ✓ |

## 0. 가중치 내려받기가 끊길 때

2026-09-16 밤에는 회선이 불안정해 큰 파일이 여러 번 끊겼다(DNS 실패, 연결 초기화,
xet 클라이언트 오류). 받다 만 파일은 이어받을 수 있으니 같은 명령을 다시 실행한다.

```bash
# xet 대신 일반 HTTP로 받고, 끊기면 다시 시도한다
for i in $(seq 1 40); do HF_HUB_DISABLE_XET=1 hf download microsoft/TRELLIS.2-4B && break; sleep 15; done
```

`--include`는 값을 하나만 받는다. 여러 개를 붙이면 파일 이름으로 해석돼 아무것도
받지 않고 성공으로 끝나므로, 특정 파일만 받을 때는 파일 경로를 그대로 적는다.

```bash
hf download Tongyi-MAI/Z-Image-Turbo transformer/diffusion_pytorch_model-00001-of-00003.safetensors
```

`npm run doctor`의 가중치 항목은 `blobs/*.incomplete`가 남아 있으면 미완료로 본다.

## 1. Hugging Face 로그인과 DINOv3 승인 — 완료 (2026-09-16)

로그인과 Meta의 DINOv3 접근 승인이 끝났다. TRELLIS.2가 입력 이미지를 읽는 데 쓰는
모델이라 이것이 3D 생성의 전제였다.

상업 이용·지역·재배포/표기 조건은 2026-09-22 원문으로 확인해 [DECISIONS](DECISIONS.md)에 기록했다.
모델 접근 승인 이외에 이번 기능을 위해 새로 로그인하거나 설치할 것은 없다.

`briaai/RMBG-2.0` 접근 요청은 하지 않는다. 비상업 라이선스라서, 엔진이 TRELLIS의
배경 제거 모델을 BiRefNet(MIT)으로 바꿔 불러오도록 해 두었다.

## 2. Xcode와 Metal 텍스처 가속 (보류, 선택)

**회사 기기라 설치를 보류했다.** 관리 정책을 우회하지 않는다. 필요해지면 IT에 정식으로 요청한다.

현재 품질 경로는 CPU 표면 재구성과 원본 PBR 굽기를 사용하므로 **Xcode가 필요하지 않다**.
아래는 이전 Mac 포트의 Metal 경로를 별도로 비교하려는 경우에만 해당한다. 설치만으로 현재
생성 경로가 바뀌지는 않으며, 도입하려면 같은 입력·시드의 품질·메모리를 다시 검증한다.

1. App Store에서 Xcode를 설치한다. 설치 용량이 10GB 이상이다.
2. 터미널에서 다음을 차례로 실행한다.
   ```bash
   sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
   sudo xcodebuild -license accept
   xcodebuild -downloadComponent MetalToolchain
   xcrun -f metal   # 경로가 나오면 성공
   ```
3. `scripts/setup_trellis.sh`를 다시 실행한다. 빠졌던 Metal 패키지만 새로 빌드한다.
4. `npm run doctor`에서 "TRELLIS Metal 텍스처 가속"이 ✓인지 본다.

## 3. 첫 3D 확인

1. `./app.sh`로 앱을 연다. 3D 소품 → 설명으로 시작 → 한 번에 3D까지 → "3D 만들기".
   터미널로는 다음과 같다.
   ```bash
   .venv/bin/python -m local_assets_engine run text-to-3d --params '{"subject": "wooden treasure chest"}'
   ```
2. 첫 실행은 DINOv3 가중치 내려받기가 포함돼 오래 걸린다.
   기본 결과는 `output/jobs/<작업 ID>/mesh/asset.glb`(품질본) 하나다.
   각각의 `.opt.glb`는 전송용 사본이다. 카드에는 실제 GLB 렌더가 보이고 여섯 방향 검수도 제공한다.
3. 실패하면 앱 작업 카드의 오류와 `output/jobs/<작업 ID>/job.log`를 본다.
   외부 모니터를 여러 대 쓰는 중에 "GPU 감시" 오류가 나면 모니터를 줄이고 다시 시도한다.
4. 앱의 환경 화면 → 측정 기록에 단계별 시간과 최대 메모리가 쌓인다.
   `npm run bench`로도 볼 수 있다. 장비 교체 판단의 근거로 쓴다.

   결과 메시는 앱의 3D 미리보기로 보거나, 같은 각도로 렌더해 비교할 수 있다.
   텍스처 품질처럼 통계로 드러나지 않는 문제는 이렇게 봐야 보인다.

   ```bash
   .venv/bin/python scripts/preview_glb.py output/jobs/<작업 ID>/mesh/asset.glb preview.png
   ```

## 4. 2D 모델 정하기

현재 기본값은 비교 후 채택한 **FLUX.2 klein 4B**다. 아래 표는 2026-09-16 후보 조사 기록이다.
실사·애니도 같은 가중치로 만들 수 있다. 앱의 생성 설정에서 이미 설치된 Z-Image Turbo를 선택해 비교할 수 있다.
새 모델 설치는 필요 없다. 기본 모델을 바꾸려면 같은 프롬프트·시드로 비교하고 승인 후
`config/presets.json`의 `imageModel`을 바꾼다. 선택지는 `imageModels`, 종류별 기본은 프리셋의 `imageModel` id다.

| 후보 | 라이선스 | 게이트 | 저장소 크기 | 최종 수정 | mflux 명령 | 메모 |
| --- | --- | --- | --- | --- | --- | --- |
| Z-Image Turbo | Apache-2.0 | 없음 | 32.9GB | 2026-01-30 | `mflux-generate-z-image-turbo` | 예전 기본값. 가중치를 받아 두었으므로 되돌리기 쉽다 |
| Z-Image (비-Turbo) | Apache-2.0 | 없음 | 20.5GB | 2026-01-28 | `mflux-generate-z-image` | 스텝이 많아 느린 대신 품질 여지가 있다 |
| FLUX.2 klein 4B | Apache-2.0 | 없음 | 23.7GB | 2026-02-24 | `mflux-generate-flux2` | **현재 기본값**(2026-09-16 채택). 이 명령의 기본 모델이라 인자가 필요 없다. 9B판은 라이선스가 달라 확인 전 사용 금지 |
| Qwen-Image-2512 | Apache-2.0 | 없음 | 57.7GB | 2025-12-31 | `mflux-generate-qwen` | 이미지 안 글자에 강하다고 알려짐. 가장 무거워 이 기기에는 부담 |
| FLUX.1 schnell | Apache-2.0 | 자동 승인 | 57.8GB | 2024-08-16 | `mflux-generate` + `--model schnell` | 오래된 세대 |

Qwen3-Image는 오픈웨이트가 없다(2026-09-16 조회에서 저장소 없음).

크기와 라이선스는 2026-09-16에 Hugging Face API의 표기를 읽은 값이다. 채택 전에 확인한다.

- 라이선스 원문과 이용 지역 제한. Hunyuan 계열처럼 한국을 제외하는 경우가 있다.
- 같은 프롬프트·시드로 만든 결과의 스타일 일관성
- 환경 화면 측정 기록의 생성 시간과 최대 메모리

바꾸는 예시는 다음과 같다. `command`는 엔진 환경의 `.venv/bin/`에 있는 mflux 명령이다.
명령마다 모델 선택 옵션이 다르면 `recipes/image.py`의 인자 조립도 함께 고친다.

```json
"imageModel": {
  "id": "qwen-image",
  "label": "Qwen-Image",
  "command": "mflux-generate-qwen",
  "repo": "Qwen/Qwen-Image",
  "license": "Apache-2.0",
  "steps": null,
  "quantize": 8
}
```

채택하면 [DECISIONS](DECISIONS.md)에 날짜·출처·비교 결과를 남긴다.
