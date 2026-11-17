"""Exercise native enumeration without reading or changing real windows."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from order_exporter.native import Desktop, Window
from order_exporter.vision import Text


@unittest.skipUnless(sys.platform == 'win32', 'Windows enumeration adapter')
class WindowDetectionTests(unittest.TestCase):
    def scan(self, rows):
        desktop = Desktop.__new__(Desktop)
        desktop.u = Mock()
        indexed = {row['hwnd']: row for row in rows}
        desktop.u.IsWindowVisible.side_effect = lambda hwnd: indexed[hwnd].get('visible', True)
        def title(hwnd, buffer, size):
            buffer.value = indexed[hwnd].get('title', '')
        def kind(hwnd, buffer, size):
            buffer.value = indexed[hwnd].get('kind', 'Qt51514QWindowIcon')
        def pid(hwnd, pointer):
            pointer._obj.value = indexed[hwnd].get('pid', 100 + hwnd)
        desktop.u.GetWindowTextW.side_effect = title
        desktop.u.GetClassNameW.side_effect = kind
        desktop.u.GetWindowThreadProcessId.side_effect = pid
        desktop.u.GetWindowLongW.side_effect = lambda hwnd, _: indexed[hwnd].get('style', 0x100)
        desktop.u.EnumWindows.side_effect = lambda callback, _: [callback(row['hwnd'], 0) for row in rows]
        desktop.process_path = lambda hwnd: indexed[hwnd]['path']
        return desktop, desktop.windows()

    def test_logged_in_main_window_with_account_suffix_is_found(self):
        _, windows = self.scan([{'hwnd': 1, 'title': '微信 (2)', 'path': r'D:\Programs\Weixin\Weixin.exe'}])
        self.assertEqual([(w.hwnd, w.pid) for w in windows], [(1, 101)])

    def test_main_window_hidden_in_tray_is_still_selectable(self):
        desktop, windows = self.scan([{'hwnd': 2, 'title': '微信', 'visible': False,
                                       'path': r'D:\Programs\Weixin\Weixin.exe'}])
        self.assertEqual([w.hwnd for w in windows], [2])
        desktop.hwnd = 0
        desktop.background = False
        desktop.search_pending = desktop.search_center_pending = False
        desktop.check_stop = desktop.wait = desktop.guard = Mock()
        desktop.u.IsIconic.return_value = False
        desktop.bind(windows[0])
        desktop.u.ShowWindow.assert_called_once_with(2, 9)

    def test_legacy_main_window_class_handles_localized_title(self):
        _, windows = self.scan([{'hwnd': 3, 'title': '微信桌面版', 'kind': 'WeChatMainWndForPC',
                                'path': r'C:\Tencent\WeChat\WeChat.exe'}])
        self.assertEqual([w.hwnd for w in windows], [3])

    def test_miniapp_does_not_require_the_developer_profile_directory_name(self):
        _, windows = self.scan([{'hwnd': 4, 'title': '微信小店',
            'path': r'D:\Tencent\WeChat\XPlugin\RadiumWMPF\WeChatAppEx.exe'}])
        self.assertEqual([w.hwnd for w in windows], [4])

    def test_other_apps_chat_popups_and_tray_helpers_are_not_account_windows(self):
        _, windows = self.scan([
            {'hwnd': 1, 'title': '微信', 'path': r'C:\Windows\notepad.exe'},
            {'hwnd': 2, 'title': '联系人甲', 'path': r'C:\Tencent\Weixin.exe'},
            {'hwnd': 3, 'title': '微信', 'style': 0x80, 'path': r'C:\Tencent\Weixin.exe'},
            {'hwnd': 4, 'title': 'WxTrayIconMessageWindow', 'visible': False,
             'kind': 'Qt51514WxTrayIconMessageWindowClass', 'path': r'C:\Tencent\Weixin.exe'},
            {'hwnd': 5, 'title': '微信', 'visible': False, 'path': r'C:\Tencent\WeChatAppEx.exe'},
        ])
        self.assertEqual(windows, [])

    def test_search_uses_native_shortcut_without_reading_or_clicking_the_main_window(self):
        desktop = Desktop.__new__(Desktop)
        desktop.background = False
        desktop.bind = Mock()
        desktop.read = Mock(side_effect=AssertionError('主窗口搜索入口不能依赖 OCR'))
        desktop.click = Mock(side_effect=AssertionError('不能猜测主窗口搜索框位置'))
        desktop.key = Mock()
        desktop.paste_text = Mock()
        desktop.wait = Mock()
        target = Window(1, '微信', r'D:\Apps\Weixin.exe', 100)
        desktop.search_wechat(target)
        desktop.bind.assert_called_once_with(target)
        self.assertEqual([call.args for call in desktop.key.call_args_list],
                         [(0x46, (0x11,)), (0x41, (0x11,))])
        desktop.paste_text.assert_called_once_with('微信小店')
        self.assertTrue(desktop.search_pending)

    def test_search_does_not_type_during_background_collection(self):
        from order_exporter.native import PageError
        desktop = Desktop.__new__(Desktop)
        desktop.background = True
        desktop.bind = Mock()
        desktop.key = Mock()
        with self.assertRaises(PageError):
            desktop.search_wechat(Window(1, '微信', r'D:\Apps\Weixin.exe', 100))
        desktop.bind.assert_not_called()
        desktop.key.assert_not_called()

    def fallback_desktop(self, nodes, scale=1):
        desktop = Desktop.__new__(Desktop)
        desktop.background = False
        desktop.hwnd, desktop.pid = 1, 100
        desktop.bind = desktop.key = desktop.paste_text = desktop.wait = Mock()
        desktop.rect = Mock(return_value=(0, 0, 3840, 2160))
        desktop.u = Mock()
        desktop.u.GetDpiForWindow.return_value = 96 * scale
        desktop.bridge_enabled = True
        desktop.bridge = Mock()
        desktop.bridge.read.return_value = {'rect': [0, 0, 3840, 2160], 'nodes': nodes}
        desktop.capture = Mock(return_value=Image.new('RGB', (3840, 2160)))
        desktop.ocr = Mock(return_value=[Text('搜索', (80*scale, 40*scale, 120*scale, 60*scale))])
        desktop.click = Mock()
        return desktop

    def test_control_fallback_finds_search_without_a_screenshot(self):
        desktop = self.fallback_desktop([{'text': '搜索', 'role': 'edit', 'rect': [80, 40, 120, 60]}])
        desktop.search_wechat(Window(1, '微信', r'D:\Apps\Weixin.exe', 100), locate=True)
        desktop.capture.assert_not_called()
        desktop.ocr.assert_not_called()
        self.assertEqual(desktop.click.call_args.args[1:], (100, 50))

    def test_visual_fallback_uses_only_a_dpi_scaled_header_crop_on_a_4k_window(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                desktop = self.fallback_desktop([], scale)
                desktop.search_wechat(Window(1, '微信', r'D:\Apps\Weixin.exe', 100), locate=True)
                self.assertEqual(desktop.ocr.call_args.args[0].size, (560*scale, 120*scale))
                self.assertEqual(desktop.click.call_args.args[1:], (100*scale, 50*scale))


class EntryRecoveryTests(unittest.TestCase):
    def collector(self):
        from order_exporter.automation import Collector
        from order_exporter.vision import Page
        desktop = Mock()
        main = Window(1, '微信', r'D:\Apps\Weixin.exe', 100)
        search = Window(2, '微信', r'D:\Apps\WeChatAppEx.exe', 101)
        mini = Window(3, '微信小店', r'D:\Apps\WeChatAppEx.exe', 101)
        result = Page(1, (0, 0, 880, 640), [Text('搜索网络结果', (60, 80, 200, 110)),
                       Text('微信小店', (80, 135, 200, 160))])
        center = Page(2, (0, 0, 880, 640), [Text('订单中心', (100, 180, 220, 220))])
        collector = Collector(desktop, lambda _: None)
        collector.wait_window = Mock(side_effect=[search, mini])
        return collector, desktop, main, mini, result, center

    def test_transient_search_load_retries_the_same_account_without_pausing(self):
        collector, desktop, main, mini, result, center = self.collector()
        collector.wait_page = Mock(side_effect=[None, result, center])
        self.assertEqual(collector._open_from_search([main], main), mini)
        self.assertEqual([call.args[0] for call in desktop.search_wechat.call_args_list], [main, main])
        self.assertEqual(desktop.search_wechat.call_args_list[-1].kwargs, {})

    def test_control_and_image_route_runs_after_two_shortcut_failures(self):
        collector, desktop, main, mini, result, center = self.collector()
        collector.wait_page = Mock(side_effect=[None, None, result, center])
        self.assertEqual(collector._open_from_search([main], main), mini)
        self.assertEqual([call.args[0] for call in desktop.search_wechat.call_args_list], [main]*3)
        self.assertEqual(desktop.search_wechat.call_args.kwargs, {'locate': True})

    def test_user_stop_interrupts_the_recovery_chain_immediately(self):
        from order_exporter.native import Paused
        collector, desktop, main, *_ = self.collector()
        desktop.search_wechat.side_effect = Paused('F8')
        with self.assertRaises(Paused):
            collector._open_from_search([main], main)
        desktop.search_wechat.assert_called_once_with(main)

    def test_reuses_verified_existing_search_page_without_selecting_another_account(self):
        from order_exporter.automation import Collector
        own = Window(2, '微信', r'D:\Apps\WeChatAppEx.exe', 101)
        other = Window(4, '微信', r'D:\Apps\WeChatAppEx.exe', 201)
        desktop = Mock()
        desktop.windows.return_value = [own, other]
        desktop.active_window.return_value = other
        collector = Collector(desktop, lambda _: None, accept_window=lambda w: w.pid == 101)
        self.assertEqual(collector.wait_window([own, other], lambda w: w.title == '微信', '未打开'), own)


class DetectionInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.profile = Path(self.directory.name) / 'profile'
        self.data_patch = patch('order_exporter.ui.data_dir', return_value=self.profile)
        self.data_patch.start()
        self.desktop_patch = patch('order_exporter.ui.Desktop')
        self.desktop = self.desktop_patch.start().return_value
        self.desktop.windows.return_value = []
        self.desktop.detection_details.return_value = {'processes': [], 'window_candidates': []}
        from order_exporter.ui import MainWindow
        self.window = MainWindow()

    def tearDown(self):
        self.window.license_timer.stop()
        self.window.close()
        self.desktop_patch.stop()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_redetect_selects_the_only_window_and_reports_success(self):
        target = Window(1, '微信 (2)', r'D:\Apps\Weixin.exe', 100)
        self.desktop.windows.return_value = [target]
        self.window.detect.click()
        self.assertEqual(self.window.target.currentData(), target)
        self.assertIn('已自动选择', self.window.message.text())
        self.assertFalse(self.window.detect_details.isHidden())

    def test_running_wechat_without_a_window_has_a_copyable_saved_report(self):
        self.desktop.detection_details.return_value = {
            'processes': [{'pid': 100, 'name': 'Weixin.exe', 'exe': r'D:\Apps\Weixin.exe'}],
            'window_candidates': []}
        self.window.detect.click()
        self.assertIn('检测到微信进程', self.window.message.text())
        self.assertFalse(self.window.detect_details.isHidden())
        saved = json.loads((self.profile / 'last-wechat-detection.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['processes'][0]['pid'], 100)
        self.assertEqual(saved['windows'], [])
        from PySide6 import QtWidgets
        def inspect_dialog(dialog):
            details = dialog.findChild(QtWidgets.QPlainTextEdit)
            self.assertTrue(details.isReadOnly())
            self.assertIn('Weixin.exe', details.toPlainText())
            self.assertIn('复制检测信息', [b.text() for b in dialog.findChildren(QtWidgets.QPushButton)])
            return QtWidgets.QDialog.Accepted
        with patch.object(QtWidgets.QDialog, 'exec', inspect_dialog):
            self.window.show_detection_details()

    def test_native_error_is_shown_instead_of_silently_clearing_the_list(self):
        self.desktop.windows.side_effect = OSError(5, 'Access is denied')
        self.window.detect.click()
        self.assertIn('窗口检测失败', self.window.message.text())
        self.assertIn('Access is denied', self.window.detection_report['error'])
        self.assertIn('未检测到', self.window.target.currentText())

    def test_multiple_accounts_keep_the_users_selection_and_do_not_start_collection(self):
        targets = [Window(i, '微信', r'D:\Apps\Weixin.exe', 100 + i) for i in (1, 2)]
        self.desktop.windows.return_value = targets
        self.window.detect.click()
        self.assertIsNone(self.window.target.currentData())
        self.window.target.setCurrentIndex(2)
        self.window.detect.click()
        self.assertEqual(self.window.target.currentData(), targets[1])
        self.assertIn('2 个', self.window.message.text())
        self.assertIsNone(self.window.worker)
