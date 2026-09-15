"""Process entry points for stages. Each runs in its own measured process."""

import json


def progress_line(done: int, total: int, detail: str | None = None) -> str:
    return "@@progress " + json.dumps({"done": done, "total": total, "detail": detail}, ensure_ascii=False)
