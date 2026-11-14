"""#187 有意偏离：知乎正文图同一 v2 哈希只留原图。

固化文件不改。比较前对冻结解析源码做单向替换：

1. ``extract_image_urls_from_html`` 在返回前按 ``v2-<32位十六进制>`` 合并。
2. ``zhihu_content_image_assets`` 在资产键去重前做同一次合并。
"""

from __future__ import annotations


PARSER_REPLACEMENTS = (
    (
        "    return image_urls\n",
        "    return collapse_zhihu_v2_image_urls(image_urls)\n",
    ),
    (
        "    assets: List[dict] = []\n"
        "    seen = set()\n"
        "    for value in content_item.image_list:\n"
        "        try:\n"
        "            url = normalize_image_url(value)\n"
        "        except ImageStagingError:\n"
        "            continue\n"
        "        path = urlsplit(url).path.lower().rstrip(\"/\")\n"
        "        if url in excluded_urls or path.endswith(\"/equation\") or \"/equation/\" in f\"{path}/\":\n"
        "            continue\n"
        "        asset_key = zhihu_source_asset_key(url)\n",
        "    normalized_urls: List[str] = []\n"
        "    for value in content_item.image_list:\n"
        "        try:\n"
        "            url = normalize_image_url(value)\n"
        "        except ImageStagingError:\n"
        "            continue\n"
        "        path = urlsplit(url).path.lower().rstrip(\"/\")\n"
        "        if url in excluded_urls or path.endswith(\"/equation\") or \"/equation/\" in f\"{path}/\":\n"
        "            continue\n"
        "        normalized_urls.append(url)\n"
        "    assets: List[dict] = []\n"
        "    seen = set()\n"
        "    for url in collapse_zhihu_v2_image_urls(normalized_urls):\n"
        "        asset_key = zhihu_source_asset_key(url)\n",
    ),
)


def parser_source(text: str) -> str:
    for old, new in PARSER_REPLACEMENTS:
        if old not in text:
            continue
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text
