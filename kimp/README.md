# 김프/역프 차익거래 스캐너

업비트(국내 KRW 마켓) ↔ 해외거래소 USDT 무기한선물 공통 상장 코인의
가격 괴리(김치 프리미엄)를 실시간 스캔한다.

```
ratio = 업비트가격 ÷ (해외가격 × USDKRW)
김프% = (ratio − 1) × 100
```

## 1단계 결과: API 연결 테스트 (2026-07-13)

Claude Code 원격 실행 환경(클라우드 컨테이너)에서는 **모든 거래소·환율
API가 이그레스 정책에 의해 차단**되어 라이브 테스트가 불가능했다.
프록시 게이트웨이가 CONNECT 단계에서 403을 반환한다.

| 호스트 | 용도 | 결과 |
|---|---|---|
| api.upbit.com | 업비트 시세 | ❌ 403 (프록시 정책 차단) |
| fapi.binance.com | 바이낸스 USDT 선물 | ❌ 403 |
| api.binance.com | 바이낸스 현물 | ❌ 403 |
| api.bitget.com | Bitget 선물 | ❌ 403 |
| www.okx.com | OKX 스왑 | ❌ 403 |
| api.bybit.com | Bybit | ❌ 403 |
| api.gateio.ws | Gate.io | ❌ 403 |
| quotation-api-cdn.dunamu.com | USDKRW (업비트 고시환율) | ❌ 403 |
| open.er-api.com / api.frankfurter.app | USDKRW 대체 소스 | ❌ 403 |

→ 특정 거래소가 아니라 환경 자체의 네트워크 정책 문제.
**해결 방법**: (a) 로컬 PC에서 실행하거나, (b) Claude Code 환경 설정에서
네트워크 정책을 위 도메인 허용으로 변경 후 재시도.

이 때문에 스캐너는 **어느 거래소가 뚫려도 동작하도록 폴백 체인**을 내장했고,
로직은 실제 API 응답 구조를 본뜬 픽스처로 단위 테스트(12개 전부 통과)했다.

- 해외 거래소: 바이낸스 → Bitget → OKX 자동 폴백 (`--exchange`로 고정 가능)
- 환율: 두나무(업비트 고시환율) → open.er-api.com → frankfurter.app,
  전부 불통이면 `--fx 1385.5`로 수동 지정

## 사용법

의존성 없음. Python 3.8+만 있으면 된다.

```bash
python3 kimp/scanner.py                      # 1회 스캔, 김프 내림차순 전체
python3 kimp/scanner.py --top 20             # 상위 20개
python3 kimp/scanner.py --min-volume-krw 1e9 # 업비트 24h 거래대금 10억 이상만
python3 kimp/scanner.py --exchange okx       # 해외 거래소 고정
python3 kimp/scanner.py --fx 1385.5          # 환율 수동 지정
python3 kimp/scanner.py --watch 10           # 10초 간격 반복
python3 kimp/scanner.py --json >> kimp.jsonl # 시계열 수집 (2단계 백테스트용)
```

출력 예 (픽스처 데이터 데모):

```
=== 김프 스캐너 | 2026-07-13 06:29 | 해외: binance | USDKRW 1,400.00 (dunamu) | 공통코인 3개 ===
심볼          김프%      업비트(KRW)    해외(USDT)   업비트24h대금   해외24h대금($)
PEPE          +2.04           0.015      1.05e-05          200억            $8억
BTC           -0.12     165,000,000       118,000        3,000억           $90억
ETH           -1.79       5,500,000         4,000        1,500억           $40억
```

테스트:

```bash
cd kimp && python3 -m unittest test_scanner -v
```

## 구현 노트

- **배수 접두사 처리**: 해외 선물의 `1000PEPE`, `1000000MOG` 류 심볼은
  배수를 나눠 코인 단가로 환산 후 업비트 티커와 매칭.
- **동명이인 티커 방어**: 같은 티커·다른 코인(예: BTT)은
  `SYMBOL_BLACKLIST`로 제외. 실 데이터 확인 후 목록 보강 필요.
- **거래대금**: 업비트는 `acc_trade_price_24h`(KRW), 바이낸스/Bitget은
  quote 거래대금(USDT) 그대로, OKX는 기초자산 수량(`volCcy24h`)×가격으로 환산.
- **API 호출 수**: 전 종목 스캔에 4~5회 (업비트 목록 1 + 업비트 시세 1~2 +
  해외 목록/시세 1~2 + 환율 1). 레이트리밋 여유 충분, `--watch 10`도 안전.

## 다음 단계 (2단계: 통계 검증)

"극단 괴리 → 평균 수렴" 전략의 통계 검증 계획:

1. **데이터 수집**: `--json --watch`로 시계열 축적, 또는 과거 캔들 API
   (업비트 `/v1/candles`, 바이낸스 `/fapi/v1/klines`)로 김프 시계열 재구성
2. **정상성/평균회귀 검정**: ADF 검정, 반감기(half-life) 추정 (OU 프로세스 적합)
3. **전략 시뮬레이션**: 김프 z-score 극단값 진입 → 평균 복귀 청산,
   수수료·슬리피지·환전 비용 반영한 순수익 분포 확인
4. **주의점**: 김프는 자본통제(국내 원화 출금/해외 송금 제약) 때문에
   차익이 즉시 소멸하지 않음 — 수렴 시간 분포가 전략 성립의 핵심 변수
