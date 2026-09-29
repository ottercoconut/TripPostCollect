"""旧脚本导入名转发到根包的唯一实现。"""

import sys

from trippostcollect.core import execution_state as _implementation

# 保持私有辅助函数与模块属性的 monkeypatch 命中实际实现。
sys.modules[__name__] = _implementation
