"""Fenêtres Windows, en lecture et sans geste visible : l'utilisateur est-il dans Codex ou Antigravity ? Codex relancé est
rendu au fond et le premier plan rendu à l'utilisateur. (L'UI Automation de Claude Desktop est retirée le 20/10 : Claude
n'est plus réveillé.)
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

SW_SHOWMINNOACTIVE = 7
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


def windows(process: str, hidden: bool = False) -> list[int]:
    """Fenêtres de premier niveau (visibles ou réduites, avec un titre) de ce processus (nom d'image, minuscules) ;
    hidden=True : aussi les fenêtres cachées (appli fermée dans la zone de notification, encore en marche)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    found: list[int] = []

    def image(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(handle)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        if (hidden or user32.IsWindowVisible(hwnd)) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if image(pid.value) == process:
                found.append(int(hwnd))
        return True

    user32.EnumWindows(visit, 0)
    return found


def user32() -> Any:
    dll = ctypes.WinDLL("user32", use_last_error=True)
    dll.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    return dll


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def user_idle_s() -> float:
    """Secondes depuis la dernière entrée de l'utilisateur (clavier ou souris), d'après Windows."""
    info = _LastInput(ctypes.sizeof(_LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def foreground(hwnd: int) -> bool:
    """L'utilisateur est-il dans cette fenêtre (premier plan) ?"""
    return int(user32().GetForegroundWindow() or 0) == hwnd


def minimize(hwnd: int) -> None:
    user32().ShowWindow(hwnd, SW_SHOWMINNOACTIVE)  # rendue comme l'utilisateur l'avait laissée : réduite


# --- premier plan de l'utilisateur : rendu aussitôt si la cible le prend
def _attempt_give_back(prev: int, expected: int) -> None:
    """Une tentative : on s'attache à la file d'entrée de la fenêtre qui a PRIS le premier plan (jamais à celle de
    l'utilisateur), puis SetForegroundWindow(prev) (technique de nexus-windows, foreground.py, mesurée ≈ 10 ms)."""
    dll = user32()
    kernel32 = ctypes.WinDLL("kernel32")
    if int(dll.GetForegroundWindow() or 0) != expected:
        return
    own = kernel32.GetCurrentThreadId()
    thread = dll.GetWindowThreadProcessId(expected, None) if expected else 0
    attached = bool(thread and thread != own and dll.AttachThreadInput(own, thread, True))
    try:
        if int(dll.GetForegroundWindow() or 0) == expected:
            dll.BringWindowToTop(prev)
            dll.SetForegroundWindow(prev)
    finally:
        if attached:
            dll.AttachThreadInput(own, thread, False)


def give_back(prev: int, taken_by: int, timeout_s: float = 0.6) -> bool:
    """Rend le premier plan à la fenêtre de l'utilisateur `prev`, prise par `taken_by` (une fenêtre de la cible)."""
    import threading

    dll = user32()
    if not prev or not dll.IsWindow(prev) or dll.IsIconic(prev):
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = int(dll.GetForegroundWindow() or 0)
        if current == prev:
            return True
        if current != taken_by:
            return False  # l'utilisateur est passé ailleurs : on le suit sans lutter
        worker = threading.Thread(target=_attempt_give_back, args=(prev, current), daemon=True)
        worker.start()
        worker.join(0.25)
        time.sleep(0.02)
    return int(dll.GetForegroundWindow() or 0) == prev
