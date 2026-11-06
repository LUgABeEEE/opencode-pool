"""适配器注册表。

扩展点（按侵入程度从低到高）：
  1. 纯配置：配置里 `providers` 加一段 `adapter: openai-compatible` —— 零代码接新上游
  2. 掉落模块：把实现 Adapter 的 .py 放进 `relay/adapters/ext/`，暴露 `ADAPTER` 或
     `register(registry)`，启动时自动发现并登记
  3. 内置：像 opencode.py 一样写进本包并由 builtins() 列名
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from typing import Iterable

from .base import Adapter

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXT_DIR = os.path.join(_HERE, "ext")

_REGISTRY: dict[str, Adapter] = {}
_LOAD_ERRORS: list[str] = []


def register(adapter: Adapter) -> Adapter:
    if not adapter.id:
        raise ValueError("adapter 必须有 id")
    _REGISTRY[adapter.id] = adapter
    return adapter


def get(adapter_id: str) -> Adapter | None:
    return _REGISTRY.get(adapter_id)


def all() -> list[Adapter]:
    return list(_REGISTRY.values())


def load_errors() -> list[str]:
    return list(_LOAD_ERRORS)


def _import_builtin(module: str, name: str) -> None:
    from importlib import import_module

    mod = import_module(f".{module}", __package__)
    register(getattr(mod, "ADAPTER", None) or getattr(mod, "adapter"))
    if not hasattr(mod, "ADAPTER"):
        _LOAD_ERRORS.append(f"{name}: 内置模块没有 ADAPTER 变量")


def load_builtins() -> None:
    for module, name in (("opencode", "opencode-go"), ("openai_compat", "openai-compatible")):
        _import_builtin(module, name)


def load_extensions() -> None:
    """扫 ext/*.py —— 用户的私有适配器丢进来就能用，不用改核心代码。"""
    if not os.path.isdir(_EXT_DIR):
        return
    for fname in sorted(os.listdir(_EXT_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(_EXT_DIR, fname)
        mod_name = f"relay_ext_{fname[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                _LOAD_ERRORS.append(f"{fname}: 无法加载")
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            if hasattr(module, "register"):
                module.register(register)
            elif hasattr(module, "ADAPTER"):
                register(module.ADAPTER)
            else:
                _LOAD_ERRORS.append(f"{fname}: 既没有 register() 也没有 ADAPTER")
        except Exception as exc:  # noqa: BLE001 - 扩展崩了不该拖垮主进程
            _LOAD_ERRORS.append(f"{fname}: {type(exc).__name__}: {exc}")
            traceback.print_exc()


def load_all() -> None:
    load_builtins()
    load_extensions()


def descriptors() -> Iterable[dict]:
    return (a.descriptor() for a in _REGISTRY.values())
