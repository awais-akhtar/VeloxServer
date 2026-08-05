from __future__ import annotations

import importlib.util
import ctypes
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


@dataclass(frozen=True)
class PluginResult:
    allowed: bool = True
    status: int = 200
    message: str = "OK"


class PluginManager:
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths
        self.modules = tuple(load_plugin(path) for path in paths if path.suffix == ".py")
        self.native_modules = tuple(load_native_module(path) for path in paths if path.suffix != ".py")

    def check_request(self, request: Any) -> PluginResult:
        result = self._run_hook("on_request", request)
        if not result.allowed:
            return result
        return self._run_native_hook(request)

    def check_waf(self, request: Any) -> PluginResult:
        return self._run_hook("on_waf_request", request)

    def check_auth(self, request: Any) -> PluginResult:
        return self._run_hook("on_auth_request", request)

    def _run_hook(self, name: str, request: Any) -> PluginResult:
        for module in self.modules:
            hook = getattr(module, name, None)
            if hook is None:
                continue
            result = normalize_plugin_result(hook(request))
            if not result.allowed:
                return result
        return PluginResult()

    def _run_native_hook(self, request: Any) -> PluginResult:
        payload = json.dumps(
            {
                "method": getattr(request, "method", ""),
                "target": getattr(request, "target", ""),
                "version": getattr(request, "version", ""),
                "headers": getattr(request, "headers", {}),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for module in self.native_modules:
            result = module.on_request(payload)
            if not result.allowed:
                return result
        return PluginResult()


def load_plugin(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"velox_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_plugin_result(result: Any) -> PluginResult:
    if result is False:
        return PluginResult(False, 403, "Forbidden")
    if isinstance(result, dict) and not result.get("allowed", True):
        return PluginResult(
            False,
            int(result.get("status", 403)),
            str(result.get("message", "Forbidden")),
        )
    return PluginResult()


class NativeModule:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.library = ctypes.CDLL(str(path))
        self.hook = getattr(self.library, "velox_module_on_request_json", None)
        self.free = getattr(self.library, "velox_module_free", None)
        if self.hook is not None:
            self.hook.argtypes = [ctypes.c_char_p]
            self.hook.restype = ctypes.c_void_p
        if self.free is not None:
            self.free.argtypes = [ctypes.c_void_p]
            self.free.restype = None
        init = getattr(self.library, "velox_module_init", None)
        if init is not None:
            init()

    def on_request(self, payload: bytes) -> PluginResult:
        if self.hook is None:
            return PluginResult()
        ptr = self.hook(payload)
        if not ptr:
            return PluginResult()
        try:
            raw = ctypes.cast(ptr, ctypes.c_char_p).value or b"{}"
            return normalize_plugin_result(json.loads(raw.decode("utf-8")))
        finally:
            if self.free is not None:
                self.free(ptr)


def load_native_module(path: Path) -> NativeModule:
    return NativeModule(path)
