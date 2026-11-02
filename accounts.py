from __future__ import annotations

import json
import re
from pathlib import Path

from ocr_backend import normalize
from reader import BASE_DIR


def get_accounts(config: dict) -> list[dict]:
    accounts_config = config.get("accounts", {})
    result: list[dict] = []
    for item in accounts_config.get("items", []):
        if not item.get("enabled", True):
            continue
        matches = item.get("matches") or [item.get("match", ""), item.get("label", "")]
        matches = [str(m).strip() for m in matches if str(m).strip()]
        if not matches:
            continue
        result.append(
            {
                "key": str(item.get("key") or item.get("label") or matches[0]),
                "label": str(item.get("label") or matches[0]),
                "matches": matches,
                "uid": str(item.get("uid") or "").strip(),
                "enabled": True,
            }
        )
    return result


def parse_uid(lines) -> str | None:
    text = "".join(normalize(line.text) for line in lines)
    match = re.search(r"UID[:：]?(\d{10,})", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


class AccountStore:
    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir) if base_dir else BASE_DIR
        self.path = self.base_dir / "accounts_state.json"

    def load_uids(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def save_uids(self, uids: dict[str, str]) -> None:
        try:
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(uids, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self.path)
        except OSError:
            pass
