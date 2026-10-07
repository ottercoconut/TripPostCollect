"""包内资源唯一读取入口；散列固定于迁移基线，路径只在上下文内有效。"""

from contextlib import contextmanager
from hashlib import sha256
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath


RESOURCE_SHA256 = {
    "js/douyin.js": "ff5cb3133e2717523ffb3f96679e3f5d23bd31a999411ea604cff9a51da9e26e",
    "js/zhihu.js": "9753572dc21148975600ca8083e92245e69130cd11414a3e3089013b3aaee3f1",
    "js/stealth.min.js": "02ae012addcdb30b0ed1a512406feb487699b1b217566e87a8086b54dfcc1d4d",
    "licenses/MediaCrawler-LICENSE": "aeff21de8609bec9d6e939bbbba7c2914ae0a6e7c9470ea7945c03f7d17a2a33",
}
# 构建生成的包内资源 → 仓库内唯一编辑真源。构建模块 build_support.py 读取此表复制进 wheel，
# 源码树不保存副本；源码 checkout 中包内没有生成副本时直接读真源。
GENERATED_RESOURCES = {
    "sql/crawl_scheduler.sql": "db/crawl_scheduler.sql",
    "sql/ctf_captures.sql": "db/ctf_captures.sql",
    "sql/source_platforms.sql": "db/source_platforms.sql",
    "sql/web_posts.sql": "db/web_posts.sql",
    "sql/xhs_control.sql": "db/xhs_control.sql",
    "contracts/formal-crawl-contract.md": "docs/formal-crawl-contract.md",
}
_RESOURCE_ROOTS = {"js", "licenses", "sql", "contracts"}
# src 布局下本文件位于 <checkout>/src/trippostcollect/core/resources.py。
_SOURCE_CHECKOUT = Path(__file__).resolve().parents[3]


def _resource(name: str):
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or PureWindowsPath(name).drive
        or "\\" in name
        or ".." in relative.parts
        or len(relative.parts) < 2
        or relative.parts[0] not in _RESOURCE_ROOTS
    ):
        raise ValueError(f"非法包资源路径：{name}")
    resource = resources.files("trippostcollect.resources").joinpath(*relative.parts)
    if resource.is_file():
        return resource
    source = GENERATED_RESOURCES.get(name)
    if source is not None and (_SOURCE_CHECKOUT / source).is_file():
        return _SOURCE_CHECKOUT / source
    raise FileNotFoundError(f"包资源不存在：{name}")


def read_bytes(name: str) -> bytes:
    return _resource(name).read_bytes()


def read_text(name: str) -> str:
    return read_bytes(name).decode("utf-8-sig")


@contextmanager
def path(name: str):
    """在资源使用结束后才释放可能从压缩包中解出的临时文件。"""
    with resources.as_file(_resource(name)) as location:
        yield location


def verify_package_resources() -> None:
    for name, expected in RESOURCE_SHA256.items():
        try:
            actual = sha256(read_bytes(name)).hexdigest()
        except OSError as error:
            raise RuntimeError(f"包资源无法读取：{name}") from error
        if actual != expected:
            raise RuntimeError(f"包资源 SHA-256 与基线不一致：{name}")
