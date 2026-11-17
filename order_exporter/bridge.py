"""Read and invoke a named HWND's public UIA controls via an in-memory worker.

All rectangles are [left, top, right, bottom] in absolute screen pixels.
Same-PID owned popups are included only where they intersect the named window.
Only public UIA names are read; no control values, files or logs are written.
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import queue
import subprocess
import sys
import threading
import time


_MAX_RESPONSE_BYTES = 4_000_000
_ROLES = frozenset({"text", "button", "hyperlink", "edit", "listitem"})

_HOST = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase
$assemblies = @(
    [System.Windows.Automation.AutomationElement].Assembly.Location,
    [System.Windows.Automation.ControlType].Assembly.Location,
    [System.Windows.Rect].Assembly.Location
) | Select-Object -Unique
Add-Type -ReferencedAssemblies $assemblies -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;

public static class OrderBridgeSnapshot
{
    [StructLayout(LayoutKind.Sequential)]
    private struct WindowRect { public int Left, Top, Right, Bottom; }

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")]
    private static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    private delegate bool EnumWindowProc(IntPtr hwnd, IntPtr parameter);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EnumWindows(EnumWindowProc callback, IntPtr parameter);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetWindowRect(IntPtr hwnd, out WindowRect rect);
    [DllImport("user32.dll")]
    private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);

    private static bool Window(IntPtr hwnd, uint expectedPid, out WindowRect rect)
    {
        rect = new WindowRect();
        uint pid;
        return hwnd != IntPtr.Zero && expectedPid != 0 && IsWindow(hwnd)
            && IsWindowVisible(hwnd) && !IsIconic(hwnd)
            && GetWindowThreadProcessId(hwnd, out pid) != 0 && pid == expectedPid
            && GetWindowRect(hwnd, out rect)
            && rect.Right > rect.Left && rect.Bottom > rect.Top;
    }

    private static bool Finite(double value)
    {
        return !Double.IsNaN(value) && !Double.IsInfinity(value);
    }

    private static bool OwnedBy(IntPtr popup, IntPtr root)
    {
        if (GetAncestor(popup, 3) != GetAncestor(root, 3)) return false;
        for (int i = 0; i < 32; i++)
        {
            popup = GetWindow(popup, 4);
            if (popup == IntPtr.Zero) return false;
            if (popup == root) return true;
        }
        return false;
    }

    private static List<IntPtr> Roots(IntPtr hwnd, uint expectedPid, WindowRect bounds)
    {
        var roots = new List<IntPtr> { hwnd };
        EnumWindows(delegate(IntPtr candidate, IntPtr unused) {
            WindowRect popup;
            if (candidate != hwnd && Window(candidate, expectedPid, out popup)
                && OwnedBy(candidate, hwnd)
                && popup.Right > bounds.Left && popup.Left < bounds.Right
                && popup.Bottom > bounds.Top && popup.Top < bounds.Bottom) roots.Add(candidate);
            return true;
        }, IntPtr.Zero);
        return roots;
    }

    private static CacheRequest Properties(bool full)
    {
        var cache = new CacheRequest();
        cache.AutomationElementMode = full ? AutomationElementMode.Full : AutomationElementMode.None;
        cache.TreeScope = TreeScope.Element;
        cache.TreeFilter = Automation.RawViewCondition;
        cache.Add(AutomationElement.NameProperty);
        cache.Add(AutomationElement.ControlTypeProperty);
        cache.Add(AutomationElement.BoundingRectangleProperty);
        cache.Add(AutomationElement.IsOffscreenProperty);
        cache.Add(AutomationElement.IsPasswordProperty);
        cache.Add(AutomationElement.IsEnabledProperty);
        if (!full) cache.Add(SelectionItemPattern.IsSelectedProperty);
        return cache;
    }

    private static Condition Types()
    {
        return new OrCondition(
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit),
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.ListItem));
    }

    private static bool Visible(Rect r, WindowRect bounds)
    {
        return !r.IsEmpty && Finite(r.Left) && Finite(r.Top) && Finite(r.Right) && Finite(r.Bottom)
            && r.Width > 0 && r.Height > 0 && r.Right > bounds.Left && r.Left < bounds.Right
            && r.Bottom > bounds.Top && r.Top < bounds.Bottom;
    }

    private static bool InRoot(AutomationElement element, AutomationElement root)
    {
        // Walk identity only, never names or desktop-wide searches. A vanished
        // or reparented element must not be invoked in another HWND's subtree.
        for (int i = 0; element != null && i < 64; i++)
        {
            if (Automation.Compare(element, root)) return true;
            element = TreeWalker.RawViewWalker.GetParent(element);
        }
        return false;
    }

    private static bool SmallCover(Rect parent, Rect target, WindowRect bounds, ControlType type)
    {
        return type != ControlType.Window && type != ControlType.Document && type != ControlType.List
            && type != ControlType.Tree && type != ControlType.DataGrid && type != ControlType.Table
            && Visible(parent, bounds) && parent.Left <= target.Left && parent.Top <= target.Top
            && parent.Right >= target.Right && parent.Bottom >= target.Bottom
            && parent.Width <= bounds.Right - bounds.Left
            && parent.Height <= Math.Min(450, (bounds.Bottom - bounds.Top) * .65);
    }

    public static bool Invoke(long handle, uint expectedPid, string text, double[] hint)
    {
        if (String.IsNullOrWhiteSpace(text) || hint == null || hint.Length != 4) return false;
        foreach (double v in hint) if (!Finite(v)) return false;
        if (hint[2] <= hint[0] || hint[3] <= hint[1]) return false;
        IntPtr previousDpi = IntPtr.Zero;
        bool dispatched = false;
        try
        {
            try { previousDpi = SetThreadDpiAwarenessContext(new IntPtr(-4)); }
            catch (EntryPointNotFoundException) { }
            IntPtr hwnd = new IntPtr(handle);
            WindowRect bounds;
            if (!Window(hwnd, expectedPid, out bounds)) return false;
            if (!Visible(new Rect(hint[0], hint[1], hint[2] - hint[0], hint[3] - hint[1]), bounds)) return false;
            AutomationElement target = null, targetRoot = null;
            IntPtr targetScope = IntPtr.Zero;
            double best = Double.PositiveInfinity;
            bool ambiguous = false;
            var cache = Properties(true);
            var condition = new AndCondition(Types(),
                new PropertyCondition(AutomationElement.NameProperty, text),
                new PropertyCondition(AutomationElement.IsOffscreenProperty, false),
                new PropertyCondition(AutomationElement.IsPasswordProperty, false),
                new PropertyCondition(AutomationElement.IsEnabledProperty, true));
            foreach (IntPtr scope in Roots(hwnd, expectedPid, bounds))
            {
                WindowRect scopeBounds;
                if (scope != hwnd && (!Window(scope, expectedPid, out scopeBounds) || !OwnedBy(scope, hwnd))) return false;
                AutomationElement root = AutomationElement.FromHandle(scope);
                if (root == null) return false;
                AutomationElementCollection matches;
                using (cache.Activate()) { matches = root.FindAll(TreeScope.Descendants, condition); }
                foreach (AutomationElement element in matches)
                {
                    var info = element.Cached;
                    if (info.Name != text || info.IsOffscreen || info.IsPassword || !info.IsEnabled) continue;
                    Rect r = info.BoundingRectangle;
                    if (!Visible(r, bounds)) continue;
                    double score = Math.Pow(r.Left - hint[0], 2) + Math.Pow(r.Top - hint[1], 2)
                        + Math.Pow(r.Right - hint[2], 2) + Math.Pow(r.Bottom - hint[3], 2);
                    if (score < best - 1) {
                        target = element; targetRoot = root; targetScope = scope; best = score; ambiguous = false;
                    }
                    else if (Math.Abs(score - best) <= 1 && !Automation.Compare(element, target)) ambiguous = true;
                }
            }
            if (target == null || ambiguous) return false;
            var targetInfo = target.Current;
            if (targetInfo.Name != text || targetInfo.IsOffscreen || targetInfo.IsPassword || !targetInfo.IsEnabled
                || !Visible(targetInfo.BoundingRectangle, bounds)) return false;
            AutomationElement callable = target;
            for (int up = 0; up <= 3 && callable != null; up++)
            {
                if (Automation.Compare(callable, targetRoot)) return false;
                var info = callable.Current;
                if (info.IsOffscreen || info.IsPassword || !info.IsEnabled || !Visible(info.BoundingRectangle, bounds)) return false;
                if (up > 0 && !SmallCover(info.BoundingRectangle, targetInfo.BoundingRectangle, bounds, info.ControlType)) return false;
                object pattern;
                if (callable.TryGetCurrentPattern(InvokePattern.Pattern, out pattern))
                {
                    WindowRect after, popup;
                    if (!Window(hwnd, expectedPid, out after) || bounds.Left != after.Left || bounds.Top != after.Top
                        || bounds.Right != after.Right || bounds.Bottom != after.Bottom
                        || (targetScope != hwnd && (!Window(targetScope, expectedPid, out popup) || !OwnedBy(targetScope, hwnd)))
                        || !InRoot(callable, targetRoot) || !InRoot(target, targetRoot)) return false;
                    var latest = target.Current;
                    if (latest.Name != text || latest.IsOffscreen || latest.IsPassword || !latest.IsEnabled
                        || !Visible(latest.BoundingRectangle, bounds)) return false;
                    if (up > 0) {
                        var ancestor = callable.Current;
                        if (!SmallCover(ancestor.BoundingRectangle, latest.BoundingRectangle, bounds, ancestor.ControlType)) return false;
                    }
                    var invoke = pattern as InvokePattern;
                    if (invoke == null) return false;
                    // No coordinate click and no automatic retry after this call.
                    dispatched = true;
                    invoke.Invoke();
                    return true;
                }
                if (up == 3) break;
                callable = TreeWalker.RawViewWalker.GetParent(callable);
            }
            return false;
        }
        catch
        {
            if (dispatched) throw;
            return false;
        }
        finally
        {
            if (previousDpi != IntPtr.Zero) SetThreadDpiAwarenessContext(previousDpi);
        }
    }

    public static Dictionary<string, object> Read(long handle, uint expectedPid)
    {
        IntPtr previousDpi = IntPtr.Zero;
        try
        {
            // UIA bounding rectangles are physical screen pixels, including on
            // scaled/negative-coordinate monitors; match Win32's root bounds.
            try { previousDpi = SetThreadDpiAwarenessContext(new IntPtr(-4)); }
            catch (EntryPointNotFoundException) { }
            IntPtr hwnd = new IntPtr(handle);
            WindowRect bounds;
            if (!Window(hwnd, expectedPid, out bounds)) return null;

            var cache = Properties(false);
            var types = new AndCondition(Types(),
                new PropertyCondition(AutomationElement.IsOffscreenProperty, false),
                new PropertyCondition(AutomationElement.IsPasswordProperty, false));
            var nodes = new List<Dictionary<string, object>>();
            foreach (IntPtr scope in Roots(hwnd, expectedPid, bounds))
            {
                WindowRect popupBefore;
                if (scope != hwnd && (!Window(scope, expectedPid, out popupBefore) || !OwnedBy(scope, hwnd))) return null;
                AutomationElement root = AutomationElement.FromHandle(scope);
                if (root == null) return null;
                AutomationElementCollection elements;
                using (cache.Activate())
                {
                    // Never use Desktop RootElement or filter renderer child PIDs.
                    elements = root.FindAll(TreeScope.Descendants, types);
                }
                foreach (AutomationElement element in elements)
                {
                    try
                    {
                        var info = element.Cached;
                        if (info.IsOffscreen || info.IsPassword || String.IsNullOrWhiteSpace(info.Name)) continue;
                        Rect r = info.BoundingRectangle;
                        if (!Visible(r, bounds)) continue;
                        ControlType type = info.ControlType;
                        string role = type == ControlType.Text ? "text"
                            : type == ControlType.Button ? "button"
                            : type == ControlType.Hyperlink ? "hyperlink"
                            : type == ControlType.Edit ? "edit"
                            : type == ControlType.ListItem ? "listitem" : null;
                        if (role == null) continue;
                        var node = new Dictionary<string, object> {
                            { "text", info.Name.Trim() }, { "role", role },
                            { "rect", new double[] { r.Left, r.Top, r.Right, r.Bottom } }
                        };
                        object selected = element.GetCachedPropertyValue(SelectionItemPattern.IsSelectedProperty, true);
                        if (selected is bool) node["selected"] = (bool)selected;
                        // Disabled controls may still contain visible order text.
                        nodes.Add(node);
                    }
                    catch (ElementNotAvailableException) { }
                    catch (InvalidOperationException) { }
                    catch (COMException) { }
                }
                WindowRect popupAfter;
                if (scope != hwnd && (!Window(scope, expectedPid, out popupAfter) || !OwnedBy(scope, hwnd))) return null;
            }
            WindowRect after;
            if (!Window(hwnd, expectedPid, out after)
                || bounds.Left != after.Left || bounds.Top != after.Top
                || bounds.Right != after.Right || bounds.Bottom != after.Bottom) return null;
            return new Dictionary<string, object> {
                { "rect", new int[] { bounds.Left, bounds.Top, bounds.Right, bounds.Bottom } },
                { "nodes", nodes.ToArray() }
            };
        }
        finally
        {
            if (previousDpi != IntPtr.Zero) SetThreadDpiAwarenessContext(previousDpi);
        }
    }
}
'@
while ($null -ne ($line = [Console]::In.ReadLine())) {
    $requestId = $null
    $invoking = $false
    try {
        $p = $line | ConvertFrom-Json
        $requestId = $p.request_id
        $invoking = $p.action -eq 'invoke'
        if ($invoking) {
            $result = [OrderBridgeSnapshot]::Invoke([long]$p.hwnd, [uint32]$p.pid, [string]$p.text, [double[]]$p.rect)
        } elseif (!$p.action -or $p.action -eq 'read') {
            $result = [OrderBridgeSnapshot]::Read([long]$p.hwnd, [uint32]$p.pid)
        } else { $result = $null }
        [Console]::Out.WriteLine((@{request_id=$requestId; result=$result} | ConvertTo-Json -Depth 7 -Compress))
    } catch {
        # An exception may happen after Invoke was dispatched: result unknown.
        [Console]::Out.WriteLine((@{request_id=$requestId; result=$null} | ConvertTo-Json -Compress))
    }
}
'''


class BridgeReader:
    """A reusable worker; unavailable providers or failed reads return None.

    The first request in each worker has a six-second budget for .NET startup;
    warm requests have two seconds. ``elapsed_ms`` includes this startup and
    pipe transport. ``close`` releases the worker and permits a later restart.
    Invoke returns True for a completed call, False when no call was dispatched,
    and None when the result is unknown. An unknown invocation must not be
    retried or followed immediately by a coordinate click.
    """

    host = _HOST

    def __init__(self, timeout: float = 2.0, startup_timeout: float = 6.0):
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._process = None
        self._responses = None
        self._serial = 0

    def _command(self):
        # The C# host can exceed Windows' command-line limit when UTF-16/base64
        # encoded. Only static source is compressed here; names stay in stdin.
        packed = base64.b64encode(gzip.compress(self.host.encode("utf-8"), mtime=0)).decode("ascii")
        bootstrap = r'''
$bytes = [Convert]::FromBase64String('__HOST__')
$stream = New-Object IO.MemoryStream(,$bytes)
$gzip = New-Object IO.Compression.GZipStream($stream,[IO.Compression.CompressionMode]::Decompress)
$reader = New-Object IO.StreamReader($gzip,[Text.Encoding]::UTF8)
$source = $reader.ReadToEnd()
$reader.Dispose()
& ([ScriptBlock]::Create($source))
'''.replace("__HOST__", packed)
        encoded = base64.b64encode(bootstrap.encode("utf-16le")).decode("ascii")
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand", encoded]

    def _start(self):
        process = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        responses = queue.Queue(maxsize=2)

        def receive():
            try:
                while True:
                    line = process.stdout.readline(_MAX_RESPONSE_BYTES + 1)
                    if not line or len(line) > _MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
                        break
                    responses.put_nowait(line)
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try:
                    responses.put_nowait(None)
                except queue.Full:
                    pass

        self._process, self._responses = process, responses
        threading.Thread(target=receive, daemon=True).start()

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            self._responses = None
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=.25)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError, AttributeError):
                        pass

    @staticmethod
    def _rect(value):
        if (not isinstance(value, list) or len(value) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
                or value[2] <= value[0] or value[3] <= value[1]):
            raise ValueError("Invalid UIA rectangle")
        return value

    def _snapshot(self, value, elapsed):
        if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
            raise ValueError("Invalid UIA snapshot")
        bounds = self._rect(value.get("rect"))
        nodes = []
        for node in value["nodes"]:
            if (not isinstance(node, dict) or not isinstance(node.get("text"), str)
                    or not node["text"].strip() or node.get("role") not in _ROLES
                    or ("selected" in node and type(node["selected"]) is not bool)):
                raise ValueError("Invalid UIA node")
            rect = self._rect(node.get("rect"))
            if (rect[2] <= bounds[0] or rect[0] >= bounds[2]
                    or rect[3] <= bounds[1] or rect[1] >= bounds[3]):
                raise ValueError("UIA node outside root window")
            item = {"text": node["text"], "role": node["role"], "rect": rect}
            if "selected" in node:
                item["selected"] = node["selected"]
            nodes.append(item)
        return {"rect": bounds, "nodes": nodes, "elapsed_ms": float(elapsed)}

    @staticmethod
    def _target(hwnd, pid):
        return (sys.platform == "win32" and type(hwnd) is int and type(pid) is int
                and 0 < hwnd <= 0x7FFF_FFFF_FFFF_FFFF and 0 < pid <= 0xFFFF_FFFF)

    def _query(self, payload, invoking=False):
        with self._lock:
            started = time.monotonic()
            try:
                cold = self._process is None or self._process.poll() is not None
                budget = self.startup_timeout if cold else self.timeout
                if cold:
                    self.close()
                    self._start()
                self._serial += 1
                request = dict(payload, request_id=self._serial)
                self._process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                self._process.stdin.flush()
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    raise queue.Empty()
                raw = self._responses.get(timeout=remaining)
                value = json.loads(raw.decode("utf-8-sig")) if raw else None
                if (not isinstance(value, dict) or type(value.get("request_id")) is not int
                        or value["request_id"] != self._serial or "result" not in value):
                    raise ValueError("Invalid UIA response")
                if value["result"] is None:
                    return None
                if invoking:
                    if type(value["result"]) is not bool:
                        raise ValueError("Invalid UIA invoke response")
                    return value["result"]
                return self._snapshot(value["result"], (time.monotonic() - started) * 1000)
            except (OSError, ValueError, TypeError, AttributeError, OverflowError, queue.Empty):
                self.close()
                return None

    def read(self, hwnd: int, pid: int) -> dict | None:
        if not self._target(hwnd, pid):
            return None
        # No action field preserves the original read protocol.
        return self._query({"hwnd": hwnd, "pid": pid})

    def invoke(self, hwnd: int, pid: int, text: str, rect: list) -> bool | None:
        if not self._target(hwnd, pid) or not isinstance(text, str) or not text.strip() or len(text) > 16384:
            return False
        try:
            bounds = self._rect(rect.copy() if isinstance(rect, list) else rect)
        except (ValueError, TypeError, OverflowError):
            return False
        return self._query({"action": "invoke", "hwnd": hwnd, "pid": pid, "text": text, "rect": bounds}, invoking=True)
