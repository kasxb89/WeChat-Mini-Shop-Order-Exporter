"""不依赖 Office 的 XLSX 写入。全部数据单元格使用显式字符串，保留长编号。"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

from .models import OrderRecord

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def xml(element: Element) -> bytes:
    return tostring(element, encoding="utf-8", xml_declaration=True)


def sheet(rows: list[tuple[str, ...]], widths: tuple[int, ...], *, filter_rows: bool) -> bytes:
    root = Element("worksheet", xmlns=NS)
    views = SubElement(root, "sheetViews")
    view = SubElement(views, "sheetView", workbookViewId="0")
    SubElement(view, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    cols = SubElement(root, "cols")
    for index, width in enumerate(widths, 1):
        SubElement(cols, "col", min=str(index), max=str(index), width=str(width), customWidth="1")
    data = SubElement(root, "sheetData")
    for i, values in enumerate(rows, 1):
        row = SubElement(data, "row", r=str(i))
        for j, value in enumerate(values):
            value = BAD_XML.sub("", value)
            if len(value) > 32767:
                raise ValueError(f"第 {i} 行的字段超过 Excel 单元格容量，数据仍保存在本地采集记录。")
            c = SubElement(row, "c", r=f"{chr(65 + j)}{i}", s="1" if i == 1 else "2", t="inlineStr")
            t = SubElement(SubElement(c, "is"), "t")
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t.text = value
    if filter_rows:
        SubElement(root, "autoFilter", ref=f"A1:{chr(64 + len(widths))}{len(rows)}")
    return xml(root)


def export_xlsx(path: Path, records: list[OrderRecord], metadata: dict[str, str]) -> Path:
    if len(records) > 1048575:
        raise ValueError("订单数量超过单张工作表容量，请拆分任务导出。")
    path = path.with_suffix(".xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [("订单编号", "产品名称", "快递单号", "下单时间", "店铺", "订单状态", "物流采集状态", "备注", "收件人", "联系电话", "收货地址")]
    rows += [(r.order_id, "\n".join(r.products), "\n".join(r.tracking_numbers), r.ordered_at,
              r.shop, r.status, r.tracking_state, r.note, r.recipient, r.contact_phone, r.address) for r in records]
    info = [("项目", "内容"), ("导出订单数", str(len(records))), *metadata.items()]
    content = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'''
    styles = b'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF205649"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    workbook = Element("workbook", xmlns=NS, attrib={"xmlns:r": REL})
    sheets = SubElement(workbook, "sheets")
    for i, name in enumerate(("订单明细", "采集记录"), 1):
        SubElement(sheets, "sheet", name=name, sheetId=str(i), attrib={"r:id": f"rId{i}"})
    relns = "http://schemas.openxmlformats.org/package/2006/relationships"
    rels = Element("Relationships", xmlns=relns)
    for i in (1, 2):
        SubElement(rels, "Relationship", Id=f"rId{i}", Type=REL + "/worksheet", Target=f"worksheets/sheet{i}.xml")
    SubElement(rels, "Relationship", Id="rId3", Type=REL + "/styles", Target="styles.xml")
    rootrels = Element("Relationships", xmlns=relns)
    SubElement(rootrels, "Relationship", Id="rId1", Type=REL + "/officeDocument", Target="xl/workbook.xml")
    fd, temporary = tempfile.mkstemp(prefix=".订单导出-", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as z:
            for name, value in {
                "[Content_Types].xml": content, "_rels/.rels": xml(rootrels),
                "xl/workbook.xml": xml(workbook), "xl/_rels/workbook.xml.rels": xml(rels),
                "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": sheet(rows, (32, 65, 34, 24, 32, 16, 20, 60, 20, 22, 65), filter_rows=True),
                "xl/worksheets/sheet2.xml": sheet(info, (22, 85), filter_rows=False),
            }.items():
                z.writestr(name, value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
