# Data-Source Probe Results

## FX — fx
Recommended order: yfinance

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 3 | 3 | 230.93870002776384 |  |  |  | 3 pairs in one batch: {'USDTWD=X': Decimal('31.51099967956543'), 'USDMYR=X': Decimal('4.070000171661377'), 'MYRTWD=X': Decimal('7.747600078582764')} |
| finmind | skipped | 0 |  |  |  |  |  | no key supplied this round |

## MY — quote_history
Recommended order: yfinance

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 1 |  | 254.1747000068426 |  | True | 2021-06-08 | representative=3182.KL, 1226 rows over 5y; decimals_ok left None — yfinance Close is float64 so max_decimals reflects float noise, not true market tick precision (see adapter note) |

## MY — quote_latest
Recommended order: yfinance → klsescreener

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 5 | 5 | 441.86440005432814 |  |  |  | single yf.download() batch of 5 symbols (5/5 returned a close); misses=none |
| klsescreener | fallback | 2 | 1 | 3535.842700046487 | True |  |  | scraped #price data-value per code for 2 sample codes: {'5212': '1.700', '3182': '2.260'}; returns 3-dp STRING (e.g. '2.260') — true Bursa tick precision, corroborates yfinance's MY latest close (which loses sub-pip precision to float64) |

## TW — dividend
Recommended order: (none)

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| finmind | skipped | 0 |  |  |  |  |  | no key supplied this round |

## TW — quote_history
Recommended order: yfinance

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 1 |  | 231.62709991447628 |  | True | 2021-06-08 | representative=2330.TW, 1214 rows over 5y; decimals_ok left None — yfinance Close is float64 so max_decimals reflects float noise, not true market tick precision (see adapter note) |

## TW — quote_latest
Recommended order: tw_gov (TWSE) → yfinance → tw_gov (TPEx) → twstock

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| tw_gov (TWSE) | primary | 4 | 1 | 2282.09610003978 | True |  |  | STOCK_DAY per-stockNo calls for 4/8 listed codes on 20260608: {'0050': '100.95', '2454': '4,070.00', '2330': '2,295.00', '2543': '47.50'} — string close preserves real tick decimals (e.g. '2,295.00'), unlike yfinance float64 |
| yfinance | fallback | 10 | 12 | 3316.2435999838635 |  |  |  | single yf.download() batch of 12 symbols (10/12 returned a close); misses=['6531', '6139'] |
| tw_gov (TPEx) | fallback | 2 | 10208 | 1986.2640999490395 | True |  |  | single mainboard_daily_close_quotes call (10208 rows); 2/4 OTC_TWO codes found: {'8299': '2250.00', '6488': '768.00'}; misses=['6531', '6139'] (likely emerging-board codes not on TPEx mainboard); string close preserves real tick decimals |
| twstock | fallback | 3 | 1 | 1746.5143000008538 |  |  |  | realtime.get() per code for 3 sample codes: {'0050': '100.9500', '2454': '4070.0000', '2330': '2295.0000'}; intraday/realtime source — useful as a latest-quote fallback alongside TWSE |
| finmind | skipped | 0 |  |  |  |  |  | no key supplied this round |

## US — dividend
Recommended order: yfinance

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 1 |  | 665.8347999909893 |  |  | 1987-05-11 | representative=AAPL, 91 dividend rows since 1987-05-11; latest=0.27 |

## US — quote_history
Recommended order: yfinance

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 1 |  | 277.5197000009939 |  | True | 2021-06-07 | representative=AAPL, 1256 rows over 5y; decimals_ok left None — yfinance Close is float64 so max_decimals reflects float noise, not true market tick precision (see adapter note) |

## US — quote_latest
Recommended order: yfinance

| source | verdict | cov | batch | latency ms | 3dp | raw+adj | hist | notes |
|---|---|---|---|---|---|---|---|---|
| yfinance | primary | 16 | 16 | 2057.0423000026494 |  |  |  | single yf.download() batch of 16 symbols (16/16 returned a close); misses=none |
| alphavantage | skipped | 0 |  |  |  |  |  | no key supplied this round |
| finnhub | skipped | 0 |  |  |  |  |  | no key supplied this round |
| stockprices.dev | unusable | 0 | 1 |  |  |  |  | 400 Client Error: Bad Request for url: https://stockprices.dev/api/stocks/TSLA |
