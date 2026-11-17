from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from collections import OrderedDict
from contextlib import closing
from types import SimpleNamespace
import ctypes
from PIL import Image, ImageDraw

from order_exporter.automation import Collector
from order_exporter.models import Filters, OrderRecord
from order_exporter.native import Desktop, PageError, Paused, Window
from order_exporter.store import Store
from order_exporter.vision import Page, Text, copied_identifier, back_arrow, labelled_copy, ordered_time, order_cards, tracking_copy, detail_recipient, wechat_search_box, order_expand, wechat_search_result
from order_exporter.workbook import export_xlsx


def text(value, x, y, width=None):
    return Text(value, (x, y, x + (width if width is not None else min(220, max(30, len(value)*12))), y+17), .99)


def page(lines, selected=False):
    image = Image.new("RGB", (416, 780), "white")
    if selected:
        ImageDraw.Draw(image).line((16, 220, 98, 220), fill="black", width=2)
    return Page(1, (100,100,516,880), lines, image)


def listing(indices=(), end=True, empty=False):
    lines = [text("订单",190,150), text("全部订单",20,190,72), text("运输中",173,190)]
    for slot, order in enumerate(indices):
        y = 250+slot*220
        status = "已取消" if order == 0 else "已送达" if order == 1 else "待发货"
        lines += [text(status,340,y,60), text("测试店铺",40,y,90),
                  text(f"完整商品名称 {order}",110,y+42,200), text("￥13.90",340,y+42,60)]
    if empty:
        lines += [text("暂无订单",180,320)]
    if end:
        lines += [text("你可能还会喜欢",150,740)]
    return page(lines, selected=True)


class FakeDesktop:
    """内存中的真实流程替身，不操作任何桌面窗口。"""
    def __init__(self, screens=None, empty=False, fail_tracking=False, fail_order=False, missing_date=False):
        self.screens = screens if screens is not None else [(0,1,)]
        self.empty, self.fail_tracking, self.fail_order = empty,fail_tracking,fail_order
        self.missing_date = missing_date
        self.view = 0
        self.kind = "list"
        self.order = 0
        self.expanded = False
        self.actions = []

    def windows(self):
        return [Window(1,"小店",r"C:\Tencent\xwechat\WeChatAppEx.exe")]

    def bind(self,_):
        pass

    def wait(self,_):
        pass

    def read(self):
        if self.kind == "list":
            return listing(() if self.empty else self.screens[self.view], end=self.view==len(self.screens)-1, empty=self.empty)
        if self.kind == "logistics":
            return page([text("返回",15,60), text("物流详情",170,60), text("快递单号",20,150),
                         text("复制",340,150)])
        lines = [text("返回",15,60), text("已取消" if self.order==0 else "已送达",180,60),
                 text("天星教育旗舰店" if self.order==0 else "万唯图书店",80,170,200),text("进店",350,170),
                 text(f"完整商品名称 {self.order}",110,210,200),text("￥13.90",340,210,60),
                 text("订单编号",20,500),text("复制",340,500)]
        if self.order==1:
            lines += [text("查看物流",30,100)]
            if not self.expanded:
                lines += [text("徐* 138****0000 号码保护",50,118,240)]
        if not self.expanded:
            lines += [text("展开",185,540)]
        else:
            lines += [text("商品总价",20,450)]
            if not self.missing_date:
                moment = ("2026/11/14 23:55:38","2026/11/14 16:51:29","2026/10/01 09:00:00")[self.order]
                lines += [text("下单时间",20,560),text(moment,120,560,220),
                          text("支付时间",20,600),text("2026/11/15 08:00:00",120,600,220)]
        return page(lines)

    def click(self,source,x,y):
        hit = min(source.texts, key=lambda t:abs(t.x-x)+abs(t.y-y))
        self.actions.append((self.order,hit.text))
        if hit.text=="全部订单":
            self.kind="list"
        elif hit.text.startswith("完整商品名称"):
            self.order=int(hit.text.rsplit(" ",1)[1])
            self.kind="detail"
            self.expanded=False
        elif hit.text=="展开":
            self.expanded=True
        elif hit.text=="查看物流":
            self.kind="logistics"
        elif hit.text=="返回":
            self.kind="detail" if self.kind=="logistics" else "list"
        else:
            raise AssertionError(f"Unexpected UI action: {hit.text}")

    def copy(self,_,__):
        if self.kind=="detail":
            if self.fail_order:
                raise PageError("本次订单编号复制没有更新")
            return f"00000012345678901234567890{self.order}"
        if self.fail_tracking:
            raise PageError("本次物流复制没有更新")
        return f"YT000123456789{self.order}"

    def scroll(self,_,down):
        if self.kind=="list":
            self.view=min(self.view+1,len(self.screens)-1) if down else max(0,self.view-1)


class CollectionTests(unittest.TestCase):
    def collect(self, desktop, filters=None):
        records={}
        def save(r):
            added=r.order_id not in records
            records[r.order_id]=r
            return added
        count=Collector(desktop,lambda _:None,filters).collect(save)
        return count,list(records.values())

    def test_all_states_include_cancelled_without_a_tracking_number(self):
        desktop=FakeDesktop(screens=[(0,1),(1,2)])
        count,records=self.collect(desktop)
        self.assertEqual(count,3)
        self.assertEqual([r.status for r in records],["已取消","已送达","待发货"])
        self.assertEqual(records[0].tracking_numbers,())
        self.assertEqual(records[1].tracking_numbers,("YT0001234567891",))
        self.assertEqual(records[2].tracking_numbers,())
        self.assertFalse(any("确认收货" in action or "支付" in action or "删除订单" in action for _,action in desktop.actions))

    def test_time_filter_has_inclusive_second_boundaries(self):
        query=Filters("2026-11-14 16:51:29","2026-11-14 23:55:38")
        count,records=self.collect(FakeDesktop(screens=[(0,1),(1,2)]),query)
        self.assertEqual(count,2)
        self.assertEqual({r.ordered_at for r in records},{query.start,query.end})

    def test_recipient_is_captured_before_details_scroll_and_absence_is_explicit(self):
        count,records=self.collect(FakeDesktop())
        self.assertEqual(count,2)
        self.assertEqual(records[1].recipient,"徐*")
        self.assertEqual(records[0].recipient,"")
        self.assertIn("未显示收件人",records[0].note)
        self.assertTrue(all(r.recipient_checked for r in records))

    def test_descending_scan_stops_before_unneeded_older_pages(self):
        desktop=FakeDesktop(screens=[(0,1),(2,),(3,)])
        query=Filters("2026-11-14 00:00:00","2026-11-14 23:59:59")
        records=[]
        collector=Collector(desktop,lambda _:None,query)
        self.assertEqual(collector.collect(lambda r:(records.append(r),True)[1]),2)
        self.assertEqual(desktop.view,1)
        self.assertIn("早于",collector.end_reason)

    def test_out_of_order_dates_disable_early_finish(self):
        desktop=FakeDesktop(screens=[(0,2),(1,)])
        count,records=self.collect(desktop,Filters("2026-11-14 00:00:00","2026-11-14 23:59:59"))
        self.assertEqual(count,2)
        self.assertEqual({r.status for r in records},{"已取消","已送达"})

    def test_complete_history_mode_does_not_stop_at_the_start_date(self):
        desktop=FakeDesktop(screens=[(0,1),(2,)])
        query=Filters("2026-11-14 00:00:00","2026-11-14 23:59:59",descending=False)
        collector=Collector(desktop,lambda _:None,query)
        self.assertEqual(collector.collect(lambda _:True),2)
        self.assertIn("列表末尾",collector.end_reason)

    def test_out_of_range_order_does_not_open_logistics(self):
        desktop=FakeDesktop(screens=[(1,)])
        count,records=self.collect(desktop,Filters("2026-11-15 00:00:00","2026-11-15 23:59:59"))
        self.assertEqual((count,records),(0,[]))
        self.assertFalse(any(action=="查看物流" for _,action in desktop.actions))

    def test_shop_keyword_is_independent_of_time_filter(self):
        count,records=self.collect(FakeDesktop(),Filters(shop="万唯"))
        self.assertEqual(count,1)
        self.assertEqual(records[0].shop,"万唯图书店")

    def test_overlapping_pages_do_not_copy_the_same_order_logistics_twice(self):
        desktop=FakeDesktop(screens=[(0,1),(1,2)])
        self.assertEqual(self.collect(desktop)[0],3)
        self.assertEqual([order for order,action in desktop.actions if action=="查看物流"],[1])

    def test_resume_skips_already_completed_order_details(self):
        desktop=FakeDesktop()
        records=[]
        collector=Collector(desktop,lambda _:None,completed_ids={"000000123456789012345678901"})
        self.assertEqual(collector.collect(lambda r:(records.append(r),True)[1],initial_count=1),2)
        self.assertEqual([r.status for r in records],["已取消"])
        self.assertFalse(any(action=="查看物流" for _,action in desktop.actions))

    def test_clipboard_tracking_failure_retains_order_for_followup(self):
        count,records=self.collect(FakeDesktop(fail_tracking=True))
        self.assertEqual(count,2)
        self.assertEqual(records[1].tracking_state,"待补采")
        self.assertEqual(records[1].tracking_numbers,())
        self.assertIn("复制",records[1].note)

    def test_invalid_order_copy_cannot_commit_unknown_order(self):
        records=[]
        with self.assertRaises(PageError):
            Collector(FakeDesktop(fail_order=True),lambda _:None).collect(records.append)
        self.assertEqual(records,[])

    def test_missing_creation_time_cannot_be_silently_filtered(self):
        with self.assertRaisesRegex(ValueError,"下单时间"):
            self.collect(FakeDesktop(missing_date=True),Filters("2026-11-14 00:00:00","2026-11-14 23:59:59"))

    def test_empty_all_orders_is_a_verified_zero_export(self):
        desktop=FakeDesktop(empty=True)
        count,records=self.collect(desktop)
        self.assertEqual((count,records),(0,[]))
        self.assertFalse(any(action.startswith("完整商品") for _,action in desktop.actions))

    def test_no_end_marker_never_reports_complete(self):
        desktop=FakeDesktop()
        original=desktop.read
        def read():
            p=original()
            if desktop.kind=="list":
                p.texts=[t for t in p.texts if "喜欢" not in t.text]
            return p
        desktop.read=read
        with self.assertRaisesRegex(PageError,"未确认全部订单"):
            self.collect(desktop)

    def test_resume_uses_exact_order_ids_after_page_overlap(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"db.sqlite3"
            store=Store(path)
            job=store.create(Path(root)/"orders.xlsx")
            store.save(job,OrderRecord("000000123456789012345678900",("已采集商品",),()))
            store.close()
            store=Store(path)
            collector=Collector(FakeDesktop(screens=[(0,1),(1,2)]),lambda _:None,store.filters(job))
            self.assertEqual(collector.collect(lambda r:store.save(job,r),initial_count=store.count(job)),3)
            self.assertEqual(store.count(job),3)
            store.close()

    def test_unknown_list_without_empty_notice_is_not_complete(self):
        with self.assertRaisesRegex(PageError,"没有.*暂无订单"):
            self.collect(FakeDesktop(screens=[()]))

    def test_ads_can_move_the_next_order_without_interrupting_collection(self):
        class MovingDesktop(FakeDesktop):
            shifted=False
            def click(self, source, x, y):
                old=self.kind
                super().click(source,x,y)
                if old=="detail" and self.kind=="list":
                    self.shifted=True
            def read(self):
                p=super().read()
                if self.kind=="list" and self.shifted:
                    p.texts=[Text(t.text,(t.box[0],t.box[1]-70,t.box[2],t.box[3]-70),t.confidence)
                             if 440<t.y<700 else t for t in p.texts]
                return p
        count,records=self.collect(MovingDesktop())
        self.assertEqual(count,2)
        self.assertEqual({r.status for r in records},{"已取消","已送达"})

    def test_ad_insertion_can_push_the_next_order_offscreen(self):
        class InsertedAdDesktop(FakeDesktop):
            shifted=False
            def click(self, source, x, y):
                old=self.kind
                super().click(source,x,y)
                if old=="detail" and self.kind=="list":
                    self.shifted=True
            def read(self):
                p=super().read()
                if self.kind=="list" and self.view==0 and self.shifted:
                    p.texts=[Text(t.text,(t.box[0],t.box[1]+320,t.box[2],t.box[3]+320),t.confidence)
                             if 440<t.y<700 else t for t in p.texts]
                return p
        count,records=self.collect(InsertedAdDesktop(screens=[(0,1),(1,)]))
        self.assertEqual(count,2)
        self.assertEqual({r.status for r in records},{"已取消","已送达"})

    def test_back_waits_for_delayed_navigation_instead_of_old_frame(self):
        class DelayedDesktop(FakeDesktop):
            old_frame=None
            remaining=0
            def click(self, source, x, y):
                label=min(source.texts,key=lambda t:abs(t.x-x)+abs(t.y-y)).text
                before=self.read()
                super().click(source,x,y)
                if label=="返回":
                    self.old_frame=before
                    self.remaining=3
            def read(self):
                if self.remaining:
                    self.remaining-=1
                    return self.old_frame
                return super().read()
        self.assertEqual(self.collect(DelayedDesktop())[0],2)

    def test_old_frames_during_asynchronous_back_do_not_interrupt_export(self):
        class DelayedDesktop(FakeDesktop):
            remaining=0
            old_page=None
            def click(self, source, x, y):
                hit=min(source.texts,key=lambda t:abs(t.x-x)+abs(t.y-y))
                if hit.text=="返回":
                    self.old_page=source
                    self.remaining=3
                super().click(source,x,y)
            def read(self):
                if self.remaining:
                    self.remaining-=1
                    return self.old_page
                return super().read()
        count,records=self.collect(DelayedDesktop())
        self.assertEqual(count,2)
        self.assertEqual(records[1].tracking_numbers,("YT0001234567891",))


class NavigationTests(unittest.TestCase):
    def test_uia_search_card_opens_orders_when_brand_name_includes_its_account_role(self):
        desktop=FakeDesktop()
        search=Window(2,"微信",r"C:\xwechat\WeChatAppEx.exe",20)
        mini=Window(3,"小店",r"C:\xwechat\WeChatAppEx.exe",30)
        opened=[]
        desktop.windows=lambda:[search,mini] if opened else [search]
        desktop.active_window=lambda _:mini if opened else search
        desktop.read=lambda:Page(2,(0,0,900,880),[
            Text("微信小店 服务号 账号描述:",(116,245,230,290),role="button"),
            Text("订单中心",(116,298,388,340),role="button"),
            Text("订单中心",(128,310,188,330))],source="uia")
        def click(current,x,y):
            self.assertEqual(current.find("订单中心")[0].x,x)
            opened.append(True)
        desktop.click=click
        result=Collector(desktop,lambda _:None).open_from_search([search],search)
        self.assertEqual(result,mini)
        self.assertEqual(opened,[True])

    def test_mini_open_waits_for_order_content_after_the_window_title_appears(self):
        desktop = FakeDesktop()
        frames = [page([text("小店", 15, 15)]) for _ in range(3)]
        original = desktop.read
        desktop.read = lambda: frames.pop(0) if frames else original()
        ready = Collector(desktop, lambda _: None).mini(open_if_missing=True)
        self.assertTrue(ready.tab_selected("全部订单"))
        self.assertEqual(frames, [])

    def test_readonly_check_opens_the_selected_wechat_without_starting_a_collection(self):
        desktop=FakeDesktop()
        main=Window(2,"微信",r"C:\Weixin.exe",20)
        mini=Window(3,"小店",r"C:\xwechat\WeChatAppEx.exe",30)
        desktop.windows=lambda:[main]
        collector=Collector(desktop,lambda _:None,target=main)
        with patch.object(collector,"open_from_search",return_value=mini) as opening:
            self.assertIn("全部订单",collector.check_page())
        self.assertEqual(opening.call_args.args[1],main)
        self.assertEqual(collector.count,0)

    def test_two_wechat_mains_require_a_choice_and_never_search_the_other_account(self):
        desktop=FakeDesktop()
        desktop.windows=lambda:[Window(1,"微信",r"C:\Weixin.exe",10),Window(2,"微信",r"C:\Weixin.exe",20)]
        desktop.search_wechat=lambda _:self.fail("不能猜选一个账号")
        with self.assertRaisesRegex(PageError,"采集窗口"):
            Collector(desktop,lambda _:None).mini(open_if_missing=True)

    def test_selected_mini_is_used_when_two_accounts_are_open(self):
        desktop=FakeDesktop()
        targets=[Window(1,"小店",r"C:\xwechat\WeChatAppEx.exe",10),Window(2,"小店",r"C:\xwechat\WeChatAppEx.exe",20)]
        desktop.windows=lambda:targets
        desktop.bind=lambda w:desktop.actions.append(w.hwnd)
        collector=Collector(desktop,lambda _:None,target=targets[1])
        collector.mini(open_if_missing=True)
        self.assertEqual(desktop.actions,[2])
        self.assertEqual(collector.bound_window,targets[1])

    def test_selected_main_does_not_reuse_the_other_accounts_existing_mini(self):
        desktop=FakeDesktop()
        main=Window(2,"微信",r"C:\Weixin.exe",20)
        other=Window(1,"小店",r"C:\xwechat\WeChatAppEx.exe",10)
        correct=Window(3,"小店",r"C:\xwechat\WeChatAppEx.exe",30)
        desktop.windows=lambda:[other,main]
        desktop.bind=lambda w:desktop.actions.append(w.hwnd)
        collector=Collector(desktop,lambda _:None,target=main)
        with patch.object(collector,"open_from_search",return_value=correct) as opening:
            collector.mini(open_if_missing=True)
        self.assertEqual(opening.call_args.args[1],main)
        self.assertEqual(desktop.actions,[3])

    def test_closed_or_reused_target_does_not_fall_back_to_another_wechat(self):
        desktop=FakeDesktop()
        desktop.windows=lambda:[Window(2,"微信",r"C:\Weixin.exe",21),Window(3,"小店",r"C:\xwechat\WeChatAppEx.exe",30)]
        with self.assertRaisesRegex(PageError,"重新检测"):
            Collector(desktop,lambda _:None,target=Window(2,"微信",r"C:\Weixin.exe",20)).mini(open_if_missing=True)

    def test_search_failure_in_selected_main_does_not_try_another_account(self):
        desktop=FakeDesktop()
        first=Window(1,"微信",r"C:\Weixin.exe",10)
        second=Window(2,"微信",r"C:\Weixin.exe",20)
        attempted=[]
        def search(w):
            attempted.append(w.hwnd)
            raise PageError("搜索框未识别")
        desktop.search_wechat=search
        with self.assertRaisesRegex(PageError,"搜索框"):
            Collector(desktop,lambda _:None).open_from_search([first,second],second)
        self.assertEqual(attempted,[2])

    def test_delayed_open_cannot_bind_an_existing_inactive_mini_from_another_account(self):
        desktop=FakeDesktop()
        old=Window(1,"小店",r"C:\xwechat\WeChatAppEx.exe",10)
        desktop.windows=lambda:[old]
        desktop.active_window=lambda _:None
        with self.assertRaisesRegex(PageError,"尚未打开"):
            Collector(desktop,lambda _:None).wait_window([old],lambda w:w.title=="小店","尚未打开")

    def test_opening_details_and_logistics_waits_for_the_new_page(self):
        class DelayedDesktop(FakeDesktop):
            old_frame=None
            remaining=0
            def click(self, source, x, y):
                label=min(source.texts,key=lambda t:abs(t.x-x)+abs(t.y-y)).text
                before=self.read()
                super().click(source,x,y)
                if label.startswith("完整商品名称") or label=="查看物流":
                    self.old_frame=before
                    self.remaining=3
            def read(self):
                if self.remaining:
                    self.remaining-=1
                    return self.old_frame
                return super().read()
        records=[]
        self.assertEqual(Collector(DelayedDesktop(),lambda _:None).collect(lambda r:(records.append(r),True)[1]),2)
        self.assertEqual(records[1].tracking_numbers,("YT0001234567891",))


class ForegroundTests(unittest.TestCase):
    def desktop(self, frames):
        desktop = Desktop.__new__(Desktop)
        desktop.background = False
        desktop.hwnd = 1
        desktop.check_stop = lambda: None
        frames = iter(frames)
        desktop.u = SimpleNamespace(IsWindow=lambda _:True, GetForegroundWindow=lambda:next(frames),
                                    GetAncestor=lambda hwnd,_:hwnd,
                                    GetWindowTextW=lambda hwnd,buffer,_:setattr(buffer,"value","测试窗口"))
        desktop.wait = lambda _:None
        return desktop

    def test_temporary_focus_gap_during_navigation_does_not_pause(self):
        self.desktop([0,1]).guard()

    def test_staying_in_another_app_still_pauses_before_input(self):
        with self.assertRaisesRegex(Paused,"测试窗口"):
            self.desktop([2,2]).guard()


class FilterTests(unittest.TestCase):
    def test_invalid_and_partial_ranges_are_rejected(self):
        for start,end in [("2026-11-15 00:00:00","2026-11-14 23:59:59"),("2026-11-14 00:00:00","")]:
            with self.assertRaises(ValueError):
                Filters(start,end)

    def test_payment_time_is_not_order_time(self):
        p=page([text("下单时间",20,300),text("2026/11/14 16:51:29",120,300,220),
                text("支付时间",20,340),text("2026/11/14 16:58:05",120,340,220)])
        self.assertEqual(ordered_time(p),"2026-11-14 16:51:29")
        self.assertEqual(ordered_time(page([text("支付时间 2026/11/14 16:58:05",20,340)])),"")

    def test_truncated_shop_does_not_silently_exclude_matches(self):
        with self.assertRaises(ValueError):
            Filters(shop="旗舰店").includes_shop("万唯中考官方旗...")
        self.assertTrue(Filters(shop="万唯").includes_shop("万唯中考官方旗..."))


class StoreTests(unittest.TestCase):
    def test_clear_current_task_keeps_other_tasks_exports_and_a_complete_backup(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            store=Store(path/"db.sqlite3")
            outputs=[path/"one.xlsx",path/"two.xlsx"]
            for output in outputs:
                output.write_bytes(b"existing export")
            activation=path/"activation.json"
            activation.write_bytes(b"existing activation")
            first,second=[store.create(output) for output in outputs]
            for job in (first,second):
                store.save(job,OrderRecord("001234567890",("商品",),("YT000123456789",),recipient="张*",recipient_checked=True))
            backup=store.clear(first)
            self.assertEqual([j["id"] for j in store.history()],[second])
            self.assertEqual(store.records(first),[])
            self.assertEqual(store.count(second),1)
            with closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0],2)
                self.assertEqual(db.execute("SELECT recipient FROM orders WHERE job_id=?",(first,)).fetchone()[0],"张*")
            self.assertTrue(all(p.read_bytes()==b"existing export" for p in outputs))
            self.assertEqual(activation.read_bytes(),b"existing activation")
            store.close()

    def test_clear_all_removes_tasks_and_order_rows_but_preserves_export_files(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            output=path/"orders.xlsx"
            output.write_bytes(b"keep this export")
            store=Store(path/"db.sqlite3")
            for _ in range(2):
                job=store.create(output)
                store.save(job,OrderRecord("001234567890",("商品",),()))
            backup=store.clear()
            self.assertEqual(store.history(),[])
            self.assertEqual(store.db.execute("SELECT count(*) FROM orders").fetchone()[0],0)
            self.assertEqual(output.read_bytes(),b"keep this export")
            with closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0],2)
            store.close()

    def test_clear_failure_rolls_back_order_deletion(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"db.sqlite3")
            job=store.create(Path(root)/"one.xlsx")
            store.save(job,OrderRecord("001234567890",("商品",),()))
            store.db.executescript("CREATE TRIGGER reject_clear BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT,'test failure'); END;")
            with self.assertRaises(sqlite3.IntegrityError):
                store.clear(job)
            self.assertEqual(store.count(job),1)
            self.assertIsNotNone(store.job(job))
            self.assertEqual(len(list((Path(root)/"backups").glob("clear_*.sqlite3"))),1)
            store.close()

    def test_single_record_lookup_keeps_merged_parcels_scoped_to_its_job(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/"db.sqlite3")
            first, second = store.create(Path(root)/"one.xlsx"), store.create(Path(root)/"two.xlsx")
            store.save(first, OrderRecord("001234567890", ("商品",), ("YT000123456789",)))
            store.save(first, OrderRecord("001234567890", ("商品新版",), ("SF000123456789",)))
            store.save(second, OrderRecord("001234567890", ("另一个任务",), ()))
            self.assertEqual(store.record(first,"001234567890"), store.records(first)[0])
            self.assertEqual(store.record(first,"001234567890").tracking_numbers,("YT000123456789","SF000123456789"))
            self.assertEqual(store.record(second,"001234567890").products,("另一个任务",))
            store.close()

    def test_history_retains_recorded_jobs_after_new_empty_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"db.sqlite3")
            source={"hwnd":11,"title":"小店","path":r"C:\xwechat\WeChatAppEx.exe","pid":101}
            old=store.create(Path(root)/"old.xlsx",source=source)
            store.save(old,OrderRecord("001234567890",("商品",),(),recipient="张*",recipient_checked=True))
            store.state(old,"complete")
            new=store.create(Path(root)/"new.xlsx")
            store.state(new,"paused","搜索未打开")
            history=store.history()
            self.assertEqual([j["id"] for j in history],[new,old])
            self.assertEqual([j["orders"] for j in history],[0,1])
            self.assertEqual(store.records(old)[0].recipient,"张*")
            self.assertEqual(store.job(old)["output"],str(Path(root)/"old.xlsx"))
            self.assertIn("窗口 11",store.metadata(old)["采集窗口"])
            store.set_source(new,source|{"hwnd":22})
            self.assertIn("窗口 11",store.metadata(old)["采集窗口"])
            self.assertIn("窗口 22",store.metadata(new)["采集窗口"])
            store.close()

    def test_old_schema_migrates_without_losing_orders_or_mixing_scope(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"legacy.sqlite3"
            db=sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE jobs(id TEXT PRIMARY KEY,created TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL DEFAULT '',output TEXT NOT NULL);
                CREATE TABLE orders(job_id TEXT NOT NULL,order_id TEXT NOT NULL,products TEXT NOT NULL,tracking TEXT NOT NULL,captured TEXT NOT NULL,PRIMARY KEY(job_id,order_id));
                INSERT INTO jobs VALUES('old','2026-11-14','paused','old', 'old.xlsx');
                INSERT INTO orders VALUES('old','001234567890123456','["product"]','["YT000123456789"]','2026-11-14');
            """)
            db.close()
            store=Store(path)
            self.assertEqual(store.count("old"),1)
            self.assertEqual(store.filters("old").scope,"shipping")
            self.assertEqual(store.records("old")[0].order_id,"001234567890123456")
            self.assertEqual(store.records("old")[0].recipient,"")
            self.assertEqual(store.uncollected_recipients("old"),1)
            store.close()

    def test_recipient_backfill_revisits_legacy_rows_and_does_not_duplicate_orders(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"db.sqlite3")
            job=store.create(Path(root)/"one.xlsx")
            store.save(job,OrderRecord("000000123456789012345678901",("旧商品",),("YT0001234567891",)))
            store.state(job,"complete")
            self.assertEqual(store.uncollected_recipients(job),1)
            completed={r.order_id for r in store.records(job) if r.recipient_checked and r.tracking_state!="待补采"}
            collector=Collector(FakeDesktop(),lambda _:None,completed_ids=completed)
            self.assertEqual(collector.collect(lambda r:store.save(job,r),initial_count=store.count(job)),2)
            self.assertEqual(store.count(job),2)
            self.assertEqual(store.uncollected_recipients(job),0)
            self.assertEqual(next(r for r in store.records(job) if r.recipient).recipient,"徐*")
            store.close()

    def test_saved_filters_are_immutable_when_another_job_starts(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"db.sqlite3")
            query=Filters("2026-11-14 00:00:00","2026-11-14 23:59:59","万唯")
            first=store.create(Path(root)/"one.xlsx",query)
            second=store.create(Path(root)/"two.xlsx",Filters())
            self.assertEqual(store.filters(first),query)
            self.assertEqual(store.filters(second),Filters())
            self.assertIn(query.start,store.metadata(first)["下单时间范围"])
            store.close()

    def test_merge_parcels_and_review_status(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/"db.sqlite3")
            job=store.create(Path(root)/"one.xlsx")
            store.save(job,OrderRecord("001234567890123",("商品A",),(),tracking_state="待补采"))
            self.assertEqual(store.pending(job),1)
            self.assertFalse(store.save(job,OrderRecord("001234567890123",("商品A","商品B"),("YT000123456789",))))
            self.assertEqual(store.pending(job),0)
            self.assertEqual(store.records(job)[0].products,("商品A","商品B"))
            store.close()

    def test_no_logistics_is_valid_but_order_identity_is_required(self):
        self.assertIn("无物流",OrderRecord("001234567890",("商品",),()).tracking_state)
        for record in [("",("商品",),()),("001234567890",(),())]:
            with self.assertRaises(ValueError):
                OrderRecord(*record)


class ParserTests(unittest.TestCase):
    def test_search_suggestion_accepts_the_magnifier_prefix_but_not_other_queries(self):
        p=Page(1,(0,0,880,640),[text("微信小店",95,48),text("六搜索网络结果",80,79),
                text("Q微信小店",80,108),text("Q微信小店订单中心",80,142),text("微信小店",600,270)])
        self.assertEqual(wechat_search_result(p),p.texts[2])
        p.texts[2]=text("微信小店",100,108)
        self.assertEqual(wechat_search_result(p),p.texts[2])
        p.texts.pop(2)
        self.assertIsNone(wechat_search_result(p))

    def test_chat_text_and_search_input_are_not_network_suggestions(self):
        p=Page(1,(0,0,880,640),[text("微信小店",95,48),text("Q微信小店",80,108)])
        self.assertIsNone(wechat_search_result(p))

    def test_order_header_locates_back_when_the_pointer_covers_the_arrow(self):
        p=page([text("已送达",180,54),text("订单编号",20,500)])
        ImageDraw.Draw(p.image).polygon([(10,48),(30,57),(18,75)], fill="black")
        point=back_arrow(p)
        self.assertTrue(10<point[0]<40 and 54<point[1]<75)

    def test_copy_uses_the_current_number_row_after_a_review_panel_moves_it(self):
        desktop = Desktop.__new__(Desktop)
        desktop.background = False
        source = page([text("订单编号",20,500), text("复制",360,500), text("展开",185,540)])
        desktop.metrics = {"copies":0,"ocr_reads":0,"ocr_ms":0.0}
        label, current = text("订单编号",20,550), text("复制",360,550)
        expanded = text("展开",185,590)
        top = round(source.texts[1].y)-100
        desktop.guard = lambda: None
        desktop.grab = lambda **_: Image.new("RGB",(source.width,200),"white")
        desktop.vision = SimpleNamespace(read=lambda _: [Text(t.text,(t.box[0],t.box[1]-top,
                                                                     t.box[2],t.box[3]-top)) for t in (label,current,expanded)])
        copied = ctypes.create_unicode_buffer("1234567890123456789")
        sequence = [1]
        clicked = []
        def click(frame,x,y):
            clicked.append((x,y))
            sequence[0] += 1
        desktop.click = click
        desktop.u = SimpleNamespace(GetClipboardSequenceNumber=lambda:sequence[0], OpenClipboard=lambda _:True,
                                    GetClipboardData=lambda _:1, CloseClipboard=lambda:None)
        desktop.k = SimpleNamespace(GlobalLock=lambda _:ctypes.addressof(copied),GlobalUnlock=lambda _:None)
        self.assertEqual(desktop.copy(source,source.texts[1]),"1234567890123456789")
        self.assertEqual(clicked,[(current.x,current.y)])
        self.assertEqual(order_expand(source).y, expanded.y)

    def test_wechat_search_box_is_not_a_chat_message_or_search_suggestion(self):
        p=Page(1,(0,0,880,640),[text("搜索",95,46,40),text("微信小店",110,125,80),text("搜索",400,420,50)],Image.new("RGB",(880,640),"white"))
        self.assertLess(wechat_search_box(p).y,80)
        p.texts=p.texts[1:]
        self.assertIsNone(wechat_search_box(p))

    def test_wechat_search_field_can_be_located_with_an_existing_query(self):
        image=Image.new("RGB",(880,640),"#ededed")
        ImageDraw.Draw(image).rounded_rectangle((74,42,252,70),radius=7,fill="white",outline="#75caaa")
        p=Page(1,(0,0,880,640),[],image)
        target=wechat_search_box(p)
        self.assertIsNotNone(target)
        self.assertTrue(74<target.x<252 and 42<target.y<70)

    def test_recipient_does_not_use_the_logistics_signed_location_or_courier(self):
        p=page([text("已签收 您的快件已投递，收件人:示例小区门卫",40,140,340),
                text("快递员 李师傅 13800000000",40,175,330),
                text("张测试138****0000号码保护>",60,215,230),text("进店",350,290)])
        self.assertEqual(detail_recipient(p),"张测试")

    def test_recipient_supports_separate_boxes_and_keeps_name_masking(self):
        p=page([text("张*",40,215,40),text("138****0000",90,215,105),
                text("号码保护",220,215,65),text("进店",350,290)])
        self.assertEqual(detail_recipient(p),"张*")

    def test_recipient_label_can_be_read_without_a_phone_number(self):
        self.assertEqual(detail_recipient(page([text("收件人：欧阳·云",40,210,200),text("进店",350,290)])),"欧阳·云")

    def test_fully_masked_recipient_is_preserved_without_guessing(self):
        self.assertEqual(detail_recipient(page([text("** 138****0000 号码保护",40,210,230),text("进店",350,290)])),"**")

    def test_logistics_description_alone_cannot_invent_a_recipient(self):
        self.assertEqual(detail_recipient(page([text("已签收 收件人：示例小区门卫",40,140,340),text("进店",350,290)])),"")

    def test_tracking_box_can_extend_beyond_the_number_into_the_copy_icon(self):
        p=page([Text("圆通速递YT1234567890123",(30,244,235,262))])
        asset=Image.open(Path(__file__).parents[1]/"order_exporter/assets/copy-icon.png").convert("RGB")
        p.image.paste(asset,(220,245))
        target=tracking_copy(p)
        self.assertIsNotNone(target)
        self.assertTrue(222<=target.x<=236)

    def test_carrier_and_number_can_be_separate_ocr_boxes(self):
        p=page([Text("圆通速递",(19,240,82,258)),Text("YT1234567890123",(88,240,208,258))])
        asset=Image.open(Path(__file__).parents[1]/"order_exporter/assets/copy-icon.png").convert("RGB")
        p.image.paste(asset,(210,242))
        target=tracking_copy(p)
        self.assertIsNotNone(target)
        self.assertTrue(210<=target.x<=230)

    def test_unchanged_pixels_reuse_ocr_but_new_pixels_refresh_text(self):
        class Reader:
            calls=0
            def read(self,image):
                self.calls+=1
                return [text(str(image.getpixel((0,0))),0,0)]
        desktop=Desktop.__new__(Desktop)
        desktop.background=False
        desktop.hwnd=1
        desktop.bridge_enabled=False
        desktop.metrics={"ocr_reads":0,"ocr_ms":0.0}
        desktop.guard=lambda:None
        desktop.rect=lambda:(0,0,8,8)
        desktop.ocr_cache=OrderedDict()
        frames=iter([Image.new("RGB",(8,8),color) for color in ("white","white","black","white")])
        desktop.grab=lambda **_:next(frames)
        desktop.vision=Reader()
        first,second,third,fourth=desktop.read(),desktop.read(),desktop.read(),desktop.read()
        self.assertEqual(first.texts,second.texts)
        self.assertNotEqual(second.texts,third.texts)
        self.assertEqual(first.texts,fourth.texts)
        self.assertEqual(desktop.vision.calls,2)

    def test_copy_labels_preserve_leading_zeros(self):
        self.assertEqual(copied_identifier("订单编号： 0001234567890123456789","order"),"0001234567890123456789")
        for value in ("复制成功","1.234567E+18","张三 12345678901"):
            with self.assertRaises(ValueError):
                copied_identifier(value,"order")

    def test_merged_order_copy_target_is_at_the_blue_suffix(self):
        p=page([text("订单编号",14,405),text("2234567890123456789复制",204,405,176)])
        target=labelled_copy(p,("订单编号",))
        self.assertGreater(target.x,350)
        self.assertLess(target.x,381)

    def test_copy_never_uses_the_address_row(self):
        p=page([text("收货地址",20,100),text("复制",340,100),text("订单编号",20,400),text("复制",340,400)])
        self.assertEqual(labelled_copy(p,("订单编号",)).y,408.5)

    def test_repurchase_ad_is_not_an_order(self):
        p=listing((0,))
        p.texts += [text("回购享补贴",20,550),text("广告商品名称",100,600),text("￥39.80",340,600)]
        cards=order_cards(p)
        self.assertEqual(len(cards),1)
        self.assertTrue(cards[0].title.text.startswith("完整商品名称"))

    def test_arrow_is_not_a_home_icon(self):
        p=page([])
        ImageDraw.Draw(p.image).line([(24,52),(15,62),(24,72)],fill="black",width=2)
        self.assertIsNotNone(back_arrow(p))
        p=page([])
        ImageDraw.Draw(p.image).line([(12,63),(22,53),(32,63),(29,63),(29,73),(16,73),(16,63)],fill="black",width=2)
        self.assertIsNone(back_arrow(p))


class WorkbookTests(unittest.TestCase):
    def test_text_numbers_unicode_and_filter_metadata_roundtrip(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as root:
            record=OrderRecord("000123456789012345678901234567890",("=1+1","产品 <测试> & 新版"),
                               ("0000001234567890","YT12345678901234"),"2026-11-14 16:51:29","测试店铺","已送达",recipient="张*",recipient_checked=True)
            output=export_xlsx(Path(root)/"orders.xlsx",[record],{"下单时间范围":"2026-11-14 全天"})
            book=openpyxl.load_workbook(output)
            self.assertEqual(book.sheetnames,["订单明细","采集记录"])
            sheet=book.worksheets[0]
            self.assertEqual(sheet["A2"].value,record.order_id)
            self.assertEqual(sheet["B2"].value,"\n".join(record.products))
            self.assertEqual(sheet["C2"].value,"\n".join(record.tracking_numbers))
            self.assertEqual(sheet["D2"].value,record.ordered_at)
            for name in ("A2","B2","C2"):
                self.assertEqual(sheet[name].data_type,"s")
                self.assertEqual(sheet[name].number_format,"@")
            self.assertEqual(sheet["I1"].value,"收件人")
            self.assertEqual(sheet["I2"].value,"张*")
            self.assertEqual(sheet["I2"].data_type,"s")
            self.assertEqual(sheet.auto_filter.ref,"A1:K2")
            book.close()

    def test_cancelled_order_exports_with_empty_tracking_and_status(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as root:
            output=export_xlsx(Path(root)/"cancelled.xlsx",[OrderRecord("001234567890",("图书",),(),status="已取消")],{})
            book=openpyxl.load_workbook(output)
            self.assertIn(book.worksheets[0]["C2"].value,("",None))
            self.assertEqual(book.worksheets[0]["F2"].value,"已取消")
            book.close()

    def test_failed_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as root:
            output=Path(root)/"orders.xlsx"
            output.write_bytes(b"previous")
            with patch("order_exporter.workbook.os.replace",side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    export_xlsx(output,[],{})
            self.assertEqual(output.read_bytes(),b"previous")
            self.assertEqual(list(Path(root).glob(".订单导出-*.xlsx")),[])

    def test_zero_matching_orders_is_a_valid_workbook(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as root:
            output=export_xlsx(Path(root)/"empty.xlsx",[],{"说明":"没有符合时间范围的订单"})
            book=openpyxl.load_workbook(output)
            self.assertEqual(book.worksheets[0].max_row,1)
            self.assertIn(("说明","没有符合时间范围的订单"),list(book.worksheets[1].values))
            book.close()


if __name__=="__main__":
    unittest.main()
