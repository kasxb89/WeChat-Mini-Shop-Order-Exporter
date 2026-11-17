from __future__ import annotations

import re
import time

from .models import Filters, OrderRecord, Progress
from .native import PageError
from .vision import (Page, compact, labelled_copy, copied_identifier, back_arrow, order_cards,
                     ordered_time, order_expand, detail_shop, detail_products, logistics_entry,
                     tracking_copy, clean_product, detail_recipient, wechat_search_result, read_identifier, wechat_order_center)


class Collector:
    def __init__(self, desktop, progress, filters: Filters | None = None, completed_ids=None, completed_times=None, target=None, before_open=None):
        self.desktop = desktop
        self.progress = progress
        self.filters = filters or Filters()
        self.count = self.inspected = self.skipped = 0
        self.seen_ids = set()
        self.completed_ids = set(completed_ids or ())
        self.completed_times = completed_times or {}
        self.target = target
        self.before_open = before_open
        self.bound_window = None
        self.last_order_time = ""
        self.time_samples = 0
        self.time_descending = True
        self.ended_before_range = False
        self.end_reason = ""

    def report(self, stage, message):
        self.progress(Progress(stage, message, self.count))

    def observe_time(self, moment):
        if not moment:
            return
        if self.last_order_time and moment > self.last_order_time:
            self.time_descending = False
        self.last_order_time = moment
        self.time_samples += 1
        if (self.filters.start and self.filters.descending and self.time_descending
                and self.time_samples >= 3 and moment < self.filters.start):
            self.ended_before_range = True
            self.end_reason = "已按从新到旧的顺序读到早于起始时间的订单。"

    def mini(self, *, open_if_missing: bool):
        desktop = self.desktop
        windows = desktop.windows()
        candidates = [w for w in windows if w.title in {"小店", "微信小店"}]
        if self.target:
            selected = next((w for w in windows if w.hwnd == self.target.hwnd and w.path.lower() == self.target.path.lower()
                             and (not self.target.pid or w.pid == self.target.pid)), None)
            if selected is None:
                raise PageError("所选微信窗口已关闭或重新启动，请点击「重新检测」并选择窗口。")
            if selected.title in {"小店", "微信小店"}:
                candidates = [selected]
            elif open_if_missing:
                candidates = [self.open_from_search(windows, selected)]
            else:
                raise PageError("请选择该账号已打开的微信小店窗口。")
        elif len(candidates) > 1:
            raise PageError("检测到多个微信小店，请在「采集窗口」选择要导出的那个；两个微信都可以保持打开。")
        elif not candidates and open_if_missing:
            candidates = [self.open_from_search(windows)]
        if len(candidates) != 1:
            raise PageError("未检测到微信小店窗口。请在已登录微信中打开「微信小店 → 订单中心 → 全部订单」。")
        desktop.bind(candidates[0])
        self.bound_window = candidates[0]
        return self.wait_page(lambda p: bool(p.find("全部订单", "订单中心")) or p.has("订单编号", "订单号")
                              or back_arrow(p) is not None or tracking_copy(p) is not None,
                              "微信小店尚未载入订单页面，请稍后继续。")

    def wait_window(self, old_windows, predicate, message):
        old_ids = {w.hwnd for w in old_windows}
        for _ in range(40):
            self.desktop.wait(.25)
            windows = self.desktop.windows()
            matches = [w for w in windows if predicate(w)]
            active = self.desktop.active_window(windows)
            if active in matches:
                return active
            new = [w for w in matches if w.hwnd not in old_ids]
            if len(new) == 1:
                return new[0]
        raise PageError(message)

    def open_from_search(self, old_windows, selected=None):
        if not getattr(self.desktop,"background",False):
            return self._open_from_search(old_windows,selected)
        previous = self.desktop.begin_preparation()
        self.report("自动准备（前台）", "正在自动打开微信小店，请暂时不要切换窗口；完成后自动转入后台采集。")
        try:
            window = self._open_from_search(old_windows,selected)
            self.desktop.bind(window)
            self.wait_page(lambda p: bool(p.find("全部订单","订单中心")) or p.has("订单编号","订单号")
                           or tracking_copy(p) is not None, "小店订单页面尚未准备完成，请稍后继续。")
            return window
        finally:
            self.desktop.end_preparation(previous)

    def _open_from_search(self, old_windows, selected=None):
        desktop = self.desktop
        if selected is None:
            main = [w for w in old_windows if w.path.lower().endswith(("\\weixin.exe", "\\wechat.exe"))]
            if not main:
                raise PageError("未检测到微信，请先打开并登录微信。")
            if len(main) != 1:
                raise PageError("检测到多个微信，请在「采集窗口」选择账号对应的窗口，再开始采集。")
            selected = main[0]
        if selected.path.lower().endswith(("\\weixin.exe", "\\wechat.exe")):
            self.report("自动准备（前台）", "正在自动搜索微信小店并打开订单中心，请暂时不要切换窗口。")
            desktop.search_wechat(selected)
            page = self.wait_page(lambda p: wechat_search_result(p) is not None, "所选微信的搜索结果尚未载入，请稍后继续。")
            target = wechat_search_result(page)
            desktop.click(page, target.x, target.y)
            selected = self.wait_window(old_windows, lambda w: w.title == "微信" and w.path.lower().endswith("\\wechatappex.exe"),
                                        "所选微信的搜索页未打开，请先在该微信打开「微信小店」。")
        desktop.bind(selected)
        desktop.search_center_pending = True
        page = self.wait_page(lambda p: wechat_order_center(p) is not None,
                              "当前搜索页尚未显示微信小店「订单中心」，请稍后继续。")
        center = wechat_order_center(page)
        if self.before_open:
            self.before_open(selected)
        desktop.click(page, center.x, center.y)
        return self.wait_window(old_windows, lambda w: w.title in {"小店", "微信小店"},
                                "所选账号的订单中心尚未打开，请在该微信打开「订单中心」后重新检测。")

    def stable(self) -> Page:
        return self.wait_page(lambda p: bool(p.texts) and not p.has("加载中", "正在加载"),
                              "页面尚未载入，请稍后继续。")

    def wait_page(self, predicate, message, *, timeout=6, visual=False):
        # 目标页面出现即继续，不为已经读到的同一画面再等一轮。
        until = time.monotonic() + timeout
        next_visual = time.monotonic() + .3
        while True:
            page = self.desktop.read()
            if page.has("登录后查看", "请先登录", "登录微信"):
                raise PageError("请先在微信完成登录，再点击「继续上次」。")
            if predicate(page):
                return page
            if visual and page.source=="uia" and hasattr(self.desktop,"read_visual") and time.monotonic()>=next_visual:
                image_page=self.desktop.read_visual(page)
                if predicate(image_page):
                    return image_page
                next_visual=time.monotonic()+1
            if time.monotonic() >= until:
                raise PageError(message)
            self.desktop.wait(.08)

    def check_page(self) -> str:
        page = self.mini(open_if_missing=True)
        if page.empty_orders() and page.tab_selected("全部订单"):
            return "已识别微信小店「全部订单」，当前暂无订单。"
        if page.tab_selected("全部订单"):
            return f"已识别全部订单列表，当前可见 {len(order_cards(page))} 笔可打开的订单。"
        if page.has("订单编号", "订单号"):
            return "已识别订单详情。下单时间位于订单编号下方的「展开」信息中。"
        if tracking_copy(page):
            return "已识别物流页及单号复制入口。开始采集时将先返回全部订单。"
        return "已检测到微信小店。开始采集时会先打开「全部订单」。"

    def back(self, expected=None, page=None):
        page = page if page is not None else self.stable()
        point = back_arrow(page)
        if point is None:
            raise PageError("未确认当前页的返回按钮，请手动返回「全部订单」后继续。")
        self.desktop.click(page, *point)
        # 返回动画和列表滚动位置恢复比顶部标签稍晚，避免用过渡帧定位下一笔。
        self.desktop.wait(.2)
        if expected == "list" or (expected is None and (page.has("订单编号", "订单号") or detail_shop(page))):
            return self.wait_page(self.list_ready, "返回订单列表尚未完成，请稍后继续。")
        if expected == "detail" or tracking_copy(page):
            return self.wait_page(self.detail_ready, "返回订单详情尚未完成，请稍后继续。")
        previous = page.fingerprint()
        return self.wait_page(lambda p: p.fingerprint() != previous, "返回页面尚未完成，请稍后继续。")

    @staticmethod
    def detail_ready(page):
        return (page.has("订单编号", "订单号") or bool(detail_shop(page))) and not (
            page.find("全部订单") or page.has("展开地图", "包裹内商品"))

    @staticmethod
    def list_ready(page):
        return not page.has("订单编号", "订单号") and page.tab_selected("全部订单") and bool(order_cards(page) or page.empty_orders()
            or page.has("没有更多订单", "没有更多了", "已显示全部订单", "你可能还会喜欢"))

    @staticmethod
    def logistics_ready(page):
        return (not page.has("订单编号", "订单号") and not page.find("全部订单")
                and (read_identifier(page,"tracking") is not None
                     or page.has("展开地图", "包裹内商品", "物流详情", "快递单号", "运单号", "查看签收照片")))

    def all_orders(self, page):
        for _ in range(4):
            tabs = page.find("全部订单")
            if tabs:
                if len(tabs) != 1:
                    raise PageError("出现多个全部订单入口，请先打开订单列表。")
                if not page.tab_selected("全部订单"):
                    self.desktop.click(page, tabs[0].x, tabs[0].y)
                    return self.wait_page(self.list_ready, "全部订单尚未打开，请稍后继续。")
                return page if self.list_ready(page) else self.wait_page(self.list_ready, "全部订单尚未载入，请稍后继续。")
            centers = page.find("订单中心")
            if len(centers) == 1:
                self.desktop.click(page, centers[0].x, centers[0].y)
                page = self.stable()
            else:
                page = self.back(page=page)
        raise PageError("未能返回订单列表，请打开「订单中心 → 全部订单」后继续。")

    def top(self, page):
        previous = None
        while not page.empty_orders():
            fingerprint = page.fingerprint()
            if previous == fingerprint:
                return page
            previous = fingerprint
            page = self.scrolled(page, down=False)
        return page

    def scrolled(self, page, *, down):
        self.desktop.scroll(page, down=down)
        next_page = self.stable()
        # 微信在后台降低重绘频率，首个捕获帧可能仍是滚动前的页面。
        for _ in range(8 if getattr(self.desktop,"background",False) else 1):
            if next_page.fingerprint() != page.fingerprint():
                break
            self.desktop.wait(.12)
            next_page = self.stable()
        return next_page

    def collect(self, save, *, initial_count=0) -> int:
        if self.filters.scope != "all":
            raise PageError("旧运输中任务可继续导出已有记录；请点击「开始新采集」使用全部订单及时间筛选。")
        self.count = initial_count
        self.report("回到列表顶部", "正在打开全部订单，从顶部扫描…")
        page = self.top(self.all_orders(self.mini(open_if_missing=True)))
        if page.empty_orders():
            self.end_reason = "全部订单列表暂无订单。"
            return self.count
        while True:
            cards = order_cards(page)
            for card in cards:
                fresh = page
                def key(value):
                    return compact(clean_product(value.title.text)).rstrip(".．…"), value.status
                # 回购推荐会插入或消失，订单整体移动时重新定位内容；相同商品按可见顺序逐个打开。
                previous_group = [c for c in cards if key(c) == key(card)]
                ordinal = previous_group.index(card)
                matches = [c for c in order_cards(fresh) if key(c) == key(card)]
                if ordinal >= len(matches):
                    # 推荐卡把下一笔挤出屏幕时，留给后续带重叠的滚动重新发现。
                    continue
                self.report("读取订单", f"已查看 {self.inspected} 次，已保存 {self.count} 笔，范围外 {self.skipped} 次。")
                record, page = self.order(fresh, matches[ordinal])
                self.inspected = len(self.seen_ids)
                if record is not None and save(record):
                    self.count += 1
                if self.ended_before_range:
                    return self.count
            if self.end(page):
                if not self.inspected:
                    raise PageError("尚未识别到订单，且没有「暂无订单」提示，不能判定全部导出。")
                self.end_reason = "已到全部订单列表末尾。"
                return self.count
            next_page = self.scrolled(page, down=True)
            if next_page.fingerprint() == page.fingerprint():
                raise PageError("列表停止滚动，但未确认全部订单已到末尾。已有结果已保存，可以继续核对。")
            page = next_page

    @staticmethod
    def end(page):
        if page.has("没有更多订单", "没有更多了", "已显示全部订单"):
            return True
        recommendations = page.find("你可能还会喜欢")
        return bool(recommendations) and not any(c.header_y > min(t.y for t in recommendations) for c in order_cards(page))

    def metadata(self, initial, card):
        page = initial
        products = []
        shop = card.shop
        order_id = moment = ""
        recipient = ""
        expanded = False
        while True:
            for product in ([] if expanded and page.source=="ocr" else detail_products(page)):
                if compact(product) not in [compact(p) for p in products]:
                    products.append(product)
            shop = detail_shop(page) or shop
            moment = ordered_time(page) or moment
            recipient = detail_recipient(page) or recipient
            if not order_id:
                direct = read_identifier(page, "order")
                button = labelled_copy(page, ("订单编号", "订单号", "订单号码"))
                if direct is not None or button:
                    order_id = direct or copied_identifier(self.desktop.copy(page, button), "order")
                    if order_id in self.seen_ids:
                        return None, page
                    self.seen_ids.add(order_id)
                    if order_id in self.completed_ids:
                        self.observe_time(self.completed_times.get(order_id, ""))
                        return None, page
            if order_id and moment:
                break
            expand = order_expand(page)
            if expand and not expanded:
                for attempt in range(2):
                    try:
                        self.desktop.click(page, expand.x, expand.y)
                    except PageError:
                        fresh = self.desktop.read()
                        visual_page = self.desktop.read_visual(fresh) if hasattr(self.desktop,"read_visual") else fresh
                        # 控件快照晚于页面更新时，按钮已消失，但同一订单已展开。
                        if order_id and read_identifier(fresh,"order") == order_id and (
                                ordered_time(visual_page) or visual_page.has("商品总价","订单总价","订单运费")):
                            page = visual_page
                            break
                        raise
                    try:
                        page = self.wait_page(lambda p: bool(ordered_time(p)) or p.has("商品总价", "订单总价", "订单运费"),
                                              "订单信息尚未展开，请稍后继续。", timeout=.8 if not attempt else 6, visual=True)
                        break
                    except PageError:
                        page = self.desktop.read()
                        if ordered_time(page) or page.has("商品总价","订单总价","订单运费"):
                            break
                        expand = order_expand(page)
                        current_id = read_identifier(page,"order")
                        if attempt or expand is None or (current_id is not None and current_id != order_id):
                            raise
                expanded = True
                continue
            next_page = self.scrolled(page, down=True)
            if next_page.fingerprint() == page.fingerprint():
                if not order_id:
                    raise PageError("订单详情中未找到订单编号的复制入口，请保持该详情页供核对。")
                break
            page = next_page
        note = []
        if not products:
            products = [clean_product(card.title.text)]
            note.append("产品名称取自订单列表")
        if any(re.search(r"[.．…]{2,}|…", p) for p in products):
            note.append("标题含省略号，产品名称按页面显示保存")
        if not moment:
            note.append("未显示下单时间")
        return (order_id, tuple(products), moment, shop, note, recipient), page

    def tracking(self, page):
        numbers = []
        parcels = sorted([t for t in page.find("包裹", contains=True) if re.fullmatch(r"包裹\d+", compact(t.text))], key=lambda t: t.x)
        names = list(dict.fromkeys(t.text for t in parcels)) or [None]
        for name in names:
            if name:
                targets = page.find(name)
                if len(targets) != 1:
                    raise PageError("包裹分页发生变化，需重新核对物流。")
                self.desktop.click(page, targets[0].x, targets[0].y)
            direct = read_identifier(page, "tracking")
            target = tracking_copy(page) if direct is None else None
            if name or (direct is None and target is None):
                page = self.wait_page(lambda p: read_identifier(p, "tracking") is not None or tracking_copy(p) is not None,
                                      "物流页的快递单号尚未载入")
                direct = read_identifier(page, "tracking")
                target = tracking_copy(page)
            number = direct or copied_identifier(self.desktop.copy(page, target), "tracking")
            if number not in numbers:
                numbers.append(number)
        return tuple(numbers), page

    def return_list(self, page):
        return self.back(expected="list", page=page)

    def order(self, listing, card):
        # 先确认订单编号和下单时间；物流直接从同一详情进入，不跨列表配对。
        self.desktop.click(listing, card.title.x, card.title.y)
        detail = self.wait_page(self.detail_ready,
                                "订单详情尚未打开，请稍后继续。")
        had_logistics = logistics_entry(detail) is not None
        metadata, detail = self.metadata(detail, card)
        if metadata is None:
            return None, self.return_list(detail)
        order_id, products, moment, shop, notes, recipient = metadata
        self.observe_time(moment)
        if not self.filters.includes_time(moment) or not self.filters.includes_shop(shop):
            self.skipped += 1
            return None, self.return_list(detail)
        self.report("读取物流", "已确认订单符合筛选范围，正在核对物流信息…")
        entry = logistics_entry(detail)
        if entry is None and (had_logistics or card.status not in {"已取消", "待付款", "待支付", "待发货", "交易关闭", "已关闭"}):
            detail = self.top(detail)
            entry = logistics_entry(detail)
        recipient = detail_recipient(detail) or recipient
        if not recipient:
            notes.append("页面未显示收件人")
        numbers = ()
        state = "无物流（页面未显示）"
        if entry:
            self.desktop.click(detail, entry.x, entry.y)
            logistics = self.wait_page(self.logistics_ready,
                                       "物流详情尚未打开，请稍后继续。")
            if logistics.fingerprint() == detail.fingerprint():
                state = "待补采"
                notes.append("物流详情未打开")
            else:
                try:
                    numbers, logistics = self.tracking(logistics)
                    state = "已取得单号"
                except (PageError, ValueError) as error:
                    state = "待补采"
                    notes.append(str(error))
                detail = self.back(expected="detail", page=logistics)
        listing = self.return_list(detail)
        return OrderRecord(order_id, products, numbers, moment, shop, card.status, "；".join(notes), state,
                           recipient=recipient, recipient_checked=True), listing
