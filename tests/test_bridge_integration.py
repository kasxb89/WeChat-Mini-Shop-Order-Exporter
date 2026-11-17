"""Whole collector and desktop adapter tests; never access a live window."""
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from PIL import Image

from order_exporter.automation import Collector
from order_exporter.native import Desktop, get_bridge, close_bridge
from order_exporter.vision import Page, Text, read_identifier, wechat_order_center
from test_exporter import FakeDesktop, listing, page, text


class StructuredDesktop(FakeDesktop):
    def read(self):
        page = super().read()
        page.source = "uia"
        if self.kind in {"detail", "logistics"}:
            label = "订单编号" if self.kind == "detail" else "快递单号"
            value = (f"00000012345678901234567890{self.order}" if self.kind == "detail"
                     else f"YT000123456789{self.order}")
            row = page.find(label)[0]
            page.texts = [t for t in page.texts if t.text != "复制"]
            page.texts.append(Text(value, (row.box[2]+4,row.box[1],335,row.box[3])))
        return page

    def copy(self, *args):
        raise AssertionError("Structured fields must not use the clipboard")


class BridgeIntegrationTests(unittest.TestCase):
    def test_order_entry_accepts_a_suffix_icon_and_ignores_explanatory_titles(self):
        current=Page(1,(0,0,900,880),[
            Text("如何打开订单中心",(20,100,300,130)),
            Text("订单中心介绍详细教程",(20,140,300,170)),
            Text("订单中心ⓘ",(116,298,388,340))])
        self.assertEqual(wechat_order_center(current),current.texts[2])

    def test_list_waits_for_the_old_detail_panel_to_finish_sliding_out(self):
        current = listing((0,1))
        self.assertTrue(Collector.list_ready(current))
        current.texts.append(text("订单编号",349,495,61))
        self.assertFalse(Collector.list_ready(current))

    def test_logistics_waits_for_the_old_order_panel_to_leave(self):
        current = page([text("展开地图",300,150),text("快递单号",20,230)])
        self.assertTrue(Collector.logistics_ready(current))
        current.texts.append(text("订单编号",349,495,61))
        self.assertFalse(Collector.logistics_ready(current))

    def desktop(self, snapshot):
        desktop = Desktop.__new__(Desktop)
        desktop.hwnd, desktop.pid = 1, 20
        desktop.background = False
        desktop.guard = lambda: None
        desktop.rect = lambda: (-100, 50, 316, 830)
        desktop.grab = lambda **_: Image.new("RGB", (416,780), "white")
        desktop.bridge = SimpleNamespace(read=Mock(return_value=snapshot),invoke=Mock(return_value=False))
        desktop.bridge_enabled = True
        desktop.search_pending = False
        desktop.search_center_pending = False
        desktop.vision = Mock()
        desktop.vision.read.return_value = [Text("fallback", (10,10,80,30))]
        desktop.metrics = {"bridge_reads":0,"bridge_ms":0.0,"ocr_reads":0,"ocr_ms":0.0,"copies":0,"invokes":0}
        desktop.ocr_cache = OrderedDict()
        return desktop

    def test_complete_collection_reads_three_orders_without_copy_buttons(self):
        actual, expected = [], []
        direct = Collector(StructuredDesktop(screens=[(0,1),(1,2)]), lambda _:None)
        fallback = Collector(FakeDesktop(screens=[(0,1),(1,2)]), lambda _:None)
        self.assertEqual(direct.collect(lambda r:actual.append(r) or True), 3)
        self.assertEqual(fallback.collect(lambda r:expected.append(r) or True), 3)
        self.assertEqual(actual, expected)

    def test_order_expand_is_relocated_once_if_an_async_review_swallowed_the_click(self):
        class DroppedExpand(StructuredDesktop):
            dropped = False
            def click(self,source,x,y):
                hit=min(source.texts,key=lambda t:abs(t.x-x)+abs(t.y-y))
                if hit.text=="展开" and not self.dropped:
                    self.dropped=True
                    return
                return super().click(source,x,y)
        import itertools
        ticks=itertools.count()
        records=[]
        desktop=DroppedExpand()
        with patch("order_exporter.automation.time.monotonic",side_effect=lambda:next(ticks)*.4):
            count=Collector(desktop,lambda _:None).collect(lambda r:records.append(r) or True)
        self.assertTrue(desktop.dropped)
        self.assertEqual(count,2)
        self.assertEqual(records[1].tracking_numbers,("YT0001234567891",))

    def test_order_already_expanded_during_button_relocation_is_accepted(self):
        from order_exporter.native import PageError
        class ExpandedDuringRelocation(StructuredDesktop):
            def click(self,source,x,y):
                hit=min(source.texts,key=lambda t:abs(t.x-x)+abs(t.y-y))
                if hit.text=="展开":
                    self.expanded=True
                    raise PageError("当前页面未找到展开")
                return super().click(source,x,y)
        records=[]
        self.assertEqual(Collector(ExpandedDuringRelocation(),lambda _:None).collect(lambda r:records.append(r) or True),2)
        self.assertEqual(records[1].tracking_numbers,("YT0001234567891",))

    def test_order_expanding_just_after_the_first_wait_is_accepted_without_another_click(self):
        from order_exporter.native import PageError
        desktop=StructuredDesktop()
        collector=Collector(desktop,lambda _:None)
        original=collector.wait_page
        delayed=[False]
        def wait(predicate,message,**kwargs):
            if message.startswith("订单信息") and not delayed[0]:
                delayed[0]=True
                desktop.expanded=True
                raise PageError(message)
            return original(predicate,message,**kwargs)
        collector.wait_page=wait
        records=[]
        self.assertEqual(collector.collect(lambda r:records.append(r) or True),2)
        self.assertTrue(delayed[0])

    def test_cached_snapshot_maps_absolute_negative_monitor_coordinates(self):
        snapshot = {"rect":[-100,50,316,830],"elapsed_ms":3.5,"nodes":[
            {"text":"订单编号", "rect":[-80,550,0,570]},
            {"text":"00012345678901234567", "rect":[5,550,230,570]},
            {"text":"00012345678901234567", "rect":[5,550,230,570]},
            {"text":"关闭", "rect":[280,50,300,70]}]}
        desktop = self.desktop(snapshot)
        result = desktop.read()
        self.assertEqual(result.source, "uia")
        self.assertEqual(len(result.texts), 2)
        self.assertEqual(result.texts[0].box, (20,500,100,520))
        self.assertEqual(read_identifier(result,"order"), "00012345678901234567")
        desktop.vision.read.assert_not_called()
        self.assertEqual(desktop.metrics["bridge_reads"], 1)

    def test_new_snapshot_is_read_even_when_screen_pixels_do_not_change(self):
        desktop = self.desktop({"rect":[-100,50,316,830],"elapsed_ms":1,"nodes":[
            {"text":"订单编号 00012345678901234567", "rect":[-80,550,250,570]}]})
        first = desktop.read()
        desktop.bridge.read.return_value["nodes"][0]["text"] = "订单编号 00012345678901234568"
        second = desktop.read()
        self.assertNotEqual(read_identifier(first,"order"), read_identifier(second,"order"))
        self.assertEqual(desktop.bridge.read.call_count, 2)

    def test_transport_failure_falls_back_once_without_repeated_timeout(self):
        desktop = self.desktop(None)
        self.assertEqual(desktop.read().source, "ocr")
        desktop.read()
        desktop.bridge.read.assert_called_once_with(1,20)
        desktop.vision.read.assert_called_once()

    def test_empty_controls_do_not_disable_the_working_bridge(self):
        desktop = self.desktop({"rect":[-100,50,316,830],"elapsed_ms":1,"nodes":[]})
        self.assertEqual(desktop.read().source,"ocr")
        desktop.bridge.read.return_value["nodes"] = [{"text":"全部订单","rect":[-80,200,10,220]}]
        self.assertEqual(desktop.read().source,"uia")
        self.assertEqual(desktop.bridge.read.call_count,2)

    def test_incomplete_search_controls_use_the_same_frame_ocr_for_the_order_entry(self):
        desktop = self.desktop({"rect":[-100,50,316,830],"elapsed_ms":1,"nodes":[
            {"text":"搜索","rect":[80,100,110,120]}]})
        desktop.search_center_pending=True
        self.assertEqual(desktop.read().source,"ocr")
        desktop.vision.read.assert_called_once()

    def test_moved_snapshot_does_not_click_with_stale_coordinates(self):
        desktop = self.desktop({"rect":[0,50,416,830],"elapsed_ms":1,"nodes":[
            {"text":"订单编号 00012345678901234567","rect":[20,550,300,570]}]})
        result = desktop.read()
        self.assertEqual(result.source,"ocr")
        self.assertIsNone(read_identifier(result,"order"))

    def test_app_reuses_one_worker_across_tasks_and_closes_it_once(self):
        close_bridge()
        with patch("order_exporter.native.BridgeReader") as factory:
            first, second = get_bridge(), get_bridge()
            self.assertIs(first,second)
            factory.assert_called_once()
            close_bridge()
            close_bridge()
            first.close.assert_called_once()

    def test_click_follows_the_same_order_after_a_recommendation_moves_it(self):
        desktop = self.desktop({"rect":[-100,50,316,830],"elapsed_ms":1,"nodes":[
            {"text":"当前订单的完整商品名称","rect":[24,350,217,370]}]})
        previous = desktop.read()
        desktop.bridge.read.return_value["nodes"][0]["rect"] = [24,500,217,520]
        desktop.u = SimpleNamespace(SetPhysicalCursorPos=Mock(),mouse_event=Mock())
        desktop.wait = Mock()
        desktop.click(previous, previous.texts[0].x, previous.texts[0].y)
        desktop.u.SetPhysicalCursorPos.assert_called_once_with(120,510)

    def test_supported_control_action_skips_coordinate_reads_and_mouse_input(self):
        desktop = self.desktop({"rect":[-100,50,316,830],"elapsed_ms":1,"nodes":[
            {"text":"展开","role":"button","rect":[80,500,110,520]}]})
        page = desktop.read()
        desktop.bridge.invoke.return_value=True
        desktop.u = SimpleNamespace(SetPhysicalCursorPos=Mock(),mouse_event=Mock())
        desktop.wait=Mock()
        desktop.click(page,page.texts[0].x,page.texts[0].y)
        desktop.bridge.invoke.assert_called_once_with(1,20,"展开",[80,500,110,520])
        desktop.u.SetPhysicalCursorPos.assert_not_called()
        self.assertEqual(desktop.bridge.read.call_count,1)
        self.assertEqual(desktop.metrics["invokes"],1)

    def test_unknown_action_result_is_observed_without_a_second_click(self):
        desktop = self.desktop({"rect":[-100,50,316,830],"elapsed_ms":1,"nodes":[
            {"text":"展开","role":"button","rect":[80,500,110,520]}]})
        page = desktop.read()
        desktop.bridge.invoke.return_value=None
        desktop.u = SimpleNamespace(SetPhysicalCursorPos=Mock(),mouse_event=Mock())
        desktop.wait=Mock()
        desktop.click(page,page.texts[0].x,page.texts[0].y)
        desktop.u.mouse_event.assert_not_called()
        self.assertEqual(desktop.bridge.read.call_count,1)


if __name__ == "__main__":
    unittest.main()
