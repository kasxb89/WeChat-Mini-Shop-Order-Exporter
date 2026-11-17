"""沿用客服平台的离线 RapidOCR；页面定位使用文字，编号使用复制结果。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from functools import lru_cache
import os
import re


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":")


@dataclass(frozen=True)
class Text:
    text: str
    box: tuple[float, float, float, float]
    confidence: float = 1.0
    role: str = "text"

    @property
    def x(self):
        return (self.box[0] + self.box[2]) / 2

    @property
    def y(self):
        return (self.box[1] + self.box[3]) / 2


@dataclass
class Page:
    hwnd: int
    rect: tuple[int, int, int, int]
    texts: list[Text]
    image: object = None
    source: str = "ocr"
    verified_identifiers: dict[str,str] | None = None

    @property
    def width(self):
        return self.rect[2] - self.rect[0]

    @property
    def height(self):
        return self.rect[3] - self.rect[1]

    def find(self, *labels: str, contains: bool = False) -> list[Text]:
        wanted = {compact(t) for t in labels}
        return [t for t in self.texts if t.confidence >= .65 and (
            any(s in compact(t.text) for s in wanted) if contains else compact(t.text) in wanted
        )]

    def has(self, *labels: str) -> bool:
        return bool(self.find(*labels, contains=True))

    def empty_shipping(self) -> bool:
        return (bool(self.find("运输中")) and self.has("暂无订单", "没有相关订单", "暂无相关订单")
                and not self.find("查看物流"))

    def empty_orders(self) -> bool:
        return bool(self.find("全部订单")) and self.has("暂无订单", "没有相关订单", "暂无相关订单")

    def fingerprint(self) -> tuple[str, ...]:
        if self.find("全部订单") and self.find("运输中", "待发货", contains=True):
            # 回购推荐、直播标记会变化，但不属于订单，避免因此一直等待或重扫。
            headers = order_headers(self)
            values = ([f"{compact(t.text)}:{round(t.y/6)}" for t in headers] +
                         [f"{c.status}:{compact(c.title.text)}:{round(c.title.y/6)}" for c in order_cards(self)] +
                         [t.text for t in self.find("暂无订单", "没有相关订单", "暂无相关订单", "没有更多订单", "没有更多了", "你可能还会喜欢")])
            return tuple(values or [f"{compact(t.text)}:{round(t.y/6)}" for t in self.texts if t.y > 180]) or ("订单列表",)
        recommendations = self.find("你可能还会喜欢", "猜你喜欢")
        cutoff = min((t.box[1] for t in recommendations), default=self.height)
        return tuple(compact(t.text) for t in sorted(self.texts, key=lambda t: (round(t.y / 10), t.x)) if t.y < cutoff)

    def shipping_selected(self) -> bool:
        return self.tab_selected("运输中")

    def tab_selected(self, label: str) -> bool:
        tabs = self.find(label)
        if len(tabs) != 1 or self.image is None:
            return False
        tab = tabs[0]
        gray = self.image.convert("L")
        left, right = max(0, int(tab.box[0]) - 5), min(self.width, int(tab.box[2]) + 5)
        # 真实小店页的当前标签下方有横线，不用灰色文字推测当前筛选条件。
        for y in range(int(tab.box[3]) + 3, min(self.height, int(tab.box[3]) + 19)):
            if sum(gray.getpixel((x, y)) < 85 for x in range(left, right)) >= (right - left) * .75:
                return True
        return False


def wechat_search_box(page: Page) -> Text | None:
    """主聊天窗口左上搜索框，不匹配会话区或下方搜索建议。"""
    labels = [t for t in page.find("搜索", "微信小店")
              if page.width*.04 < t.x < page.width*.42 and 25 < t.y < min(110, page.height*.18)]
    if len(labels) == 1:
        return labels[0]
    if page.image is None:
        return None
    # 搜索框里已有其他关键词时，用同一区域的圆角输入框边界定位。
    import cv2
    import numpy as np
    gray = np.asarray(page.image.convert("L"))
    height, width = min(round(page.height*.18), 120), round(page.width*.45)
    edges = cv2.Canny(gray[:height, :width], 12, 35)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    matches = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (page.width*.05 < x < page.width*.28 and 25 < y < height-18
                and max(80, page.width*.10) < w < page.width*.4 and 19 < h < 60 and 3 < w/h < 12):
            candidate = Text("搜索框", (x,y,x+w,y+h))
            if not any(abs(t.x-candidate.x)<6 and abs(t.y-candidate.y)<6 for t in matches):
                matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


class Vision:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.ocr = RapidOCR(intra_op_num_threads=min(4, os.cpu_count() or 1), inter_op_num_threads=1,
                            det_limit_type="max", det_limit_side_len=1200)

    def read(self, image) -> list[Text]:
        import numpy as np
        # 原始分辨率已能识别页面字段，避免放大后重复计算商品缩略图中的文字。
        source = image.convert("RGB")
        results, _ = self.ocr(np.asarray(source)[:, :, ::-1], use_cls=False)
        return [Text(text, (min(p[0] for p in box), min(p[1] for p in box),
                                 max(p[0] for p in box), max(p[1] for p in box)), float(score))
                for box, text, score in results or []]


@lru_cache(maxsize=1)
def get_vision():
    return Vision()


def wechat_search_result(page: Page) -> Text | None:
    markers = page.find("搜索网络结果", contains=True)
    if not markers:
        return None
    top = min(t.box[3] for t in markers)
    # 放大镜与结果文字会被 OCR 合并成「Q微信小店」，只在搜索建议中去掉该前缀。
    matches = [t for t in page.texts if t.confidence >= .65 and t.y > top
               and t.x < min(page.width*.5, 450) and compact(t.text).lstrip("Qq") == "微信小店"]
    return min(matches, key=lambda t: t.y, default=None)


def wechat_order_center(page: Page) -> Text | None:
    # 同一个入口可同时暴露按钮和子文字，OCR 也可能把末尾的小图标合入文字。
    targets = [t for t in page.texts if t.confidence >= .65
               and compact(t.text).startswith("订单中心") and len(compact(t.text)) <= 8]
    return min(targets, key=lambda t: (t.box[1], t.x), default=None)


def copied_identifier(text: str, kind: str) -> str:
    """只接受本次复制得到的完整编号；不把 OCR 数字修补为订单号。"""
    text = text.strip().replace("\u200b", "")
    if kind == "order":
        text = re.sub(r"^(?:订单编号|订单号|订单号码)\s*[:：]?\s*", "", text)
        pattern = r"[0-9A-Za-z-]{10,80}"
    else:
        text = re.sub(r"^(?:快递单号|运单号|物流单号)\s*[:：]?\s*", "", text)
        pattern = r"[0-9A-Za-z-]{6,80}"
    if not re.fullmatch(pattern, text):
        raise ValueError("复制结果不是完整的订单编号。" if kind == "order" else "复制结果不是完整的快递单号。")
    return text


def read_identifier(page: Page, kind: str) -> str | None:
    """只读完整控件值，或已在后台独立核对过的完整编号。"""
    if page.verified_identifiers is not None:
        return page.verified_identifiers.get(kind)
    if page.source != "uia" or kind not in ("order", "tracking"):
        return None
    order_labels = r"订单编号|订单号码|订单号"
    tracking_labels = (r"快递单号|运单号|物流单号|快递公司|顺丰(?:速运|快递)?|圆通(?:速递|快递)?|"
                       r"中通(?:快递)?|申通(?:快递)?|韵达(?:快递)?|极兔(?:速递|快递)?|"
                       r"邮政(?:快递|速递)?|京东(?:快递|物流)?|德邦(?:快递|物流)?|百世(?:快递)?|"
                       r"(?<![A-Za-z0-9])EMS(?![A-Za-z0-9])")
    labels = re.compile(order_labels if kind == "order" else tracking_labels, re.I)
    boundaries = re.compile(order_labels + "|" + tracking_labels +
                            r"|手机|电话|联系|客服|签收时间|下单时间|支付时间|发货时间|"
                            r"创建时间|价格|金额|总价|合计|实付款|[￥¥]", re.I)
    recommended = re.compile(r"你可能还会喜欢|猜你喜欢|为你推荐|推荐商品")
    cutoff = min((t.box[1] for t in page.texts if recommended.fullmatch(compact(t.text))),
                 default=page.height)
    texts = [t for t in page.texts if t.confidence >= .65 and t.box[1] < cutoff
             and not recommended.search(t.text)]
    candidates = set()
    for label in texts:
        lines = label.text.splitlines()
        for line in lines:
            for match in labels.finditer(line):
                field = line[match.end():]
                if len(lines) == 1:
                    # 标签和值可由两个公开控件提供；只连接同一行、标签右侧的控件。
                    peers = [t for t in texts if t is not label and len(t.text.splitlines()) == 1
                             and t.box[0] >= label.box[2] - 3
                             and abs(t.y - label.y) <= min(10, max(3, min(
                                 t.box[3] - t.box[1], label.box[3] - label.box[1]) * .5))]
                    field += " " + " ".join(t.text for t in sorted(peers, key=lambda t: t.x))
                field = boundaries.split(field, maxsplit=1)[0]
                field = field.replace("\u200b", "")
                if re.search(r"\*|＊|…|\.{2,}", field):
                    return None
                values = set()
                invalid = False
                # 不拼接被空格拆开的数字，也不从带小数点或下划线的内容中截取编号。
                for token in re.findall(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9-]+(?![A-Za-z0-9_.-])", field):
                    if not re.search(r"\d", token):
                        continue
                    try:
                        value = copied_identifier(token, kind)
                    except ValueError:
                        invalid = True
                        continue
                    if (re.fullmatch(r"(?:86)?1[3-9]\d{9}", value)
                            or re.fullmatch(r"(?:19|20)\d{2}-\d{1,2}-\d{1,2}", value)
                            or re.search(re.escape(token) + r"\s*(?:元|件|人|折|%)", field)):
                        invalid = True
                        continue
                    values.add(value)
                if invalid or len(values) > 1:
                    return None
                candidates.update(values)
                if len(candidates) > 1:
                    return None
    return next(iter(candidates)) if len(candidates) == 1 else None


def labelled_copy(page: Page, labels: tuple[str, ...]) -> Text | None:
    """匹配同一行、右侧最近的复制按钮，避免复制收货地址等无关字段。"""
    labels_found = page.find(*labels, contains=True)
    copies = copy_buttons(page)
    pairs = []
    for label in labels_found:
        for copy in copies:
            if copy.x > label.x and abs(copy.y - label.y) <= max(18, label.box[3] - label.box[1]):
                pairs.append((copy.x - label.x, copy))
    return min(pairs, key=lambda pair: pair[0])[1] if pairs else None


def copy_buttons(page: Page) -> list[Text]:
    buttons = []
    for t in page.texts:
        value = compact(t.text)
        if not value.endswith("复制"):
            continue
        # 实际订单截图中编号和「复制」会被 OCR 合成同一行，单独取中文按钮区域。
        units = sum(.55 if c.isascii() else 1 for c in value)
        width = (t.box[2] - t.box[0]) * 2 / units
        buttons.append(Text("复制", (t.box[2]-width, t.box[1], t.box[2], t.box[3]), t.confidence))
    return buttons


STATUSES = ("已取消", "已送达", "已签收", "已完成", "待付款", "待支付", "待发货", "待收货",
            "运输中", "交易成功", "交易关闭", "已关闭", "退款成功", "退款中", "售后中", "已退款", "部分发货", "已发货")


def order_headers(page: Page) -> list[Text]:
    tabs = page.find("全部订单")
    lower = min((t.box[3] for t in tabs), default=130) + 12
    return sorted([t for t in page.texts if compact(t.text) in STATUSES
                   and t.x > page.width * .72 and lower < t.y < page.height - 35], key=lambda t: t.y)


@dataclass(frozen=True)
class OrderCard:
    title: Text
    status: str
    shop: str
    header_y: float


def _card_bottom(page, header_y, next_y):
    bottom = min(page.height - 28, next_y - 12)
    if page.image is None:
        return bottom
    # 白色订单卡位于灰色页面上；同一列的灰色间隙把中间的推荐商品卡隔开。
    gray = page.image.convert("L")
    x = round(page.width * .045)
    run = 0
    for y in range(round(header_y + 18), round(bottom)):
        run = run + 1 if gray.getpixel((x, y)) < 242 else 0
        if run >= 6:
            return y - 5
    return bottom


def clean_product(text: str) -> str:
    return re.split(r"[￥¥]\s*\d", text, maxsplit=1)[0].strip()


def order_cards(page: Page) -> list[OrderCard]:
    headers = order_headers(page)
    cards = []
    for index, header in enumerate(headers):
        next_y = headers[index+1].y if index+1 < len(headers) else page.height
        bottom = _card_bottom(page, header.y, next_y)
        names = [t for t in page.texts if header.y + 16 < t.y < min(bottom - 20, page.height - 45)
                 and t.box[0] > page.width * .19 and t.box[0] < page.width * .65
                 and len(compact(clean_product(t.text))) >= 4 and t.confidence >= .80
                 and not re.search(r"共\d+件|付款|支付|退款|收货|物流|运费|保费|退货|评价|补贴|购买|进店|极速退|运费险|假一赔", t.text)]
        if not names:
            continue
        title = min(names, key=lambda t: t.y)
        shops = [t for t in page.texts if abs(t.y-header.y) < 15 and page.width*.1 < t.box[0] < page.width*.65
                 and not re.search(r"^官方旗舰店$|直播中|进店", compact(t.text))]
        shop = min(shops, key=lambda t: t.box[0]).text if shops else ""
        cards.append(OrderCard(title, compact(header.text), shop, header.y))
    return cards


DATE = re.compile(r"(20\d{2})\s*[/年.\-]\s*(\d{1,2})\s*[/月.\-]\s*(\d{1,2})日?\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?")


def ordered_time(page: Page) -> str:
    labels = page.find("下单时间", "创建时间", "下单日期", contains=True)
    for label in labels:
        candidates = [label] + sorted([t for t in page.texts if t.x > label.x and abs(t.y-label.y) < 18], key=lambda t: t.x)
        for t in candidates:
            match = DATE.search(t.text)
            if match:
                y, m, d, hour, minute, second = match.groups()
                try:
                    return datetime(int(y), int(m), int(d), int(hour), int(minute), int(second or 0)).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
    return ""


def order_expand(page: Page) -> Text | None:
    labels = page.find("订单编号", "订单号", contains=True)
    targets = [(t.y-label.y, t) for label in labels for t in page.find("展开", contains=True)
               if 10 < t.y-label.y < 80 and page.width*.25 < t.x < page.width*.7]
    return min(targets, key=lambda pair: pair[0])[1] if targets else None


def detail_shop(page: Page) -> str:
    entries = page.find("进店", contains=True)
    for entry in entries:
        names = [t for t in page.texts if abs(t.y-entry.y) < 25 and page.width*.1 < t.box[0] < page.width*.6
                 and t.x < entry.x and not re.search(r"超\d|人评价|印刷清晰|^官方旗舰店$|回头客", compact(t.text))]
        if names:
            return min(names, key=lambda t: t.y).text
    return ""


def detail_recipient(page: Page) -> str:
    """只读订单地址卡的姓名；物流的投递/签收描述不是订单收件人。"""
    lower = min((t.box[1] for t in page.find("进店", "订单编号", "订单号", contains=True)), default=page.height*.6)
    candidates = sorted((t for t in page.texts if 85 < t.y < lower and t.confidence >= .75), key=lambda t: (t.y, t.x))
    groups = []
    for item in candidates:
        if groups and abs(item.y-groups[-1][0].y) < 12:
            groups[-1].append(item)
        else:
            groups.append([item])
    phone = re.compile(r"(?<![\d*＊])1[3-9]\d(?:\s*[\d*＊]){8}(?![\d*＊])")
    label = re.compile(r"^(?:收件人(?:姓名)?|收货人(?:姓名)?|姓名)\s*[:：]?\s*")
    excluded = re.compile(r"快件|投递|签收|代收|派送|快递员|配送员|联系|客服|电话|修改|选择")
    for group in groups:
        row = " ".join(t.text.strip() for t in sorted(group, key=lambda t: t.x))
        if excluded.search(row):
            continue
        match = phone.search(row)
        if match:
            name = label.sub("", row[:match.start()].strip())
        elif label.match(row):
            name = label.sub("", row).strip()
        else:
            continue
        # 保留姓名中的星号和中点，不从地址、号码保护提示或其他行猜姓名。
        if (re.fullmatch(r"[\u3400-\u9fffA-Za-z＊*·•.\- ]{1,30}", name)
                and re.search(r"[\u3400-\u9fffA-Za-z＊*]", name)
                and not re.search(r"地址|号码|保护|信息", name)):
            return name
    return ""


def detail_products(page: Page) -> tuple[str, ...]:
    result = []
    order_rows = page.find("订单编号", "订单号", contains=True)
    lower = min((t.box[1] for t in order_rows), default=page.height - 40)
    excluded = re.compile(r"共\d+件|商品总价|订单总价|优惠|合计|实付款|需付款|实付|退款|运费|支付|服务保障|极速退|运费险|假一赔|客服|收货|再次购买")
    for price in sorted(page.texts, key=lambda t: t.y):
        if not re.search(r"[￥¥]\s*\d", price.text) or price.y >= lower or excluded.search(price.text):
            continue
        name = clean_product(price.text)
        source = price
        if not name or name.replace(".", "").isdigit():
            names = [t for t in page.texts if t.box[0] > page.width*.18 and t.box[0] < page.width*.65
                     and abs(t.y-price.y) < 16 and t.x < price.x and not excluded.search(t.text)]
            if not names:
                continue
            source = min(names, key=lambda t: abs(t.y-price.y))
            name = clean_product(source.text)
        if len(compact(name)) < 4 or source.box[0] < page.width*.18 or excluded.search(name):
            continue
        lines = [name]
        previous_y = source.y
        for t in sorted(page.texts, key=lambda t: t.y):
            if (source.y+9 < t.y < min(lower, source.y+85) and page.width*.18 < t.box[0] < page.width*.75
                    and t.confidence >= .8 and not re.search(r"[￥¥]|^[×xX]\s*\d", t.text)):
                if excluded.search(t.text) or t.y - previous_y > 32:
                    break
                lines.append(t.text.strip())
                previous_y = t.y
        value = " ".join(lines)
        if compact(value) not in [compact(p) for p in result]:
            result.append(value)
    return tuple(result)


def logistics_entry(page: Page) -> Text | None:
    exact = page.find("查看物流", "物流详情", "物流信息")
    if exact:
        return min(exact, key=lambda t: t.y)
    candidates = [t for t in page.texts if 95 < t.y < page.height*.55 and
                  re.search(r"快件|已签收|派送中|包裹已|正在运输|运输途中|已揽收|运输中", t.text)
                  and not re.search(r"签收照片|发货时间|下单时间|支付时间", t.text)]
    return min(candidates, key=lambda t: t.y) if candidates else None


def tracking_copy(page: Page) -> Text | None:
    text_button = labelled_copy(page, ("运单号", "快递单号", "物流单号", "快递", "速递"))
    if text_button:
        return text_button
    # 用户提供的物流页：圆通速递 + YT... + 灰色双页图标，没有「复制」文字。
    number_rows = [t for t in page.texts if re.search(r"[A-Za-z]{0,4}\d{8,}", compact(t.text))]
    carriers = [t for t in page.texts if re.search(r"快递|速递|物流|顺丰|圆通|中通|申通|韵达|极兔|邮政|京东|德邦|百世|菜鸟|EMS", t.text)
                and not re.search(r"快递员|联系|客服|电话|派送员|地址", t.text)]
    # 实际页面会把「快递公司」「运单号」拆成两个 OCR 框，仍按同一行定位右侧图标。
    rows = [t for t in number_rows if any(abs(c.y-t.y) < 18 and c.box[0] <= t.box[2] for c in carriers)]
    if page.image is None or not rows:
        return None
    import cv2
    import numpy as np
    from PIL import Image
    template = np.asarray(Image.open(Path(__file__).with_name("assets") / "copy-icon.png").convert("L"))
    gray = np.asarray(page.image.convert("L"))
    for row in rows:
        number = re.search(r"[A-Za-z]{0,4}\d{8,}", compact(row.text))
        value = compact(row.text)
        units = lambda s: sum(.55 if c.isascii() else 1 for c in s)
        estimate = row.box[0] + (row.box[2]-row.box[0]) * units(value[:number.end()]) / units(value)
        # 实机 OCR 框有时包含图标但文字不含图标，保留足够回看宽度，不能裁掉图标左边缘。
        left = max(0, round(estimate)-max(32, round((row.box[3]-row.box[1])*2)))
        right = min(page.width, round(row.box[2])+36)
        top, bottom = max(0, round(row.y)-20), min(page.height, round(row.y)+20)
        region = gray[top:bottom, left:right]
        best = None
        for scale in (.75, 1, 1.25, 1.5, 1.75, 2):
            icon = cv2.resize(template, (round(template.shape[1]*scale), round(template.shape[0]*scale)))
            if icon.shape[0] > region.shape[0] or icon.shape[1] > region.shape[1]:
                continue
            scores = cv2.matchTemplate(region, icon, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(scores)
            if score >= .82 and (best is None or score > best[0]):
                x, y = left+loc[0], top+loc[1]
                best = score, Text("复制快递单号", (x, y, x+icon.shape[1], y+icon.shape[0]))
        if best:
            return best[1]
    return None


def back_arrow(page: Page) -> tuple[float, float] | None:
    """只匹配内容标题栏中的左向箭头；首页的房屋图标不视为返回。"""
    explicit = page.find("返回")
    if len(explicit) == 1 and explicit[0].x < page.width * .3:
        return explicit[0].x, explicit[0].y
    titles = [t for t in page.find(*STATUSES, "订单详情", "物流详情")
              if 38 < t.y < 130 and page.width*.25 < t.x < page.width*.75]
    if titles:
        title = min(titles, key=lambda t: t.y)
        return max(16, min(40, (title.box[3]-title.box[1])*1.1)), title.y
    if page.image is None:
        return None
    import cv2
    import numpy as np
    image = np.asarray(page.image.convert("L"))
    top, bottom = round(page.height * .04), round(min(page.height * .18, 130))
    crop = (image[top:bottom, :round(page.width * .17)] < 100).astype("uint8")
    count, labels, stats, _ = cv2.connectedComponentsWithStats(crop)
    matches = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not (5 <= w <= 26 and 11 <= h <= 32 and .25 < w / h < .9):
            continue
        shape = labels[y:y+h, x:x+w] == i
        # 左向折线：中点最靠左，两端向右展开。
        centers = []
        for part in np.array_split(shape, 5, axis=0):
            _, xs = np.where(part)
            centers.append(float(xs.mean()) if len(xs) else -100)
        if (min(centers[0], centers[4]) - centers[2] > w * .35
                and abs(centers[0] - centers[4]) < w * .3
                and area < w * h * .65):
            matches.append((float(x + w/2), float(top + y + h/2)))
    return matches[0] if len(matches) == 1 else None
