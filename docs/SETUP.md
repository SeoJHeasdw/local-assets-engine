# 직접 해야 하는 준비

에이전트가 대신할 수 없는 로그인·승인·설치와, 아직 정하지 않은 2D 모델 선택을
모았다. 단계마다 끝나면 `npm run doctor`로 확인한다. 기준일은 2026-09-16이다.

## 지금 상태

| 항목 | 상태 |
| --- | --- |
| 엔진 Python 환경(bpy 5.2.2, mflux 0.19.1, BiRefNet 의존성), gltfpack, Electron | 설치됨 |
| TRELLIS.2 Mac 엔진 `engines/trellis-mac` | 설치됨. Metal 텍스처 가속은 빠짐 (Xcode 없음) |
| Z-Image Turbo, TRELLIS.2, BiRefNet 가중치 | 내려받기 완료 (2026-09-16) |
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

남은 일이 하나 있다. **판매 전에** DINOv3 License 원문에서 상업 이용 조건과 표기 의무를
확인하고 [DECISIONS](DECISIONS.md)에 날짜와 함께 적는다. 조사 단계에서 "Built with
DINOv3" 표기가 필요하다는 보고가 있었지만 원문으로 확인하지 않았다.

`briaai/RMBG-2.0` 접근 요청은 하지 않는다. 비상업 라이선스라서, 엔진이 TRELLIS의
배경 제거 모델을 BiRefNet(MIT)으로 바꿔 불러오도록 해 두었다.

## 2. Xcode와 Metal 텍스처 가속 (3D 텍스처 품질에 필수)

없어도 3D 형태는 제대로 나오지만 **텍스처를 쓸 수 없는 수준으로 굽는다.** 2026-09-16
첫 3D 결과를 렌더해 보니 표면 전체에 잡티가 깔렸고, 감축 전 원본도 똑같았다. 즉 원인은
면 줄이기가 아니라 굽기다. Metal 래스터라이저가 없으면 512³ 복셀에서 KDTree로 색을
퍼뜨려 1024² 텍스처를 채우므로 구멍이 남는다. 게임에 쓸 텍스처를 얻으려면 이 단계가 필요하다.

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

1. `./app.sh`로 앱을 연다. 3D 에셋 → 설명으로 → 후보 수 1 → "3D 바로 만들기".
   터미널로는 다음과 같다.
   ```bash
   .venv/bin/python -m local_assets_engine run text-to-3d --params '{"subject": "wooden treasure chest"}'
   ```
2. 첫 실행은 DINOv3 가중치 내려받기가 포함돼 오래 걸린다.
   결과는 `output/jobs/<작업 ID>/mesh/asset.glb`(정리본)와 `asset.opt.glb`(게임용)이다.
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

현재 기본값은 임시로 **Z-Image Turbo**다. 2D 조사는 중간에 멈췄으므로 아래 후보를
직접 비교해 정한다. `config/presets.json`의 `imageModel`만 바꾸면 앱·CLI·javis가 함께 바뀐다.

| 후보 | HF 라이선스 표기 | 게이트 | 저장소 크기 | mflux 명령 | 메모 |
| --- | --- | --- | --- | --- | --- |
| Z-Image Turbo | Apache-2.0 | 없음 | 32.9GB | `mflux-generate-z-image-turbo` | 현재 기본값. 36GB에서는 8비트 양자화로 쓴다(양자화 없이 1024px 생성 시 최대 37.9GB로 스왑) |
| Qwen-Image | Apache-2.0 | 없음 | 57.7GB | `mflux-generate-qwen` | 이미지 안 글자에 강하다고 알려짐. 36GB에서는 양자화(`quantize`)가 필요할 것으로 추정 |
| FLUX.1 schnell | Apache-2.0 | 자동 승인 | 57.8GB | `mflux-generate` + `--model schnell` | HF 로그인 후 약관 동의 필요 |
| FLUX.2 klein 4B·9B | 확인 필요 | 확인 필요 | 확인 필요 | `mflux-generate-flux2` | 크기별 라이선스가 다르다는 보고가 있음. 원문 확인 전 사용 금지 |

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
