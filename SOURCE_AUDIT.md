# Bear Market Radar v6 데이터 정의 감사

## 결론

v5까지의 봇은 지표 방향성은 대체로 맞지만, 일부 지표의 라벨이 실제 계산 범위를 충분히 설명하지 못했습니다. v6에서는 다음을 수정했습니다.

1. `미국 10Y 실질금리` → `미국 10Y TIPS 실질금리(DFII10)`로 명확화
2. `반도체 수출 YoY` → `반도체 수출 프록시(HS8541+8542)`로 명확화
3. 관세청 품목별 수출 API를 pageNo=1만 읽던 문제를 수정해 전체 페이지를 순회
4. 하이퍼스케일러 CAPEX를 FMP 우선에서 SEC companyfacts 우선으로 변경
5. CAPEX 메시지에 `실제 cash CAPEX TTM`, `가이던스/리스/약정 제외`를 명시

## 지표별 신뢰도

### 미국 10Y TIPS 실질금리(DFII10)

- 데이터: FRED `DFII10`
- 정의: Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Inflation-Indexed
- 해석: TIPS 시장수익률 기반 실질금리
- 신뢰도: 높음
- 주의: 명목금리 - CPI로 직접 계산한 실질금리나 모델 추정 실질금리가 아님

### 하이퍼스케일러 CAPEX

- 데이터: SEC companyfacts 우선, FMP cash-flow-statement fallback
- 대상: MSFT, GOOGL, META, AMZN, ORCL
- 정의: 실제 공시 cash CAPEX TTM
- 신뢰도: 중상
- 주의: 2026E CAPEX guidance/컨센서스, finance lease, 구매약정, off-balance-sheet AI 인프라 약정은 포함하지 않음
- 따라서 외부 보고서의 `2026E top-5 hyperscaler capex $600B+`와 직접 비교하지 말 것

### 반도체 수출 프록시(HS8541+8542)

- 데이터: 관세청_품목별 수출입실적(GW)
- 정의: HS 8541 + HS 8542 수출액 합산 프록시
- 신뢰도: 중간
- 주의: 산업통상자원부 보도자료의 15대 품목 `반도체` 총액과 완전히 같은 정의가 아닐 수 있음
- v6 수정: API 페이지네이션을 적용해 pageNo=1만 읽는 오류 가능성을 줄임

### Forward EPS / EPS Revision Ratio

- 데이터: FMP analyst-estimates가 가능할 때만 자동수집
- 신뢰도: FMP 접근권한과 데이터 품질에 의존
- FMP 403이면 자동 제외되고 종합점수에 반영되지 않음

## 분석용 해석 원칙

- 종합점수는 공식 하락장 확률이 아님
- 종합점수는 수집 가능한 지표들의 위험 점수 가중평균
- 데이터 커버리지가 100 미만이면 제외 지표가 있으므로 해석 신뢰도를 낮춰야 함
- 반도체 수출과 CAPEX는 특히 `정의`를 같이 읽어야 함
