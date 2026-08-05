from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path


class NativeBuffer(ctypes.Structure):
    _fields_ = [
        ("ptr", ctypes.c_void_p),
        ("len", ctypes.c_size_t),
    ]


class NativeStaticResponse(ctypes.Structure):
    _fields_ = [
        ("status", ctypes.c_uint16),
        ("headers", NativeBuffer),
        ("body", NativeBuffer),
        ("error", NativeBuffer),
    ]


@dataclass(frozen=True)
class NativeStaticResult:
    status: int
    response: bytes
    body_len: int


class NativeCore:
    def __init__(self, library: ctypes.CDLL, path: Path) -> None:
        self.library = library
        self.path = path
        self._static_response = getattr(library, "veloxcore_static_response", None)
        self._parse_request_json = getattr(library, "veloxcore_parse_request_json", None)
        self._cache_key = getattr(library, "veloxcore_cache_key", None)
        self._free_buffer = getattr(library, "veloxcore_free_buffer", None)
        if self._static_response is not None:
            self._static_response.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_bool,
                ctypes.c_bool,
            ]
            self._static_response.restype = NativeStaticResponse
        if self._parse_request_json is not None:
            self._parse_request_json.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            self._parse_request_json.restype = NativeBuffer
        if self._cache_key is not None:
            self._cache_key.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
            ]
            self._cache_key.restype = NativeBuffer
        if self._free_buffer is not None:
            self._free_buffer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            self._free_buffer.restype = None

    @property
    def supports_static_response(self) -> bool:
        return self._static_response is not None and self._free_buffer is not None

    @property
    def supports_request_parser(self) -> bool:
        return self._parse_request_json is not None and self._free_buffer is not None

    @property
    def supports_cache_key(self) -> bool:
        return self._cache_key is not None and self._free_buffer is not None

    def build_static_response(
        self,
        root: Path,
        target: str,
        method: str,
        index: str,
        keep_alive: bool,
        security_headers: bool,
    ) -> NativeStaticResult | None:
        if not self.supports_static_response:
            return None
        response = self._static_response(
            os.fsencode(root),
            target.encode("utf-8", errors="surrogatepass"),
            method.encode("ascii"),
            index.encode("utf-8", errors="surrogatepass"),
            keep_alive,
            security_headers,
        )
        try:
            if response.error.ptr and response.error.len:
                return None
            headers = buffer_to_bytes(response.headers)
            body = buffer_to_bytes(response.body)
            return NativeStaticResult(int(response.status), headers + body, len(body))
        finally:
            self._free_native_buffer(response.headers)
            self._free_native_buffer(response.body)
            self._free_native_buffer(response.error)

    def parse_request_head(self, head: bytes) -> dict[str, object] | None:
        if not self.supports_request_parser:
            return None
        import json

        response = self._parse_request_json(head, len(head))
        try:
            data = buffer_to_bytes(response)
            if not data:
                return None
            parsed = json.loads(data.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
        finally:
            self._free_native_buffer(response)

    def build_cache_key(
        self,
        template: str,
        method: str,
        scheme: str,
        host: str,
        uri: str,
        remote_addr: str,
    ) -> str | None:
        if not self.supports_cache_key:
            return None
        response = self._cache_key(
            template.encode("utf-8"),
            method.encode("utf-8"),
            scheme.encode("utf-8"),
            host.encode("utf-8"),
            uri.encode("utf-8"),
            remote_addr.encode("utf-8"),
        )
        try:
            data = buffer_to_bytes(response)
            return data.decode("utf-8") if data else ""
        finally:
            self._free_native_buffer(response)

    def _free_native_buffer(self, buffer: NativeBuffer) -> None:
        if buffer.ptr and buffer.len:
            self._free_buffer(buffer.ptr, buffer.len)


@dataclass(frozen=True)
class NativeCoreStatus:
    requested: str
    available: bool
    path: Path | None
    message: str
    core: NativeCore | None = None


def default_library_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("veloxcore.dll",)
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return ("libveloxcore.dylib",)
    return ("libveloxcore.so",)


def find_native_core(search_dir: Path | None = None) -> Path | None:
    roots = []
    if search_dir is not None:
        roots.append(search_dir)
    roots.append(Path(__file__).resolve().parent / "native_libs")
    roots.append(Path(__file__).resolve().parents[2] / "native" / "rust" / "target" / "release")
    for root in roots:
        for name in default_library_names():
            candidate = root / name
            if candidate.exists():
                return candidate
    return None


def buffer_to_bytes(buffer: NativeBuffer) -> bytes:
    if not buffer.ptr or not buffer.len:
        return b""
    return ctypes.string_at(buffer.ptr, buffer.len)


def load_native_core(requested: str, search_dir: Path | None = None) -> NativeCoreStatus:
    if requested == "python":
        return NativeCoreStatus(requested, False, None, "python core selected")
    path = find_native_core(search_dir)
    if path is None:
        return NativeCoreStatus(requested, False, None, "native core library not built")
    try:
        library = ctypes.CDLL(str(path))
        library.veloxcore_is_available.restype = ctypes.c_bool
        available = bool(library.veloxcore_is_available())
    except Exception as exc:
        return NativeCoreStatus(requested, False, path, f"failed to load native core: {exc}")
    core = NativeCore(library, path) if available else None
    return NativeCoreStatus(requested, available, path, "native core library loaded", core)
