import unittest

from order_exporter.vision import Page, Text, read_identifier


def text(value, x=20, y=100, width=100, height=20):
    return Text(value, (x, y, x + width, y + height))


def page(*texts, source="uia"):
    return Page(1, (0, 0, 600, 800), list(texts), None, source)


class DirectIdentifierTests(unittest.TestCase):
    def test_page_keeps_old_positional_constructor_and_ocr_default(self):
        marker = object()
        old = Page(1, (0, 0, 600, 800), [], marker)
        self.assertIs(old.image, marker)
        self.assertEqual(old.source, "ocr")

    def test_order_reads_separate_controls_and_preserves_leading_zeroes(self):
        value = "000123456789012345678901234567890"
        p = page(text("订单编号：", width=90), text(value, x=130, width=330),
                 text("复制", x=480, width=40))
        self.assertEqual(read_identifier(p, "order"), value)

    def test_order_reads_merged_text(self):
        for label in ("订单编号", "订单号", "订单号码"):
            with self.subTest(label=label):
                p = page(text(label + "：0001234567890123456789复制", width=480))
                self.assertEqual(read_identifier(p, "order"), "0001234567890123456789")

    def test_tracking_reads_label_or_carrier_in_both_layouts(self):
        for label in ("运单号", "快递单号", "物流单号", "快递公司：圆通速递", "圆通速递"):
            with self.subTest(label=label):
                merged = page(text(label + "：YT0001234567890123 复制", width=480))
                split = page(text(label, width=160), text("YT0001234567890123", x=200, width=300))
                self.assertEqual(read_identifier(merged, "tracking"), "YT0001234567890123")
                self.assertEqual(read_identifier(split, "tracking"), "YT0001234567890123")

    def test_numeric_tracking_keeps_leading_zeroes(self):
        p = page(text("运单号：00000123456789012345", width=480))
        self.assertEqual(read_identifier(p, "tracking"), "00000123456789012345")

    def test_ems_identifier_prefix_is_preserved(self):
        value = "EMS0001234567890123"
        p = page(text("运单号：" + value, width=480))
        self.assertEqual(read_identifier(p, "tracking"), value)
        p = page(text("EMS", width=50), text(value, x=100, width=300))
        self.assertEqual(read_identifier(p, "tracking"), value)

    def test_only_uia_is_trusted(self):
        for source in ("ocr", "OCR", "", "clipboard"):
            with self.subTest(source=source):
                p = page(text("订单编号：0001234567890123456789"), source=source)
                self.assertIsNone(read_identifier(p, "order"))

    def test_does_not_repair_letters_or_invent_missing_digits(self):
        value = "OO01234567890123456789"
        self.assertEqual(read_identifier(page(text("订单号：" + value)), "order"), value)
        self.assertIsNone(read_identifier(page(text("订单号：1234 5678901234567890")), "order"))
        self.assertIsNone(read_identifier(page(text("订单号：" + "1" * 81)), "order"))

    def test_invisible_separator_keeps_full_identifier_but_masking_falls_back(self):
        p = page(text("订单编号：000\u200b1234567890123456789", width=480))
        self.assertEqual(read_identifier(p, "order"), "0001234567890123456789")
        for value in ("***1234567890123456789", "0001234567890123456789…", "0001234567890123456789..."):
            with self.subTest(value=value):
                self.assertIsNone(read_identifier(page(text("订单编号：" + value, width=560)), "order"))

    def test_multiple_candidates_fall_back(self):
        for p in (page(text("订单编号：1234567890123456789 2234567890123456789", width=560)),
                  page(text("订单号：1234567890123456789"), text("订单号：2234567890123456789", y=150))):
            self.assertIsNone(read_identifier(p, "order"))

    def test_repeated_public_controls_are_not_ambiguous(self):
        p = page(text("订单号：1234567890123456789", width=400),
                 text("订单号：1234567890123456789", width=400))
        self.assertEqual(read_identifier(p, "order"), "1234567890123456789")

    def test_rejects_numbers_on_another_line_or_left_of_label(self):
        for value in (text("0001234567890123456789", x=150, y=125, width=350),
                      text("0001234567890123456789", x=0, y=100, width=80)):
            p = page(text("订单编号", x=90, width=55), value)
            self.assertIsNone(read_identifier(p, "order"))
        self.assertIsNone(read_identifier(page(text("订单号：\n0001234567890123456789")), "order"))

    def test_does_not_treat_phone_price_or_signed_time_as_identifier(self):
        for value in ("订单号：13812345678", "运单号：13812345678", "订单号：￥1234567890",
                      "运单号：签收时间 2026-11-16 12:30:45", "订单号：1234567890元",
                      "快递公司：圆通速递 联系电话：13812345678"):
            with self.subTest(value=value):
                kind = "order" if value.startswith("订单") else "tracking"
                self.assertIsNone(read_identifier(page(text(value, width=560)), kind))

    def test_unlabelled_and_recommendation_numbers_are_ignored(self):
        self.assertIsNone(read_identifier(page(text("0001234567890123456789")), "order"))
        p = page(text("订单号", y=100), text("你可能还会喜欢", y=150),
                 text("订单号：0001234567890123456789", y=200, width=450))
        self.assertIsNone(read_identifier(p, "order"))
        p = page(text("猜你喜欢 运单号：YT0001234567890123", width=500))
        self.assertIsNone(read_identifier(p, "tracking"))

    def test_order_and_tracking_fields_on_one_row_do_not_mix(self):
        p = page(text("订单号：0001234567890123456789 运单号：YT0001234567890123", width=580))
        self.assertEqual(read_identifier(p, "order"), "0001234567890123456789")
        self.assertEqual(read_identifier(p, "tracking"), "YT0001234567890123")

    def test_unknown_kind_falls_back(self):
        self.assertIsNone(read_identifier(page(text("订单号：0001234567890123456789")), "unknown"))


if __name__ == "__main__":
    unittest.main()
