"""One step of the contract self-test: wait, report progress, optionally fail.

No model, no GPU. It exists so the job lane (queue, measurement, cancellation,
failure records) can be checked in seconds.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import progress_line

TICK_S = 0.1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=0.5)
    parser.add_argument("--fail", action="store_true")
    args = parser.parse_args(argv)
    if args.fail:
        print("selftest: 요청한 단계에서 실패합니다.", file=sys.stderr, flush=True)
        return 1
    ticks = max(1, round(args.seconds / TICK_S))
    for tick in range(1, ticks + 1):
        time.sleep(args.seconds / ticks)
        print(progress_line(tick, ticks), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
