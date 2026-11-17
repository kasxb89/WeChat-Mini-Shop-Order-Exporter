"""Batch order collection; UI automation is limited to opening the miniapp."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from .automation import Collector as EntryPreparer
from .models import Filters, OrderRecord, Progress, TIME_FORMAT
from .native import PageError
from .order_api import OrderAPI, browser_pid, source_window, wechat_pid
from .vision import wechat_order_center


def order_time(value):
    if not value:
        return ''
    moment = float(value)
    if moment > 100_000_000_000:
        moment /= 1000
    return datetime.fromtimestamp(moment, timezone(timedelta(hours=8))).strftime(TIME_FORMAT)


def record_from_payload(value, listing):
    order_id = str(value['order_id'])
    if order_id != listing['order_id']:
        raise PageError('接口返回的订单号与请求不一致，本次数据未写入。')
    numbers = tuple(dict.fromkeys(str(v) for v in value.get('tracking_numbers', ()) if v))
    pending = not numbers and value.get('shipping_known', False)
    return OrderRecord(order_id, tuple(value.get('products') or listing['products']), numbers,
        order_time(value.get('ordered_at') or listing.get('ordered_at')),
        value.get('shop') or listing.get('shop', ''), value.get('status') or listing.get('status', ''),
        '接口尚未返回已发货包裹的完整单号，请继续补采。' if pending else '',
        '已取得单号' if numbers else '待补采' if pending else '无物流（接口未返回）',
        value.get('recipient', ''), True)


class Collector:
    def __init__(self, desktop, progress, filters=None, completed_ids=None, completed_times=None, target=None, api=None):
        self.desktop = desktop
        self.progress = progress
        self.filters = filters or Filters()
        self.completed_ids = set(completed_ids or ())
        self.target = target
        self.api = api or OrderAPI(desktop.stop, desktop.check_stop)
        self.bound_window = None
        self.count = 0
        self.end_reason = ''

    def report(self, stage, message):
        self.progress(Progress(stage, message, self.count))

    def prepare(self):
        windows = self.desktop.windows()
        selected = self.target
        if selected:
            selected = next((w for w in windows if w == selected), None)
            if selected is None:
                raise PageError('所选微信窗口已关闭，请重新检测并选择账号。')
        else:
            mini = [w for w in windows if w.title in {'小店', '微信小店'}]
            main = [w for w in windows if w.path.lower().endswith(('\\weixin.exe', '\\wechat.exe'))]
            candidates = mini or main
            if len(candidates) != 1:
                raise PageError('请在「采集窗口」选择需要导出的微信账号。')
            selected = candidates[0]
        return_previous = self.desktop.background
        previous = self.desktop.begin_preparation()
        try:
            self.report('自动建立数据连接', '正在自动打开小店订单中心，准备好后批量读取订单，请暂时不要切换微信。')
            owner = wechat_pid(selected)
            if selected.title in {'小店', '微信小店'}:
                selected = source_window(windows, selected)
            miniapps = [w for w in windows if w.title in {'小店', '微信小店'} and wechat_pid(w) == owner]
            for miniapp in miniapps:
                self.desktop.bind(miniapp)
                self.desktop.post(miniapp.hwnd, 0x0010, 0, 0)  # Reopen only this account's miniapp.
                until = time.monotonic() + 4
                while any(w.hwnd == miniapp.hwnd for w in self.desktop.windows()):
                    self.desktop.check_stop()
                    if time.monotonic() > until:
                        raise PageError('微信小店尚未关闭，请关闭后继续建立数据连接。')
                    self.desktop.wait(.05)
            if selected.path.lower().endswith(('\\weixin.exe', '\\wechat.exe')):
                searches = [w for w in windows if w.title == '微信' and w.path.lower().endswith('\\wechatappex.exe')
                            and wechat_pid(w) == owner]
                for search in searches:
                    self.desktop.bind(search)
                    if wechat_order_center(self.desktop.read()) is not None:
                        selected = search
                        break
            def connect(search):
                self.bound_window = search
                self.api.start(browser_pid(search))
            entry = EntryPreparer(self.desktop, self.progress, target=selected, before_open=connect)
            window = entry._open_from_search(self.desktop.windows(), selected)
            self.desktop.bind(window)
            self.api.wait(self.api.ready, 20, '小店业务数据尚未准备好，请保持微信登录后继续。')
        finally:
            if return_previous:
                self.desktop.end_preparation(previous)
            else:
                self.desktop.background = True

    def check_page(self):
        self.prepare()
        result = self.api.list(page_size=1)
        return '订单数据连接正常，当前账号有可读取的订单。' if result['orders'] else '订单数据连接正常，当前账号暂无订单。'

    def collect(self, save, *, initial_count=0):
        if self.filters.scope != 'all':
            raise PageError('历史运输中任务可导出已有结果；请开始新采集读取全部订单。')
        self.count = initial_count
        self.prepare()
        cursor = '0'
        cursors = set()
        seen = set()
        previous_time = ''
        descending = True
        inspected = 0
        while True:
            self.api.check()
            if cursor in cursors:
                raise PageError('订单分页游标重复，已有记录已保存，请继续上次任务。')
            cursors.add(cursor)
            self.report('批量读取订单', f'正在读取订单列表，已保存 {self.count} 笔。')
            page = self.api.list(cursor)
            eligible = []
            before_start = False
            for order in page['orders']:
                moment = order_time(order.get('ordered_at'))
                if previous_time and moment > previous_time:
                    descending = False
                if moment:
                    previous_time = moment
                    inspected += 1
                before_start |= bool(self.filters.start and moment and moment < self.filters.start)
                identifier = order['order_id']
                if identifier in seen:
                    continue
                seen.add(identifier)
                if (identifier in self.completed_ids or not self.filters.includes_time(moment)
                        or not self.filters.includes_shop(order.get('shop', ''))):
                    continue
                eligible.append(order)
            for offset in range(0, len(eligible), 4):
                self.api.check()
                batch = eligible[offset:offset + 4]
                self.report('批量读取详情', f'直接读取商品、收件人和快递单号，已保存 {self.count} 笔。')
                values = self.api.details(batch)
                if len(values) != len(batch):
                    raise PageError('订单详情返回不完整，已有记录已保存，请继续上次任务。')
                for listing, value in zip(batch, values):
                    record = record_from_payload(value, listing)
                    # Use the detail timestamp as the final filter authority.
                    if self.filters.includes_time(record.ordered_at) and self.filters.includes_shop(record.shop):
                        if save(record):
                            self.count += 1
            if self.filters.start and self.filters.descending and descending and inspected >= 3 and before_start:
                self.end_reason = '已按从新到旧的订单数据读到早于起始时间的记录。'
                return self.count
            if not page['has_more']:
                self.end_reason = '已读取全部订单数据。'
                return self.count
            value = page.get('cursor')
            cursor = str(value) if value is not None else ''
            if not cursor:
                raise PageError('平台未返回下一页游标，已有记录已保存，请继续上次任务。')

    def close(self):
        self.api.close()
