<!-- proposalId: {{proposalId}} | targetMonth: {{targetMonth}} | generatedAt: {{generatedAt}} | inputAsOf: {{inputAsOf}} -->

# 박스 주문 제안서

`AI 생성` · 생성 {{generatedAtDisplay}} · 발주 대상 기간 {{periodStart}} – {{periodEnd}}

> **{{targetMonthLabel}} 예상 출고 {{forecastShipments}}건 기준, {{specCount}}개 규격 총 {{totalQty}}개 주문을 제안합니다.**
>
> 수요예측 정확도 {{forecastAccuracy}}% · 규격별 안전 재고({{safetyDays}}일분) 반영

## 규격별 제안 수량

| 규격 | 현재 재고 | 다음 달 예상 소모 | 안전 재고 | 제안 수량 |
|:---|---:|---:|---:|---:|
| {{spec}} | {{currentQty}} | {{expectedUsage}} | {{safetyQty}} | **{{proposedQty}}개** |
| **합계** | {{sumCurrent}} | {{sumUsage}} | {{sumSafety}} | **{{totalQty}}개** |

## 제안 근거

- {{reason1}}
- {{reason2}}
- {{reason3}}
- 제안 수량 = 다음 달 예상 소모 + 안전 재고 − 현재 재고 ({{roundingUnit}}개 단위 반올림)

## 신규 규격 도입 효과

{{effectBasis}}

| 항목 | 기존 박스 | 신규 규격 도입 후 | 변화 |
|:---|---:|---:|---:|
| {{metric}} | {{before}} | {{after}} | {{change}} |

## 예상 총 주문금액

**{{totalAmount}}** (VAT 별도)

| 비교 | 기준 금액 | 증감 |
|:---|---:|---:|
| 전월 대비 ({{prevMonthLabel}} 발주) | {{prevMonthAmount}} | {{momSign}} {{momRate}}% ({{momDiff}}) |
| 전년 동월 대비 ({{lastYearLabel}} 발주) | {{lastYearAmount}} | {{yoySign}} {{yoyRate}}% ({{yoyDiff}}) |

## 확인 필요

- {{checkItem}}
