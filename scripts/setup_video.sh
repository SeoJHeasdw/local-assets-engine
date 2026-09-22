#!/bin/zsh
# 영상 모델(Wan2.2 TI2V-5B) 실행 환경을 engines/video/.venv에 재현 가능하게 만든다.
# 여러 번 실행해도 된다. 가중치(약 34GB)는 받지 않는다. docs/SETUP.md의 명령으로 따로 받는다.
#
# 엔진과 같은 Python 3.13·torch 2.14를 쓴다. 성능은 MPS 커널이 정하고 인터프리터 버전과
# 무관하며, 같은 버전이면 uv 캐시의 torch를 그대로 재사용한다. 환경을 엔진과 나누는 이유는
# diffusers가 요구하는 transformers·huggingface-hub 버전이 mflux의 고정 범위와 따로 움직이기 때문이다.
set -euo pipefail

ROOT="${0:A:h:h}"
ENV="$ROOT/engines/video/.venv"
PYTHON="$ENV/bin/python"

mkdir -p "$ROOT/engines/video"
if [[ ! -x "$PYTHON" ]]; then
  uv venv --python 3.13 "$ENV"
fi

VIRTUAL_ENV="$ENV" uv pip install \
  torch==2.14.0 \
  diffusers==0.40.0 \
  transformers==5.17.0 \
  accelerate==1.15.0 \
  ftfy==6.3.1 \
  sentencepiece==0.2.2 \
  imageio-ffmpeg==0.6.0

"$PYTHON" - <<'PY'
import importlib.metadata as metadata
import importlib.util

for module, package in (("torch", "torch"), ("diffusers", "diffusers"), ("transformers", "transformers"),
                        ("accelerate", "accelerate"), ("ftfy", "ftfy"), ("imageio_ffmpeg", "imageio-ffmpeg")):
    found = importlib.util.find_spec(module)
    print("✓" if found else "–", module, metadata.version(package) if found else "")
import torch
import imageio_ffmpeg

print("MPS", torch.backends.mps.is_available())
print("ffmpeg", imageio_ffmpeg.get_ffmpeg_version())
PY

print "다음: 가중치가 없으면 docs/SETUP.md의 영상 모델 내려받기 명령을 실행하고 npm run doctor로 확인합니다."
