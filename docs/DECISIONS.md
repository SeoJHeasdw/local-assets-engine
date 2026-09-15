# 채택·기각 근거와 라이선스

현재 동작은 [ARCHITECTURE](ARCHITECTURE.md)를 따른다. 여기에는 채택·기각을 유지해야 할
이유와 당시 확인 출처만 남긴다. 아래 날짜의 확인 기록은 최신 재검증을 뜻하지 않는다.

## 구조

| 날짜 | 결정과 이유 |
| --- | --- |
| 2026-09-16 | 유료 API·구독(Higgsfield 등) 대신 보유한 M4 Max 36GB에서 로컬 생성. 좋은 결과가 쌓이면 M5 Ultra 512GB급 교체를 검토하므로 모든 단계의 시간과 최대 메모리를 기록 |
| 2026-09-16 | 노드 그래프 편집기(ComfyUI 방식) 대신 레시피·프리셋 단위. 양산형 에셋은 같은 흐름을 반복해 쓰기 때문 |
| 2026-09-16 | 엔진을 127.0.0.1 HTTP 서버로 분리하고 Electron은 화면과 엔진 수명만 담당. javis가 같은 엔진을 호출하고, 장비를 바꾸면 생성 서버만 옮기기 위해 |
| 2026-09-16 | 모델을 서버에 상주시키지 않고 단계마다 프로세스로 실행. 36GB에서 이미지 모델과 TRELLIS가 함께 올라가지 않게 하고 단계별 측정을 얻기 위해. TRELLIS 적재 시간(M4 Pro 약 100초 보고)은 감수. 상주 캐시는 써 본 뒤 결정 |
| 2026-09-16 | 도시 규모 장면(예: 강남역 사거리)은 예시였을 뿐 목표가 아님을 사용자가 확인. GIS 의존성 제거 |
| 2026-09-16 | Blender 썸네일 렌더 대신 컨셉 이미지와 앱 3D 뷰어로 미리보기. 헤드리스 Cycles Metal 첫 렌더가 256px에 81.3초(커널 컴파일 포함) |
| 2026-09-16 | 엔진 재시작 시 대기 작업을 다시 돌리지 않음. 사용자가 모르는 사이 GPU를 오래 점유하지 않기 위해 |
| 2026-09-16 | OCR은 이 저장소가 아니라 javis 공용 인식 모듈에 둔다. 결과물 검수용 VLM은 그 모듈과 공유할 수 있음 |

## 3D 생성 모델

2026-09-16 조사 기준: Mac(MPS) 실행 근거, 상업 이용, 이용 지역(한국).

| 모델 | 판정 | 근거 |
| --- | --- | --- |
| TRELLIS.2-4B + trellis-mac 포트 | **채택** | 모델 MIT. 포트가 CUDA 전용 의존성을 Metal·순수 PyTorch로 바꿔 PBR GLB까지 생성(M4 Pro 약 5분 13초 보고) |
| Hunyuan3D 2·2.1과 파생(Mini, Turbo, Omni, Part, PartCrafter) | **기각** | LICENSE의 적용 지역에서 EU·영국·한국을 제외하고, 지역 밖에서는 결과물 사용도 금지. MLX 포트가 있어도 쓰지 않음 |
| Stable Fast 3D, SPAR3D | 보류 | 공식 MPS 지원, 빠름. Stability Community License의 매출 조건 원문 미확인 |
| Step1X-3D, Direct3D-S2, Pixal3D, UniRig | 보류 | 라이선스는 가능하나 CUDA 전용 커널이라 Mac 실행 근거 없음 |
| Roblox Cube 3D | **기각** | 연구 전용 라이선스 |
| TripoSR, TripoSG | 보류 | MIT. 품질이 낮거나 형상만 생성, MPS 동작 미검증 |

설치는 커밋으로 고정한다: trellis-mac `d58628f4`, trellis2-apple `17347247`, TRELLIS.2
`75fbf018`, mtlbvh `6b2a0f63`, mtldiffrast `c9499ba2`, mtlgemm `566c1337`, mtlmesh
`7de3864f`, utils3d `9a4eb15e`. 모두 저장소 LICENSE가 MIT다(2026-09-16 확인). o-voxel은
PyTorch 2.14가 C++20을 요구해 빌드 플래그만 바꿨다.

출처: [trellis-mac](https://github.com/shivampkumar/trellis-mac),
[TRELLIS.2-4B](https://huggingface.co/microsoft/TRELLIS.2-4B),
[Hunyuan3D-2.1 LICENSE](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE),
[Roblox Cube LICENSE](https://github.com/Roblox/cube/blob/main/LICENSE) (모두 2026-09-16).

## 배경 제거와 이미지 조건 모델

| 모델 | 판정 | 근거 |
| --- | --- | --- |
| BiRefNet `ZhengPeng7/BiRefNet` @ `e2bf8e44` | **채택** | HF 표기 MIT, 게이트 없음. `trust_remote_code` 모델이라 커밋 고정 |
| RMBG-2.0 `briaai/RMBG-2.0` | **기각** | 비상업 라이선스, 게이트. TRELLIS.2 기본 설정이 불러오므로 실행기에서 BiRefNet으로 교체 |
| DINOv3 `facebook/dinov3-vitl16-pretrain-lvd1689m` | 필수, 조건 확인 중 | TRELLIS.2의 이미지 조건 모델이라 바꿀 수 없음. 수동 승인 게이트. 상업 조건과 표기 의무 원문 미확인 ([SETUP](SETUP.md) 1단계) |

## 2D 이미지 모델

| 모델 | 판정 | 근거 |
| --- | --- | --- |
| Z-Image Turbo `Tongyi-MAI/Z-Image-Turbo` | 임시 기본값 | HF 표기 Apache-2.0, 게이트 없음(2026-09-16 API). 로그인 없이 파이프라인을 검증할 수 있어 골랐다. 품질·일관성 비교 전이라 채택 확정 아님 |
| 비교 후보 | 미정 | [SETUP](SETUP.md) 4단계 표. 사용자가 비교한 뒤 결정 |

## 도구

| 도구 | 라이선스와 메모 |
| --- | --- |
| Blender 5.2.2 LTS, bpy 5.2.2 | GPL. 도구로만 쓰며, Blender로 만든 결과물은 GPL 대상이 아니라는 Blender 재단 안내를 따름. 앱에 Blender를 묶어 배포할 때는 재검토 |
| mflux 0.19.1 | MIT (패키지 메타데이터, 2026-09-16) |
| gltfpack 1.2 (meshoptimizer) | MIT |
| @google/model-viewer 4.3.1 | Apache-2.0 (npm 표기) |
| Electron 44.3.0 | MIT |
