from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Filters:
    start: str = ""
    end: str = ""
    shop: str = ""
    scope: str = "all"
    descending: bool = True

    def __post_init__(self):
        if bool(self.start) != bool(self.end):
            raise ValueError("请同时填写开始时间和结束时间，或选择不限时间。")
        if self.start and datetime.strptime(self.start, TIME_FORMAT) > datetime.strptime(self.end, TIME_FORMAT):
            raise ValueError("开始时间不能晚于结束时间。")

    def includes_time(self, value: str) -> bool:
        if not self.start:
            return True
        if not value:
            raise ValueError("尚未取得该订单的下单时间，无法判断是否在指定时间范围内。")
        moment = datetime.strptime(value, TIME_FORMAT)
        return datetime.strptime(self.start, TIME_FORMAT) <= moment <= datetime.strptime(self.end, TIME_FORMAT)

    def includes_shop(self, value: str) -> bool:
        if not self.shop.strip():
            return True
        key = "".join(self.shop.split()).casefold()
        name = "".join(value.split()).casefold()
        if key in name:
            return True
        if not name or re.search(r"[.．…]{2,}|…", name):
            raise ValueError("店铺名尚未完整显示，无法判断筛选条件；可填写已显示的店铺关键词。")
        return False

    def description(self):
        return f"{self.start} 至 {self.end}（含起止时刻）" if self.start else "不限时间"


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    products: tuple[str, ...]
    tracking_numbers: tuple[str, ...]
    ordered_at: str = ""
    shop: str = ""
    status: str = ""
    note: str = ""
    tracking_state: str = "已取得单号"
    recipient: str = ""
    recipient_checked: bool = False
    address: str = ""
    contact_phone: str = ""
    shipping_info_checked: bool = False

    def __post_init__(self):
        if not self.order_id.strip() or not self.products:
            raise ValueError("订单编号和产品名称不能为空。没有物流的订单可以不填快递单号。")
        if any(not v.strip() for v in (*self.products, *self.tracking_numbers)):
            raise ValueError("订单字段不能为空。")
        if not self.tracking_numbers and self.tracking_state == "已取得单号":
            object.__setattr__(self, "tracking_state", "无物流（页面未显示）")
        if not self.tracking_numbers and self.tracking_state == "已取得单号":
            object.__setattr__(self, "tracking_state", "无物流（页面未显示）")


@dataclass(frozen=True)
class Progress:
    stage: str
    message: str
    count: int


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
