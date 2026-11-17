from __future__ import annotations

import os
import json
from dataclasses import asdict
from pathlib import Path
import threading
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .activation import ActivationStore, LicenseStatus
from .models import Filters, Progress, TIME_FORMAT
from .native import Desktop, Paused, PageError, Window
from .store import Store
from .workbook import export_xlsx


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WechatOrderExporter"


class Worker(QtCore.QThread):
    progress = QtCore.Signal(object)
    record = QtCore.Signal(object)
    outcome = QtCore.Signal(str, str)

    def __init__(self, database: Path, job: str | None, output: Path | None, target: Window | None = None, *, background=False):
        super().__init__()
        self.database, self.job, self.output = database, job, output
        self.target = target
        self.background = background
        self.bound_window = None
        self.stop = threading.Event()

    def run(self):
        from .direct import Collector
        from .native import Desktop
        store = Store(self.database)
        status, message = "paused", ""
        collector = None
        desktop = None
        started = time.monotonic()
        try:
            self.progress.emit(Progress("连接订单数据", "正在为所选微信建立本地订单数据连接…", store.count(self.job) if self.job else 0))
            desktop = Desktop(self.stop, background=self.background)
            completed = [r for r in store.records(self.job) if r.tracking_state != "待补采" and r.recipient_checked and r.shipping_info_checked] if self.job else []
            collector = Collector(desktop, self.progress.emit, store.filters(self.job) if self.job else None,
                                  {r.order_id for r in completed}, {r.order_id:r.ordered_at for r in completed}, target=self.target)
            if self.job is None:
                message = collector.check_page()
                status = "checked"
            else:
                store.state(self.job, "running")
                def save(value):
                    inserted = store.save(self.job, value)
                    self.record.emit(store.record(self.job, value.order_id))
                    return inserted
                count = collector.collect(save, initial_count=store.count(self.job))
                pending = store.pending(self.job)
                status = "review" if pending else "complete"
                message = f"指定范围采集完成，保存 {count} 笔订单。{collector.end_reason}"
                if pending:
                    message += f"其中 {pending} 笔单号待补采，可查看 Excel 备注后继续。"
        except Paused as error:
            message = str(error)
        except (PageError, ValueError) as error:
            message = str(error)
        except Exception as error:
            # 详细堆栈保存在工具自己的目录，不弹出终端窗口。
            import traceback
            (data_dir() / "last-error.log").write_text(traceback.format_exc(), encoding="utf-8")
            message = f"采集已暂停：{type(error).__name__}。记录已保存，可查看本地 last-error.log。"
        finally:
            if collector is not None:
                try:
                    collector.close()
                except Exception as error:
                    message += f" 本地连接退出异常：{error}"
            self.bound_window = collector.bound_window if collector else None
            if desktop is not None:
                from .window_capture import close_capture
                close_capture()
                metrics = desktop.metrics
                message += " 订单数据直读 · 不逐单点击、不复制单号。"
                if collector:
                    message += (f" 本次 {time.monotonic()-started:.1f} 秒 · 数据请求 {collector.api.requests} 次"
                                f"（等待 {collector.api.request_seconds:.2f} 秒） · 入口准备识别 {metrics['ocr_reads']} 次。")
            if self.job:
                if collector and collector.bound_window:
                    store.set_source(self.job, asdict(collector.bound_window))
                store.state(self.job, status, message)
                try:
                    export_xlsx(self.output, store.records(self.job), store.metadata(self.job))
                    message += " Excel 已保存。"
                except PermissionError:
                    message += " Excel 正被占用，请关闭文件后点击「导出已采集」。"
                except Exception as error:
                    message += f" Excel 暂未写入（{type(error).__name__}），本地采集记录完整保留。"
            store.close()
            self.outcome.emit(status, message)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"微信小店订单导出器 · {__version__}")
        self.resize(1120, 800)
        self.setMinimumSize(880, 720)
        self.setWindowIcon(make_icon())
        self.database = data_dir() / "orders.sqlite3"
        self.activation = ActivationStore(data_dir() / "activation.json")
        self.license_status = LicenseStatus("invalid")
        self.worker = None
        self.job = None
        self.output = None
        self.closing = False
        self.running = False
        self.rows_by_id = {}
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(18)

        title = QtWidgets.QLabel("全部订单，按时间整理。")
        title.setObjectName("Title")
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(title,1)
        self.license_label = QtWidgets.QLabel()
        self.license_label.setObjectName("Muted")
        heading.addWidget(self.license_label)
        self.activation_button = button("输入激活码", self.activate_product)
        heading.addWidget(self.activation_button)
        layout.addLayout(heading)
        subtitle = QtWidgets.QLabel("微信小店  /  订单数据直读 · 按下单时间筛选 · 保留全部订单状态")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        target_row = QtWidgets.QHBoxLayout()
        target_row.setSpacing(10)
        target_row.addWidget(QtWidgets.QLabel("采集窗口"))
        self.target = QtWidgets.QComboBox()
        self.target.setAccessibleName("选择要采集的微信账号窗口")
        self.target.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.target.setMinimumContentsLength(20)
        self.target.setToolTip("两个微信可以同时打开；选择目标后点「查看窗口」确认。切换到另一个账号时请开始新采集。")
        target_row.addWidget(self.target, 1)
        self.detect = button("重新检测", self.refresh_targets)
        self.show_target_button = button("查看窗口", self.show_target)
        target_row.addWidget(self.detect)
        target_row.addWidget(self.show_target_button)
        layout.addLayout(target_row)

        time_row = QtWidgets.QHBoxLayout()
        time_row.setSpacing(10)
        time_row.addWidget(QtWidgets.QLabel("下单时间"))
        current = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        self.start_time = QtWidgets.QDateTimeEdit(QtCore.QDateTime.fromString(
            (current-timedelta(days=30)).strftime("%Y-%m-%d 00:00:00"), "yyyy-MM-dd HH:mm:ss"))
        self.end_time = QtWidgets.QDateTimeEdit(QtCore.QDateTime.fromString(
            current.strftime("%Y-%m-%d 23:59:59"), "yyyy-MM-dd HH:mm:ss"))
        for control, name in ((self.start_time, "下单开始时间"), (self.end_time, "下单结束时间")):
            control.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            control.setCalendarPopup(True)
            control.setAccessibleName(name)
            control.setMinimumWidth(200)
            control.setMaximumWidth(230)
        time_row.addWidget(self.start_time)
        time_row.addWidget(QtWidgets.QLabel("至"))
        time_row.addWidget(self.end_time)
        self.all_time = QtWidgets.QCheckBox("不限时间")
        time_row.addWidget(self.all_time)
        time_row.addStretch()
        layout.addLayout(time_row)
        self.range_error = QtWidgets.QLabel("含开始和结束时刻，按下单时间计算。")
        self.range_error.setObjectName("Muted")
        self.range_error.setMinimumHeight(20)
        time_help = QtWidgets.QHBoxLayout()
        time_help.addWidget(self.range_error, 1)
        self.background = QtWidgets.QCheckBox("准备后切回原窗口")
        self.background.setChecked(True)
        self.background.setToolTip("开始时自动打开微信小店建立数据连接；准备完成后批量读取订单，无需逐单点击。")
        time_help.addWidget(self.background)
        self.ordering = QtWidgets.QCheckBox("按时间从新到旧")
        self.ordering.setChecked(True)
        self.ordering.setToolTip("适用于微信默认的时间顺序，早于起始时间后结束。若列表有置顶或顺序调整，请取消此项，逐笔核对全部历史订单。")
        time_help.addWidget(self.ordering)
        layout.addLayout(time_help)

        shop_row = QtWidgets.QHBoxLayout()
        shop_row.setSpacing(10)
        shop_row.addWidget(QtWidgets.QLabel("店铺关键词"))
        self.shop = QtWidgets.QLineEdit()
        self.shop.setPlaceholderText("留空采集全部店铺；填写关键词只导出匹配的店铺")
        self.shop.setAccessibleName("店铺关键词，留空全部店铺")
        shop_row.addWidget(self.shop, 1)
        layout.addLayout(shop_row)

        destination = QtWidgets.QHBoxLayout()
        destination.setSpacing(10)
        destination.addWidget(QtWidgets.QLabel("保存位置"))
        self.folder = QtWidgets.QLineEdit(str(Path.home() / "Desktop" / "微信小店订单"))
        self.folder.setAccessibleName("Excel 保存文件夹")
        destination.addWidget(self.folder, 1)
        self.browse = button("选择文件夹", self.choose_folder)
        destination.addWidget(self.browse)
        layout.addLayout(destination)

        status = QtWidgets.QWidget()
        status.setObjectName("Status")
        status_layout = QtWidgets.QHBoxLayout(status)
        status_layout.setContentsMargins(18, 16, 18, 16)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(6)
        self.stage = QtWidgets.QLabel("准备采集")
        self.stage.setObjectName("Stage")
        words.addWidget(self.stage)
        self.message = QtWidgets.QLabel("自动建立小店连接后，批量读取订单和物流；准备时短暂切换微信，随后可继续办公。")
        self.message.setWordWrap(True)
        self.message.setObjectName("Muted")
        self.message.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        words.addWidget(self.message)
        status_layout.addLayout(words, 1)
        self.count = QtWidgets.QLabel("0")
        self.count.setObjectName("Count")
        self.count.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        status_layout.addWidget(self.count)
        status_layout.addWidget(QtWidgets.QLabel("笔已保存"))
        layout.addWidget(status)

        actions = QtWidgets.QHBoxLayout()
        self.start = button("开始新采集", self.start_job)
        self.start.setObjectName("Primary")
        self.start.setMinimumWidth(140)
        actions.addWidget(self.start)
        self.resume = button("继续上次", self.resume_job)
        actions.addWidget(self.resume)
        self.pause = button("暂停  F8", self.pause_job)
        self.pause.setEnabled(False)
        actions.addWidget(self.pause)
        actions.addStretch()
        self.check = button("检查数据连接", self.check_page)
        actions.addWidget(self.check)
        layout.addLayout(actions)

        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["订单编号", "产品名称", "快递单号", "下单时间", "收件人", "联系电话", "收货地址", "状态"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(90)
        for index, width in ((0, 180), (1, 260), (2, 165), (3, 160), (4, 90), (5, 140), (6, 280), (7, 110)):
            self.table.setColumnWidth(index, width)
        self.table.setShowGrid(False)
        self.table.setAccessibleName("已经完整采集的订单")
        layout.addWidget(self.table, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(QtWidgets.QLabel("历史采集"))
        self.history = QtWidgets.QComboBox()
        self.history.setAccessibleName("历史采集任务，按日期和订单数选择")
        self.history.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.history.setMinimumContentsLength(18)
        self.history.setPlaceholderText("选择历史采集")
        footer.addWidget(self.history, 1)
        self.clear_button = button("清除记录", lambda: None)
        self.clear_button.setAccessibleName("清除采集记录，保留 Excel 文件")
        clear_menu = QtWidgets.QMenu(self)
        self.clear_current = clear_menu.addAction("清除当前任务", lambda: self.clear_records())
        self.clear_all = clear_menu.addAction("清除全部历史", lambda: self.clear_records(all_jobs=True))
        self.clear_button.setMenu(clear_menu)
        footer.addWidget(self.clear_button)
        self.export = button("导出已采集", self.export_saved)
        footer.addWidget(self.export)
        self.open_folder = button("打开文件夹", self.open_output_folder)
        footer.addWidget(self.open_folder)
        layout.addLayout(footer)
        self.start_time.dateTimeChanged.connect(self.validate_dates)
        self.end_time.dateTimeChanged.connect(self.validate_dates)
        self.all_time.toggled.connect(self.validate_dates)
        self.history.currentIndexChanged.connect(self.history_changed)
        self.refresh_targets()
        self.load_latest()
        self.refresh_license()
        self.license_timer = QtCore.QTimer(self)
        self.license_timer.setInterval(30000)
        self.license_timer.timeout.connect(self.refresh_license)
        self.license_timer.start()
        self.validate_dates()

    def refresh_license(self):
        try:
            self.license_status = self.activation.status()
        except OSError:
            self.license_status = LicenseStatus("invalid")
        self.license_label.setText(self.license_status.label)
        self.activation_button.setVisible(self.license_status.state != "activated")
        if not self.license_status.can_collect:
            if self.worker and self.worker.isRunning():
                self.pause_job()
            self.start.setEnabled(False)
            self.resume.setEnabled(False)
            self.check.setEnabled(False)
        elif not self.running:
            self.check.setEnabled(True)
            self.resume.setEnabled(bool(self.job) and self.resume.property("canResume") is not False)
            self.validate_dates()

    def activate_product(self):
        dialog = ActivationDialog(self.activation,self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self.refresh_license()
            self.message.setText("已激活，这个 Windows 用户以后无需重复输入激活码。")

    def ensure_authorized(self):
        self.refresh_license()
        if self.license_status.can_collect:
            return True
        self.message.setText("试用已到期或授权记录需验证。输入激活码后继续采集；历史记录和 Excel 仍可查看、导出。")
        return False

    def load_latest(self):
        self.refresh_history(load=True)

    def refresh_history(self, *, load=False):
        store = Store(self.database)
        jobs = store.history()
        store.close()
        preferred = self.job or next((j["id"] for j in jobs if j["orders"]), jobs[0]["id"] if jobs else None)
        names = {"ready":"待开始", "running":"采集中", "paused":"已暂停", "complete":"已完成", "review":"有单号待补采"}
        self.history.blockSignals(True)
        self.history.clear()
        for job in jobs:
            created = job["created"].replace("T", " ")[:19]
            self.history.addItem(f"{created} · {job['orders']} 笔 · {names.get(job['status'],job['status'])}", job["id"])
            source = json.loads(job["source"])
            source_label = Window(**source).label() if source else "旧任务未记录采集窗口"
            self.history.setItemData(self.history.count()-1, source_label+"\n"+job["output"], QtCore.Qt.ToolTipRole)
        self.history.setCurrentIndex(self.history.findData(preferred))
        if not jobs:
            self.history.addItem("暂无历史采集", None)
        self.history.blockSignals(False)
        self.clear_button.setEnabled(bool(jobs) and not self.running)
        self.clear_current.setEnabled(bool(self.job))
        self.clear_all.setEnabled(bool(jobs))
        if load and preferred:
            self.load_job(preferred)
        elif not jobs:
            self.resume.setEnabled(False)
            self.export.setEnabled(False)

    def history_changed(self, index):
        job = self.history.itemData(index)
        if job and not self.running:
            self.load_job(job)

    def load_job(self, job):
        store = Store(self.database)
        latest = store.job(job)
        if latest:
            self.job = latest["id"]
            self.output = Path(latest["output"])
            self.folder.setText(str(self.output.parent))
            self.refresh_records(store)
            filters = store.filters(self.job)
            missing_shipping = store.uncollected_shipping_info(self.job)
            can_resume = (latest["status"] != "complete" or missing_shipping > 0) and filters.scope == "all"
            self.resume.setEnabled(can_resume and self.license_status.can_collect)
            self.resume.setProperty("canResume", can_resume)
            if filters.scope == "all":
                self.set_filters(filters)
            self.stage.setText("上次采集已完成" if latest["status"] == "complete" else "可继续上次采集")
            self.message.setText(latest["message"] or "本地进度已恢复。")
            self.export.setEnabled(True)
            self.clear_current.setEnabled(True)
            source = json.loads(latest["source"])
            for index in range(1, self.target.count()):
                target = self.target.itemData(index)
                if source and target.hwnd == source["hwnd"] and target.path.lower() == source["path"].lower() and target.pid == source.get("pid",0):
                    self.target.setCurrentIndex(index)
                    break
            if missing_shipping and filters.scope == "all":
                self.stage.setText("可补采收货信息")
                self.message.setText(f"上次 {missing_shipping} 笔订单尚未完整采集收货信息。点击「继续上次」补采收件人、电话和地址，并更新原 Excel。")
        else:
            self.resume.setEnabled(False)
            self.export.setEnabled(False)
        store.close()

    def refresh_targets(self):
        previous = self.target.currentData()
        try:
            windows = Desktop(threading.Event(), None).windows()
        except OSError:
            windows = []
        windows.sort(key=lambda w:(w.title not in {"小店","微信小店"}, w.path.lower().endswith("\\wechatappex.exe"), w.hwnd))
        self.target.clear()
        self.target.addItem("自动选择唯一的小店或微信窗口", None)
        for window in windows:
            self.target.addItem(window.label(), window)
            self.target.setItemData(self.target.count()-1, window.path, QtCore.Qt.ToolTipRole)
            if previous == window:
                self.target.setCurrentIndex(self.target.count()-1)

    def show_target(self):
        target = self.target.currentData()
        if target is None:
            self.message.setText("先在「采集窗口」下拉框选择一个窗口，再点「查看窗口」确认是哪个微信账号。")
            return
        try:
            Desktop(threading.Event(), None).bind(target)
        except (PageError, Paused) as error:
            self.message.setText(str(error))

    def refresh_records(self, store):
        self.table.setRowCount(0)
        self.rows_by_id.clear()
        for value in store.records(self.job):
            self.add_record(value)
        self.count.setText(str(self.table.rowCount()))

    def add_record(self, record):
        row = self.rows_by_id.get(record.order_id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.rows_by_id[record.order_id] = row
        values = (record.order_id, "\n".join(record.products), "\n".join(record.tracking_numbers),
                  record.ordered_at, record.recipient, record.contact_phone, record.address,
                  record.status + (" · 单号待补采" if record.tracking_state == "待补采" else ""))
        for col, text in enumerate(values):
            item = QtWidgets.QTableWidgetItem(text)
            item.setToolTip("\n".join(filter(None, (text, "店铺："+record.shop, record.tracking_state, record.note))))
            self.table.setItem(row, col, item)
        self.table.resizeRowToContents(row)
        self.count.setText(str(self.table.rowCount()))

    def choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 Excel 保存文件夹", self.folder.text())
        if path:
            self.folder.setText(path)

    def start_job(self):
        if not self.ensure_authorized():
            return
        if not self.validate_dates():
            return
        folder = Path(self.folder.text().strip())
        if not folder.is_absolute():
            self.message.setText("请选择完整的本地文件夹路径。")
            return
        self.output = folder / f"微信小店_全部订单_{datetime.now():%Y%m%d_%H%M%S_%f}.xlsx"
        store = Store(self.database)
        target = self.target.currentData()
        self.job = store.create(self.output, self.selected_filters(), asdict(target) if target else None)
        self.resume.setProperty("canResume", True)
        store.close()
        self.table.setRowCount(0)
        self.rows_by_id.clear()
        self.count.setText("0")
        self.refresh_history()
        self.launch(self.job, self.output)

    def resume_job(self):
        if not self.ensure_authorized():
            return
        if self.job:
            store = Store(self.database)
            filters = store.filters(self.job)
            store.close()
            if filters.scope != "all":
                self.message.setText("这是旧的运输中任务，可导出已有结果；采集全部订单请开始新任务。")
                return
            self.set_filters(filters)
            self.launch(self.job, self.output)

    def selected_filters(self):
        return Filters("" if self.all_time.isChecked() else self.start_time.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
                       "" if self.all_time.isChecked() else self.end_time.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
                       self.shop.text().strip(), descending=self.ordering.isChecked())

    def set_filters(self, filters):
        self.all_time.setChecked(not bool(filters.start))
        if filters.start:
            self.start_time.setDateTime(QtCore.QDateTime.fromString(filters.start, "yyyy-MM-dd HH:mm:ss"))
            self.end_time.setDateTime(QtCore.QDateTime.fromString(filters.end, "yyyy-MM-dd HH:mm:ss"))
        self.shop.setText(filters.shop)
        self.ordering.setChecked(filters.descending)

    def validate_dates(self):
        valid = True
        try:
            self.selected_filters()
            self.range_error.setText("含开始和结束时刻；继续上次将沿用该任务原有筛选条件。")
            self.range_error.setStyleSheet("")
        except ValueError as error:
            valid = False
            self.range_error.setText(str(error))
            self.range_error.setStyleSheet("color: #ffb7a5;")
        self.start_time.setEnabled(not self.running and not self.all_time.isChecked())
        self.end_time.setEnabled(not self.running and not self.all_time.isChecked())
        self.ordering.setEnabled(not self.running and not self.all_time.isChecked())
        self.start.setEnabled(not self.running and valid and self.license_status.can_collect)
        return valid

    def check_page(self):
        if not self.ensure_authorized():
            return
        self.launch(None, None)

    def launch(self, job, output):
        self.worker = Worker(self.database, job, output, self.target.currentData(), background=self.background.isChecked())
        self.worker.progress.connect(self.on_progress)
        self.worker.record.connect(self.add_record)
        self.worker.outcome.connect(self.on_outcome)
        self.worker.finished.connect(self.worker_finished)
        self.set_running(True)
        self.worker.start()

    def on_progress(self, progress):
        self.stage.setText(progress.stage)
        self.message.setText(progress.message)

    def on_outcome(self, status, message):
        self.stage.setText({"complete": "采集完成", "review": "遍历完成，有单号待补采", "checked": "数据连接正常"}.get(status, "采集已暂停"))
        self.message.setText(message)
        if status != "checked":
            self.resume.setProperty("canResume", status != "complete")
        if not self.license_status.can_collect:
            self.message.setText("试用已到期，采集已停用。已采集记录保留，输入激活码后可继续。")

    def worker_finished(self):
        self.set_running(False)
        if self.worker.bound_window:
            self.refresh_targets()
            for index in range(self.target.count()):
                if self.target.itemData(index) == self.worker.bound_window:
                    self.target.setCurrentIndex(index)
                    break
        self.refresh_history()
        self.worker.deleteLater()
        self.worker = None
        self.refresh_license()
        if self.closing:
            self.close()

    def set_running(self, running):
        self.running = running
        for control in (self.start, self.check, self.folder, self.browse, self.shop, self.all_time, self.ordering,
                        self.history, self.target, self.detect, self.show_target_button, self.background):
            control.setEnabled(not running)
        self.resume.setEnabled(not running and bool(self.job) and self.resume.property("canResume") is not False)
        self.pause.setEnabled(running)
        self.export.setEnabled(not running and bool(self.job))
        self.clear_button.setEnabled(not running and self.history.itemData(0) is not None)
        self.validate_dates()

    def clear_records(self, *, all_jobs=False):
        if self.running:
            return
        store = Store(self.database)
        try:
            jobs = [j for j in store.history() if all_jobs or j["id"] == self.job]
            if not jobs:
                return
            count = sum(j["orders"] for j in jobs)
            dialog = QtWidgets.QMessageBox(self)
            dialog.setWindowTitle("清除采集记录")
            dialog.setText("清除全部历史？" if all_jobs else "清除当前任务？")
            dialog.setInformativeText(f"将清除 {len(jobs)} 个任务、{count} 笔记录。\nExcel 文件会保留。\n记录将先备份到本机。")
            confirm = dialog.addButton("清除", QtWidgets.QMessageBox.DestructiveRole)
            confirm.setObjectName("Danger")
            cancel = dialog.addButton("取消", QtWidgets.QMessageBox.RejectRole)
            dialog.setDefaultButton(cancel)
            dialog.setEscapeButton(cancel)
            dialog.exec()
            if dialog.clickedButton() != confirm:
                return
            backup = store.clear(None if all_jobs else self.job)
            self.job = self.output = None
            self.table.setRowCount(0)
            self.rows_by_id.clear()
            self.count.setText("0")
            self.resume.setProperty("canResume", False)
            self.refresh_history()
            if self.history.itemData(0) is not None:
                self.history.setCurrentIndex(-1)
            self.set_running(False)
            self.stage.setText("记录已清除")
            self.message.setText(f"已清除 {len(jobs)} 个任务、{count} 笔记录；Excel 文件已保留，清除前记录已备份。")
            self.message.setToolTip(f"清除前备份：{backup}")
        except (OSError, sqlite3.Error) as error:
            self.stage.setText("清除未完成")
            self.message.setText(f"无法清除记录：{error}")
        finally:
            store.close()

    def pause_job(self):
        if self.worker:
            self.worker.stop.set()
            self.pause.setEnabled(False)
            self.stage.setText("正在暂停…")

    def export_saved(self):
        if not self.job:
            return
        store = Store(self.database)
        try:
            export_xlsx(self.output, store.records(self.job), store.metadata(self.job))
            self.message.setText(f"已保存到 {self.output}")
            self.open_export_folder(self.output)
        except PermissionError:
            self.message.setText("Excel 文件正在使用中，请关闭后再导出。采集记录已保存在本地。")
        except (OSError, ValueError) as error:
            self.message.setText(f"暂时无法导出：{error}")
        finally:
            store.close()

    def open_output_folder(self):
        path = self.output.parent if self.output else Path(self.folder.text())
        if path.is_dir():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def open_export_folder(self, output):
        folder = Path(output).parent
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder))):
            self.message.setText(f"Excel 已保存；暂未打开文件夹，可点击「打开文件夹」：{folder}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.closing = True
            self.pause_job()
            self.message.setText("正在保存采集进度，完成后关闭。")
            event.ignore()
        else:
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self.table.resizeRowsToContents)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "table"):
            QtCore.QTimer.singleShot(0, self.table.resizeRowsToContents)


class ActivationDialog(QtWidgets.QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("激活微信小店订单导出器")
        self.setFixedWidth(420)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(26,24,26,24)
        layout.setSpacing(14)
        layout.addWidget(QtWidgets.QLabel("输入安装方提供的激活码"))
        self.code = QtWidgets.QLineEdit()
        self.code.setPlaceholderText("6 位激活码")
        self.code.setMaxLength(6)
        self.code.setAccessibleName("激活码")
        layout.addWidget(self.code)
        self.error = QtWidgets.QLabel("正确输入一次后长期有效。")
        self.error.setWordWrap(True)
        self.error.setObjectName("Muted")
        layout.addWidget(self.error)
        self.submit = button("激活",self.activate)
        self.submit.setObjectName("Primary")
        self.code.returnPressed.connect(self.activate)
        layout.addWidget(self.submit)

    def activate(self):
        try:
            if self.store.activate(self.code.text()):
                self.accept()
            else:
                self.error.setText("激活码不正确，请向安装方核对。")
        except OSError:
            self.error.setText("无法保存激活状态，请检查当前 Windows 用户的数据目录权限。")


def button(text, callback):
    result = QtWidgets.QPushButton(text)
    result.setMinimumHeight(38)
    result.setCursor(QtCore.Qt.PointingHandCursor)
    result.clicked.connect(callback)
    return result


def make_icon():
    pixmap = QtGui.QPixmap(64, 64)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setBrush(QtGui.QColor("#6fd2ae"))
    painter.setPen(QtCore.Qt.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QtGui.QPen(QtGui.QColor("#14241e"), 4))
    painter.drawRoundedRect(17, 15, 30, 36, 3, 3)
    for y in (26, 34, 42):
        painter.drawLine(23, y, 41, y)
    painter.end()
    return QtGui.QIcon(pixmap)


def configure_app(app):
    # Qt 离屏平台不会枚举系统字体；显式加载系统微软雅黑也使布局核验与实窗一致。
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        if (fonts / filename).is_file():
            QtGui.QFontDatabase.addApplicationFont(str(fonts / filename))
    QtCore.QLocale.setDefault(QtCore.QLocale("zh_CN"))
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Microsoft YaHei", 10))
    arrow = (Path(__file__).with_name("assets") / "calendar-down.png").as_posix()
    app.setStyleSheet(STYLE + f'QDateTimeEdit::down-arrow, QComboBox::down-arrow, QPushButton::menu-indicator {{ image: url("{arrow}"); width: 10px; height: 6px; }}')


STYLE = """
QWidget { background: #1c2420; color: #edf4f0; font-family: 'Microsoft YaHei'; font-size: 14px; }
QLabel#Title { font-size: 28px; font-weight: 650; }
QLabel#Muted { color: #b5c4bb; }
QLabel#Stage { font-size: 18px; font-weight: 600; }
QLabel#Count { font-size: 34px; font-weight: 600; color: #8fdfbf; min-width: 60px; }
QWidget#Status { background: #26382f; border-radius: 10px; }
QWidget#Status QLabel { background: transparent; }
QPushButton { background: #29352e; border: 1px solid #5f7568; border-radius: 6px; padding: 4px 14px; }
QPushButton:hover { background: #354b3f; border-color: #8ac7a8; }
QPushButton:pressed { background: #1b2a22; }
QPushButton:focus { border: 2px solid #a5e6c8; }
QPushButton:disabled { color: #8b9a90; background: #242d27; border-color: #3f4e44; }
QPushButton#Primary { background: #85d8b6; color: #10271d; border-color: #85d8b6; font-weight: 600; }
QPushButton#Primary:hover { background: #a1e8cb; }
QPushButton#Primary:disabled { background: #3c6551; border-color: #3c6551; color: #b3c5ba; }
QPushButton#Danger { color: #ffb7a5; border-color: #b17d6e; }
QMenu { background: #25382c; color: #edf4f0; border: 1px solid #5f7568; padding: 5px; }
QMenu::item { padding: 10px 18px; }
QMenu::item:selected { background: #426b54; }
QMenu::item:disabled { color: #83958a; }
QLineEdit { border: 1px solid #5f7568; border-radius: 6px; padding: 9px; background: #202d25; selection-background-color: #426b54; }
QLineEdit:focus { border-color: #9cdec0; }
QComboBox { border: 1px solid #5f7568; border-radius: 6px; padding: 8px 26px 8px 9px; background: #202d25; min-height: 20px; }
QComboBox:focus { border-color: #9cdec0; }
QComboBox::drop-down { width: 22px; border-left: 1px solid #5f7568; }
QComboBox QAbstractItemView { background: #25382c; color: #edf4f0; selection-background-color: #426b54; border: 1px solid #5f7568; }
QDateTimeEdit { border: 1px solid #5f7568; border-radius: 6px; padding: 8px; background: #202d25; }
QDateTimeEdit:focus { border-color: #9cdec0; }
QDateTimeEdit:disabled { color: #83958a; background: #222c25; }
QDateTimeEdit::drop-down { width: 22px; border-left: 1px solid #5f7568; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; }
QCheckBox::indicator:unchecked { background: #202d25; border: 1px solid #7e9a88; border-radius: 3px; }
QCheckBox::indicator:checked { background: #85d8b6; border: 2px solid #85d8b6; border-radius: 3px; }
QCalendarWidget QWidget { background: #25382c; color: #edf4f0; }
QTableWidget { background: #19221c; border: 1px solid #43574a; border-radius: 6px; selection-background-color: #305c43; }
QTableWidget::item { padding: 10px 8px; border-bottom: 1px solid #324339; }
QHeaderView::section { background: #28392e; color: #c9d9cf; padding: 12px 8px; border: none; font-weight: 600; }
QScrollBar:vertical { width: 12px; background: #1c2720; }
QScrollBar::handle:vertical { background: #5f7b69; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { color: #edf4f0; background: #2c4033; border: 1px solid #688876; padding: 5px; }
"""
