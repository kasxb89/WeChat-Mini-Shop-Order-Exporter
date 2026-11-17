"""Live DWM window frames, including occluded Chromium windows."""
import ctypes as C
from ctypes import wintypes as W
import threading

_stream=None


class WindowStream:
    def __init__(self,hwnd):
        from windows_capture import WindowsCapture
        from PIL import Image
        self.hwnd=hwnd
        self.ready=threading.Event()
        self.lock=threading.Lock()
        self.image=None
        self.closed=False
        self.capture=WindowsCapture(cursor_capture=False,window_hwnd=hwnd)
        @self.capture.event
        def on_frame_arrived(frame,control):
            image=Image.fromarray(frame.frame_buffer[:,:,:3][:,:,::-1].copy())
            with self.lock:
                self.image=image
            self.ready.set()
        @self.capture.event
        def on_closed():
            self.closed=True
            self.ready.set()
        self.control=self.capture.start_free_threaded()

    def read(self):
        if not self.ready.wait(3) or self.closed:
            raise OSError("微信后台画面暂未就绪，请稍后继续。")
        with self.lock:
            if self.image is None:
                raise OSError("未取得微信后台画面。")
            return self.image.copy()

    def close(self):
        if not self.control.is_finished():
            self.control.stop()


def close_capture():
    global _stream
    previous,_stream=_stream,None
    if previous is not None:
        previous.close()


def capture_window(hwnd,width,height):
    global _stream
    from PIL import Image
    if _stream is None or _stream.hwnd!=hwnd:
        close_capture()
        _stream=WindowStream(hwnd)
    image=_stream.read()
    if image.size==(width,height):
        return image
    # WGC 使用 DWM 可见边界；补回 Win32 透明边框，保持点击坐标一致。
    u=C.WinDLL("user32",use_last_error=True)
    d=C.WinDLL("dwmapi",use_last_error=True)
    u.GetWindowRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
    d.DwmGetWindowAttribute.argtypes=[W.HWND,W.DWORD,C.c_void_p,W.DWORD]
    outer,visible=W.RECT(),W.RECT()
    if not u.GetWindowRect(hwnd,C.byref(outer)) or d.DwmGetWindowAttribute(hwnd,9,C.byref(visible),C.sizeof(visible))!=0:
        raise OSError("无法核对微信后台画面边界。")
    if image.size!=(visible.right-visible.left,visible.bottom-visible.top):
        raise OSError(f"微信后台画面尺寸正在变化，请稍后继续。（{image.size}）")
    canvas=Image.new("RGB",(width,height))
    canvas.paste(image,(visible.left-outer.left,visible.top-outer.top))
    return canvas
