"""scanner.py 오프라인 로직 검증 (네트워크 불필요).

실제 API 응답 구조를 그대로 본뜬 픽스처로 fetch_json을 대체해
교집합 추출, 김프 계산, 배수 접두사 처리, 폴백 체인을 검증한다.
"""

import unittest
from unittest import mock

import scanner


UPBIT_MARKETS = [
    {"market": "KRW-BTC", "korean_name": "비트코인"},
    {"market": "KRW-ETH", "korean_name": "이더리움"},
    {"market": "KRW-PEPE", "korean_name": "페페"},
    {"market": "KRW-BTT", "korean_name": "비트토렌트"},   # 블랙리스트
    {"market": "KRW-ONLYKR", "korean_name": "국내단독"},  # 해외 미상장
    {"market": "BTC-ETH", "korean_name": "이더리움"},     # KRW 마켓 아님
]

UPBIT_TICKERS = [
    {"market": "KRW-BTC", "trade_price": 165_000_000, "acc_trade_price_24h": 3.0e11},
    {"market": "KRW-ETH", "trade_price": 5_500_000, "acc_trade_price_24h": 1.5e11},
    {"market": "KRW-PEPE", "trade_price": 0.0150, "acc_trade_price_24h": 2.0e10},
]

BINANCE_INFO = {"symbols": [
    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "TRADING"},
    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "TRADING"},
    {"symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "TRADING"},
    {"symbol": "BTCUSDT_260327", "baseAsset": "BTC", "quoteAsset": "USDT",
     "contractType": "CURRENT_QUARTER", "status": "TRADING"},  # 분기물 제외
    {"symbol": "XYZUSDT", "baseAsset": "XYZ", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "SETTLING"},        # 거래중지 제외
]}

BINANCE_24H = [
    {"symbol": "BTCUSDT", "lastPrice": "118000", "quoteVolume": "9.0e9"},
    {"symbol": "ETHUSDT", "lastPrice": "4000", "quoteVolume": "4.0e9"},
    {"symbol": "1000PEPEUSDT", "lastPrice": "0.0105", "quoteVolume": "8.0e8"},
    {"symbol": "BTCUSDT_260327", "lastPrice": "119000", "quoteVolume": "1e8"},
    {"symbol": "XYZUSDT", "lastPrice": "1.0", "quoteVolume": "0"},
]

FX = 1400.0


def fake_fetch(url, **kw):
    if "api.upbit.com/v1/market/all" in url:
        return UPBIT_MARKETS
    if "api.upbit.com/v1/ticker" in url:
        wanted = url.split("markets=")[1].split(",")
        return [t for t in UPBIT_TICKERS if t["market"] in wanted]
    if "fapi.binance.com/fapi/v1/exchangeInfo" in url:
        return BINANCE_INFO
    if "fapi.binance.com/fapi/v1/ticker/24hr" in url:
        return BINANCE_24H
    if "dunamu.com" in url:
        return [{"basePrice": FX}]
    raise ConnectionError(f"unexpected url {url}")


class TestSymbols(unittest.TestCase):
    def test_split_multiplier(self):
        self.assertEqual(scanner.split_multiplier("1000PEPE"), (1000.0, "PEPE"))
        self.assertEqual(scanner.split_multiplier("1000000MOG"), (1000000.0, "MOG"))
        self.assertEqual(scanner.split_multiplier("BTC"), (1.0, "BTC"))
        # 접두사 바로 뒤가 숫자면 배수 접두사로 보지 않음
        self.assertEqual(scanner.split_multiplier("10001INCH"), (1.0, "10001INCH"))

    def test_upbit_krw_only(self):
        with mock.patch.object(scanner, "fetch_json", fake_fetch):
            bases = scanner.upbit_krw_bases()
        self.assertEqual(set(bases), {"BTC", "ETH", "PEPE", "BTT", "ONLYKR"})
        self.assertEqual(bases["BTC"], "KRW-BTC")

    def test_binance_perp_filter_and_multiplier(self):
        with mock.patch.object(scanner, "fetch_json", fake_fetch):
            fut = scanner.binance_perp_tickers()
        self.assertEqual(set(fut), {"BTC", "ETH", "PEPE"})  # 분기물/중지 심볼 제외
        price, vol = fut["PEPE"]
        self.assertAlmostEqual(price, 0.0105 / 1000)  # 1000PEPE → PEPE 단가 환산
        self.assertEqual(vol, 8.0e8)


class TestKimpMath(unittest.TestCase):
    def _rows(self):
        with mock.patch.object(scanner, "fetch_json", fake_fetch):
            bases = scanner.upbit_krw_bases()
            fut = scanner.binance_perp_tickers()
            common = [c for b, c in bases.items() if b in fut]
            prices = scanner.upbit_tickers(common)
        return scanner.compute_rows(prices, bases, fut, FX)

    def test_intersection_and_blacklist(self):
        rows = self._rows()
        self.assertEqual({r.symbol for r in rows}, {"BTC", "ETH", "PEPE"})

    def test_kimp_values(self):
        rows = {r.symbol: r for r in self._rows()}
        # BTC: 165,000,000 / (118,000 * 1400) = ratio
        expected_btc = (165_000_000 / (118_000 * FX) - 1) * 100
        self.assertAlmostEqual(rows["BTC"].kimp_pct, expected_btc, places=6)
        # PEPE: 0.0150 / (0.0105 * 1400)
        expected_pepe = (0.0150 / ((0.0105 / 1000) * FX) - 1) * 100
        self.assertAlmostEqual(rows["PEPE"].kimp_pct, expected_pepe, places=6)
        # 역프 확인: ETH = 5,500,000 / (4000*1400=5,600,000) - 1 < 0
        self.assertLess(rows["ETH"].kimp_pct, 0)

    def test_sorted_desc(self):
        kimps = [r.kimp_pct for r in self._rows()]
        self.assertEqual(kimps, sorted(kimps, reverse=True))


class TestFallbacks(unittest.TestCase):
    def test_futures_fallback_to_bitget(self):
        def failing_binance():
            raise ConnectionError("blocked")
        bitget_result = {"BTC": (118000.0, 5e9)}
        with mock.patch.object(scanner, "FUTURES_SOURCES", [
            ("binance", failing_binance),
            ("bitget", lambda: bitget_result),
        ]):
            name, tickers = scanner.get_futures_tickers("auto")
        self.assertEqual(name, "bitget")
        self.assertEqual(tickers, bitget_result)

    def test_futures_all_fail(self):
        with mock.patch.object(scanner, "FUTURES_SOURCES", [
            ("binance", mock.Mock(side_effect=ConnectionError("x"))),
        ]):
            with self.assertRaises(ConnectionError):
                scanner.get_futures_tickers("auto")

    def test_fx_fallback_and_sanity(self):
        with mock.patch.object(scanner, "FX_SOURCES", [
            ("dunamu", mock.Mock(side_effect=ConnectionError("x"))),
            ("er-api", lambda: 99999.0),   # 비상식적 값 → 건너뜀
            ("frankfurter", lambda: 1400.0),
        ]):
            rate, src = scanner.get_usdkrw()
        self.assertEqual((rate, src), (1400.0, "frankfurter"))

    def test_fx_manual(self):
        self.assertEqual(scanner.get_usdkrw(1385.5), (1385.5, "manual"))


class TestParsers(unittest.TestCase):
    def test_okx_parser_volume_conversion(self):
        okx_resp = {"data": [
            {"instId": "BTC-USDT-SWAP", "last": "118000", "volCcy24h": "50000"},
            {"instId": "BTC-USD-SWAP", "last": "118000", "volCcy24h": "1"},   # USDT 아님
            {"instId": "1000PEPE-USDT-SWAP", "last": "0.0105", "volCcy24h": "100"},
        ]}
        with mock.patch.object(scanner, "fetch_json", lambda url, **kw: okx_resp):
            out = scanner.okx_perp_tickers()
        self.assertEqual(set(out), {"BTC", "PEPE"})
        self.assertAlmostEqual(out["BTC"][1], 50000 * 118000)   # 기초수량→USDT 환산
        self.assertAlmostEqual(out["PEPE"][0], 0.0105 / 1000)

    def test_bitget_parser(self):
        bg_resp = {"data": [
            {"symbol": "BTCUSDT", "lastPr": "118000", "usdtVolume": "5e9"},
            {"symbol": "ETHUSDC", "lastPr": "3900", "usdtVolume": "1"},  # USDT 아님
            {"symbol": "DEADUSDT", "lastPr": "0", "usdtVolume": "0"},    # 가격 0 제외
        ]}
        with mock.patch.object(scanner, "fetch_json", lambda url, **kw: bg_resp):
            out = scanner.bitget_perp_tickers()
        self.assertEqual(set(out), {"BTC"})
        self.assertEqual(out["BTC"], (118000.0, 5e9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
