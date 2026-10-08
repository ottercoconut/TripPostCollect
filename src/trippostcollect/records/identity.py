# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# 本文件为 MediaCrawler 教学版的一部分。
# TripPostCollect：微博、抖音、知乎、小红书根解析保存作者平台原始用户 ID 与原始昵称，统一经
# platform_user_id / platform_nickname 做类型归一，不再哈希或脱敏（B站解析本就直接取原始
# mid/作者名，不经本模块）。
# anonymize_user_id / mask_nickname 的调用方只剩 fork 旧导入出口 tools/user_hash.py 与冻结
# 旧投影的测试模块，随旧桥在 T14 删除；根包其他模块不得再调用。
import hashlib


def platform_user_id(user_id) -> str:
    """平台原始用户 ID 统一为字符串（整数 ID 同样转为字符串）并去首尾空白；缺失为空串。"""
    if user_id is None:
        return ""
    return str(user_id).strip()


def platform_nickname(name) -> str:
    """平台原始昵称按原文保存；缺失为空串，非字符串值转为字符串。"""
    if name is None:
        return ""
    return str(name)


def anonymize_user_id(user_id) -> str:
    """把原始用户 ID 转成匿名哈希，用于内容/评论记录的创作者分组，
    不暴露真实身份。返回 sha256 截断 16 位的十六进制串。"""
    if user_id is None:
        return ""
    s = str(user_id).strip()
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def mask_nickname(name) -> str:
    """昵称中间脱敏：首尾各保留 1 字，中间替换为星号。
    - 长度 <= 1：返回 "*"
    - 长度 == 2：首字 + "*"
    - 长度 >= 3：首字 + "***" + 尾字
    这样既保留教学分析所需的内容归属语义，又无法据昵称定位到真人。
    """
    if name is None:
        return ""
    s = str(name)
    if len(s) <= 1:
        return "*"
    if len(s) == 2:
        return s[0] + "*"
    return s[0] + "***" + s[-1]
