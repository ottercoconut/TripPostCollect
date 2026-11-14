"""#194 有意偏离：知乎修复不再从旧行带回作者字段。

固化文件不改。T10 比较 ``load_post_repair_fallbacks`` 前，把冻结实现改成
只为知乎回落关键词和发帖时间。
"""

from __future__ import annotations


_FALLBACK_BEFORE = """        except (TypeError, json.JSONDecodeError):
            pass
        fallback = {"""

_FALLBACK_AFTER = """        except (TypeError, json.JSONDecodeError):
            pass
        if platform_key == "zhihu":
            fallback = {
                key: value
                for key, value in {
                    "keyword": row_value("keyword"),
                    "published_at": row_value("published_at"),
                    "created_time": raw.get("created_time"),
                }.items()
                if value not in (None, "")
            }
            fallbacks[str(row["platform_post_id"])] = fallback
            continue
        fallback = {"""


def t10_load_post_repair_fallbacks(source: str) -> str:
    if source.count(_FALLBACK_BEFORE) != 1:
        raise AssertionError("冻结 load_post_repair_fallbacks 片段不唯一")
    return source.replace(_FALLBACK_BEFORE, _FALLBACK_AFTER, 1)
