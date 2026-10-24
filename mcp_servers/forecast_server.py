"""
forecast MCP 서버 — 월별 출고 건수 수요 예측 (규칙 계산, LLM 사용 안 함)

예측 = 최근 3개월 평균(seasonal=True 이면 × 전년 동월 계절 지수 = 전년 동월 / 전년 같은 3개월 평균).
계절 지수용 전년 이력이 없으면 최근 평균만 쓴다. 정확도 = 100 − 최근 N개월 백테스트 MAPE(같은 방식으로).
상태를 갖지 않으며 입력으로 받은 이력만 계산한다.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("forecast", log_level="WARNING")


def _prev_month(ym: str, k: int = 1) -> str:
    y, m = map(int, ym.split("-"))
    m -= k
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def _seasonal_forecast(hist: dict, target: str):
    """최근 3개월 평균 × (전년 동월 / 전년 같은 3개월 평균)"""
    recent = [hist.get(_prev_month(target, k)) for k in (1, 2, 3)]
    ly_recent = [hist.get(_prev_month(target, k + 12)) for k in (1, 2, 3)]
    ly_target = hist.get(_prev_month(target, 12))
    if None in recent or None in ly_recent or ly_target is None:
        return None
    return sum(recent) / 3 * (ly_target / (sum(ly_recent) / 3))


def _recent_forecast(hist: dict, target: str):
    """최근 3개월 평균(3개월이 다 있을 때만)"""
    recent = [hist.get(_prev_month(target, k)) for k in (1, 2, 3)]
    return None if None in recent else sum(recent) / 3


@mcp.tool()
def forecast_shipments(history: list[dict], target_month: str, backtest_months: int = 6, seasonal: bool = False) -> dict:
    """월별 출고 이력으로 target_month 출고 건수를 예측하고(seasonal 이면 전년 동월 계절 지수 적용),
    최근 backtest_months개월 백테스트로 정확도(100 - MAPE)를 계산한다."""
    hist = {h["month"]: h["shipments"] for h in history}
    if not hist:
        return {"targetMonth": target_month, "forecast": None, "method": "출고 이력 없음", "accuracy_pct": None,
                "backtest": [], "sameMonthLastYear": None}
    model = _seasonal_forecast if seasonal else _recent_forecast
    fc = model(hist, target_month)
    method = "최근 3개월 평균 × 전년 동월 계절 지수" if seasonal else "최근 3개월 평균"
    if fc is None:                                  # 이력 15개월 미만: 계절 지수 없이 최근 평균
        recent = [v for v in (hist.get(_prev_month(target_month, k)) for k in (1, 2, 3)) if v is not None]
        recent = recent or [hist[max(hist)]]
        fc, method = sum(recent) / len(recent), f"최근 {len(recent)}개월 평균" + (" (계절 지수용 이력 부족)" if seasonal else "")
    bt = []
    last = max(hist)
    for k in range(backtest_months - 1, -1, -1):
        mth = _prev_month(last, k)
        p = model(hist, mth)
        if p is not None:
            bt.append({"month": mth, "actual": hist[mth], "forecast": round(p),
                       "ape": round(abs(p - hist[mth]) / hist[mth] * 100, 2)})
    mape = sum(b["ape"] for b in bt) / len(bt) if bt else None
    return {"targetMonth": target_month, "forecast": round(fc) if fc else None,
            "method": method, "accuracy_pct": round(100 - mape, 1) if mape is not None else None,
            "backtest": bt, "sameMonthLastYear": hist.get(_prev_month(target_month, 12))}


if __name__ == "__main__":
    mcp.run()
