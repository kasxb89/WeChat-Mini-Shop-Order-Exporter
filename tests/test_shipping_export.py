"""Shipping data from the miniapp projection through SQLite and Excel."""
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from order_exporter.direct import record_from_payload
from order_exporter.models import OrderRecord
from order_exporter.order_api import PROJECT_JS
from order_exporter.store import Store
from order_exporter.ui import Worker
from order_exporter.workbook import export_xlsx


NODE = shutil.which('node')


def payload(address=None, **order_fields):
    delivery = {'address_info': address or {}}
    finder = {'order_key': {'order_id': '0001234567890123456789'},
              'create_time': 1794646289, 'order_info': {'delivery_info': delivery}}
    return {'baseResp': {'errcode': 0}, 'orderDetail': {
        'orderId': 'UID-0001234567890123456789', 'orderStateStr': '待发货',
        'skuList': [{'spuSnapshot': {'spuName': '测试图书'}}],
        'finderShopOrderStr': json.dumps(finder, ensure_ascii=False), **order_fields}}


def project(value):
    script = PROJECT_JS + '\nconsole.log(JSON.stringify(detailRecord(' + json.dumps(value, ensure_ascii=False) + ')));'
    result = subprocess.run([NODE, '-'], input=script, text=True, encoding='utf-8',
                            capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, 'Node.js is required to execute the miniapp JavaScript projection')
class ShippingProjectionTests(unittest.TestCase):
    def test_pending_shipment_roundtrips_masked_phone_and_full_address_to_excel(self):
        value = project(payload({'user_name': '张测试', 'tel_number': '138****0000',
            'province_name': '示例省', 'city_name': '示例市', 'county_name': '示例区',
            'detail_info': '测试路1号2室', 'number_protect': 1}))
        record = record_from_payload(value, value)
        with tempfile.TemporaryDirectory() as root:
            with_store = Store(Path(root) / 'orders.sqlite3')
            job = with_store.create(Path(root) / 'orders.xlsx')
            with_store.save(job, record)
            saved = with_store.record(job, record.order_id)
            self.assertEqual(saved, record)
            self.assertEqual(with_store.uncollected_shipping_info(job), 0)
            output = export_xlsx(Path(root) / 'orders.xlsx', with_store.records(job), {})
            with_store.close()
            import openpyxl
            book = openpyxl.load_workbook(output)
            try:
                sheet = book.worksheets[0]
                self.assertEqual(sheet['A2'].value, '0001234567890123456789')
                self.assertFalse(sheet['C2'].value)
                self.assertEqual(sheet['F2'].value, '待发货')
                self.assertEqual(sheet['I2'].value, '张测试')
                self.assertEqual(sheet['J1'].value, '联系电话')
                self.assertEqual(sheet['J2'].value, '138****0000')
                self.assertEqual(sheet['K1'].value, '收货地址')
                self.assertEqual(sheet['K2'].value, '示例省示例市示例区测试路1号2室')
                for cell in ('A2', 'I2', 'J2', 'K2'):
                    self.assertEqual(sheet[cell].data_type, 's')
                    self.assertEqual(sheet[cell].number_format, '@')
                self.assertEqual(sheet.auto_filter.ref, 'A1:K2')
            finally:
                book.close()

    def test_only_current_receiving_address_is_exported(self):
        response = payload({'user_name': '当前收件人', 'tel_number': '010-00000000',
                            'detail_info': '当前地址'},
            customerAddressInfo={'contactName': '旧收件人', 'detailInfo': '旧地址', 'telNumber': '旧电话'},
            deliveryInfo={'expressInfo': {'waybillId': 'YT1234567890123',
                'courierName': '快递员', 'courierPhone': '快递电话', 'signedBy': '门卫'}})
        finder = json.loads(response['orderDetail']['finderShopOrderStr'])
        finder['order_info']['delivery_info'].update({
            'history_address_list': [{'user_name': '历史收件人', 'tel_number': '历史电话'}],
            'approving_address': {'address_info': {'detail_info': '待审核地址'}},
            'virtual_tel_number_info': {'tel_number': '其他电话'}})
        response['orderDetail']['finderShopOrderStr'] = json.dumps(finder)
        value = project(response)
        self.assertEqual((value['recipient'], value['address'], value['contact_phone']),
                         ('当前收件人', '当前地址', '010-00000000'))
        self.assertEqual(value['tracking_numbers'], ['YT1234567890123'])
        self.assertNotIn('finderShopOrderStr', value)

    def test_legacy_camel_case_address_and_leading_zero_phone_are_preserved(self):
        value = project(payload(customerAddressInfo={
            'contactName': '李测试', 'province': '示例省', 'city': '示例市', 'county': '示例区',
            'detailInfo': '测试街2号', 'telNumber': '00123456789'}))
        self.assertEqual(value['recipient'], '李测试')
        self.assertEqual(value['address'], '示例省示例市示例区测试街2号')
        self.assertEqual(value['contact_phone'], '00123456789')

    def test_fully_qualified_detail_does_not_repeat_administrative_area(self):
        value = project(payload({'province_name': '示例省', 'city_name': '示例市',
            'county_name': '示例区', 'detail_info': '示例省示例市示例区测试路3号'}))
        self.assertEqual(value['address'], '示例省示例市示例区测试路3号')

    def test_missing_shipping_data_is_complete_without_inventing_contact_fields(self):
        value = project(payload(deliveryInfo={'expressInfo': {'courierPhone': '快递电话'}}))
        record = record_from_payload(value, value)
        self.assertEqual((record.recipient, record.address, record.contact_phone), ('', '', ''))
        self.assertTrue(record.shipping_info_checked)


class ShippingHistoryTests(unittest.TestCase):
    def test_previous_version_rows_survive_migration_and_backfill_without_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.sqlite3'
            output = Path(root) / 'history.xlsx'
            store = Store(path)
            job = store.create(output)
            store.save(job, OrderRecord('0001234567890123456789', ('测试图书',), (),
                                        recipient='张测试', recipient_checked=True))
            store.state(job, 'complete')
            store.close()
            # Recreate the exact pre-0.9.1 schema with the new columns absent.
            with closing(sqlite3.connect(path)) as db, db:
                for column in ('address', 'contact_phone', 'shipping_info_checked'):
                    db.execute(f'ALTER TABLE orders DROP COLUMN {column}')
            store = Store(path)
            self.assertEqual(store.count(job), 1)
            self.assertEqual(store.records(job)[0].recipient, '张测试')
            self.assertEqual(store.uncollected_shipping_info(job), 1)
            store.close()
            item = {'order_id': '0001234567890123456789', 'products': ['测试图书'],
                    'recipient': '张测试', 'address': '测试路4号', 'contact_phone': '138****0000'}
            worker = Worker(path, job, output)
            outcomes = []
            worker.outcome.connect(lambda status, message: outcomes.append(status))
            with patch('order_exporter.native.Desktop'), patch('order_exporter.direct.Collector') as cls:
                collector = cls.return_value
                collector.bound_window = None
                collector.end_reason = ''
                collector.api.requests = 1
                collector.api.request_seconds = 0
                def collect(save, initial_count):
                    self.assertEqual(cls.call_args.args[3], set())
                    self.assertFalse(save(record_from_payload(item, item)))
                    return initial_count
                collector.collect.side_effect = collect
                worker.run()
            self.assertEqual(outcomes, ['complete'])
            store = Store(path)
            self.assertEqual(store.count(job), 1)
            self.assertEqual(store.uncollected_shipping_info(job), 0)
            self.assertEqual(store.records(job)[0].address, '测试路4号')
            store.close()

    def test_resume_skips_rows_with_verified_empty_shipping_data(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.sqlite3'
            output = Path(root) / 'history.xlsx'
            store = Store(path)
            job = store.create(output)
            store.save(job, OrderRecord('001', ('测试图书',), (),
                recipient_checked=True, shipping_info_checked=True))
            store.close()
            worker = Worker(path, job, output)
            with patch('order_exporter.native.Desktop'), patch('order_exporter.direct.Collector') as cls:
                collector = cls.return_value
                collector.bound_window = None
                collector.end_reason = ''
                collector.api.requests = 0
                collector.api.request_seconds = 0
                collector.collect.return_value = 1
                worker.run()
                self.assertEqual(cls.call_args.args[3], {'001'})
