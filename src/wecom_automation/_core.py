"""企业微信 Windows UIA 核心实现。

先激活运行中客户端的无障碍 Provider，再通过窗口句柄访问 UIA 元素。
企业微信版本变化时，在对应业务方法中更新 Control 定位参数。
"""

import ctypes
import ctypes.wintypes as wt
import os
import re
import struct
import time
from datetime import datetime

import uiautomation as auto

try:
    import pefile
except Exception:
    pefile = None

try:
    import win32api
    import win32con
    import win32gui
    import win32process
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False


class WorkWechatError(Exception):
    pass


class WindowNotFound(WorkWechatError):
    pass


class UIAActivationError(WorkWechatError):
    pass


class ElementNotFound(WorkWechatError):
    pass


class ContactNotFound(WorkWechatError):
    pass


class AmbiguousContact(WorkWechatError):
    pass


class SendFailed(WorkWechatError):
    pass


# ---------------------------------------------------------------------------
# 企业微信 DuiLib UIA Provider 热激活（内聚自 WeWorkUIA-Universal.py）
ONGET_EXPORT = "?OnGetObject@CWindowWnd@DuiLib@@MAEJIIJAA_N@Z"
HANDLEMSG_SLOTS_BACK = 5
WM_GETOBJECT = 0x3D
OBJID_CLIENT = -4

_K32 = ctypes.windll.kernel32
_PSAPI = ctypes.windll.psapi
_USER32 = ctypes.windll.user32

_K32.OpenProcess.restype = wt.HANDLE
_K32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_K32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
_K32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
_K32.VirtualProtectEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
_PSAPI.EnumProcessModulesEx.argtypes = [
    wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
    ctypes.POINTER(wt.DWORD), wt.DWORD,
]
_PSAPI.GetModuleFileNameExW.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD,
]
_USER32.SendMessageW.restype = ctypes.c_ssize_t
_USER32.WaitForInputIdle.argtypes = [wt.HANDLE, wt.DWORD]
_USER32.WaitForInputIdle.restype = wt.DWORD


class _RemoteProcess(object):
    def __init__(self, pid):
        self.handle = _K32.OpenProcess(0x1F0FFF, False, int(pid))
        if not self.handle:
            raise UIAActivationError(
                "OpenProcess({}) 失败 err={}".format(pid, ctypes.GetLastError())
            )

    def read(self, address, size):
        buffer_ = (ctypes.c_char * size)()
        read_size = ctypes.c_size_t(0)
        ok = _K32.ReadProcessMemory(
            self.handle, address, buffer_, size, ctypes.byref(read_size)
        )
        if not ok or read_size.value != size:
            raise UIAActivationError(
                "ReadProcessMemory 失败 @0x{:x} err={}".format(
                    address, ctypes.GetLastError()
                )
            )
        return buffer_.raw

    def read_u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def write(self, address, data):
        data = bytes(data)
        written = ctypes.c_size_t(0)
        ok = _K32.WriteProcessMemory(
            self.handle, address, data, len(data), ctypes.byref(written)
        )
        if not ok or written.value != len(data):
            raise UIAActivationError(
                "WriteProcessMemory 失败 @0x{:x} err={}".format(
                    address, ctypes.GetLastError()
                )
            )

    def protect(self, address, size, protection):
        old = wt.DWORD()
        if not _K32.VirtualProtectEx(
            self.handle, address, size, protection, ctypes.byref(old)
        ):
            raise UIAActivationError(
                "VirtualProtectEx 失败 @0x{:x}".format(address)
            )
        return old.value

    def close(self):
        if self.handle:
            _K32.CloseHandle(self.handle)
            self.handle = None


class _UIAActivator(object):
    """仅负责激活 Provider，不参与任何元素操作。"""

    @staticmethod
    def find_main_window():
        if not _HAS_WIN32:
            return None, None
        candidates = []
        visible_shadow_owners = set()

        def collect(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                class_name = win32gui.GetClassName(hwnd)
                if class_name == "WeWorkWindow":
                    candidates.append(hwnd)
                elif class_name == "PerryShadowWnd":
                    owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                    if owner:
                        visible_shadow_owners.add(owner)
            except Exception:
                pass

        win32gui.EnumWindows(collect, None)
        if not candidates:
            return None, None

        def score(item):
            try:
                left, top, right, bottom = win32gui.GetWindowRect(item)
                area = max(0, right - left) * max(0, bottom - top)
                return item in visible_shadow_owners, not win32gui.IsIconic(item), area
            except Exception:
                return False, False, 0

        hwnd = max(candidates, key=score)
        return hwnd, win32process.GetWindowThreadProcessId(hwnd)[1]

    @staticmethod
    def wait_input_idle(pid, timeout=5.0):
        handle = _K32.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            return False
        try:
            return _USER32.WaitForInputIdle(
                handle, max(1, int(float(timeout) * 1000))
            ) == 0
        finally:
            _K32.CloseHandle(handle)

    @staticmethod
    def wake_window(hwnd):
        return bool(_USER32.SendMessageW(
            hwnd, WM_GETOBJECT, 1, OBJID_CLIENT & 0xFFFFFFFF
        ))

    @staticmethod
    def _module_map(handle):
        modules = (ctypes.c_void_p * 2048)()
        needed = wt.DWORD()
        if not _PSAPI.EnumProcessModulesEx(
            handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3
        ):
            raise UIAActivationError("EnumProcessModulesEx 失败")
        result = {}
        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        for index in range(count):
            path = ctypes.create_unicode_buffer(520)
            _PSAPI.GetModuleFileNameExW(handle, modules[index], path, 520)
            base = int(modules[index] or 0)
            result[os.path.basename(path.value).lower()] = (base, path.value)
        return result

    @classmethod
    def _find_flag_rvas(cls, pe):
        text = next(section for section in pe.sections if b".text" in section.Name)
        data_sections = [
            section for section in pe.sections
            if section is not text and section.Characteristics & 0x80000000
        ]
        image_base = pe.OPTIONAL_HEADER.ImageBase
        data = text.get_data()
        hits = []
        offset = 0
        while True:
            offset = data.find(b"\x80\x3d", offset)
            if offset < 0:
                break
            if offset + 7 <= len(data) and data[offset + 6] == 0:
                immediate = struct.unpack("<I", data[offset + 2:offset + 6])[0]
                rva = immediate - image_base
                if any(
                    section.VirtualAddress <= rva <
                    section.VirtualAddress + section.Misc_VirtualSize
                    for section in data_sections
                ):
                    hits.append((offset, rva))
            offset += 1
        for first in range(len(hits)):
            for second in range(first + 1, len(hits)):
                if hits[second][0] - hits[first][0] > 0x100:
                    break
                for third in range(second + 1, len(hits)):
                    if hits[third][0] - hits[first][0] > 0x100:
                        break
                    rvas = sorted(hits[index][1] for index in (first, second, third))
                    if len(set(rvas)) == 3 and rvas[2] - rvas[0] <= 4:
                        return rvas
        # 5.0.10 及部分低版本没有三门控标志。相邻 .data 字节可能是无关
        # 调试开关，只有确认 OnGetObject 为无门控旧 ABI 时才跳过写入。
        try:
            onget_rva = cls._find_onget_rva(pe)
            code = pe.get_data(onget_rva, 0x80)
            compares_client = (
                b"\x83\x7d\x10\xfc" in code[:0x30]
                or b"\x81\x7d\x10\xfc\xff\xff\xff" in code[:0x30]
            )
            if (
                code[:3] == b"\x55\x8b\xec"
                and compares_client
                and b"\xc2\x10\x00" in code
            ):
                return []
        except Exception:
            pass
        raise UIAActivationError("未找到 DuiLib UIA 门控标志签名簇")

    @staticmethod
    def _find_onget_rva(pe):
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
        for export in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            name = export.name.decode("ascii", "ignore") if export.name else ""
            if name == ONGET_EXPORT or name.startswith(
                "?OnGetObject@CWindowWnd@DuiLib@@"
            ):
                return export.address
        raise UIAActivationError("DuiLib 导出表中未找到 OnGetObject")

    @staticmethod
    def _find_cave_rva(pe, min_length=65, remote=None, image_base=0):
        text = next(section for section in pe.sections if b".text" in section.Name)
        data = text.get_data()
        pattern = b"\xCC" * min_length
        offset = 0
        while True:
            start = data.find(pattern, offset)
            if start < 0:
                break
            end = start + min_length
            while end < len(data) and data[end] == 0xCC:
                end += 1
            reserve = 4 if end - start >= min_length + 4 else 0
            rva = text.VirtualAddress + start + reserve
            if remote is None or remote.read(image_base + rva, min_length) == pattern:
                return rva
            offset = end
        raise UIAActivationError("WXWork.exe 中未找到可用代码洞穴")

    @staticmethod
    def _resolves_to(remote, address, target, exe_base, exe_size):
        if address == target:
            return True
        if not exe_base <= address < exe_base + exe_size:
            return False
        try:
            code = remote.read(address, 6)
        except UIAActivationError:
            return False
        if code[:2] == b"\xff\x25":
            try:
                return remote.read_u32(struct.unpack("<I", code[2:6])[0]) == target
            except UIAActivationError:
                return False
        if code[0] == 0xE9:
            return address + 5 + struct.unpack("<i", code[1:5])[0] == target
        return struct.unpack("<I", code[:4])[0] == target

    @classmethod
    def _resolve_handle_message(
        cls, remote, hwnd, onget_va, exe_base, exe_size, exe_text
    ):
        this_ptr = _USER32.GetWindowLongW(hwnd, -21) & 0xFFFFFFFF
        if not this_ptr:
            raise UIAActivationError("企业微信 GWLP_USERDATA 为空")
        vtable = remote.read_u32(this_ptr)
        entries = struct.unpack("<20I", remote.read(vtable, 80))
        slot = None
        for index, entry in enumerate(entries):
            if cls._resolves_to(remote, entry, onget_va, exe_base, exe_size):
                slot = index
                break
        if slot is None or slot < HANDLEMSG_SLOTS_BACK:
            raise UIAActivationError("无法从 DuiLib 虚表定位 HandleMessage")
        address = entries[slot - HANDLEMSG_SLOTS_BACK]
        if not exe_text[0] <= address - exe_base < exe_text[1]:
            raise UIAActivationError("HandleMessage 不在 WXWork.exe .text 内")
        head = remote.read(address, 6)
        if head[0] == 0xE9:
            return address, True, slot
        body = head[2:] if head[:2] == b"\x66\x90" else head
        if body[:3] != b"\x55\x8b\xec":
            raise UIAActivationError("HandleMessage 序言异常：{}".format(head.hex()))
        return address, False, slot

    @staticmethod
    def _build_stub(entry_va, cave_va, call_offset):
        back = entry_va + 5
        stub = bytearray()
        stub += bytes.fromhex("837c24043d")
        jump1 = len(stub); stub += b"\x75\x00"
        stub += bytes.fromhex("837c240cfc")
        jump2 = len(stub); stub += b"\x75\x00"
        stub += bytes.fromhex("83ec08c60424008d042450")
        stub += bytes.fromhex("8b44241850")
        stub += bytes.fromhex("8b442418506a008b01")
        stub += b"\xff\x50" + bytes([call_offset])
        stub += bytes.fromhex("803c2400")
        jump3 = len(stub); stub += b"\x75\x00"
        stub += bytes.fromhex("83c408")
        fallthrough = len(stub)
        stub += b"\x55\x8b\xec"
        stub += b"\xe9" + struct.pack("<i", back - (cave_va + len(stub) + 5))
        handled = len(stub)
        stub += bytes.fromhex("83c408c20c00")
        stub[jump1 + 1] = (fallthrough - (jump1 + 2)) & 0xFF
        stub[jump2 + 1] = (fallthrough - (jump2 + 2)) & 0xFF
        stub[jump3 + 1] = (handled - (jump3 + 2)) & 0xFF
        return bytes(stub)

    @classmethod
    def patch_once(cls, dry_run=False, hwnd=None):
        if pefile is None:
            raise UIAActivationError("缺少 pefile，无法解析企业微信注入偏移")
        if hwnd:
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        else:
            hwnd, pid = cls.find_main_window()
        if not hwnd:
            raise WindowNotFound("未找到企业微信目标窗口")
        remote = _RemoteProcess(pid)
        try:
            modules = cls._module_map(remote.handle)
            if "duilib.dll" not in modules or "wxwork.exe" not in modules:
                raise UIAActivationError("目标进程中未找到 DuiLib.dll/WXWork.exe")
            duilib_base, duilib_path = modules["duilib.dll"]
            exe_base, exe_path = modules["wxwork.exe"]
            duilib_pe = pefile.PE(duilib_path, fast_load=True)
            exe_pe = pefile.PE(exe_path, fast_load=True)
            text = next(section for section in exe_pe.sections if b".text" in section.Name)
            exe_text = (
                text.VirtualAddress,
                text.VirtualAddress + text.Misc_VirtualSize,
            )
            flag_rvas = cls._find_flag_rvas(duilib_pe)
            onget_va = duilib_base + cls._find_onget_rva(duilib_pe)
            handle_message, already, slot = cls._resolve_handle_message(
                remote, hwnd, onget_va, exe_base,
                exe_pe.OPTIONAL_HEADER.SizeOfImage, exe_text,
            )
            cave_va = None
            if not already:
                stub_length = len(cls._build_stub(0x1000, 0x2000, 0))
                cave_va = exe_base + cls._find_cave_rva(
                    exe_pe, stub_length, remote, exe_base
                )
            if dry_run:
                return pid
            current = b"".join(remote.read(duilib_base + rva, 1) for rva in flag_rvas)
            if current != b"\x01" * len(flag_rvas):
                for rva in flag_rvas:
                    remote.write(duilib_base + rva, b"\x01")
            if not already:
                stub = cls._build_stub(handle_message, cave_va, slot * 4)
                old_cave = remote.protect(cave_va & ~0xFFF, 0x2000, 0x40)
                old_entry = remote.protect(handle_message & ~0xFFF, 0x1000, 0x40)
                try:
                    remote.write(cave_va, stub)
                    remote.write(
                        handle_message,
                        b"\xe9" + struct.pack(
                            "<i", cave_va - (handle_message + 5)
                        ),
                    )
                    _K32.FlushInstructionCache(remote.handle, None, 0)
                finally:
                    remote.protect(cave_va & ~0xFFF, 0x2000, old_cave)
                    remote.protect(handle_message & ~0xFFF, 0x1000, old_entry)
        finally:
            remote.close()
        if not cls.wake_window(hwnd):
            raise UIAActivationError("WM_GETOBJECT 未返回企业微信 UIA Provider")
        return pid


# ---------------------------------------------------------------------------
# UIA 客户端
def _parse_dt(value):
    if not value:
        return None
    for format_ in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, format_)
        except ValueError:
            pass
    return None


def extract_all_text(value):
    return re.sub(r"</?c(?:\s+[^>]*)?>", "", str(value or ""))


def _is_exact(match_type):
    return str(match_type or "").strip() in (
        "相等", "等于", "精确", "相等匹配", "精确匹配",
    )


class ClassName(object):
    Message = "HorizontalLayoutUI"
    CompoundMessage = "compound_message_item_layout"
    File = "VerticalLayoutUI"
    Markdown = "markdown_content_layout"
    Image = "chat_content"
    TencentMeeting = "tencent_meeting_item_container"
    LinkCard = "LinkCardRegion"
    Emotion = "emotion_panel"
    Video = "bubble_region"
    WholeItem = "whole_item"


class WorkWxHandler(object):
    def __init__(self, timeout=25.0, search_timeout=2.0):
        self.timeout = float(timeout)
        self.win_wechat = None
        self._pid = None
        auto.SetGlobalSearchTimeout(float(search_timeout))

    @staticmethod
    def _safe_attr(control, name, default=""):
        try:
            return getattr(control, name, default)
        except Exception:
            return default

    @staticmethod
    def _exists(control, timeout=1.0):
        try:
            return control.Exists(float(timeout), 0.1)
        except Exception:
            return False

    def _control(self, root, timeout=0.5, **selector):
        try:
            control = root.Control(**selector)
        except Exception:
            return None
        return control if self._exists(control, timeout) else None

    @classmethod
    def _force_foreground(cls, hwnd):
        if not _HAS_WIN32 or not hwnd:
            return False
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            current_thread = win32api.GetCurrentThreadId()
            foreground = win32gui.GetForegroundWindow()
            foreground_thread = (
                win32process.GetWindowThreadProcessId(foreground)[0]
                if foreground else 0
            )
            if foreground_thread:
                win32process.AttachThreadInput(
                    current_thread, foreground_thread, True
                )
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if foreground_thread:
                    win32process.AttachThreadInput(
                        current_thread, foreground_thread, False
                    )
            return win32gui.GetForegroundWindow() == hwnd
        except Exception:
            return False

    def _activate(self, window):
        hwnd = self._safe_attr(window, "NativeWindowHandle", 0)
        if not self._force_foreground(hwnd):
            try:
                window.SetActive()
            except Exception:
                pass
        if _HAS_WIN32 and hwnd:
            try:
                win32gui.RedrawWindow(
                    hwnd, None, None,
                    win32con.RDW_INVALIDATE | win32con.RDW_ERASE |
                    win32con.RDW_ALLCHILDREN | win32con.RDW_UPDATENOW |
                    win32con.RDW_FRAME,
                )
                win32gui.UpdateWindow(hwnd)
            except Exception:
                pass
        time.sleep(0.2)

    @staticmethod
    def _main_handle():
        return _UIAActivator.find_main_window()

    def _anchor(self, hwnd):
        try:
            return auto.ControlFromHandle(hwnd)
        except Exception:
            return None

    def _main_content_ready(self, window):
        try:
            children = window.GetChildren()
        except Exception:
            return False
        for child in children:
            if self._safe_attr(child, "ClassName") in (
                "TitleBarWindow", "PerryShadowWnd",
            ):
                continue
            try:
                if self._safe_attr(child, "Name") or child.GetChildren():
                    return True
            except Exception:
                pass
        return False

    def _activate_uia_tree(self, dry_run=False):
        """解析当前版本偏移并激活企业微信 DuiLib UIA 树。"""
        hwnd, pid = self._main_handle()
        if not hwnd:
            raise WindowNotFound("未找到企业微信，请先启动并登录客户端")
        _UIAActivator.wait_input_idle(pid, min(self.timeout, 10.0))
        time.sleep(0.2)
        pid = _UIAActivator.patch_once(dry_run=dry_run, hwnd=hwnd)
        if dry_run:
            return pid
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            hwnd, current_pid = self._main_handle()
            if hwnd and current_pid == pid:
                window = self._anchor(hwnd)
                if window is not None:
                    self._activate(window)
                    _UIAActivator.wake_window(hwnd)
                    if self._main_content_ready(window):
                        self.win_wechat = window
                        self._pid = pid
                        return window
            time.sleep(0.2)
        raise UIAActivationError("注入完成，但企业微信 UIA 内容树未就绪")

    def ensure_window(self, wake=True, timeout=None):
        """按句柄锚定企业微信；树未展开时对当前进程执行热激活。"""
        timeout = self.timeout if timeout is None else float(timeout)
        hwnd, pid = self._main_handle()
        if not hwnd:
            raise WindowNotFound("未找到企业微信，请先启动并登录客户端")
        if self._pid == pid:
            if not wake or _UIAActivator.wake_window(hwnd):
                window = self._anchor(hwnd)
                if window is not None:
                    self._activate(window)
                    if not wake or self._main_content_ready(window):
                        self.win_wechat = window
                        return window
        if not wake:
            raise WindowNotFound("企业微信窗口存在，但 UIA 树尚未激活")
        old_timeout = self.timeout
        self.timeout = timeout
        try:
            return self._activate_uia_tree()
        finally:
            self.timeout = old_timeout

    def _descendants(self, root, max_depth=12):
        result = []
        stack = [(root, 0)]
        while stack:
            control, depth = stack.pop()
            if depth:
                result.append(control)
            if depth >= max_depth:
                continue
            try:
                children = control.GetChildren()
            except Exception:
                children = []
            for child in reversed(children):
                stack.append((child, depth + 1))
        return result

    def _control_text(self, control, include_children=True, fallback_name=True):
        if control is None:
            return ""
        controls = [control]
        if include_children:
            controls.extend(self._descendants(control))
        values = []
        for item in controls:
            value = ""
            for pattern_name in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
                try:
                    pattern = getattr(item, pattern_name)()
                    value = str(getattr(pattern, "Value", "") or "").strip()
                    if value:
                        break
                except Exception:
                    pass
            if not value and fallback_name:
                value = str(self._safe_attr(item, "Name", "") or "").strip()
            if value and value not in values:
                values.append(value)
        return "\n".join(values)

    def _descendant_by_text(self, root, text, max_depth=6, name=None):
        for control in self._descendants(root, max_depth):
            if name is not None and self._safe_attr(control, "Name") != name:
                continue
            value = self._normalized_name(
                self._control_text(control, include_children=False)
            )
            if self._name_matches(value, text, exact=True):
                return control
        return None

    @staticmethod
    def _clip_set(text):
        auto.SetClipboardText(str(text))

    def _paste_into(self, control, text, clear=True):
        control.Click()
        time.sleep(0.1)
        if clear:
            control.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        self._clip_set(text)
        control.SendKeys("{Ctrl}v", waitTime=0.05)

    def _uia_window(self, predicate, timeout, ready=None, error_message=None):
        deadline = time.time() + float(timeout)
        activated = set()
        last_error = None
        while time.time() < deadline:
            matches = []

            def collect(hwnd, _):
                try:
                    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                    class_name = win32gui.GetClassName(hwnd)
                    title = win32gui.GetWindowText(hwnd)
                    if (
                        pid == self._pid
                        and win32gui.IsWindowVisible(hwnd)
                        and predicate(class_name, title)
                    ):
                        matches.append(hwnd)
                except Exception:
                    pass

            win32gui.EnumWindows(collect, None)
            for hwnd in matches:
                try:
                    if hwnd not in activated:
                        _UIAActivator.patch_once(hwnd=hwnd)
                        activated.add(hwnd)
                    window = self._anchor(hwnd)
                    if window is not None and (ready is None or ready(window)):
                        return window
                except Exception as error:
                    last_error = error
            time.sleep(0.1)
        message = error_message or "未找到或无法激活企业微信窗口的 UIA 树"
        if last_error is not None:
            message = "{}：{}".format(message, last_error)
        raise ElementNotFound(message)

    def _search_window(self, timeout=4.0):
        return self._uia_window(
            lambda class_name, title: (
                title == "全局搜索" or class_name.startswith("SearchTabWnd")
            ),
            timeout,
            error_message="未找到或无法激活全局搜索窗口的 UIA 树",
        )

    def _open_global_search(self, keyword, find_type):
        window = self.ensure_window()
        self._activate(window)
        auto.SendKeys("{Ctrl}{Alt}f", waitTime=0.05)
        search_window = self._search_window()
        search_ele = search_window.EditControl(searchDepth=20, Name="")
        if not self._exists(search_ele, 0.5):
            raise ElementNotFound("未找到全局搜索输入框")
        self._paste_into(search_ele, keyword, clear=True)
        time.sleep(0.6)

        tab_ele = search_window.RadioButtonControl(
            searchDepth=20, Name=find_type
        )
        if not self._exists(tab_ele, 0.5):
            raise ElementNotFound("未找到搜索类型标签：{}".format(find_type))
        tab_ele.Click()
        time.sleep(0.4)
        return search_window

    @staticmethod
    def _close_window(window):
        if window is None:
            return
        try:
            window.GetWindowPattern().Close()
            return
        except Exception:
            pass
        try:
            window.SendKeys("{Esc}")
        except Exception:
            pass

    @staticmethod
    def _normalized_name(value):
        value = extract_all_text(value)
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return lines[0] if lines else value.strip()

    @staticmethod
    def _name_matches(value, keyword, exact):
        value = str(value or "").replace(" ", "")
        keyword = str(keyword or "").replace(" ", "")
        return value == keyword if exact else keyword in value

    @staticmethod
    def _search_result_name(value, find_type, expected=None):
        """去掉影刀元素文本中的列表项、联系方式和群成员摘要。"""
        value = re.sub(
            r"\s*[\(（](?:列表项目|列表项)[\)）]\s*$",
            "",
            extract_all_text(value).strip(),
        )
        if find_type == "联系人":
            value = re.split(r"\s+(?:邮箱|手机)\s*[:：]", value, maxsplit=1)[0]
            expected = str(expected or "").strip()
            if expected and (
                value == expected or value.startswith(expected + " ")
            ):
                return expected
        elif find_type == "群聊":
            value = re.split(
                r"\s+(?:[\(（]\d+[\)）]\s*)?"
                r"(?:(?:外部|内部)(?:\s+包含\s*[:：])?|包含\s*[:：])"
                r"(?:\s|$)",
                value,
                maxsplit=1,
            )[0]
        return value.strip()

    @staticmethod
    def _without_unread(value):
        return re.sub(
            r"\s+\d+\+?\s*条未读.*$", "", str(value or "").strip()
        ).strip()

    @classmethod
    def _session_list_name(cls, value):
        return re.split(
            r"\s+(?:外部|内部|全员|部门|\d+\+?\s*条未读)(?:\s|$)",
            cls._without_unread(value),
            maxsplit=1,
        )[0].strip()

    def _result_display_name(
        self, control, raw, name="search_name", find_type=None, expected=None
    ):
        value = ""
        for item in self._descendants(control):
            if self._safe_attr(item, "Name") != name:
                continue
            value = self._normalized_name(
                self._control_text(item, include_children=False)
            )
            if value:
                break
        value = value or self._normalized_name(raw)
        if find_type:
            value = self._search_result_name(value, find_type, expected)
        return value

    def _result_click_control(self, control, name):
        return (
            self._descendant_by_text(
                control, name, max_depth=12, name="search_name"
            )
            or self._descendant_by_text(control, name, max_depth=12)
            or control
        )

    def _list_results(self, keyword, find_type="群聊"):
        search_window = self._open_global_search(keyword, find_type)
        try:
            result_list = search_window.GetChildren()[1].GetChildren()[0]
            result_list = result_list.GetChildren()[0].GetChildren()[2]
            result_list = result_list.GetChildren()[2].GetChildren()[0]
            result_list = result_list.GetChildren()[0].GetChildren()[0].GetChildren()[0]
        except Exception as error:
            raise ElementNotFound("未找到搜索结果列表") from error
        if not self._exists(result_list, 1):
            raise ElementNotFound("未找到搜索结果列表")
        results = []
        seen = set()
        for index, control in enumerate(result_list.GetChildren()):
            raw = self._control_text(control)
            display_name = self._result_display_name(
                control, raw, find_type=find_type, expected=keyword
            )
            key = (display_name, raw)
            if not display_name or key in seen:
                continue
            seen.add(key)
            results.append({
                "index": index,
                "name": display_name,
                "raw": raw,
                "control": control,
            })
        return search_window, results

    def find_contact(self, name, find_match="相等", find_type="群聊",
                     check_unique=False):
        if name is None or not str(name).strip():
            return None
        name = str(name).strip()
        if find_type not in ("联系人", "群聊"):
            raise ValueError("查找类型必须是 联系人 或 群聊")
        search_window, results = self._list_results(name, find_type)
        exact = _is_exact(find_match)
        normalized_phone = re.sub(r"\s+", "", name)

        def matches(item):
            if self._name_matches(item["name"], name, exact):
                return True
            if not exact:
                return False
            phone = re.search(
                r"手机\s*[:：]\s*([^\s\(（]+)",
                extract_all_text(item["raw"]),
            )
            return bool(
                phone
                and re.sub(r"\s+", "", phone.group(1)) == normalized_phone
            )

        matched = [item for item in results if matches(item)]
        if not matched:
            self._close_window(search_window)
            raise ContactNotFound("未找到企微{} [{}]".format(find_type, name))
        if check_unique and len(matched) > 1:
            self._close_window(search_window)
            raise AmbiguousContact(
                "匹配到多个同名结果：{}".format(
                    [item["name"] for item in matched]
                )
            )
        chosen = matched[0]
        self._result_click_control(
            chosen["control"], chosen["name"]
        ).Click()
        time.sleep(0.6)
        self._close_window(search_window)
        return chosen["name"] or name

    def _chat_input(self, window=None):
        window = window or self.ensure_window()
        try:
            send_button = window.Control(searchDepth=30, Name="发送(S)")
            edit = send_button.GetParentControl().GetPreviousSiblingControl().GetChildren()[0]
        except Exception as error:
            raise SendFailed("未找到消息输入框") from error
        if not self._exists(edit, 3):
            raise SendFailed("未找到消息输入框")
        return edit

    def get_session_name(self):
        self.ensure_window()
        try:
            control = self.win_wechat.Control(searchDepth=30, SubName="聊天信息")
            for _ in range(8):
                control = control.GetChildren()[0]
        except Exception:
            return None
        if not self._exists(control, 0.5):
            return None
        value = self._control_text(control)
        return self._normalized_name(value) if value else None

    def get_session_type(self):
        self.ensure_window()
        return self._control(self.win_wechat, searchDepth=30, Name="群成员") is not None

    def _send(self, msg="", clip=False, send_by_clip=False):
        if not msg and not clip:
            raise ValueError("发送内容不能为空")
        edit = self._chat_input()
        if clip:
            edit.Click()
            edit.SendKeys("{Ctrl}v", waitTime=0.05)
        else:
            text = re.sub(r"[\n\r]+", "\n", str(msg))
            # 与 Wechatv4 一致：文本统一走剪贴板，避免中文和特殊字符被按键语法解析。
            self._paste_into(edit, text, clear=True)
        return edit

    def _noiminee(self, notify_all=False, nominee_list=None):
        nominee_list = nominee_list or []
        if not isinstance(nominee_list, list):
            raise TypeError("nominee_list 必须是列表类型")
        mentions = ["@"] if notify_all else [
            "@" + str(item).strip() for item in nominee_list
            if str(item).strip()
        ]
        edit = self._chat_input()
        for mention in mentions:
            self._clip_set(mention)
            edit.SendKeys("{Ctrl}v", waitTime=0.05)
            time.sleep(0.2)
            edit.SendKeys("{Enter}", waitTime=0.05)

    def _ensure_target(self, name, find_match, find_type, check_unique):
        if name is None or not str(name).strip():
            return
        if find_type not in ("联系人", "群聊"):
            raise ValueError("查找类型必须是 联系人 或 群聊")
        expected = str(name).strip()
        exact = _is_exact(find_match)
        current = self.get_session_name()
        is_group = find_type == "群聊"
        if not self._name_matches(current, expected, exact) or self.get_session_type() != is_group:
            expected = self.find_contact(name, find_match, find_type, check_unique)
            exact = True
        current = self.get_session_name()
        if not self._name_matches(current, expected, exact) or self.get_session_type() != is_group:
            raise SendFailed(
                "当前聊天窗口 [{}] 与目标 [{}] 的名称或类型不匹配，已取消发送".format(
                    current, expected
                )
            )

    def send_message(self, name=None, text="", find_match="相等",
                     find_type="群聊", send_by_clip=False, notify_all=False,
                     nominee_list=None, check_unique=True):
        self._ensure_target(name, find_match, find_type, check_unique)
        edit = self._send(msg=text, send_by_clip=send_by_clip)
        self._noiminee(notify_all, nominee_list)
        edit.SendKeys("{Enter}", waitTime=0.05)
        self._check_send_success()
        return True

    def send_clip(self, name=None, find_match="相等", find_type="群聊",
                  check_unique=True):
        self._ensure_target(name, find_match, find_type, check_unique)
        edit = self._send(clip=True)
        edit.SendKeys("{Enter}", waitTime=0.05)
        self._check_send_success()
        return True

    def switch_business(self, business):
        if not business:
            raise ValueError("企业名称不能为空")
        self.ensure_window()
        switch_ele = self._control(
            self.win_wechat, timeout=1, searchDepth=20, Name="我的企业"
        )
        if switch_ele is None:
            raise ElementNotFound("未找到切换企业按钮")
        switch_ele.Click()

        def popup_ready(window):
            marker = self._control(
                window, timeout=0.2, searchDepth=20, Name="创建/加入企业"
            )
            return marker is not None or self._descendant_by_text(
                window, "创建/加入企业", max_depth=20
            ) is not None

        popup = self._uia_window(
            lambda class_name, _: class_name.lower() == "wxworkwindow",
            4.0,
            ready=popup_ready,
            error_message="未找到或无法激活企业列表弹窗的 UIA 树",
        )
        business_ele = self._control(
            popup, timeout=1, searchDepth=20, SubName=business
        )
        if business_ele is None:
            raise ElementNotFound("未找到企业 [{}]".format(business))
        if self._safe_attr(business_ele, "Name") == "corp_name":
            try:
                business_ele.GetSelectionItemPattern().Select()
                return True
            except Exception:
                pass
        business_ele.Click()
        return True

    def get_session_list(self, session_tag, wheel_times=3):
        self.ensure_window()
        try:
            session_group_list = self.win_wechat.Control(
                searchDepth=20, Name="分组"
            ).GetNextSiblingControl().GetChildren()[0].GetChildren()[0]
            session_list = self.win_wechat.Control(
                searchDepth=25, Name="会话"
            ).Control(searchDepth=5, Name="会话").GetChildren()[0].GetChildren()[0]
        except Exception as error:
            raise ElementNotFound("未找到会话分组或会话列表") from error
        if not self._exists(session_group_list, 1) or not self._exists(session_list, 1):
            raise ElementNotFound("未找到会话分组或会话列表")

        expected_tag = str(session_tag or "").strip()
        selected = next((
            item for item in session_group_list.GetChildren()
            if self._without_unread(self._control_text(item)) == expected_tag
        ), None)
        if selected is None:
            raise ElementNotFound("未找到会话分组 [{}]".format(session_tag))
        selected.Click()
        try:
            session_list.WheelUp(wheelTimes=100)
        except Exception:
            pass
        names = []
        unchanged = 0
        for _ in range(100):
            before = len(names)
            for item in session_list.GetChildren():
                raw = self._control_text(item)
                value = self._session_list_name(self._result_display_name(
                    item, raw, name="groupbuddyname"
                ))
                if value and value != "ControlUI" and value not in names:
                    names.append(value)
            unchanged = unchanged + 1 if len(names) == before else 0
            if unchanged >= 3:
                break
            session_list.WheelDown(wheelTimes=int(wheel_times))
            time.sleep(0.15)
        return names

    def _message_root(self, control):
        current = control
        for _ in range(6):
            if self._safe_attr(current, "Name") == "ChatItem":
                return current
            try:
                parent = current.GetParentControl()
            except Exception:
                break
            if parent is None:
                break
            current = parent
        return control

    def _message_type(self, root):
        names = set(
            str(self._safe_attr(item, "Name", "") or "")
            for item in [root] + self._descendants(root, 10)
        )
        mapping = (
            (ClassName.CompoundMessage, "组合消息"),
            (ClassName.File, "文件"),
            (ClassName.Markdown, "Markdown"),
            (ClassName.TencentMeeting, "腾讯会议"),
            (ClassName.LinkCard, "链接卡片"),
            (ClassName.Emotion, "表情包"),
            (ClassName.Video, "视频"),
            (ClassName.WholeItem, "转发消息"),
            (ClassName.Image, "图片"),
        )
        for marker, message_type in mapping:
            if marker in names:
                return message_type
        return "消息"

    @staticmethod
    def _sender_and_business(value):
        sender = str(value or "").strip()
        business = "内部"
        if "<i common/" in sender:
            sender, metadata = sender.split("<i common/", 1)
            labels = re.findall(r"(?<=\[text=')(.+?)(?=')", metadata)
            if labels:
                business = "个微" if labels[0] == "微信" else labels[0]
        elif "@" in sender:
            sender_name, _, label = sender.rpartition("@")
            if sender_name.strip() and label.strip():
                sender = sender_name.strip()
                label = label.strip()
                business = "个微" if label == "微信" else label
        return sender.strip() or None, business

    @classmethod
    def _flat_message_record(cls, value):
        match = re.match(
            r"^(.+?)\s+((?:(?:\d{4}[/-])?\d{1,2}[/-]\d{1,2})\s+"
            r"\d{1,2}:\d{2}(?::\d{2})?)\s*([\s\S]*)$",
            str(value or "").strip(),
        )
        if not match:
            return None
        sender, sender_time, message = match.groups()
        sender, business = cls._sender_and_business(sender)
        sender_time = sender_time.replace("/", "-")
        if sender_time.split(" ", 1)[0].count("-") == 1:
            sender_time = "{}-{}".format(datetime.now().year, sender_time)
        message = re.sub(
            r"\s*[\(（](?:列表项目|列表项)[\)）]\s*$", "", message
        ).rstrip()
        message = re.sub(
            r"(?:^|\s+)\d+\s*人已读\s*$", "", message
        ).rstrip()
        return {
            "sender": sender,
            "business": business,
            "sender_time": sender_time,
            "message_type": "消息",
            "message": message,
        }

    def _message_content(self, root, fallback):
        markers = {
            ClassName.Message, ClassName.CompoundMessage, ClassName.File,
            ClassName.Markdown, ClassName.Image, ClassName.TencentMeeting,
            ClassName.LinkCard, ClassName.Emotion, ClassName.Video,
            ClassName.WholeItem,
        }
        # 先沿用影刀版的 ChatItem 层级取气泡内容，再用名称扫描兜底。
        try:
            children = root.GetChildren()
            candidate = children[-1]
            candidate_children = candidate.GetChildren()
            if self._safe_attr(candidate, "Name") == "chatMessageParent":
                candidate = candidate_children[0].GetChildren()[0]
            elif candidate_children:
                candidate = candidate_children[0]
            nested = candidate.GetChildren()
            if nested and self._safe_attr(nested[0], "Name") in markers:
                return nested[0]
            if self._safe_attr(candidate, "Name") in markers:
                return candidate
        except Exception:
            pass
        for item in self._descendants(root, 10):
            if self._safe_attr(item, "Name") in markers:
                return item
        return fallback

    def _message_values(self, root, sender=None, sender_time=None):
        values = []
        message_type = self._message_type(root)
        for item in [root] + self._descendants(root, 12):
            name = self._safe_attr(item, "Name", "")
            if name == "title":
                message_type = "群公告"
            elif name == "quote_message_sender":
                message_type = "引用"
            value = self._control_text(
                item, include_children=False, fallback_name=False
            ).strip()
            if (
                value
                and value not in (sender, sender_time)
                and (not values or value != values[-1])
            ):
                values.append(value)
        if not values:
            value = self._control_text(root, include_children=False).strip()
            if value and value not in (sender, sender_time):
                values.append(value)
        return values, message_type

    def _message_record(self, region):
        flat_record = self._flat_message_record(
            self._control_text(region, include_children=False)
        )
        if flat_record is not None:
            return flat_record
        root = self._message_root(region)
        sender_control = self._control(
            root, timeout=0.1, searchDepth=12, Name="chatSender"
        )
        time_control = self._control(
            root, timeout=0.1, searchDepth=12, Name="chatSendTime"
        )
        sender = self._normalized_name(self._control_text(sender_control)) or None
        raw_sender_time = self._control_text(time_control) or None
        sender_time = raw_sender_time
        if sender_time:
            sender_time = sender_time.lstrip("[").rstrip("]").replace("/", "-")
            if len(sender_time.split(" ", 1)[0]) < 7:
                sender_time = "{}-{}".format(datetime.now().year, sender_time)
        sender, sender_business = self._sender_and_business(sender)
        all_names = set(
            self._safe_attr(item, "Name", "")
            for item in self._descendants(root, 8)
        )
        business = sender_business
        if "robotIcon" in all_names:
            business = "机器人"
        try:
            direct_children = root.GetChildren()
        except Exception:
            direct_children = []
        if direct_children and self._safe_attr(
            direct_children[-1], "Name"
        ) == "chatMessageParent":
            business = "自己"

        content_root = self._message_content(root, region)
        texts, message_type = self._message_values(
            content_root, sender, raw_sender_time
        )
        if sender is None and not texts:
            return None
        record = {
            "sender": sender,
            "business": business,
            "sender_time": sender_time,
            "message_type": message_type,
        }
        if message_type == "引用":
            if len(texts) > 1:
                record["quote_message_sender"] = texts.pop(0)
                record["quote_message"] = texts.pop(0)
            else:
                record["quote_message_sender"] = texts.pop(0) if texts else ""
                record["quote_message"] = ""
        record["message"] = "\n".join(texts)
        return record

    def get_chat_msg(self, sub_num=0, start_time=None, end_time=None):
        limit = 10000000 if start_time or end_time else int(sub_num or 10000000)
        self.ensure_window()
        try:
            message_list = self.win_wechat.ListControl(
                searchDepth=30, Name="消息"
            )
        except Exception as error:
            raise ElementNotFound("未找到聊天消息列表") from error
        if not self._exists(message_list, 1):
            raise ElementNotFound("未找到聊天消息列表")
        previous_count = -1
        unchanged = 0
        scroll_count = 0
        regions = []
        while scroll_count < 100:
            regions = message_list.GetChildren()
            if start_time and regions:
                try:
                    first = self._message_record(regions[0])
                    first_time = first and first.get("sender_time")
                    if (
                        first_time
                        and _parse_dt(first_time)
                        and _parse_dt(start_time)
                        and _parse_dt(first_time) < _parse_dt(start_time)
                    ):
                        break
                except Exception:
                    pass
            if len(regions) >= limit:
                break
            if len(regions) == previous_count:
                unchanged += 1
                if unchanged >= 3:
                    break
            else:
                unchanged = 0
            previous_count = len(regions)
            try:
                message_list.WheelUp(wheelTimes=10)
            except Exception:
                break
            scroll_count += 1
            time.sleep(0.15)
        records = []
        for region in regions:
            record = self._message_record(region)
            if record is None:
                continue
            if record["sender"] is None:
                if not records:
                    continue
                record["sender"] = records[-1]["sender"]
                record["sender_time"] = records[-1]["sender_time"]
                record["business"] = records[-1]["business"]
            record["message"] = re.sub(
                r"<a __TimeNlpUrlPrefix.+?<u>(.+?)</u></a>",
                r"\1",
                record["message"],
            )
            records.append(record)
        records = records[-limit:]
        if start_time:
            start = _parse_dt(start_time)
            records = [
                item for item in records
                if not start or not _parse_dt(item["sender_time"])
                or _parse_dt(item["sender_time"]) >= start
            ]
        if end_time:
            end = _parse_dt(end_time)
            records = [
                item for item in records
                if not end or not _parse_dt(item["sender_time"])
                or _parse_dt(item["sender_time"]) <= end
            ]
        for _ in range(scroll_count):
            try:
                message_list.WheelDown(wheelTimes=10)
            except Exception:
                break
        return records

    def _check_send_success(self):
        try:
            self._uia_window(
                lambda class_name, title: (
                    class_name == "WeWorkMessageBoxFrame" or title == "提示"
                ),
                0.5,
            )
        except ElementNotFound:
            pass
        desktop = auto.GetRootControl()
        box = self._control(desktop, searchDepth=4, Name="提示")
        if box is None:
            return True
        button = self._control(box, searchDepth=12, Name="确认发送")
        if button is not None:
            button.Click()
        return False

work_wx = WorkWxHandler()
