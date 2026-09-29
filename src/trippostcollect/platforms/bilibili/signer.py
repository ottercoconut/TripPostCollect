"""B站WBI 参数签名。"""

from __future__ import annotations

import time
from hashlib import md5
from typing import Any
from urllib.parse import urlencode


BILIBILI_WBI_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)


def sign_bilibili_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, str]:
    mixin_key = img_key + sub_key
    salt = "".join(mixin_key[index] for index in BILIBILI_WBI_MIXIN_TABLE)[:32]
    signed = {**params, "wts": int(time.time())}
    filtered = {
        key: "".join(character for character in str(value) if character not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urlencode(filtered)
    filtered["w_rid"] = md5((query + salt).encode("utf-8")).hexdigest()
    return filtered

