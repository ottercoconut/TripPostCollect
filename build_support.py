"""树内 PEP 517 后端与 setuptools 构建扩展：把仓库内唯一真源的 SQL 与契约文档复制进 wheel 的包内资源目录。

后端钩子（build_wheel/build_sdist/build_editable 等）全部原样重导出自 setuptools.build_meta。

白名单只在 `src/trippostcollect/core/resources.py:GENERATED_RESOURCES` 定义一次，这里按 AST 读取字面量，
不导入运行包；生成副本只写入 build_lib，不进源码树。复制后逐个校验 SHA-256。
构建不读取 fork、运行状态或网络。
"""

import ast
import hashlib
import shutil
from pathlib import Path

from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
RESOURCES_MODULE = ROOT / "src" / "trippostcollect" / "core" / "resources.py"


def generated_resources() -> dict[str, str]:
    tree = ast.parse(RESOURCES_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "GENERATED_RESOURCES" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError("core/resources.py 缺少 GENERATED_RESOURCES 白名单")


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            # 可编辑安装直接从源码 checkout 的真源读取，不生成副本。
            return
        target_root = Path(self.build_lib) / "trippostcollect" / "resources"
        for name, source in generated_resources().items():
            origin = ROOT / source
            target = target_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
            if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(origin.read_bytes()).digest():
                raise RuntimeError(f"生成资源与真源字节不一致：{name}")


# PEP 517/660 钩子：原样使用 setuptools 后端（__all__ 含 build_wheel/build_sdist/build_editable 及其 get_requires/prepare_metadata）。
from setuptools.build_meta import *  # noqa: E402,F401,F403
