# Bear Market Radar 데이터 정의 / 검증 메모

이 봇은 투자 판단 보조용 리스크 대시보드입니다. `종합점수`는 하락장 확률이 아니라 지표 기반 위험 점수입니다.

## 공식/정확 데이터로 취급 가능한 항목

- HY Spread: FRED `BAMLH0A0HYM2`, ICE BofA US High Yield OAS.
- 미국 10Y TIPS 실질금리: FRED `DFII10`, 10년 Treasury Inflation-Indexed Securities constant maturity yield.
- 달러지수 프록시: FRED `DTWEXBGS`, Nominal Broad U.S. Dollar Index.
- 원/달러: FRED `DEXKOUS`, South Korean Won to One U.S. Dollar spot exchange rate.
- 제조업 경기 프록시: FRED `IPMAN` YoY. 정확한 ISM PMI가 아니라 공개 제조업 산업생산 프록시입니다.
- 미국 실업률: FRED `UNRATE`.

## 주의가 필요한 항목

### 반도체 수출 프록시(HS8541+8542)

현재 자동수집은 관세청 품목별 수출입실적 API에서 HS 8541 + HS 8542를 합산합니다. 이는 **공식 산업부 '반도체 수출' 분류와 정확히 같지 않은 프록시**입니다.

- 공식 보도자료 수치와 다르면 봇 값보다 공식 산업부 수치를 우선합니다.
- v6부터 메시지 이름을 `반도체 수출 프록시(HS8541+8542)`로 바꿨습니다.

### 하이퍼스케일러 CAPEX 실적 TTM

현재 자동수집은 FMP cash-flow 또는 SEC companyfacts 기반의 실제 trailing-twelve-month CAPEX입니다. 이는 **2026E CAPEX 전망치/가이던스와 다릅니다.**

- 예: 외부 리서치의 `$600B+ 2026E capex`는 전망치입니다.
- 봇의 `TTM $...B`는 최근 보고된 실제 실적 기준 TTM 합계입니다.
- v6부터 메시지에 회사별 TTM breakdown을 표시합니다.

### Forward EPS / EPS Revision Ratio

FMP analyst-estimates 권한이 필요합니다. 권한이 없으면 종합점수에서 제외됩니다.
