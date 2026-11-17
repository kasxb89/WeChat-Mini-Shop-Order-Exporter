"""Windows 公开控件读取及窗口消息输入，不注入微信。"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import os
from functools import lru_cache
import time

from .bridge import BridgeReader
from .vision import Page, Text, compact, copy_buttons, tracking_copy, wechat_search_box, wechat_search_result, wechat_order_center, read_identifier


@lru_cache(maxsize=1)
def get_bridge():
    return BridgeReader()


def close_bridge():
    if get_bridge.cache_info().currsize:
        get_bridge().close()
        get_bridge.cache_clear()


class Paused(RuntimeError):
    pass


class PageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    path: str
    pid: int = 0

    def label(self):
        kind = "微信小店" if self.title in {"小店", "微信小店"} else "微信搜索页" if self.path.lower().endswith("\\wechatappex.exe") else self.title
        return f"{kind} · 窗口 {self.hwnd}"


def enable_dpi():
    if hasattr(C, "windll"):
        C.windll.user32.SetProcessDpiAwarenessContext(C.c_void_p(-4))


class Desktop:
    CLICK_DELAY = .12
    SCROLL_DELAY = .2
    KEY_DELAY = .08
    BIND_DELAY = .12

    def __init__(self, stop, vision=None, *, background=False):
        from PIL import ImageGrab
        self.grab = ImageGrab.grab
        self.stop = stop
        self.background = background
        self.vision = vision
        self.bridge = get_bridge()
        self.bridge_enabled = True
        self.search_pending = False
        self.search_center_pending = False
        self.metrics = {"bridge_reads": 0, "bridge_ms": 0.0, "ocr_reads": 0, "ocr_ms": 0.0, "copies": 0, "invokes":0}
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        # Qt 的工作线程也明确使用物理像素，避免多屏缩放下命中到另一个位置。
        self.u.SetThreadDpiAwarenessContext.argtypes = [C.c_void_p]
        self.u.SetThreadDpiAwarenessContext.restype = C.c_void_p
        self.u.SetThreadDpiAwarenessContext(C.c_void_p(-4))
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.GetAncestor.argtypes = [W.HWND, W.UINT]
        self.u.GetAncestor.restype = W.HWND
        self.u.SetPhysicalCursorPos.argtypes = [C.c_int, C.c_int]
        self.u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.u.IsWindowVisible.argtypes = [W.HWND]
        self.u.IsWindow.argtypes = [W.HWND]
        self.u.GetWindow.argtypes = [W.HWND, W.UINT]
        self.u.GetWindow.restype = W.HWND
        self.u.GetWindowLongW.argtypes = [W.HWND, C.c_int]
        self.u.GetWindowLongW.restype = C.c_long
        self.u.IsIconic.argtypes = [W.HWND]
        self.u.ShowWindow.argtypes = [W.HWND, C.c_int]
        self.u.SetForegroundWindow.argtypes = [W.HWND]
        self.u.ScreenToClient.argtypes = [W.HWND, C.POINTER(W.POINT)]
        self.u.ChildWindowFromPointEx.argtypes = [W.HWND, W.POINT, W.UINT]
        self.u.ChildWindowFromPointEx.restype = W.HWND
        self.u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.u.PostMessageW.restype = W.BOOL
        self.k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.k.OpenProcess.restype = W.HANDLE
        self.k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.GlobalLock.argtypes = [W.HGLOBAL]
        self.k.GlobalLock.restype = C.c_void_p
        self.k.GlobalUnlock.argtypes = [W.HGLOBAL]
        self.k.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
        self.k.GlobalAlloc.restype = W.HGLOBAL
        self.k.GlobalFree.argtypes = [W.HGLOBAL]
        self.u.GetClipboardData.argtypes = [W.UINT]
        self.u.GetClipboardData.restype = W.HANDLE
        self.u.OpenClipboard.argtypes = [W.HWND]
        self.u.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
        self.u.SetClipboardData.restype = W.HANDLE
        self.u.GetClipboardSequenceNumber.restype = W.DWORD
        self.hwnd = 0
        self.pid = 0
        self.path = ""
        self.ocr_cache = OrderedDict()

    def check_stop(self):
        if self.stop.is_set() or self.u.GetAsyncKeyState(0x77) & 0x8001:  # F8
            raise Paused("已暂停，采集进度已保存。")

    def wait(self, seconds: float):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check_stop()
            time.sleep(min(.08, max(0, until - time.monotonic())))

    def process_path(self, hwnd: int) -> str:
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
        process = self.k.OpenProcess(0x1000, False, pid.value)
        if not process:
            return ""
        try:
            path = C.create_unicode_buffer(32768)
            size = W.DWORD(len(path))
            return path.value if self.k.QueryFullProcessImageNameW(process, 0, path, C.byref(size)) else ""
        finally:
            self.k.CloseHandle(process)

    def windows(self) -> list[Window]:
        result = []
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

        @callback_type
        def enum(hwnd, _):
            if not self.u.IsWindowVisible(hwnd):
                return True
            title = C.create_unicode_buffer(512)
            self.u.GetWindowTextW(hwnd, title, len(title))
            if title.value in {"微信", "WeChat", "Weixin", "小店", "微信小店"}:
                path = self.process_path(hwnd)
                lower = path.lower().replace("/", "\\")
                if ((lower.endswith(("\\weixin.exe", "\\wechat.exe")) and title.value in {"微信", "WeChat", "Weixin"}
                     and not self.u.GetWindowLongW(hwnd,-20) & (0x80 | 0x08000000))
                        or (lower.endswith("\\wechatappex.exe") and "\\xwechat\\" in lower and title.value in {"小店", "微信小店", "微信"})):
                    pid = W.DWORD()
                    self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
                    result.append(Window(int(hwnd), title.value, path, pid.value))
            return True
        self.u.EnumWindows(enum, 0)
        return result

    def active_window(self, windows=None):
        front = self.u.GetAncestor(self.u.GetForegroundWindow(), 2)
        return next((w for w in (windows if windows is not None else self.windows()) if w.hwnd == front), None)

    def begin_preparation(self):
        previous = int(self.u.GetForegroundWindow() or 0)
        self.background = False
        return previous

    def end_preparation(self, previous):
        try:
            front = self.u.GetForegroundWindow()
            path = self.process_path(front).lower() if front else ""
            # 只从正在准备的微信切回；用户已切到其他软件时保持其选择。
            if previous and self.u.IsWindow(previous) and (
                    path.endswith(("\\weixin.exe", "\\wechat.exe"))
                    or (path.endswith("\\wechatappex.exe") and "\\xwechat\\" in path)):
                self.u.SetForegroundWindow(previous)
                self.wait(.15)
                owner=W.DWORD()
                self.u.GetWindowThreadProcessId(previous,C.byref(owner))
                if owner.value==os.getpid() and self.u.GetForegroundWindow()==previous:
                    # 在自己的标题栏完成一次输入交还，撤销微信刚才取得的前台输入资格。
                    rect=W.RECT()
                    if self.u.GetWindowRect(previous,C.byref(rect)):
                        self.u.SetPhysicalCursorPos((rect.left+rect.right)//2,rect.top+12)
                        self.u.mouse_event(0x0002,0,0,0,0)
                        self.u.mouse_event(0x0004,0,0,0,0)
                        self.wait(.08)
        finally:
            self.background = True

    def bind(self, window: Window):
        self.check_stop()
        if self.process_path(window.hwnd).lower() != window.path.lower():
            raise PageError("微信窗口已变化，请重新检测。")
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(window.hwnd, C.byref(pid))
        if window.pid and window.pid != pid.value:
            raise PageError("所选微信已重新启动，请重新检测并选择窗口。")
        self.hwnd, self.path, self.pid = window.hwnd, window.path, pid.value
        self.search_pending = False
        self.search_center_pending = False
        if not self.background:
            if self.u.IsIconic(self.hwnd):
                self.u.ShowWindow(self.hwnd, 9)
            self.u.SetForegroundWindow(self.hwnd)
        self.wait(self.BIND_DELAY)
        self.guard()

    def guard(self):
        self.check_stop()
        if not self.hwnd or not self.u.IsWindow(self.hwnd):
            raise Paused("微信小店窗口已关闭，采集已暂停。")
        if self.background:
            if self.u.IsIconic(self.hwnd):
                raise Paused("后台采集可被其他窗口遮住；请保持微信小店打开，不要最小化。")
            return
        for attempt in range(2):
            front = self.u.GetForegroundWindow()
            if front == self.hwnd or self.u.GetAncestor(front, 3) == self.u.GetAncestor(self.hwnd, 3):
                return
            if not attempt:
                self.wait(.2)
        title = C.create_unicode_buffer(256)
        self.u.GetWindowTextW(front, title, len(title))
        raise Paused(f"当前前台是「{title.value or '其他窗口'}」，采集已暂停；回到导出器点击「继续上次」。")

    def rect(self):
        rect = W.RECT()
        if not self.u.GetWindowRect(self.hwnd, C.byref(rect)):
            raise PageError("无法读取微信小店窗口位置。")
        return rect.left, rect.top, rect.right, rect.bottom

    def ocr(self, image):
        if self.vision is None:
            from .vision import get_vision
            self.vision = get_vision()
        started = time.monotonic()
        texts = self.vision.read(image)
        self.metrics["ocr_reads"] += 1
        self.metrics["ocr_ms"] += (time.monotonic() - started) * 1000
        return texts

    def capture(self, rect):
        if not self.background:
            return self.grab(bbox=rect, all_screens=True)
        # 只取指定窗口，不能退回桌面截图，否则会读到用户正在办公的应用。
        from .window_capture import capture_window
        try:
            return capture_window(self.hwnd, rect[2]-rect[0], rect[3]-rect[1])
        except OSError as error:
            raise PageError(str(error)) from error

    def read_visual(self, page):
        """页面已变化但公开控件尚未更新时，仅用当前窗口图像确认展开状态。"""
        self.guard()
        key = page.hwnd, page.rect, hashlib.blake2b(page.image.tobytes(),digest_size=16).digest()
        if key not in self.ocr_cache:
            self.ocr_cache[key] = self.ocr(page.image)
            if len(self.ocr_cache)>12:
                self.ocr_cache.popitem(last=False)
        return Page(page.hwnd,page.rect,self.ocr_cache[key],page.image)

    def background_page(self,page,uia_page=None):
        # 微信后台的可访问控件有时停留在旧页面，以真实窗口图像决定当前页面。
        visual=self.read_visual(page)
        visual.verified_identifiers={}
        candidates=Page(visual.hwnd,visual.rect,[t for t in visual.texts if t.confidence>=.95],
                        visual.image,source="uia")
        for kind in ("order","tracking"):
            value=read_identifier(candidates,kind)
            if not value:
                continue
            if uia_page is not None and read_identifier(uia_page,kind)==value:
                visual.verified_identifiers[kind]=value
                continue
            target=next((t for t in visual.texts if value in compact(t.text)),None)
            if target is None:
                continue
            top=max(0,int(target.box[1])-16)
            bottom=min(page.height,int(target.box[3])+16)
            strip=page.image.crop((0,top,page.width,bottom))
            strip=strip.resize((strip.width*2,strip.height*2))
            checked=self.read_visual(Page(page.hwnd,(0,0,*strip.size),[],strip))
            checked.source="uia"  # 复用字段解析；只有两次完整值一致后才记为已核对。
            if read_identifier(checked,kind)==value:
                visual.verified_identifiers[kind]=value
        return visual

    def read(self) -> Page:
        self.guard()
        rect = self.rect()
        snapshot = self.bridge.read(self.hwnd, self.pid) if self.bridge_enabled else None
        if self.bridge_enabled and snapshot is None:
            # 一次通道故障后本轮直接使用图像，不在每一帧重复等进程超时。
            self.bridge_enabled = False
        image = self.capture(rect)
        self.guard()
        if snapshot and tuple(snapshot["rect"]) == rect:
            self.metrics["bridge_reads"] += 1
            self.metrics["bridge_ms"] += snapshot["elapsed_ms"]
            texts, seen = [], set()
            for node in snapshot["nodes"]:
                value = node["text"].strip()
                if compact(value) in {"最小化", "最大化", "还原", "恢复", "关闭", "系统菜单", "菜单", "松开使用小店打开"}:
                    continue
                left, top, right, bottom = node["rect"]
                box = (max(0, left-rect[0]), max(0, top-rect[1]),
                       min(rect[2], right)-rect[0], min(rect[3], bottom)-rect[1])
                key = value, box
                if key not in seen:
                    texts.append(Text(value, box, role=node.get("role", "text")))
                    seen.add(key)
            page = Page(self.hwnd, rect, texts, image, source="uia")
            if self.background:
                return self.background_page(page,page)
            if (texts and (not self.search_pending or wechat_search_result(page) is not None)
                    and (not self.search_center_pending or wechat_order_center(page) is not None)):
                return page
        if self.background:
            return self.background_page(Page(self.hwnd,rect,[],image))
        key = self.hwnd, rect, hashlib.blake2b(image.tobytes(), digest_size=16).digest()
        if key not in self.ocr_cache:
            self.ocr_cache[key] = self.ocr(image)
            if len(self.ocr_cache) > 12:
                self.ocr_cache.popitem(last=False)
        self.ocr_cache.move_to_end(key)
        return Page(self.hwnd, rect, self.ocr_cache[key], image)

    def click(self, page: Page, x: float, y: float):
        self.guard()
        if page.hwnd != self.hwnd or page.rect != self.rect():
            raise Paused("窗口位置或大小发生变化，已暂停；继续后将重新识别。")
        if page.source == "uia":
            label = next((t for t in page.texts if abs(t.x-x)<1 and abs(t.y-y)<1), None)
            if label is not None:
                absolute = [label.box[0]+page.rect[0], label.box[1]+page.rect[1],
                            label.box[2]+page.rect[0], label.box[3]+page.rect[1]]
                invoked = (self.bridge.invoke(self.hwnd, self.pid, label.text, absolute)
                           if not self.background and self.bridge_enabled and label.role in {"button", "hyperlink", "listitem"} else False)
                if invoked is not False:
                    if invoked:
                        self.metrics["invokes"] += 1
                    # 超时意味着结果未知；已有页面等待会观察结果，不重复点第二次。
                    self.wait(self.CLICK_DELAY)
                    return
                # 回购推荐异步插入会移动订单；点击前按控件名称实时取坐标。
                previous = None
                target = None
                for sample in range(5):
                    fresh = self.read()
                    matches = [t for t in fresh.texts if t.text == label.text]
                    target = min(matches, key=lambda t: abs(t.y-y)+abs(t.x-x), default=None)
                    if target is not None and target.box == previous:
                        break
                    previous = target.box if target is not None else None
                    self.wait(.08)
                if target is None:
                    raise PageError(f"当前页面未找到「{label.text[:24]}」，可继续后重新定位。")
                page, x, y = fresh, target.x, target.y
        if not (0 <= x < page.width and 0 <= y < page.height):
            raise PageError("页面目标超出了微信窗口。")
        if self.background:
            hwnd, point = self.input_target(page, x, y)
            position = self.packed_point(point.x, point.y)
            self.post(hwnd, 0x0200, 0, position)  # WM_MOUSEMOVE
            self.post(hwnd, 0x0201, 1, position)  # WM_LBUTTONDOWN
            self.post(hwnd, 0x0202, 0, position)  # WM_LBUTTONUP
            self.wait(self.CLICK_DELAY)
            return
        self.u.SetPhysicalCursorPos(round(page.rect[0] + x), round(page.rect[1] + y))
        self.u.mouse_event(0x0002, 0, 0, 0, 0)
        self.u.mouse_event(0x0004, 0, 0, 0, 0)
        self.wait(self.CLICK_DELAY)

    def key(self, key: int, modifiers=()):
        self.guard()
        if self.background:
            raise PageError("后台采集不使用全局键盘，请先打开微信小店订单中心。")
        for modifier in modifiers:
            self.u.keybd_event(modifier, 0, 0, 0)
        self.u.keybd_event(key, 0, 0, 0)
        self.u.keybd_event(key, 0, 2, 0)
        for modifier in reversed(modifiers):
            self.u.keybd_event(modifier, 0, 2, 0)
        self.wait(self.KEY_DELAY)

    def scroll(self, page: Page, *, down: bool):
        self.guard()
        if page.rect != self.rect():
            raise Paused("窗口位置发生变化，已暂停。")
        # 灰色外边距属于纵向订单列表，避开会吞滚轮的横向回购推荐。
        left=.07 if self.background else .028
        point = W.POINT(page.rect[0] + round(page.width * left), page.rect[1] + round(page.height * .65))
        if self.background:
            hwnd, client = self.input_target(page, page.width*left, page.height*.65)
            self.post(hwnd,0x0200,0,self.packed_point(client.x,client.y))
            key,scan=(0x28,0x50) if down else (0x26,0x48)
            data=1 | (scan<<16) | (1<<24)
            for _ in range(4):
                self.post(hwnd,0x0100,key,data)
                self.post(hwnd,0x0101,key,data | 0xC0000000)
            self.wait(self.SCROLL_DELAY)
            return
        self.u.SetPhysicalCursorPos(point.x, point.y)
        # 保留足够的屏幕重叠，订单标题靠近屏幕底部时不会被整页越过。
        self.u.mouse_event(0x0800, 0, 0, -2 * 120 if down else 2 * 120, 0)
        self.wait(self.SCROLL_DELAY)

    @staticmethod
    def packed_point(x, y):
        return (round(x) & 0xffff) | ((round(y) & 0xffff) << 16)

    def input_target(self, page, x, y):
        """在已绑定窗口的子树命中，不使用会被前台遮挡影响的 WindowFromPoint。"""
        screen = W.POINT(round(page.rect[0]+x), round(page.rect[1]+y))
        hwnd = self.hwnd
        while True:
            point = W.POINT(screen.x, screen.y)
            if not self.u.ScreenToClient(hwnd, C.byref(point)):
                raise PageError("无法定位后台窗口输入区域。")
            # Chromium 的输入子窗带 WS_EX_TRANSPARENT，仍需直接向它投递消息。
            child = self.u.ChildWindowFromPointEx(hwnd, point, 1)
            if not child or child == hwnd:
                return hwnd, point
            hwnd = child

    def post(self, hwnd, message, wparam, lparam):
        if not self.u.PostMessageW(hwnd, message, wparam, lparam):
            raise PageError("微信未接受后台窗口消息，采集已暂停。")

    def copy(self, page: Page, button) -> str:
        if self.background:
            raise PageError("当前页面未提供完整编号。后台采集已暂停，避免占用你的剪贴板。")
        self.metrics["copies"] += 1
        is_order = page.has("订单编号", "订单号")
        if page.source == "uia":
            # 缺少完整字段时才点击复制；通过同一桥接刷新按钮，避免再跑 OCR。
            page = self.read()
            targets = copy_buttons(page) if is_order else [tracking_copy(page)]
            button = min((t for t in targets if t is not None),
                         key=lambda t: abs(t.y-button.y), default=None)
            if button is None:
                raise PageError("当前页没有找到编号复制入口，请稍后继续。")
        else:
            page, button = self.refresh_copy(page, button, is_order)
        before = self.u.GetClipboardSequenceNumber()
        self.click(page, button.x, button.y)
        until = time.monotonic() + 4
        while time.monotonic() < until:
            self.guard()
            if self.u.GetClipboardSequenceNumber() != before and self.u.OpenClipboard(None):
                try:
                    handle = self.u.GetClipboardData(13)
                    if handle:
                        pointer = self.k.GlobalLock(handle)
                        if pointer:
                            try:
                                return C.wstring_at(pointer)
                            finally:
                                self.k.GlobalUnlock(handle)
                finally:
                    self.u.CloseClipboard()
            self.wait(.1)
        raise PageError("本次点击没有取得新的复制结果，已停止以免使用旧的剪贴板编号。")

    def refresh_copy(self, page, button, is_order):
        # 评价区和地图异步载入会推移编号行；只读当前按钮附近，不重复 OCR 整页。
        self.guard()
        top = max(0, round(button.y) - 100)
        bottom = min(page.height, round(button.y) + 100)
        image = self.grab(bbox=(page.rect[0], page.rect[1]+top,
                                page.rect[2], page.rect[1]+bottom), all_screens=True)
        region = Page(page.hwnd, (0, 0, *image.size), self.ocr(image), image)
        target = (min(copy_buttons(region), key=lambda t: abs(t.y+top-button.y), default=None)
                  if is_order else tracking_copy(region))
        if target:
            button = Text(target.text, (target.box[0], target.box[1]+top, target.box[2], target.box[3]+top), target.confidence)
            texts = [Text(t.text, (t.box[0], t.box[1]+top, t.box[2], t.box[3]+top), t.confidence) for t in region.texts]
            page.texts = [t for t in page.texts if not (top <= t.y < bottom)] + texts
            if page.image is not None:
                page.image.paste(image, (0, top))
        return page, button

    def search_wechat(self, window: Window):
        """复用常驻桥接定位搜索框，不为一次搜索另起 PowerShell。"""
        if self.background:
            raise PageError("微信搜索需要先完成自动准备。")
        self.bind(window)
        page = self.read()
        target = wechat_search_box(page)
        if target is None:
            raise PageError("所选窗口未找到左上角搜索框，请先打开这个微信的主聊天窗口。")
        self.click(page, target.x, target.y)
        self.key(0x41, (0x11,))
        self.paste_text("微信小店")
        self.search_pending = True
        self.wait(.2)

    def paste_text(self, value: str):
        self.guard()
        if self.background:
            raise PageError("后台采集不修改剪贴板，请先打开微信小店订单中心。")
        raw = (value + "\0").encode("utf-16le")
        memory = self.k.GlobalAlloc(2, len(raw))
        if not memory:
            raise PageError("无法准备微信搜索文字。")
        transferred = False
        try:
            pointer = self.k.GlobalLock(memory)
            if not pointer:
                raise PageError("无法准备微信搜索文字。")
            C.memmove(pointer, raw, len(raw))
            self.k.GlobalUnlock(memory)
            if not self.u.OpenClipboard(None):
                raise PageError("剪贴板正被占用，请稍后继续。")
            try:
                self.u.EmptyClipboard()
                transferred = bool(self.u.SetClipboardData(13, memory))
                if not transferred:
                    raise PageError("无法填写微信搜索关键词。")
            finally:
                self.u.CloseClipboard()
        finally:
            if not transferred:
                self.k.GlobalFree(memory)
        self.key(0x56, (0x11,))
