"""测试辅助：临时设置/清除环境变量。

用于验证 ``PA_*`` 环境变量是否真正生效，同时**保证测试结束后环境被复原**——
否则会污染同进程内后续测试（如某些测试断言"裸命令行为不变"）。

用法::

    from _env_guard import set_env
    with set_env(PA_ATR_MULT="2.5"):
        ...
    with set_env(PA_PRESET=None):     # None = 临时删除
        ...

注意：本模块名以 ``_`` 开头、不以 ``test_`` 开头，因此不会被 unittest 收集为测试文件。
"""
from __future__ import annotations

import contextlib
import os
from typing import Iterator, Optional


@contextlib.contextmanager
def set_env(**kwargs: Optional[str]) -> Iterator[None]:
    """临时设置环境变量；值为 ``None`` 表示临时删除该项。

    Args:
        **kwargs: 环境变量名 → 值（``None`` 表示删除）。

    Yields:
        None。退出时（含异常）一律恢复原值。
    """
    saved: dict[str, Optional[str]] = {k: os.environ.get(k) for k in kwargs}
    try:
        for key, val in kwargs.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(val)
        yield
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
