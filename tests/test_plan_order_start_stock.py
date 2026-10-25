"""주문 수량은 지금 재고가 아니라 다음 달 1일 예상 재고(남은 소모 차감, 입고 예정 더함)에서 계산한다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_servers"))
import packing_server as packing


class PlanOrderStartStockTests(unittest.TestCase):
    BOX = {"code": "B1", "dims": [300, 200, 100], "price": 100}
    PRODUCT = {"productId": "p1", "dims": [100, 100, 50], "qty90d": 900, "box": "B1"}

    def plan(self, **kw):
        return packing.plan_order(products=[self.PRODUCT], box_specs=[self.BOX], new_specs=[],
                                  inventory=[{"code": "B1", "availableQty": 1000, "safetyQty": None}],
                                  forecast_shipments=3000, days_in_month=30, safety_days=7, rounding_unit=100, **kw)

    def test_without_adjustment_uses_current_stock(self):
        row = self.plan()["rows"][0]
        self.assertEqual(row["startQty"], 1000)
        self.assertEqual(row["proposedQty"], 2700)          # 3000 + 700 − 1000

    def test_subtracts_remaining_usage_and_adds_inbound(self):
        row = self.plan(remaining_days=5, inbound={"B1": 200})["rows"][0]
        self.assertEqual(row["consumeBeforeQty"], 500)      # 3000 / 30일 × 5일
        self.assertEqual(row["inboundQty"], 200)
        self.assertEqual(row["startQty"], 700)              # 1000 − 500 + 200
        self.assertEqual(row["proposedQty"], 3000)          # 3000 + 700 − 700

    def test_remaining_usage_never_goes_below_zero_stock(self):
        row = self.plan(remaining_days=20)["rows"][0]
        self.assertEqual(row["consumeBeforeQty"], 1000)     # 2000 이 필요하지만 재고는 1000 뿐
        self.assertEqual(row["startQty"], 0)


if __name__ == "__main__":
    unittest.main()
