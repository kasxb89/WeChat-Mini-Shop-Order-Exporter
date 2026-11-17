from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from order_exporter.order_api import RUNTIME_CONFIG, RUNTIME_SHA256
from order_exporter.runtime_profiles import PROFILES, file_version, identify_runtime, match_profile, runtime_library


class RuntimeProfileTests(unittest.TestCase):
    def test_three_modern_layouts_have_the_fields_used_by_the_scoped_hook(self):
        self.assertEqual({p['version'] for p in PROFILES}, {25710, 25715, 26073})
        for profile in PROFILES:
            config = profile['config']
            self.assertEqual(config['Version'], profile['version'])
            for name in ('LoadStartHookOffset', 'CDPFilterHookOffset', 'CastToJsonHookOffset'):
                self.assertGreater(int(config[name], 16), 0)
            layout = config['MiniAppConfigStructOffsets']
            self.assertTrue(layout['LaunchConfigOffsets'])
            self.assertTrue(layout['RemoteDebugConfigOffsets'])
            self.assertTrue(all(layout[k] > 0 for k in ('SceneOffset', 'WebSocketURLStringOffset', 'RemoteDebugModeOffset')))

    def test_live_verified_digest_takes_precedence_over_version_labels(self):
        profile = match_profile('2.5.8.99999', RUNTIME_SHA256)
        self.assertEqual(profile['version'], 26073)
        self.assertEqual({k: v for k, v in profile['config'].items() if k != 'Version'}, RUNTIME_CONFIG)

    def test_version_matching_does_not_require_only_the_developer_machines_digest(self):
        for number in (25710, 25715, 26073):
            self.assertEqual(match_profile(f'2.5.8.{number}', 'another-build')['version'], number)
        self.assertIsNone(match_profile('2.5.8.99999', 'another-build'))

    def test_launcher_runtime_argument_takes_precedence_over_the_launcher_directory(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            runtime = base / '25715'
            runtime.mkdir()
            expected = runtime / 'flue.dll'
            expected.write_bytes(b'runtime')
            (base / 'flue.dll').write_bytes(b'launcher')
            for argv in ([str(base / 'WeChatAppEx.exe'), '--flue-runtime-dir=' + str(runtime)],
                         [str(base / 'WeChatAppEx.exe'), '--flue-runtime-dir', str(runtime)]):
                process = Mock()
                process.cmdline.return_value = argv
                self.assertEqual(runtime_library(process), expected)
                process.exe.assert_not_called()

    def test_loaded_library_is_found_when_the_bootstrap_has_no_sibling_dll(self):
        with tempfile.TemporaryDirectory() as root:
            expected = Path(root) / 'actual' / 'flue.dll'
            process = Mock()
            process.cmdline.return_value = ['WeChatAppEx.exe']
            process.exe.return_value = str(Path(root) / 'WeChatAppEx.exe')
            mapped = Mock()
            mapped.path = str(expected)
            process.memory_maps.return_value = [mapped]
            self.assertEqual(runtime_library(process), expected)

    def test_changed_library_invalidates_the_cached_profile(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'flue.dll'
            path.write_bytes(b'old')
            with patch('order_exporter.runtime_profiles.file_version', return_value='2.5.8.25715') as version:
                first = identify_runtime(path)
                self.assertEqual(identify_runtime(path), first)
                version.assert_called_once()
                path.write_bytes(b'new and longer')
                second = identify_runtime(path)
                self.assertNotEqual(second['sha256'], first['sha256'])
                self.assertEqual(version.call_count, 2)

    def test_missing_version_metadata_is_returned_as_empty(self):
        self.assertEqual(file_version('Z:\\does-not-exist\\Weixin.exe'), '')


class ContextRecoveryTests(unittest.TestCase):
    def test_stale_context_reopens_from_the_main_window_once_without_pausing(self):
        from order_exporter.direct import Collector, OrderContextPending
        desktop = Mock(stop=threading.Event())
        old_api, fresh_api = Mock(), Mock()
        collector = Collector(desktop, Mock(), api=old_api)
        collector._prepare = Mock(side_effect=[OrderContextPending('还在加载'), None])
        with patch('order_exporter.direct.OrderAPI', return_value=fresh_api):
            collector.prepare()
        self.assertEqual([call.kwargs for call in collector._prepare.call_args_list],
                         [{'fresh': False}, {'fresh': True}])
        old_api.close.assert_called_once()
        self.assertIs(collector.api, fresh_api)

    def test_context_recovery_remains_stoppable_by_f8(self):
        from order_exporter.direct import Collector
        from order_exporter.native import Paused
        desktop = Mock(stop=threading.Event())
        collector = Collector(desktop, Mock(), api=Mock())
        collector._prepare = Mock(side_effect=Paused('F8'))
        with self.assertRaises(Paused):
            collector.prepare()
        collector._prepare.assert_called_once_with(fresh=False)
