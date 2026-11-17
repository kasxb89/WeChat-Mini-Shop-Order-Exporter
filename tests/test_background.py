import ctypes
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from PIL import Image

from order_exporter.native import Desktop, Window, PageError, Paused
from order_exporter.automation import Collector
from order_exporter.vision import Page, Text
import test_bridge_integration as integration


class BackgroundTests(unittest.TestCase):
    def desktop(self):
        d = integration.BridgeIntegrationTests().desktop({"rect":[-100,50,316,830], "elapsed_ms":1,
            "nodes":[{"text":"全部订单", "rect":[-80,140,10,165]}]})
        d.background = True
        d.path = r"C:\Tencent\xwechat\WeChatAppEx.exe"
        d.check_stop = Mock()
        d.wait = Mock()
        d.u = Mock()
        d.u.IsWindow.return_value = True
        d.u.IsIconic.return_value = False
        d.grab = Mock(side_effect=AssertionError("后台不能读取桌面截图"))
        return d

    def test_background_accepts_an_occluded_window_without_querying_foreground(self):
        d=self.desktop()
        Desktop.guard(d)
        d.u.GetForegroundWindow.assert_not_called()

    def test_minimized_window_pauses_instead_of_restoring_and_stealing_focus(self):
        d=self.desktop()
        d.u.IsIconic.return_value=True
        with self.assertRaisesRegex(Paused,"不要最小化"):
            Desktop.guard(d)
        d.u.ShowWindow.assert_not_called()
        d.u.SetForegroundWindow.assert_not_called()

    def test_bind_never_activates_a_background_target(self):
        d=self.desktop()
        d.process_path=lambda _:d.path
        def pid(hwnd, ptr):
            ptr._obj.value=20
        d.u.GetWindowThreadProcessId.side_effect=pid
        d.bind(Window(1,"小店",d.path,20))
        d.u.SetForegroundWindow.assert_not_called()
        d.u.ShowWindow.assert_not_called()

    def test_window_render_capture_replaces_desktop_pixels(self):
        d=self.desktop()
        image=Image.new("RGB",(416,780),"white")
        with patch("order_exporter.window_capture.capture_window",return_value=image) as capture:
            p=d.read()
        self.assertEqual(p.source,"ocr")
        self.assertIs(p.image,image)
        capture.assert_called_once_with(1,416,780)
        d.grab.assert_not_called()

    def test_capture_failure_does_not_fall_back_to_the_users_foreground_screen(self):
        d=self.desktop()
        with patch("order_exporter.window_capture.capture_window",side_effect=OSError("不可用")):
            with self.assertRaises(PageError):
                d.read()
        d.grab.assert_not_called()

    def test_stale_control_identifier_is_replaced_only_after_two_matching_visual_reads(self):
        d=self.desktop()
        image=Image.new("RGB",(416,780),"white")
        current=Page(1,(0,0,416,780),[Text("订单编号",(20,200,90,220)),
            Text("00012345678901234567",(100,200,300,220))],image)
        old=Page(1,current.rect,[Text("订单编号 99912345678901234567",(20,200,300,220))],image,source="uia")
        second=Page(1,(0,0,832,104),[Text("订单编号 00012345678901234567",(20,20,620,60))],image)
        d.read_visual=Mock(side_effect=[current,second])
        result=d.background_page(current,old)
        from order_exporter.vision import read_identifier
        self.assertEqual(read_identifier(result,"order"),"00012345678901234567")
        self.assertEqual(result.source,"ocr")

    def test_disagreeing_visual_identifier_reads_are_rejected(self):
        d=self.desktop()
        image=Image.new("RGB",(416,780),"white")
        current=Page(1,(0,0,416,780),[Text("订单编号 00012345678901234567",(20,200,300,220))],image)
        second=Page(1,(0,0,832,104),[Text("订单编号 00012345678901234568",(20,20,620,60))],image)
        d.read_visual=Mock(side_effect=[current,second])
        result=d.background_page(current)
        from order_exporter.vision import read_identifier
        self.assertIsNone(read_identifier(result,"order"))

    def test_click_is_delivered_to_the_target_without_moving_the_cursor(self):
        d=self.desktop()
        p=Page(1,(-100,50,316,830),[],source="ocr")
        d.input_target=Mock(return_value=(72,SimpleNamespace(x=11,y=22)))
        d.click(p,40,160)
        self.assertEqual([c.args[1] for c in d.u.PostMessageW.call_args_list],[0x200,0x201,0x202])
        self.assertTrue(all(c.args[0]==72 for c in d.u.PostMessageW.call_args_list))
        d.u.SetPhysicalCursorPos.assert_not_called()
        d.u.mouse_event.assert_not_called()

    def test_chromium_transparent_input_child_is_retained_with_correct_client_coordinates(self):
        d=self.desktop()
        def convert(hwnd,ptr):
            ptr._obj.x-= -100 if hwnd==1 else -92
            ptr._obj.y-= 50 if hwnd==1 else 90
            return True
        d.u.ScreenToClient.side_effect=convert
        d.u.ChildWindowFromPointEx.side_effect=[72,72]
        p=Page(1,(-100,50,316,830),[])
        hwnd,point=d.input_target(p,40,160)
        self.assertEqual((hwnd,point.x,point.y),(72,32,120))
        self.assertTrue(all(c.args[2]==1 for c in d.u.ChildWindowFromPointEx.call_args_list))

    def test_scroll_delivers_arrow_keys_to_the_window_without_global_input(self):
        d=self.desktop()
        p=Page(1,(-100,50,316,830),[])
        d.input_target=Mock(return_value=(72,SimpleNamespace(x=12,y=507)))
        d.scroll(p,down=True)
        calls=d.u.PostMessageW.call_args_list
        self.assertEqual([c.args[1] for c in calls],[0x200]+[0x100,0x101]*4)
        self.assertTrue(all(c.args[0]==72 for c in calls))
        self.assertTrue(all(c.args[2]==0x28 for c in calls[1:]))
        d.u.mouse_event.assert_not_called()
        d.u.keybd_event.assert_not_called()

    def test_background_never_injects_global_keys_or_uses_the_clipboard(self):
        d=self.desktop()
        for action in (lambda:d.key(0x41,(0x11,)),lambda:d.paste_text("test"),lambda:d.copy(None,None)):
            with self.assertRaises(PageError):
                action()
        d.u.keybd_event.assert_not_called()
        d.u.OpenClipboard.assert_not_called()

    def test_background_scroll_waits_for_delayed_window_frames_without_scrolling_twice(self):
        d=self.desktop()
        old=Page(1,(0,0,416,780),[Text("订单编号",(20,680,100,700))])
        updated=Page(1,old.rect,[Text("下单时间 2026/11/14 16:51:29",(20,500,300,520))])
        d.scroll=Mock()
        collector=Collector(d,lambda _:None)
        collector.stable=Mock(side_effect=[old,old,updated])
        self.assertIs(collector.scrolled(old,down=True),updated)
        d.scroll.assert_called_once_with(old,down=True)

    def test_automatic_preparation_returns_to_background_and_the_previous_window(self):
        d=self.desktop()
        d.u.GetForegroundWindow.side_effect=[222,1,222]
        d.process_path=lambda _:d.path
        d.bind=Mock()
        collector=Collector(d,lambda _:None)
        collector.wait_page=Mock()
        target=Window(1,"小店",d.path,20)
        def prepare(*_):
            self.assertFalse(d.background)
            return target
        with patch.object(collector,"_open_from_search",side_effect=prepare):
            self.assertEqual(collector.open_from_search([]),target)
        self.assertTrue(d.background)
        d.u.SetForegroundWindow.assert_called_once_with(222)

    def test_preparation_failure_restores_background_without_stealing_another_apps_focus(self):
        d=self.desktop()
        d.u.GetForegroundWindow.side_effect=[222,777,777]
        d.process_path=lambda _:r"C:\Office\Editor.exe"
        collector=Collector(d,lambda _:None)
        with patch.object(collector,"_open_from_search",side_effect=PageError("未登录")):
            with self.assertRaisesRegex(PageError,"未登录"):
                collector.open_from_search([])
        self.assertTrue(d.background)
        d.u.SetForegroundWindow.assert_not_called()

    def test_preparation_returns_input_to_the_exporters_own_titlebar(self):
        d=self.desktop()
        d.background=False
        d.process_path=lambda _:d.path
        d.u.GetForegroundWindow.side_effect=[1,222]
        def owner(hwnd,ptr):
            import os
            ptr._obj.value=os.getpid()
        def rect(hwnd,ptr):
            ptr._obj.left,ptr._obj.top,ptr._obj.right,ptr._obj.bottom=399,93,1521,925
            return True
        d.u.GetWindowThreadProcessId.side_effect=owner
        d.u.GetWindowRect.side_effect=rect
        d.end_preparation(222)
        d.u.SetPhysicalCursorPos.assert_called_once_with(960,105)
        self.assertEqual([c.args[0] for c in d.u.mouse_event.call_args_list],[0x0002,0x0004])
        self.assertTrue(d.background)

    def test_preparation_never_clicks_a_different_apps_titlebar(self):
        d=self.desktop()
        d.background=False
        d.process_path=lambda _:d.path
        def owner(hwnd,ptr):
            ptr._obj.value=20
        d.u.GetWindowThreadProcessId.side_effect=owner
        d.end_preparation(222)
        d.u.SetPhysicalCursorPos.assert_not_called()
        d.u.mouse_event.assert_not_called()

    def test_background_skips_provider_actions_that_can_activate_the_window(self):
        d=self.desktop()
        p=Page(1,(-100,50,316,830),[Text("展开",(20,300,80,320),role="button")],source="uia")
        d.read=Mock(return_value=p)
        d.bridge.invoke.return_value=True
        d.input_target=Mock(return_value=(72,SimpleNamespace(x=50,y=310)))
        d.click(p,50,310)
        d.bridge.invoke.assert_not_called()
        d.u.mouse_event.assert_not_called()
        self.assertEqual(d.u.PostMessageW.call_count,3)

    def test_full_collector_uses_background_messages_for_orders_logistics_and_return_navigation(self):
        # Exercise production transport with an in-memory provider, not live WeChat.
        fake=integration.StructuredDesktop(screens=[(0,1),(1,2)])
        d=self.desktop()
        d.windows=fake.windows
        d.process_path=lambda _:d.path
        d.rect=lambda:fake.read().rect
        def pid(hwnd, ptr):
            ptr._obj.value=20
        d.u.GetWindowThreadProcessId.side_effect=pid
        def snapshot(hwnd,pid):
            p=fake.read()
            return {"rect":list(p.rect),"elapsed_ms":1,"nodes":[{"text":t.text,"role":t.role,
                "rect":[t.box[0]+p.rect[0],t.box[1]+p.rect[1],t.box[2]+p.rect[0],t.box[3]+p.rect[1]]}
                for t in p.texts]}
        d.bridge.read.side_effect=snapshot
        d.input_target=lambda p,x,y:(72,SimpleNamespace(x=round(x),y=round(y)))
        scroll_keys=[]
        def receive(hwnd,message,wparam,lparam):
            if message==0x202:
                fake.click(fake.read(),lparam & 0xffff,(lparam>>16)&0xffff)
            elif message==0x101 and wparam in (0x26,0x28):
                scroll_keys.append(wparam)
                if len(scroll_keys)==4:
                    fake.scroll(fake.read(),down=wparam==0x28)
                    scroll_keys.clear()
            return True
        d.u.PostMessageW.side_effect=receive
        records=[]
        d.vision.read.side_effect=lambda _:fake.read().texts
        def frame(*_):
            image=fake.read().image.copy()
            code={"list":1,"detail":2,"logistics":3}[fake.kind]+fake.order*10+int(fake.expanded)*40+fake.view*80
            image.putpixel((0,0),(code,0,0))
            return image
        with patch("order_exporter.window_capture.capture_window",side_effect=frame):
            count=Collector(d,lambda _:None).collect(lambda r:records.append(r) or True)
        self.assertEqual(count,3)
        self.assertEqual(records[1].tracking_numbers,("YT0001234567891",))
        self.assertEqual(records[1].recipient,"徐*")
        self.assertTrue(any(action=="查看物流" for _,action in fake.actions))
        self.assertEqual(fake.kind,"list")
        d.u.SetForegroundWindow.assert_not_called()
        d.u.SetPhysicalCursorPos.assert_not_called()
        d.u.keybd_event.assert_not_called()
        d.u.mouse_event.assert_not_called()
        d.u.OpenClipboard.assert_not_called()
        self.assertEqual(d.metrics["copies"],0)

    def test_background_main_window_automatically_opens_its_own_mini(self):
        d=integration.StructuredDesktop()
        d.background=True
        target=Window(7,"微信",r"C:\Tencent\Weixin.exe",20)
        d.windows=lambda:[target]
        mini=Window(8,"小店",r"C:\Tencent\xwechat\WeChatAppEx.exe",30)
        collector=Collector(d,lambda _:None,target=target)
        with patch.object(collector,"open_from_search",return_value=mini) as opening:
            self.assertIn("全部订单",collector.check_page())
        opening.assert_called_once_with([target],target)
        self.assertEqual(collector.bound_window,mini)


if __name__=="__main__":
    unittest.main()
