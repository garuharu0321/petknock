#!/usr/bin/env python3
"""김치 프리미엄(김프/역프) 실시간 스캐너.

업비트 KRW 마켓과 해외 거래소 USDT 무기한선물의 공통 상장 코인을 찾아
가격 괴리(김프%)를 계산해 정렬 출력한다.

    ratio = 업비트가격 / (해외가격 * USDKRW)
    김프% = (ratio - 1) * 100

해외 거래소는 바이낸스 → Bitget → OKX 순으로 폴백하고,
환율은 두나무(업비트 고시환율) → open.er-api.com → frankfurter.app 순으로 폴백한다.

사용 예:
    python3 scanner.py                     # 자동 폴백, 김프 내림차순 전체 출력
    python3 scanner.py --exchange bitget   # 해외 거래소 강제 지정
    python3 scanner.py --fx 1385.5         # 환율 수동 지정 (환율 API 불통 시)
    python3 scanner.py --top 20 --min-volume-krw 1e9
    python3 scanner.py --watch 10          # 10초 간격 반복 스캔
    python3 scanner.py --json              # 기계가 읽을 JSON 출력 (기록/백테스트용)

외부 의존성 없음 (표준 라이브러리만 사용).
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict

USER_AGENT = "kimp-scanner/0.1 (+https://github.com/garuharu0321/petknock)"
HTTP_TIMEOUT = 10
HTTP_RETRIES = 2

# 업비트와 해외 거래소에서 같은 티커지만 다른 코인이거나,
# 단위가 달라(예: 1000배 토큰) 직접 비교가 무의미한 심볼은 제외한다.
SYMBOL_BLACKLIST = {
    "BTT",   # 바이낸스 선물은 1000BTTC 등 단위 상이 이력
}
# 해외 선물 심볼의 "1000PEPE" 같은 배수 접두사 → (배수, 원래 티커)
MULTIPLIER_PREFIXES = ("1000000", "10000", "1000")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_json(url: str, timeout: int = HTTP_TIMEOUT, retries: int = HTTP_RETRIES):
    """GET url and parse JSON, with small retry loop."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise ConnectionError(f"GET {url} failed: {last_err}")


# ---------------------------------------------------------------- 업비트 ----

def upbit_krw_bases() -> dict:
    """KRW 마켓 코인 목록. {티커: 마켓코드} 예: {"BTC": "KRW-BTC"}"""
    markets = fetch_json("https://api.upbit.com/v1/market/all?is_details=false")
    out = {}
    for m in markets:
        code = m["market"]
        if code.startswith("KRW-"):
            out[code.split("-", 1)[1]] = code
    return out


def upbit_tickers(market_codes: list) -> dict:
    """배치 시세 조회. {마켓코드: (현재가KRW, 24h거래대금KRW)}"""
    out = {}
    CHUNK = 100  # 업비트 ticker API는 다건 조회 지원, URL 길이 때문에 분할
    for i in range(0, len(market_codes), CHUNK):
        chunk = market_codes[i:i + CHUNK]
        data = fetch_json("https://api.upbit.com/v1/ticker?markets=" + ",".join(chunk))
        for t in data:
            out[t["market"]] = (float(t["trade_price"]), float(t["acc_trade_price_24h"]))
    return out


# ------------------------------------------------------- 해외 무기한선물 ----

def split_multiplier(base: str):
    """'1000PEPE' -> (1000.0, 'PEPE'), 'BTC' -> (1.0, 'BTC')"""
    for p in MULTIPLIER_PREFIXES:
        if base.startswith(p) and len(base) > len(p) and not base[len(p)].isdigit():
            return float(p), base[len(p):]
    return 1.0, base


def binance_perp_tickers() -> dict:
    """바이낸스 USDT 무기한. {티커: (가격USDT, 24h거래대금USDT)}"""
    info = fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    perp = {}
    for s in info["symbols"]:
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            perp[s["symbol"]] = s["baseAsset"]
    stats = fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    out = {}
    for t in stats:
        base = perp.get(t["symbol"])
        if base is None:
            continue
        mult, real = split_multiplier(base)
        out[real] = (float(t["lastPrice"]) / mult, float(t["quoteVolume"]))
    return out


def bitget_perp_tickers() -> dict:
    """Bitget USDT 무기한. {티커: (가격USDT, 24h거래대금USDT)}"""
    data = fetch_json(
        "https://api.bitget.com/api/v2/mix/market/tickers?productType=usdt-futures")
    out = {}
    for t in data.get("data", []):
        sym = t["symbol"]  # 예: BTCUSDT
        if not sym.endswith("USDT"):
            continue
        mult, real = split_multiplier(sym[:-4])
        price = float(t["lastPr"])
        if price <= 0:
            continue
        out[real] = (price / mult, float(t.get("usdtVolume") or 0))
    return out


def okx_perp_tickers() -> dict:
    """OKX USDT 무기한 스왑. {티커: (가격USDT, 24h거래대금USDT)}"""
    data = fetch_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP")
    out = {}
    for t in data.get("data", []):
        inst = t["instId"]  # 예: BTC-USDT-SWAP
        parts = inst.split("-")
        if len(parts) != 3 or parts[1] != "USDT":
            continue
        mult, real = split_multiplier(parts[0])
        price = float(t["last"])
        if price <= 0:
            continue
        # volCcy24h는 기초자산 수량 단위이므로 가격을 곱해 대금(USDT)으로 환산
        quote_vol = float(t.get("volCcy24h") or 0) * price
        out[real] = (price / mult, quote_vol)
    return out


FUTURES_SOURCES = [
    ("binance", binance_perp_tickers),
    ("bitget", bitget_perp_tickers),
    ("okx", okx_perp_tickers),
]


def get_futures_tickers(exchange: str = "auto"):
    """지정 거래소 또는 auto 폴백으로 선물 시세 확보. (거래소명, dict) 반환."""
    sources = (FUTURES_SOURCES if exchange == "auto"
               else [s for s in FUTURES_SOURCES if s[0] == exchange])
    if not sources:
        raise ValueError(f"unknown exchange: {exchange}")
    errors = []
    for name, fn in sources:
        try:
            log(f"[i] {name} 선물 시세 조회 중...")
            tickers = fn()
            if tickers:
                return name, tickers
            errors.append(f"{name}: empty response")
        except Exception as e:
            errors.append(f"{name}: {e}")
            log(f"[!] {name} 실패 → 다음 거래소로 폴백")
    raise ConnectionError("모든 해외 거래소 조회 실패:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------- 환율 ----

def fx_dunamu() -> float:
    data = fetch_json(
        "https://quotation-api-cdn.dunamu.com/v1/forex/recent?codes=FRX.KRWUSD")
    return float(data[0]["basePrice"])


def fx_erapi() -> float:
    data = fetch_json("https://open.er-api.com/v6/latest/USD")
    return float(data["rates"]["KRW"])


def fx_frankfurter() -> float:
    data = fetch_json("https://api.frankfurter.app/latest?from=USD&to=KRW")
    return float(data["rates"]["KRW"])


FX_SOURCES = [
    ("dunamu", fx_dunamu),
    ("er-api", fx_erapi),
    ("frankfurter", fx_frankfurter),
]


def get_usdkrw(manual: float = None):
    """(환율, 출처) 반환. manual 지정 시 그대로 사용."""
    if manual is not None:
        return manual, "manual"
    errors = []
    for name, fn in FX_SOURCES:
        try:
            rate = fn()
            if 500 < rate < 5000:  # 명백한 오류 응답 방어
                return rate, name
            errors.append(f"{name}: implausible rate {rate}")
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise ConnectionError("모든 환율 소스 실패:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------- 계산 ----

@dataclass
class Row:
    symbol: str
    upbit_price_krw: float
    futures_price_usdt: float
    ratio: float
    kimp_pct: float
    upbit_vol_krw_24h: float
    futures_vol_usdt_24h: float


def compute_rows(upbit_prices: dict, upbit_bases: dict,
                 futures: dict, usdkrw: float) -> list:
    """공통 상장 코인의 김프 계산. 김프% 내림차순 정렬."""
    rows = []
    for base, market_code in upbit_bases.items():
        if base in SYMBOL_BLACKLIST or base not in futures:
            continue
        if market_code not in upbit_prices:
            continue
        krw_price, krw_vol = upbit_prices[market_code]
        usdt_price, usdt_vol = futures[base]
        if usdt_price <= 0 or krw_price <= 0:
            continue
        ratio = krw_price / (usdt_price * usdkrw)
        rows.append(Row(
            symbol=base,
            upbit_price_krw=krw_price,
            futures_price_usdt=usdt_price,
            ratio=ratio,
            kimp_pct=(ratio - 1.0) * 100.0,
            upbit_vol_krw_24h=krw_vol,
            futures_vol_usdt_24h=usdt_vol,
        ))
    rows.sort(key=lambda r: r.kimp_pct, reverse=True)
    return rows


# ---------------------------------------------------------------- 출력 ----

def fmt_krw(v: float) -> str:
    if v >= 1e12:
        return f"{v / 1e12:,.1f}조"
    if v >= 1e8:
        return f"{v / 1e8:,.0f}억"
    return f"{v:,.0f}"


def fmt_price(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:.6g}"


def print_table(rows: list, exchange: str, usdkrw: float, fx_source: str,
                top: int = 0, min_volume_krw: float = 0.0) -> None:
    filtered = [r for r in rows if r.upbit_vol_krw_24h >= min_volume_krw]
    shown = filtered[:top] if top else filtered
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== 김프 스캐너 | {ts} | 해외: {exchange} | "
          f"USDKRW {usdkrw:,.2f} ({fx_source}) | "
          f"공통코인 {len(rows)}개 (필터 후 {len(filtered)}개) ===")
    header = (f"{'심볼':<10}{'김프%':>9}{'업비트(KRW)':>16}"
              f"{'해외(USDT)':>14}{'업비트24h대금':>14}{'해외24h대금($)':>16}")
    print(header)
    print("-" * len(header.expandtabs()))
    for r in shown:
        print(f"{r.symbol:<10}{r.kimp_pct:>+9.2f}{fmt_price(r.upbit_price_krw):>16}"
              f"{fmt_price(r.futures_price_usdt):>14}"
              f"{fmt_krw(r.upbit_vol_krw_24h):>14}"
              f"{'$' + fmt_krw(r.futures_vol_usdt_24h):>16}")
    if rows:
        kimps = [r.kimp_pct for r in filtered] or [r.kimp_pct for r in rows]
        mid = sorted(kimps)[len(kimps) // 2]
        print(f"\n중앙값 김프 {mid:+.2f}% | 최대 {max(kimps):+.2f}% | 최소 {min(kimps):+.2f}%")


def scan_once(args):
    log("[i] 업비트 KRW 마켓 목록 조회 중...")
    bases = upbit_krw_bases()
    log(f"[i] 업비트 KRW 마켓 {len(bases)}개")

    exchange, futures = get_futures_tickers(args.exchange)
    log(f"[i] {exchange} USDT 무기한 {len(futures)}개")

    common = [code for b, code in bases.items()
              if b in futures and b not in SYMBOL_BLACKLIST]
    log(f"[i] 공통 상장 {len(common)}개, 업비트 시세 조회 중...")
    upbit_prices = upbit_tickers(common)

    usdkrw, fx_source = get_usdkrw(args.fx)
    rows = compute_rows(upbit_prices, bases, futures, usdkrw)

    if args.json:
        print(json.dumps({
            "timestamp": time.time(),
            "exchange": exchange,
            "usdkrw": usdkrw,
            "fx_source": fx_source,
            "rows": [asdict(r) for r in rows],
        }, ensure_ascii=False))
    else:
        print_table(rows, exchange, usdkrw, fx_source,
                    top=args.top, min_volume_krw=args.min_volume_krw)


def main(argv=None):
    p = argparse.ArgumentParser(description="업비트-해외선물 김프 스캐너")
    p.add_argument("--exchange", default="auto",
                   choices=["auto", "binance", "bitget", "okx"],
                   help="해외 거래소 (기본 auto: 바이낸스→Bitget→OKX 폴백)")
    p.add_argument("--fx", type=float, default=None,
                   help="USDKRW 환율 수동 지정 (환율 API 불통 시)")
    p.add_argument("--top", type=int, default=0, help="상위 N개만 표시 (0=전체)")
    p.add_argument("--min-volume-krw", type=float, default=0.0,
                   help="업비트 24h 거래대금 최소 필터 (KRW, 예: 1e9)")
    p.add_argument("--watch", type=int, default=0,
                   help="N초 간격 반복 스캔 (0=1회)")
    p.add_argument("--json", action="store_true",
                   help="JSON 한 줄 출력 (기록/백테스트 수집용)")
    args = p.parse_args(argv)

    while True:
        try:
            scan_once(args)
        except ConnectionError as e:
            log(f"[!] {e}")
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
