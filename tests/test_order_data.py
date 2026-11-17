import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from order_exporter.direct import Collector, order_time, record_from_payload
from order_exporter.models import Filters
from order_exporter.native import PageError, Paused
from order_exporter.order_protocol import command, decode, field, response


def listing(identifier, moment, shop='测试书店'):
    return {'order_id':identifier,'products':['书籍'],'ordered_at':moment,'shop':shop,'status':'已取消'}


class FakeAPI:
    def __init__(self, pages, details=None):
        self.pages=iter(pages)
        self.details_by_id=details or {}
        self.detail_calls=[]
        self.list_calls=[]
        self.check=Mock()

    def list(self, cursor='0', page_size=50):
        self.list_calls.append(cursor)
        return next(self.pages)

    def details(self, orders):
        self.detail_calls.append([o['order_id'] for o in orders])
        return [self.details_by_id.get(o['order_id'],{**o,'tracking_numbers':[]}) for o in orders]


def page(orders, more=False, cursor=''):
    return {'orders':orders,'has_more':more,'cursor':cursor}


class DirectCollectorTests(unittest.TestCase):
    def run_collection(self, api, filters=None, completed=(), count=0):
        desktop=SimpleNamespace(stop=threading.Event())
        collector=Collector(desktop,lambda _:None,filters,completed_ids=completed,api=api)
        collector.prepare=Mock()
        records=[]
        result=collector.collect(lambda r:records.append(r) or True,initial_count=count)
        collector.prepare.assert_called_once()
        return result,records

    def test_millisecond_and_second_times_use_beijing_and_keep_inclusive_boundaries(self):
        self.assertEqual(order_time(1794646289000),'2026-11-14 16:51:29')
        self.assertEqual(order_time(1794646289),'2026-11-14 16:51:29')
        query=Filters('2026-11-14 16:51:29','2026-11-14 16:51:29')
        self.assertTrue(query.includes_time(order_time(1794646289000)))

    def test_follows_server_cursor_and_skips_existing_records_before_detail_requests(self):
        rows=[listing('001',1794646289000),listing('002',1794646200000),listing('003',1794646100000)]
        api=FakeAPI([page(rows[:2],True,'cursor-next'),page([rows[1],rows[2]])])
        count,records=self.run_collection(api,completed={'001'},count=1)
        self.assertEqual(count,3)
        self.assertEqual([r.order_id for r in records],['002','003'])
        self.assertEqual(api.list_calls,['0','cursor-next'])
        self.assertEqual(api.detail_calls,[['002'],['003']])

    def test_date_and_shop_filters_do_not_fetch_unneeded_details(self):
        rows=[listing('future',1794732689000),listing('book',1794646289000,'万唯'),
              listing('other',1794646200000,'其他店'),listing('old',1794560000000)]
        api=FakeAPI([page(rows)])
        count,records=self.run_collection(api,Filters('2026-11-14 00:00:00','2026-11-14 23:59:59','万唯'))
        self.assertEqual(count,1)
        self.assertEqual(api.detail_calls,[['book']])

    def test_out_of_order_history_keeps_paging_until_end(self):
        rows=[listing('a',1794646289000),listing('old',1794500000000),listing('b',1794646200000)]
        api=FakeAPI([page(rows,True,'next'),page([listing('c',1794646100000)])])
        count,records=self.run_collection(api,Filters('2026-11-14 00:00:00','2026-11-14 23:59:59'))
        self.assertEqual(count,3)
        self.assertEqual(api.list_calls,['0','next'])

    def test_details_are_limited_to_four_concurrent_orders(self):
        api=FakeAPI([page([listing(str(i),1794646289000) for i in range(9)])])
        count,_=self.run_collection(api)
        self.assertEqual(count,9)
        self.assertEqual([len(batch) for batch in api.detail_calls],[4,4,1])

    def test_repeated_pagination_cursor_does_not_claim_a_complete_export(self):
        api=FakeAPI([page([listing('a',1794646289000)],True,'0')])
        with self.assertRaisesRegex(PageError,'游标重复'):
            self.run_collection(api)

    def test_missing_next_cursor_does_not_restart_from_top(self):
        api=FakeAPI([page([],True,None)])
        with self.assertRaisesRegex(PageError,'下一页游标'):
            self.run_collection(api)

    def test_wrong_order_detail_never_enters_the_result(self):
        with self.assertRaisesRegex(PageError,'订单号'):
            record_from_payload({'order_id':'999','products':['书籍']},listing('123',1794646289000))

    def test_identifier_precision_recipient_and_multi_package_numbers_are_preserved(self):
        item=listing('0001234567890123456789',1794646289000)
        result=record_from_payload({**item,'recipient':'张测试','shipping_known':True,
          'tracking_numbers':['YT1234567890123','001234567890123456789','YT1234567890123']},item)
        self.assertEqual(result.order_id,'0001234567890123456789')
        self.assertEqual(result.tracking_numbers,('YT1234567890123','001234567890123456789'))
        self.assertEqual(result.recipient,'张测试')
        self.assertTrue(result.recipient_checked)

    def test_empty_cancelled_order_and_missing_shipped_tracking_have_distinct_states(self):
        item=listing('123',1794646289000)
        self.assertEqual(record_from_payload(item,item).tracking_state,'无物流（接口未返回）')
        self.assertEqual(record_from_payload({**item,'shipping_known':True},item).tracking_state,'待补采')

    def test_pause_interrupts_before_network_requests(self):
        api=FakeAPI([])
        api.check.side_effect=Paused('已暂停')
        with self.assertRaises(Paused):self.run_collection(api)
        self.assertEqual(api.list_calls,[])

    def test_preparation_reopens_only_the_selected_wechat_account(self):
        from order_exporter.native import Window
        main=Window(1,'微信',r'C:\Weixin.exe',10)
        search=Window(2,'微信',r'C:\WeChatAppEx.exe',20)
        own=Window(3,'小店',r'C:\WeChatAppEx.exe',20)
        other=Window(4,'小店',r'C:\WeChatAppEx.exe',30)
        opened=Window(5,'小店',r'C:\WeChatAppEx.exe',20)
        windows=[main,search,own,other]
        desktop=Mock()
        desktop.background=True
        desktop.windows.side_effect=lambda:list(windows)
        desktop.post.side_effect=lambda hwnd,*_:windows.remove(own)
        api=Mock()
        api.ready=threading.Event()
        entry=Mock()
        def create_entry(*args,**kwargs):
            def prepare_entry(before,selected):
                kwargs['before_open'](selected)
                return opened
            entry._open_from_search.side_effect=prepare_entry
            return entry
        collector=Collector(desktop,lambda _:None,target=main,api=api)
        with patch('order_exporter.direct.wechat_pid',side_effect=lambda w:99 if w==other else 10), \
             patch('order_exporter.direct.wechat_order_center',return_value=object()), \
             patch('order_exporter.direct.browser_pid',return_value=20), \
             patch('order_exporter.direct.EntryPreparer',side_effect=create_entry):
            collector.prepare()
        desktop.post.assert_called_once_with(own.hwnd,0x0010,0,0)
        self.assertIn(other,windows)
        self.assertEqual(entry._open_from_search.call_args.args[1],search)
        api.start.assert_called_once_with(20)
        self.assertEqual(collector.bound_window,search)
        desktop.end_preparation.assert_called_once()
        desktop.scroll.assert_not_called()

    def test_preparation_failure_returns_input_to_the_previous_window(self):
        from order_exporter.native import Window
        source=Window(2,'微信',r'C:\WeChatAppEx.exe',20)
        desktop=Mock()
        desktop.background=True
        desktop.windows.return_value=[source]
        desktop.begin_preparation.return_value=55
        entry=Mock()
        entry._open_from_search.side_effect=PageError('连接失败')
        collector=Collector(desktop,lambda _:None,target=source,api=Mock())
        with patch('order_exporter.direct.wechat_pid',return_value=10), \
             patch('order_exporter.direct.EntryPreparer',return_value=entry), \
             self.assertRaises(PageError):
            collector.prepare()
        desktop.end_preparation.assert_called_once_with(55)


class ProtocolTests(unittest.TestCase):
    def test_large_payload_and_sequence_survive_the_native_envelope(self):
        payload={'id':300,'result':{'result':{'value':'汉字'*10000}}}
        encoded=field(3,'chromeDevtoolsResult')+field(4,field(1,300)+field(2,json.dumps(payload)))
        self.assertEqual(response(encoded),payload)
        sent=decode(command(300,'Runtime.evaluate',{'expression':'1'}))
        self.assertEqual(sent[3],b'chromeDevtools')
        self.assertEqual(json.loads(decode(sent[4])[2])['id'],300)

    def test_truncated_packet_is_an_error(self):
        with self.assertRaises(ValueError):decode(b'\x12\x08x')
        with self.assertRaises(ValueError):decode(b'\x80')


if __name__=='__main__':unittest.main()
