"""旧隐私用例的窄测试装配：作者字段边界只经根实现 update_weibo_note 验证。

评论属 T12 退出切片；原先从冻结 fixture 执行的旧评论投影只验证旧哈希/脱敏行为，随旧身份函数在 T14-C 删除。
"""

import ast
from dataclasses import replace
import json
from types import SimpleNamespace

from support.weibo_adapter import ROOT, crawler, settings


FIXTURE = ROOT / "tests/fixtures/adapter_t05"
config = settings()


class Factory:
    @staticmethod
    def create_store():
        raise AssertionError("隐私用例必须显式装配 FakeStore")


async def update_weibo_note(note_item):
    instance = crawler(config)
    instance.ports = replace(instance.ports, store_factory=Factory.create_store)
    await instance.update_weibo_note(note_item)


wb = SimpleNamespace(WeibostoreFactory=Factory, update_weibo_note=update_weibo_note)


def sqlite_roundtrip(connection, model_name, record):
    """从冻结旧 ORM 列声明建 SQLite 表，仍拒绝任何旧模型没有声明的键。"""
    source = ast.parse((FIXTURE / "database/models.py.txt").read_text())
    model = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == model_name)
    columns = {}
    attributes = set()
    for node in model.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        attributes.add(name)
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "Column":
            value = node.value.args[0]
            columns[name] = value.id if isinstance(value, ast.Name) else value.func.id
    unknown = set(record) - attributes
    if unknown:
        raise TypeError(f"旧 ORM 未声明的字段：{sorted(unknown)}")
    column_sql = ", ".join(f'"{name}" {"INTEGER" if kind in {"Integer", "BigInteger"} else "TEXT"}' for name, kind in columns.items())
    connection.execute(f'CREATE TABLE "{model_name}" ({column_sql})')
    stored = {key: json.dumps(value) if columns[key] == "JSON" else value for key, value in record.items() if key in columns}
    fields = ", ".join(f'"{key}"' for key in stored)
    placeholders = ", ".join("?" for _ in stored)
    connection.execute(f'INSERT INTO "{model_name}" ({fields}) VALUES ({placeholders})', tuple(stored.values()))
    connection.commit()
    rows = connection.execute(f'SELECT * FROM "{model_name}"').fetchall()
    assert len(rows) == 1
    return SimpleNamespace(**dict(rows[0])), set(columns)
