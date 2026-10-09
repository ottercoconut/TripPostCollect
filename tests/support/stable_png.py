"""与主机无关的纯色 PNG 测试输入。

Pillow 写 PNG 时经其内置的 zlib-ng 压缩；zlib-ng 按 CPU 特性选择不同的哈希与匹配实现（如 arm64 的
硬件 CRC32 与 x86_64 的实现不同），同一图像在 macOS arm64 与 Linux x86_64 上可能得到不同的压缩字节。
固化预期逐字节比较图片与其 sha256，测试输入因此不得依赖压缩器：这里手工拼出 PNG，IDAT 使用 deflate
存储块（不压缩），只用确定的 adler32/crc32 校验，任何主机上字节都相同。
"""

from __future__ import annotations

import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """8 位 RGB、无隔行的纯色 PNG；每行滤波类型 0，整幅数据放进一个 deflate 存储块。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    assert len(raw) <= 0xFFFF
    stored = (b"\x78\x01" + b"\x01" + struct.pack("<HH", len(raw), len(raw) ^ 0xFFFF) + raw
              + struct.pack(">I", zlib.adler32(raw)))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", stored) + _chunk(b"IEND", b"")
