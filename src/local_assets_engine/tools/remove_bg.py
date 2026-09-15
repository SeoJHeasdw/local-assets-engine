"""Remove image backgrounds with BiRefNet (MIT) on MPS.

Usage: python -m local_assets_engine.tools.remove_bg --manifest items.json
where items.json is a list of {"input": path, "output": path}.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import progress_line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--repo", default="ZhengPeng7/BiRefNet")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--resolution", type=int, default=1024)
    args = parser.parse_args(argv)
    items = json.loads(args.manifest.read_text("utf-8"))
    total = len(items)

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import torch
    from PIL import Image
    from torchvision import transforms
    from transformers import AutoModelForImageSegmentation

    print(progress_line(0, total, "배경 제거 모델 불러오는 중"), flush=True)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = AutoModelForImageSegmentation.from_pretrained(
        args.repo, revision=args.revision, trust_remote_code=True,
    )
    model.to(device).eval()
    transform = transforms.Compose([
        transforms.Resize((args.resolution, args.resolution)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    for index, item in enumerate(items, start=1):
        image = Image.open(item["input"]).convert("RGB")
        batch = transform(image).unsqueeze(0).to(device)
        with torch.no_grad():
            prediction = model(batch)[-1].sigmoid().float().cpu()[0].squeeze()
        mask = transforms.ToPILImage()(prediction).resize(image.size, Image.Resampling.BILINEAR)
        cutout = image.copy()
        cutout.putalpha(mask)
        output = Path(item["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        cutout.save(output)
        print(progress_line(index, total, Path(item["input"]).name), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
