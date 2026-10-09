# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# 本文件为 MediaCrawler 教学版的一部分。
# TripPostCollect：微博、抖音、知乎、小红书根解析保存作者平台原始用户 ID 与原始昵称，统一经
# platform_user_id / platform_nickname 做类型归一，不再哈希或脱敏（B站解析本就直接取原始
# mid/作者名，不经本模块）。


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

