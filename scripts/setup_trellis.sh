#!/bin/zsh
# TRELLIS.2 Apple Silicon 엔진(trellis-mac)을 engines/ 아래에 재현 가능하게 설치한다.
# 여러 번 실행해도 된다. Xcode를 설치한 뒤 다시 실행하면 빠졌던 Metal 가속만 새로 빌드한다.
set -euo pipefail

ROOT="${0:A:h:h}"
ENGINE="$ROOT/engines/trellis-mac"
APPLE="$ENGINE/deps/trellis2-apple"
TRELLIS_MAC_COMMIT="d58628f4f5b9c3de8274cb110074154f4b31cef2"
TRELLIS2_APPLE_COMMIT="17347247c91c36c8cdc1896234983e878a457bba"

checkout() {
  local url="$1" dir="$2" commit="$3"
  if [[ ! -d "$dir/.git" ]]; then
    git clone "$url" "$dir"
  fi
  if ! git -C "$dir" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git -C "$dir" fetch --depth 1 origin "$commit"
  fi
  git -C "$dir" checkout -q "$commit"
}

mkdir -p "$ROOT/engines" "$ENGINE/deps"
checkout https://github.com/shivampkumar/trellis-mac.git "$ENGINE" "$TRELLIS_MAC_COMMIT"

# o-voxel의 setup.py는 C++17로 고정돼 있지만 PyTorch 2.14 헤더는 C++20을 요구한다.
# setup.sh는 이미 받아 둔 deps/를 건너뛰므로 먼저 받아 고쳐 둔다.
checkout https://github.com/pedronaugusto/trellis2-apple.git "$APPLE" "$TRELLIS2_APPLE_COMMIT"
sed -i '' 's/-std=c++17/-std=c++20/g' "$APPLE/o-voxel/setup.py"

# o-voxel은 Eigen 헤더가 필요하다.
if ! brew list eigen >/dev/null 2>&1; then
  brew install eigen
fi
export CPLUS_INCLUDE_PATH="$(brew --prefix eigen)/include/eigen3${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"

(cd "$ENGINE" && bash setup.sh)

PYTHON="$ENGINE/.venv/bin/python"
reinstall() {
  VIRTUAL_ENV="$ENGINE/.venv" uv pip install --reinstall --no-build-isolation "$1"
}

if xcrun -f metal >/dev/null 2>&1; then
  # 폴더 이름과 설치되는 모듈 이름이 다르다: mtlmesh → cumesh, mtlgemm → flex_gemm
  for pair in mtlbvh:mtlbvh mtldiffrast:mtldiffrast mtlmesh:cumesh mtlgemm:flex_gemm; do
    folder="${pair%%:*}"
    module="${pair##*:}"
    if ! "$PYTHON" -c "import $module" >/dev/null 2>&1; then
      print "Metal 가속 다시 빌드: $folder"
      reinstall "$ENGINE/deps/$folder" || print "  $folder 빌드 실패 — 느린 대체 경로를 씁니다."
    fi
  done
else
  print "! Xcode Metal 컴파일러가 없어 Metal 텍스처 가속을 건너뜁니다. (docs/SETUP.md 2단계)"
fi

if ! "$PYTHON" -c "import o_voxel" >/dev/null 2>&1; then
  print "o_voxel 다시 빌드"
  reinstall "$APPLE/o-voxel"
fi

"$PYTHON" - <<'PY'
import importlib.util

for module in ("torch", "o_voxel", "mtldiffrast", "mtlbvh", "cumesh", "flex_gemm"):
    print("✓" if importlib.util.find_spec(module) else "–", module)
import torch

print("MPS", torch.backends.mps.is_available())
PY
