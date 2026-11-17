from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from dataclasses import asdict
from contextlib import closing
from datetime import datetime

from .models import Filters, OrderRecord, now


class Store:
    """一笔完整订单一次事务；未完成的任务可以重新从顶部扫描并去重。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', output TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                job_id TEXT NOT NULL REFERENCES jobs(id), order_id TEXT NOT NULL,
                products TEXT NOT NULL, tracking TEXT NOT NULL, captured TEXT NOT NULL,
                PRIMARY KEY (job_id, order_id)
            );
        """)
        # 兼容已安装的 0.1.0 进度文件，不丢弃旧采集记录。
        additions = {
            "jobs": {"filters": "TEXT NOT NULL DEFAULT '{\"scope\":\"shipping\"}'", "source": "TEXT NOT NULL DEFAULT '{}'"},
            "orders": {**{name: "TEXT NOT NULL DEFAULT ''" for name in ("ordered_at", "shop", "status", "note", "tracking_state", "recipient", "address", "contact_phone")},
                       "recipient_checked": "INTEGER NOT NULL DEFAULT 0",
                       "shipping_info_checked": "INTEGER NOT NULL DEFAULT 0"},
        }
        with self.db:
            for table, columns in additions.items():
                present = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in present:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def create(self, output: Path, filters: Filters | None = None, source: dict | None = None) -> str:
        job = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO jobs (id,created,status,message,output,filters,source) VALUES (?,?,?,'',?,?,?)", (
                job, now(), "ready", str(output), json.dumps(asdict(filters or Filters()), ensure_ascii=False),
                json.dumps(source or {}, ensure_ascii=False)))
        return job

    def latest(self):
        row = self.db.execute("SELECT id,created,status,message,output FROM jobs ORDER BY created DESC,rowid DESC LIMIT 1").fetchone()
        return dict(zip(("id", "created", "status", "message", "output"), row)) if row else None

    def history(self):
        rows = self.db.execute("""SELECT j.id,j.created,j.status,j.message,j.output,j.source,
            (SELECT count(*) FROM orders o WHERE o.job_id=j.id) AS orders
            FROM jobs j ORDER BY j.created DESC,j.rowid DESC""")
        return [dict(zip(("id", "created", "status", "message", "output", "source", "orders"), row)) for row in rows]

    def job(self, job: str):
        return next((row for row in self.history() if row["id"] == job), None)

    def set_source(self, job: str, source: dict):
        with self.db:
            self.db.execute("UPDATE jobs SET source=? WHERE id=?", (json.dumps(source, ensure_ascii=False), job))

    def state(self, job: str, status: str, message: str = ""):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,message=? WHERE id=?", (status, message, job))

    def save(self, job: str, record: OrderRecord) -> bool:
        existing = self.db.execute("SELECT products,tracking FROM orders WHERE job_id=? AND order_id=?", (job, record.order_id)).fetchone()
        products = list(record.products)
        tracking = list(record.tracking_numbers)
        if existing:
            # 重扫以本次详情的商品名称为准，避免 OCR 换行差异造成同一商品重复。
            # 快递单号可能来自多个包裹，仍按完整订单号合并。
            tracking = list(dict.fromkeys(json.loads(existing[1]) + tracking))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO orders (job_id,order_id,products,tracking,captured,ordered_at,shop,status,note,tracking_state,recipient,recipient_checked,address,contact_phone,shipping_info_checked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                job, record.order_id, json.dumps(products, ensure_ascii=False),
                json.dumps(tracking, ensure_ascii=False), now(), record.ordered_at, record.shop,
                record.status, record.note, record.tracking_state, record.recipient, int(record.recipient_checked),
                record.address, record.contact_phone, int(record.shipping_info_checked),
            ))
        return existing is None

    def count(self, job: str) -> int:
        return self.db.execute("SELECT count(*) FROM orders WHERE job_id=?", (job,)).fetchone()[0]

    def records(self, job: str) -> list[OrderRecord]:
        return [OrderRecord(row[0], tuple(json.loads(row[1])), tuple(json.loads(row[2])), *row[3:])
                for row in self.db.execute("SELECT order_id,products,tracking,ordered_at,shop,status,note,tracking_state,recipient,recipient_checked,address,contact_phone,shipping_info_checked FROM orders WHERE job_id=? ORDER BY rowid", (job,))]

    def clear(self, job: str | None = None) -> Path:
        backup = self.path.parent / "backups" / f"clear_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3"
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(backup)) as destination:
            self.db.backup(destination)
        with self.db:
            if job is None:
                self.db.execute("DELETE FROM orders")
                self.db.execute("DELETE FROM jobs")
            else:
                self.db.execute("DELETE FROM orders WHERE job_id=?", (job,))
                self.db.execute("DELETE FROM jobs WHERE id=?", (job,))
        return backup

    def record(self, job: str, order_id: str) -> OrderRecord:
        row = self.db.execute("SELECT order_id,products,tracking,ordered_at,shop,status,note,tracking_state,recipient,recipient_checked,address,contact_phone,shipping_info_checked FROM orders WHERE job_id=? AND order_id=?", (job, order_id)).fetchone()
        return OrderRecord(row[0], tuple(json.loads(row[1])), tuple(json.loads(row[2])), *row[3:])

    def uncollected_recipients(self, job: str) -> int:
        return self.db.execute("SELECT count(*) FROM orders WHERE job_id=? AND recipient_checked=0", (job,)).fetchone()[0]

    def uncollected_shipping_info(self, job: str) -> int:
        return self.db.execute("SELECT count(*) FROM orders WHERE job_id=? AND (recipient_checked=0 OR shipping_info_checked=0)", (job,)).fetchone()[0]

    def filters(self, job: str) -> Filters:
        return Filters(**json.loads(self.db.execute("SELECT filters FROM jobs WHERE id=?", (job,)).fetchone()[0]))

    def pending(self, job: str) -> int:
        return self.db.execute("SELECT count(*) FROM orders WHERE job_id=? AND tracking_state='待补采'", (job,)).fetchone()[0]

    def metadata(self, job: str):
        row = self.db.execute("SELECT created,status,message FROM jobs WHERE id=?", (job,)).fetchone()
        filters = self.filters(job)
        names = {"ready": "待开始", "running": "采集中", "paused": "已暂停", "complete": "已完成", "review": "遍历完成，有单号待补采"}
        source = json.loads(self.db.execute("SELECT source FROM jobs WHERE id=?", (job,)).fetchone()[0])
        return {"任务编号": job, "创建时间": row[0], "采集状态": names.get(row[1], row[1]), "说明": row[2],
                "采集窗口": f"{source['title']} · 窗口 {source['hwnd']}" if source else "历史任务未记录窗口",
                "范围": "微信小店 / 全部订单" if filters.scope == "all" else "微信小店 / 运输中（历史任务）",
                "下单时间范围": filters.description(), "店铺关键词": filters.shop or "全部店铺",
                "扫描方式": "从新到旧，早于起点后结束" if filters.start and filters.descending else "逐笔核对全部历史订单", "导出时间": now()}

    def close(self):
        self.db.close()
