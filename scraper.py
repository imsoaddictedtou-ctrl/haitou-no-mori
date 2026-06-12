import re
import time
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 10
BASE_URL = "https://irbank.net"

# 業種 → セクタータイプ分類マップ
# 2026/06: ユーザーの分類表に合わせて3分類（ディフェンシブ/景気敏感/中間）に変更
_SECTOR_TYPE = {
    # ディフェンシブ
    "サービス業":     "ディフェンシブ",
    "情報・通信業":   "ディフェンシブ",
    "情報・通信":     "ディフェンシブ",
    "通信業":         "ディフェンシブ",
    "食料品":         "ディフェンシブ",
    "医薬品":         "ディフェンシブ",
    "陸運業":         "ディフェンシブ",
    "電気機器":       "ディフェンシブ",
    "輸送用機器":     "ディフェンシブ",
    "その他製品":     "ディフェンシブ",
    "倉庫・運輸関連業": "ディフェンシブ",
    "電気・ガス業":   "ディフェンシブ",
    "水産・農林業":   "ディフェンシブ",
    "医療・福祉":     "ディフェンシブ",
    "水産":           "ディフェンシブ",
    "農林":           "ディフェンシブ",
    "電力":           "ディフェンシブ",
    "ガス":           "ディフェンシブ",
    "陸運":           "ディフェンシブ",
    "鉄道":           "ディフェンシブ",
    # 景気敏感
    "機械":           "景気敏感",
    "金属製品":       "景気敏感",
    "その他金融業":   "景気敏感",
    "小売業":         "景気敏感",
    "卸売業":         "景気敏感",
    "化学":           "景気敏感",
    "繊維製品":       "景気敏感",
    "ガラス・土石製品": "景気敏感",
    "窯業・土石製品": "景気敏感",
    "証券、商品先物取引業": "景気敏感",
    "証券業":         "景気敏感",
    "石油・石炭製品": "景気敏感",
    "石油・石炭":     "景気敏感",
    "パルプ・紙":     "景気敏感",
    "精密機器":       "景気敏感",
    "ゴム製品":       "景気敏感",
    "鉄鋼":           "景気敏感",
    "銀行業":         "景気敏感",
    "保険業":         "景気敏感",
    "海運業":         "景気敏感",
    "空運業":         "景気敏感",
    "不動産業":       "景気敏感",
    "建設業":         "景気敏感",
    "鉱業":           "景気敏感",
    "非鉄金属":       "景気敏感",
    "海運":           "景気敏感",
    "空運":           "景気敏感",
    "造船":           "景気敏感",
    # 中間（REIT・複合等）
    "不動産投資信託": "中間",
    "REIT":           "中間",
    "J-REIT":         "中間",
    "複合":           "中間",
    "総合":           "中間",
}


def _get(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code == 404:
            return None
        resp.encoding = resp.apparent_encoding
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


def get_sector(code: str) -> dict:
    """株探から業種分類を取得する（Yahoo Finance profileが使用不可のため）。"""

    sector = "不明"

    # 有効な業種名のセット（誤検出フィルタ用）
    _VALID_SECTORS = set(_SECTOR_TYPE.keys()) | {
        "石油・石炭", "石油石炭", "情報・通信業", "情報通信業",
        "その他製品", "その他金融業", "サービス業",
    }
    # 業種として無効なワード
    _INVALID_WORDS = {"テーマ", "業種", "セクター", "ランキング", "注目", "人気"}

    # ── 方法①: 株探（kabutan.jp）──────────────────────
    soup = _get(f"https://kabutan.jp/stock/?code={code}")
    if soup:
        # ETF/REITの特別判定
        page_text = soup.get_text()
        if "REIT" in page_text or "不動産投資信託" in page_text:
            sector = "不動産投資信託"
        text = soup.get_text()
        idx = text.find("業種")
        if idx >= 0:
            # 「業種」直後から改行・空白を除いた最初のテキストを取得
            candidate = text[idx + 2: idx + 25].strip().split("\n")[0].strip()
            # 有効な業種名かチェック（短すぎ・無効ワードを除外）
            if (candidate and len(candidate) < 20
                    and candidate not in _INVALID_WORDS
                    and not any(w in candidate for w in _INVALID_WORDS)):
                sector = candidate

    # ── 方法②: minkabu（フォールバック）────────────────────
    if sector == "不明":
        soup2 = _get(f"https://minkabu.jp/stock/{code}")
        if soup2:
            import re as _re
            text2 = soup2.get_text()
            m = _re.search(r'業種[^\n]{0,5}\n([^\n]{1,20})', text2)
            if m:
                candidate = m.group(1).strip()
                if candidate and candidate not in _INVALID_WORDS:
                    sector = candidate

    # ── 方法③: Yahoo Finance（最終フォールバック）─────────
    if sector == "不明":
        soup3 = _get(f"https://finance.yahoo.co.jp/quote/{code}/profile")
        if soup3:
            tags = soup3.find_all(True)
            for i, tag in enumerate(tags):
                if tag.get_text(strip=True) in ("業種分類", "業種"):
                    for j in range(i + 1, min(i + 10, len(tags))):
                        candidate = tags[j].get_text(strip=True)
                        if candidate and len(candidate) < 30 and candidate not in ("業種分類", "業種"):
                            sector = candidate
                            break
                    break

    # ── 完全一致 → 部分一致でフォールバック ──────────────
    sector_type = _SECTOR_TYPE.get(sector)
    if sector_type is None:
        for key, val in _SECTOR_TYPE.items():
            if key in sector or sector in key:
                sector_type = val
                break
    sector_type = sector_type or "不明"
    return {"sector": sector, "sector_type": sector_type}


def get_dividend_from_yahoo_quote(code: str) -> float:
    """Yahoo Finance → minkabu の順で予想分配金・配当を取得する。

    優先順位：
      ① Yahoo Finance「予想分配金」（J-REIT）
      ② Yahoo Finance「予想配当」「1株配当」（普通株）
      ③ minkabu「分配金」（ETF：直近1年間実績）

    Returns:
        1口（株）あたりの予想/実績分配金・配当金（円）。取得できなければ 0.0。
    """
    # ── ① ② Yahoo Finance ─────────────────────────────────
    soup = _get(f"https://finance.yahoo.co.jp/quote/{code}")
    if soup is not None:
        text = soup.get_text()
        # Yahoo Financeのテキストは「予想分配金用語4,925.00円」のように間に「用語」が入ることがある
        for pattern in [
            r'予想分配金[^\d]{0,10}([\d,]+(?:\.\d+)?)',   # J-REIT
            r'予想配当[^\d]{0,10}([\d,]+(?:\.\d+)?)',      # 普通株
            r'1株配当[^\d]{0,10}([\d,]+(?:\.\d+)?)',       # 一部銘柄
        ]:
            m = re.search(pattern, text)
            if m:
                try:
                    val = float(m.group(1).replace(',', ''))
                    if val > 1:   # 1円以下は誤検出として除外
                        return val
                except ValueError:
                    pass

    # ── ③ minkabu（ETF向け：直近1年間の実績分配金額） ──────
    soup2 = _get(f"https://minkabu.jp/stock/{code}")
    if soup2 is not None:
        text2 = soup2.get_text()
        # 「分配金（注6）\n92.2円」のような形式
        m = re.search(r'分配金[^\n]{0,15}\n([\d,\.]+)円', text2)
        if m:
            try:
                val = float(m.group(1).replace(',', ''))
                if val > 0:
                    return val
            except ValueError:
                pass

    return 0.0


def get_price_yield_from_kabutan(code: str) -> dict:
    """株探から株価・予想配当利回りを取得し、1株配当を逆算する。

    IRBank・Yahoo Financeがクラウド環境（Streamlit Cloud等のAWS IP）から
    ブロックされる場合のフォールバック。株探はクラウドからもアクセス可能。

    Returns:
        {'price': float, 'yield_pct': float, 'dividend': float}
        取得失敗時は全て 0.0。
    """
    result = {'price': 0.0, 'yield_pct': 0.0, 'dividend': 0.0}
    soup = _get(f"https://kabutan.jp/stock/?code={code}")
    if soup is None:
        return result

    # 株価（<span class="kabuka">2,814.0円</span>）
    price_el = soup.select_one('span.kabuka')
    if price_el:
        try:
            result['price'] = float(
                price_el.get_text(strip=True).replace('円', '').replace(',', '')
            )
        except ValueError:
            pass

    # PER/PBR/利回り/信用倍率テーブルから利回りを取得
    for table in soup.find_all('table'):
        ths = [th.get_text(strip=True) for th in table.find_all('th')]
        if '利回り' in ths:
            tds = [td.get_text(strip=True) for td in table.find_all('td')]
            try:
                idx = ths.index('利回り')
                yield_str = tds[idx].replace('％', '').replace('%', '').strip()
                if yield_str not in ('－', '-', ''):
                    result['yield_pct'] = float(yield_str)
            except (ValueError, IndexError):
                pass
            break

    # 1株配当を逆算（株価 × 利回り）
    if result['price'] > 0 and result['yield_pct'] > 0:
        result['dividend'] = round(result['price'] * result['yield_pct'] / 100, 1)

    return result


def search_code(query: str) -> list[dict]:
    """社名・キーワードから証券コード候補リストを返す。
    IRBank検索 → Yahoo Finance検索の順でフォールバック。
    """
    soup = _get(f"{BASE_URL}/search?query={requests.utils.quote(query)}")
    candidates = []
    if soup is not None:
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            code = href.strip("/")
            if re.fullmatch(r"\d{3}[0-9A-Z]", code) and code not in seen:
                name = a.get_text(strip=True)
                if name:
                    candidates.append({"code": code, "name": name})
                    seen.add(code)

    # IRBankが使えない環境（クラウド等）→ Yahoo Finance検索
    if not candidates:
        candidates = search_code_yahoo(query)
    return candidates


def get_company_info(code: str) -> dict:
    """企業名・株価・各種指標を取得する。
    IRBank → 株探の順でフォールバック（クラウド環境対応）。
    """
    soup = _get(f"{BASE_URL}/{code}")
    if soup is None:
        return get_company_info_kabutan(code)

    title = soup.title.get_text(strip=True) if soup.title else ""
    if "NOT FOUND" in title or "404" in title:
        return {"listed": False}
    # IRBankブロック時（CloudFrontエラーページ等）は株探へ
    # 正常時のタイトルは「7203 トヨタ自動車 | 株式情報」のようにコードを含む
    if code not in title:
        kabutan_info = get_company_info_kabutan(code)
        if kabutan_info.get("listed"):
            return kabutan_info

    company_name = "不明"
    if title:
        parts = title.split("|")[0].strip().split()
        if len(parts) >= 2:
            company_name = " ".join(parts[1:])

    stock_price = "不明"
    dividend_yield = "不明"
    per = "不明"
    pbr = "不明"
    roe = "不明"
    eps = "不明"
    bps = "不明"
    equity_ratio = "不明"
    market_cap = "不明"
    dividend_amount = "不明"

    for div in soup.find_all("div"):
        text = div.get_text(strip=True)
        if "配当利回り" in text and "PER" in text and len(text) < 500:
            m = re.search(r"時価総額([\d兆億,\.]+)", text)
            if m:
                market_cap = m.group(1)
            m = re.search(r"PER（連）予([\d\.\-]+)倍", text)
            if m:
                per = m.group(1) + "倍"
            m = re.search(r"PBR（連）([\d\.\-]+)倍", text)
            if m:
                pbr = m.group(1) + "倍"
            m = re.search(r"配当利回り予([\d\.]+)%\s*\((\d+)\)", text)
            if m:
                dividend_yield = m.group(1) + "%"
                dividend_amount = m.group(2) + "円"
            elif re.search(r"配当利回り予([\d\.]+)%", text):
                m2 = re.search(r"配当利回り予([\d\.]+)%", text)
                dividend_yield = m2.group(1) + "%"
            m = re.search(r"ROE（連）予([\d\.\-]+)%", text)
            if m:
                roe = m.group(1) + "%"
            m = re.search(r"EPS（連）予([\d\.\-,]+)円", text)
            if m:
                eps = m.group(1) + "円"
            m = re.search(r"BPS（連）([\d\.\-,]+)円", text)
            if m:
                bps = m.group(1) + "円"
            m = re.search(r"株主資本比率（連）([\d\.\-]+)%", text)
            if m:
                equity_ratio = m.group(1) + "%"
            break

    for div in soup.find_all("div"):
        text = div.get_text(strip=True)
        if "終値" in text and "始値" in text and len(text) < 300:
            m = re.search(r"終値[+\-\d\.%]*?([\d,]+)出来高", text)
            if m:
                stock_price = m.group(1) + "円"
            break

    return {
        "listed": True,
        "company_name": company_name,
        "code": code,
        "stock_price": stock_price,
        "dividend_yield": dividend_yield,
        "dividend_amount": dividend_amount,
        "per": per,
        "pbr": pbr,
        "roe": roe,
        "eps": eps,
        "bps": bps,
        "equity_ratio": equity_ratio,
        "market_cap": market_cap,
    }


def get_results(code: str) -> list[dict]:
    """売上高・営業利益・純利益・EPSの年次推移（最大15年）を取得する。
    IRBank → 株探の順でフォールバック（クラウド環境対応）。
    """
    soup = _get(f"{BASE_URL}/{code}/results")
    if soup is None:
        return get_results_kabutan(code)

    # ヘッダー名→列インデックスのマッピング候補（優先順に記載・完全一致優先）
    COL_KEYS = {
        "revenue":    ["売上", "経常収益", "収益"],
        "op_profit":  ["営利", "営業利益", "経常"],
        "net_profit": ["当期利益", "純利益", "当期純利益", "純利"],
        "eps":        ["EPS"],
        "roe":        ["ROE"],
    }
    # テーブル検出に使うキーワード（部分一致）
    TABLE_DETECT = ["売上", "収益", "営利", "経常収益"]

    results = []
    for table in soup.find_all("table"):
        # th が重複している場合は先頭の出現分だけ使う
        raw_headers = [th.get_text(strip=True) for th in table.find_all("th")]
        half = len(raw_headers) // 2
        headers = raw_headers[:half] if half and raw_headers[:half] == raw_headers[half:] else raw_headers

        if not any(kw in h for h in headers for kw in TABLE_DETECT):
            continue

        # 各フィールドの列インデックスをヘッダーから動的に解決
        # 完全一致を優先し、なければ部分一致にフォールバック
        col_idx: dict[str, int] = {}
        for field, keywords in COL_KEYS.items():
            for kw in keywords:
                # 完全一致
                for i, h in enumerate(headers):
                    if h == kw:
                        col_idx[field] = i
                        break
                if field in col_idx:
                    break
                # 部分一致（フォールバック）
                for i, h in enumerate(headers):
                    if kw in h:
                        col_idx[field] = i
                        break
                if field in col_idx:
                    break

        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 3:
                continue

            def _cell(field: str) -> str:
                idx = col_idx.get(field)
                return cells[idx] if idx is not None and idx < len(cells) else "不明"

            results.append({
                "year":       cells[0] if cells[0] else "不明",
                "revenue":    _cell("revenue"),
                "op_profit":  _cell("op_profit"),
                "net_profit": _cell("net_profit"),
                "eps":        _cell("eps"),
                "roe":        _cell("roe"),
            })
        break

    # IRBankブロック時（テーブルが見つからない）→ 株探
    if not results:
        results = get_results_kabutan(code)
    return results


def get_dividends_from_results(code: str) -> list[dict]:
    """業績ページの一株配当列から配当金推移を取得する（株式分割調整済み）。
    IRBank → 株探の順でフォールバック（クラウド環境対応）。
    """
    soup = _get(f"{BASE_URL}/{code}/results")
    if soup is None:
        return get_dividends_from_results_kabutan(code)

    dividends = []
    for table in soup.find_all("table"):
        raw_headers = [th.get_text(strip=True) for th in table.find_all("th")]
        half = len(raw_headers) // 2
        headers = raw_headers[:half] if half and raw_headers[:half] == raw_headers[half:] else raw_headers

        if "一株配当" not in headers:
            continue

        def _find_col(keywords: list[str]) -> int | None:
            for kw in keywords:
                for i, h in enumerate(headers):
                    if h == kw:
                        return i
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return None

        total_idx = _find_col(["一株配当"])

        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 2:
                continue

            total = cells[total_idx] if total_idx is not None and total_idx < len(cells) else "不明"
            if not total or total in ("-", ""):
                total = "不明"

            dividends.append({
                "year":    cells[0] if cells[0] else "不明",
                "type":    "本決算",
                "interim": "不明",
                "yearend": "不明",
                "total":   total,
                "yield":   "不明",
            })
        break

    # IRBankブロック時（テーブルなし or 全行「不明」）→ 株探
    if not dividends or all(d["total"] == "不明" for d in dividends):
        kabutan_divs = get_dividends_from_results_kabutan(code)
        if kabutan_divs:
            return kabutan_divs
    return dividends


def get_dividends(code: str) -> list[dict]:
    """過去の配当金履歴を取得する。"""
    soup = _get(f"{BASE_URL}/{code}/dividend")
    if soup is None:
        return []

    dividends = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "合計" not in headers and "中間" not in headers:
            continue

        # 動的に列インデックスを解決（四半期配当など列数が異なる企業に対応）
        def _find_col(keywords: list[str]) -> int | None:
            for kw in keywords:
                for i, h in enumerate(headers):
                    if h == kw:          # 完全一致優先
                        return i
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h:          # 部分一致フォールバック
                        return i
            return None

        # 分割調整列を優先（株式分割があっても一貫したトレンドを表示するため）
        # 分割調整列がなければ合計列にフォールバック
        adjusted_idx = _find_col(["分割調整"])
        total_idx    = adjusted_idx if adjusted_idx is not None else _find_col(["合計"])
        yield_idx    = _find_col(["利回り"])
        interim_idx  = _find_col(["中間"])
        yearend_idx  = _find_col(["期末", "年末"])

        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 3:
                continue

            def _cell(idx: int | None) -> str:
                return cells[idx] if idx is not None and idx < len(cells) else "不明"

            dividends.append({
                "year":    cells[0] if cells[0] else "不明",
                "type":    cells[1] if len(cells) > 1 else "不明",
                "interim": _cell(interim_idx),
                "yearend": _cell(yearend_idx),
                "total":   _cell(total_idx),
                "yield":   _cell(yield_idx),
            })
        break
    return dividends


def get_dividend_streak(code: str) -> dict:
    """連続増配年数・コロナ禍例外・5年平均増配率を返す。

    /dividend ページの本決算合計配当を使用。取得できない場合は
    /results ページにフォールバック。

    Returns:
        streak        : 連続増配年数（コロナ例外を除いた実質年数）
        covid_exception: 2020/2021に一時減配があったか
        growth_rate   : 直近5年の年平均増配率(%)。Noneなら計算不可
        data_ok       : データが取得できたか
    """

    def _parse(s: str) -> float | None:
        if not s or s in ('不明', '-', '－', '*', ''):
            return None
        try:
            return float(str(s).replace(',', '').replace('▲', '-').replace('－', '-'))
        except ValueError:
            return None

    # ── データ取得（/dividend → /results フォールバック） ──
    rows = get_dividends(code)
    if not rows:
        rows = get_dividends_from_results(code)

    if not rows:
        return {'streak': 0, 'covid_exception': False, 'growth_rate': None, 'latest_div': None, 'data_ok': False}

    # ── 年度ごとの合計配当を整理 ─────────────────────────
    # "予" "見込" を含む行（予想）は除外し、本決算行だけ使う
    annual: list[tuple[str, float]] = []
    for r in rows:
        year = str(r.get('year', ''))
        if any(kw in year for kw in ['予', '見込', 'E', 'e']):
            continue
        # 本決算 or 合計行のみ（中間配当単独行はスキップ）
        rtype = str(r.get('type', ''))
        if '中間' in rtype and '本決算' not in rtype:
            continue
        total = _parse(r.get('total', ''))
        if total is None or total <= 0:
            continue
        # 年度を "YYYY" 形式に正規化
        m = re.search(r'(\d{4})', year)
        if not m:
            continue
        yr = m.group(1)
        annual.append((yr, total))

    # 古い順に並べ、同一年度は最後のもの（最新）を使う
    seen: dict[str, float] = {}
    for yr, val in annual:
        seen[yr] = val
    sorted_annual = sorted(seen.items())  # [(year_str, amount), ...]

    if len(sorted_annual) < 2:
        return {'streak': 0, 'covid_exception': False, 'growth_rate': None, 'latest_div': None, 'data_ok': False}

    # ── 連続増配カウント（新しい順に遡る） ──────────────
    streak = 0
    covid_exception = False
    years  = [yr for yr, _ in sorted_annual]
    vals   = [v  for _, v  in sorted_annual]

    for i in range(len(vals) - 1, 0, -1):
        curr, prev = vals[i], vals[i - 1]
        yr = years[i]
        if curr > prev:
            streak += 1
        elif curr == prev:
            # 横ばいは「維持」として増配継続扱い
            streak += 1
        else:
            # 減配 → コロナ禍(2020/2021)なら例外として継続
            if yr in ('2020', '2021'):
                covid_exception = True
                # 例外年はカウントせずに続行
            else:
                break  # それ以外の減配で打ち切り

    # ── 直近5年の年平均増配率 ────────────────────────────
    growth_rate = None
    if len(vals) >= 2:
        recent = vals[-min(6, len(vals)):]  # 最大6年分（5年の成長を見る）
        n = len(recent) - 1
        if recent[0] > 0 and n > 0:
            try:
                growth_rate = ((recent[-1] / recent[0]) ** (1 / n) - 1) * 100
            except Exception:
                pass

    return {
        'streak':          streak,
        'covid_exception': covid_exception,
        'growth_rate':     growth_rate,
        'latest_div':      vals[-1] if vals else None,   # 直近の年間配当額（円）
        'data_ok':         True,
    }


def _parse_value(text: str) -> float | None:
    """'10.8兆'や'-0.44兆'や'3253億'などを数値（億円）に変換する。"""
    if not text or text in ("不明", "-", "*", "赤字", ""):
        return None
    text = text.replace(",", "").replace("*", "").strip()
    neg = text.startswith("-")
    text = text.lstrip("-")
    try:
        m = re.match(r"([\d\.]+)兆", text)
        if m:
            val = float(m.group(1)) * 10000
            return -val if neg else val
        m = re.match(r"([\d\.]+)億", text)
        if m:
            val = float(m.group(1))
            return -val if neg else val
        m = re.match(r"([\d\.]+)", text)
        if m:
            val = float(m.group(1))
            return -val if neg else val
    except Exception:
        pass
    return None


def scrape_all(query: str) -> dict:
    """
    社名または証券コードを受け取り、全データを返す。

    戻り値のパターン:
    - 正常: {"listed": True, ...}
    - 複数候補: {"multiple": True, "candidates": [...]}
    - 未発見: {"listed": False, "error": "..."}
    """
    query = query.strip()
    # 全角数字・スペースを半角に変換
    query = query.translate(str.maketrans(
        "０１２３４５６７８９　ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
        "0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ")).strip()
    # 新形式コード（285A等）の小文字入力に対応
    if re.fullmatch(r"\d{3}[0-9A-Za-z]", query):
        query = query.upper()

    if re.fullmatch(r"\d{3}[0-9A-Z]", query):
        code = query
    else:
        candidates = search_code(query)
        if not candidates:
            return {"listed": False, "error": f"「{query}」に該当する上場企業が見つかりませんでした。"}
        if len(candidates) == 1:
            code = candidates[0]["code"]
        else:
            return {"multiple": True, "candidates": candidates[:10]}

    import time

    info = get_company_info(code)
    if not info.get("listed"):
        return {"listed": False, "error": "該当する上場企業が見つかりませんでした。"}

    time.sleep(0.5)
    results = get_results(code)
    time.sleep(0.5)
    # 業績ページの一株配当を優先（株式分割調整済みで正確）
    dividends = get_dividends_from_results(code)
    if not dividends:
        dividends = get_dividends(code)
    time.sleep(0.3)
    sector_info = get_sector(code)

    # グラフ用に数値データを整形
    chart_results = []
    for r in results:
        chart_results.append({
            **r,
            "revenue_val": _parse_value(r["revenue"]),
            "op_profit_val": _parse_value(r["op_profit"]),
            "net_profit_val": _parse_value(r["net_profit"]),
            "eps_val": _parse_value(r["eps"]),
            "roe_val": _parse_value(r["roe"]),
        })

    chart_dividends = []
    for d in dividends:
        chart_dividends.append({
            **d,
            "total_val": _parse_value(d["total"]),
            "yield_val": _parse_value(d["yield"].replace("%", "") if d["yield"] != "不明" else "不明"),
        })

    return {
        "listed": True,
        "error": None,
        **info,
        **sector_info,
        "results": chart_results,
        "dividends": chart_dividends,
    }


# IRBankカテゴリ名 → URLエンコード済みパス のマッピング
IRBANK_CATEGORY_URLS: dict[str, str] = {
    # ディフェンシブ
    '食料品':         '/category/%E9%A3%9F%E6%96%99%E5%93%81',
    '医薬品':         '/category/%E5%8C%BB%E8%96%AC%E5%93%81',
    '電気・ガス業':   '/category/%E9%9B%BB%E6%B0%97%E3%83%BB%E3%82%AC%E3%82%B9%E6%A5%AD',
    '陸運業':         '/category/%E9%99%B8%E9%81%8B%E6%A5%AD',
    '通信業':         '/category/%E9%80%9A%E4%BF%A1%E6%A5%AD',
    '水産・農林業':   '/category/%E6%B0%B4%E7%94%A3%E3%83%BB%E8%BE%B2%E6%9E%97%E6%A5%AD',
    '小売業':         '/category/%E5%B0%8F%E5%A3%B2%E6%A5%AD',
    # 景気敏感
    '輸送用機器':     '/category/%E8%BC%B8%E9%80%81%E7%94%A8%E6%A9%9F%E5%99%A8',
    '鉄鋼':           '/category/%E9%89%84%E9%8B%BC',
    '化学':           '/category/%E5%8C%96%E5%AD%A6',
    '機械':           '/category/%E6%A9%9F%E6%A2%B0',
    '電気機器':       '/category/%E9%9B%BB%E6%B0%97%E6%A9%9F%E5%99%A8',
    '非鉄金属':       '/category/%E9%9D%9E%E9%89%84%E9%87%91%E5%B1%9E',
    '鉱業':           '/category/%E9%89%B1%E6%A5%AD',
    '海運業':         '/category/%E6%B5%B7%E9%81%8B%E6%A5%AD',
    '建設業':         '/category/%E5%BB%BA%E8%A8%AD%E6%A5%AD',
    '不動産業':       '/category/%E4%B8%8D%E5%8B%95%E7%94%A3%E6%A5%AD',
    '繊維製品':       '/category/%E7%B9%8A%E7%B6%AD%E8%A3%BD%E5%93%81',
    'パルプ・紙':     '/category/%E3%83%91%E3%83%AB%E3%83%97%E3%83%BB%E7%B4%99',
    'ゴム製品':       '/category/%E3%82%B4%E3%83%A0%E8%A3%BD%E5%93%81',
    '窯業・土石製品': '/category/%E7%AA%AF%E6%A5%AD%E3%83%BB%E5%9C%9F%E7%9F%B3%E8%A3%BD%E5%93%81',
    '金属製品':       '/category/%E9%87%91%E5%B1%9E%E8%A3%BD%E5%93%81',
    '石油・石炭製品': '/category/%E7%9F%B3%E6%B2%B9%E3%83%BB%E7%9F%B3%E7%82%AD%E8%A3%BD%E5%93%81',
    # 金融
    '銀行業':         '/category/%E9%8A%80%E8%A1%8C%E6%A5%AD',
    '証券、商品先物取引業': '/category/%E8%A8%BC%E5%88%B8%E3%80%81%E5%95%86%E5%93%81%E5%85%88%E7%89%A9%E5%8F%96%E5%BC%95%E6%A5%AD',
    '保険業':         '/category/%E4%BF%9D%E9%99%BA%E6%A5%AD',
    'その他金融業':   '/category/%E3%81%9D%E3%81%AE%E4%BB%96%E9%87%91%E8%9E%8D%E6%A5%AD',
    # 中間
    '卸売業':         '/category/%E5%8D%B8%E5%A3%B2%E6%A5%AD',
    '情報・通信業':   '/category/%E6%83%85%E5%A0%B1%E3%83%BB%E9%80%9A%E4%BF%A1%E6%A5%AD',
    'サービス業':     '/category/%E3%82%B5%E3%83%BC%E3%83%93%E3%82%B9%E6%A5%AD',
    'その他製品':     '/category/%E3%81%9D%E3%81%AE%E4%BB%96%E8%A3%BD%E5%93%81',
    '精密機器':       '/category/%E7%B2%BE%E5%AF%86%E6%A9%9F%E5%99%A8',
    '空運業':         '/category/%E7%A9%BA%E9%81%8B%E6%A5%AD',
    '倉庫・運輸関連業':'/category/%E5%80%89%E5%BA%AB%E3%83%BB%E9%81%8B%E8%BC%B8%E9%96%A2%E9%80%A3%E6%A5%AD',
}


def get_category_stocks(sector_name: str, min_market_cap_oku: float = 500.0) -> list[dict]:
    """
    IRBankのカテゴリページから業種別銘柄一覧を取得する。

    Parameters
    ----------
    sector_name        : 業種名（例: '食料品', '化学'）
    min_market_cap_oku : 最低時価総額（億円）。これ未満はスキップ。

    Returns
    -------
    [{'code': '2502', 'name': 'アサヒグループHD', 'market_cap_oku': 22450, ...}, ...]
    スコア順（時価総額降順）でソート済み。
    """
    path = IRBANK_CATEGORY_URLS.get(sector_name)
    if not path:
        return []

    soup = _get(BASE_URL + path)
    if soup is None:
        return []

    results = []
    table = soup.find('table')
    if not table:
        return []

    rows = table.find_all('tr')
    for row in rows[1:]:   # ヘッダースキップ
        cells = row.find_all(['td', 'th'])
        if len(cells) < 3:
            continue

        # コードと社名はリンクから取得
        link = row.find('a')
        code = ''
        if link:
            href = link.get('href', '')
            m = re.search(r'/(\d{3}[0-9A-Z])', href)
            if m:
                code = m.group(1)
        if not code:
            # No.列と会社名列から取得
            raw_code = cells[0].get_text(strip=True)
            m = re.search(r'(\d{3}[0-9A-Z])', raw_code)
            if m:
                code = m.group(1)

        if not code:
            continue

        name = cells[1].get_text(strip=True) if len(cells) > 1 else ''

        # 時価総額をパース（'2兆2450億' → 22450.0）
        cap_text = cells[2].get_text(strip=True)
        cap_oku = _parse_cap(cap_text)

        if cap_oku is not None and cap_oku < min_market_cap_oku:
            continue

        results.append({
            'code':          code,
            'name':          name,
            'sector':        sector_name,
            'market_cap_oku': cap_oku,
        })

    # 時価総額降順でソート
    results.sort(key=lambda x: (x['market_cap_oku'] or 0), reverse=True)
    return results


def _parse_cap(text: str) -> float | None:
    """'2兆2450億' / '350億' / '' などを億円の float に変換。"""
    if not text or text in ('-', ''):
        return None
    text = text.strip()
    val = 0.0
    m_cho = re.search(r'([\d,\.]+)兆', text)
    m_oku = re.search(r'([\d,\.]+)億', text)
    if m_cho:
        val += float(m_cho.group(1).replace(',', '')) * 10000
    if m_oku:
        val += float(m_oku.group(1).replace(',', ''))
    return val if val > 0 else None


# ── 株探 業種別銘柄リスト ─────────────────────────────────
# IRBankがクラウド環境（Streamlit Cloud等）からブロックされるため、
# 業種別の候補取得は株探をプライマリとして使う。
# kabutan.jp/themes/?industry=N の業種番号マッピング（東証33業種）
KABUTAN_INDUSTRY_NUM: dict[str, int] = {
    '水産・農林業': 1,  '鉱業': 2,        '建設業': 3,      '食料品': 4,
    '繊維製品': 5,      'パルプ・紙': 6,  '化学': 7,        '医薬品': 8,
    '石油・石炭': 9,    'ゴム製品': 10,   'ガラス・土石': 11, '鉄鋼': 12,
    '非鉄金属': 13,     '金属製品': 14,   '機械': 15,       '電気機器': 16,
    '輸送用機器': 17,   '精密機器': 18,   'その他製品': 19,  '電気・ガス': 20,
    '陸運業': 21,       '海運業': 22,     '空運業': 23,     '倉庫・運輸': 24,
    '情報・通信業': 25, '卸売業': 26,     '小売業': 27,     '銀行業': 28,
    '証券・商品': 29,   '保険業': 30,     'その他金融業': 31, '不動産業': 32,
    'サービス業': 33,
    # SECTOR_TYPE_MAP側の表記ゆれエイリアス
    '電気・ガス業': 20,
    '石油・石炭製品': 9,
    'ガラス・土石製品': 11,
    '窯業・土石製品': 11,
    '倉庫・運輸関連業': 24,
    '証券、商品先物取引業': 29,
    '証券業': 29,
    '通信業': 25,
    '小売': 27,
}


def get_category_stocks_kabutan(sector_name: str, min_yield: float = 0.0,
                                prime_only: bool = True,
                                max_pages: int = 5) -> list[dict]:
    """
    株探の業種別ページから銘柄一覧（利回り付き）を取得する。

    1リクエストで複数銘柄の利回りが取れるため、IRBank方式
    （銘柄ごとに個別ページを取得）より高速かつクラウド対応。

    Parameters
    ----------
    sector_name : 業種名（例: '食料品'）
    min_yield   : 最低配当利回り%。これ未満は除外。
    prime_only  : Trueなら東証プライム銘柄のみ（大型株中心）
    max_pages   : 取得する最大ページ数（1ページ約15銘柄）

    Returns
    -------
    [{'code', 'name', 'sector', 'market', 'price', 'yield_pct'}, ...]
    利回り降順でソート済み。
    """
    num = KABUTAN_INDUSTRY_NUM.get(sector_name)
    if num is None:
        return []

    results = []
    for page in range(1, max_pages + 1):
        soup = _get(f"https://kabutan.jp/themes/?industry={num}&page={page}")
        if soup is None:
            break

        table = None
        for t in soup.find_all('table'):
            ths = [th.get_text(strip=True) for th in t.find_all('th')]
            if 'コード' in ths and '利回り' in ths:
                table = t
                break
        if table is None:
            break

        found_in_page = 0
        for tr in table.find_all('tr')[1:]:
            cells = tr.find_all(['td', 'th'])
            if len(cells) < 13:
                continue
            code = cells[0].get_text(strip=True)
            if not re.fullmatch(r'\d{3}[0-9A-Z]', code):
                continue   # 4桁コード以外（英字入り等）はスキップ
            found_in_page += 1

            market = cells[2].get_text(strip=True)
            if prime_only and 'Ｐ' not in market:
                continue

            def _f(s):
                s = s.replace(',', '').replace('％', '').replace('%', '').strip()
                try:
                    return float(s)
                except ValueError:
                    return None

            price     = _f(cells[5].get_text(strip=True))
            yield_pct = _f(cells[12].get_text(strip=True))

            if yield_pct is None or yield_pct < min_yield:
                continue

            results.append({
                'code':      code,
                'name':      cells[1].get_text(strip=True),
                'sector':    sector_name,
                'market':    market,
                'price':     price,
                'yield_pct': yield_pct,
            })

        if found_in_page == 0:
            break   # 最終ページ到達
        time.sleep(0.3)

    results.sort(key=lambda x: x['yield_pct'], reverse=True)
    return results


# ── 株探フォールバック（IRBankクラウドブロック対策） ──────
def _kabutan_finance_data(code: str) -> dict:
    """株探の決算ページから通期業績と財務指標を取得する。

    Returns:
        {'results': [{'year','revenue','op_profit','net_profit','eps','dividend'}...],
         'equity_ratio': '61.6%' or '不明'}
    """
    out = {'results': [], 'equity_ratio': '不明'}
    soup = _get(f"https://kabutan.jp/stock/finance?code={code}")
    if soup is None:
        return out

    def _oku_str(s: str) -> str:
        """百万円表記 '314,312' → '3143.1億'。パース不可なら'不明'"""
        s = s.replace(',', '').replace('－', '').strip()
        try:
            oku = float(s) / 100   # 百万円→億円
            return f"{oku:.1f}億"
        except ValueError:
            return '不明'

    # 通期業績テーブル（決算期/売上高/営業益/.../修正1株益/修正1株配）
    for t in soup.find_all('table'):
        ths = [th.get_text(strip=True) for th in t.find_all('th')]
        if '修正1株益' in ths and '決算期' in ths:
            for tr in t.find_all('tr')[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all(['th', 'td'])]
                if len(cells) < 7 or not re.match(r'\d{4}\.\d{2}', cells[0]):
                    continue
                out['results'].append({
                    'year':       cells[0].replace('.', '/'),
                    'revenue':    _oku_str(cells[1]),
                    'op_profit':  _oku_str(cells[2]),
                    'net_profit': _oku_str(cells[4]),
                    'eps':        cells[5].replace(',', '') or '不明',
                    'roe':        '不明',
                    'dividend':   cells[6].replace(',', '') or '不明',
                })
            break

    # 財務テーブルから自己資本比率（最新行）
    for t in soup.find_all('table'):
        ths = [th.get_text(strip=True) for th in t.find_all('th')]
        if '自己資本比率' in ths and '決算期' in ths:
            try:
                idx = ths.index('自己資本比率')
                last_val = None
                for tr in t.find_all('tr')[1:]:
                    cells = [c.get_text(strip=True) for c in tr.find_all(['th', 'td'])]
                    if len(cells) > idx and re.match(r'\d{4}\.\d{2}', cells[0]):
                        v = cells[idx].replace('％', '').replace('%', '').strip()
                        if v and v not in ('－', '-'):
                            last_val = v
                if last_val:
                    out['equity_ratio'] = last_val + '%'
            except (ValueError, IndexError):
                pass
            break

    return out


def get_results_kabutan(code: str) -> list[dict]:
    """株探版 get_results。IRBankと同じ形式で返す。"""
    data = _kabutan_finance_data(code)
    return [
        {k: r[k] for k in ('year', 'revenue', 'op_profit', 'net_profit', 'eps', 'roe')}
        for r in data['results']
    ]


def get_dividends_from_results_kabutan(code: str) -> list[dict]:
    """株探版 配当推移。IRBankのget_dividends_from_resultsと同形式。"""
    data = _kabutan_finance_data(code)
    return [
        {
            'year':    r['year'],
            'type':    '本決算',
            'interim': '不明',
            'yearend': '不明',
            'total':   r['dividend'],
            'yield':   '不明',
        }
        for r in data['results']
    ]


def get_company_info_kabutan(code: str) -> dict:
    """株探版 get_company_info。トップページ＋決算ページの2リクエスト。"""
    soup = _get(f"https://kabutan.jp/stock/?code={code}")
    if soup is None:
        return {'listed': False}

    title = soup.title.get_text(strip=True) if soup.title else ''
    m = re.match(r'(.+?)【', title)
    company_name = m.group(1).strip() if m else '不明'
    if company_name == '不明' or '株探' == company_name:
        return {'listed': False}

    info = {
        'listed': True, 'company_name': company_name, 'code': code,
        'stock_price': '不明', 'dividend_yield': '不明', 'dividend_amount': '不明',
        'per': '不明', 'pbr': '不明', 'roe': '不明', 'eps': '不明', 'bps': '不明',
        'equity_ratio': '不明', 'market_cap': '不明',
    }

    price_el = soup.select_one('span.kabuka')
    if price_el:
        info['stock_price'] = price_el.get_text(strip=True)

    for table in soup.find_all('table'):
        # 全角/半角どちらの表記にも対応するため正規化して照合
        ths_raw = [th.get_text(strip=True) for th in table.find_all('th')]
        _norm = str.maketrans('ＰＥＲＢ％', 'PERB%')
        ths = [h.translate(_norm) for h in ths_raw]
        if '利回り' in ths and 'PER' in ths:
            tds = [td.get_text(strip=True) for td in table.find_all('td')]

            def _v(col):
                try:
                    i = ths.index(col)
                    return tds[i] if i < len(tds) else '不明'
                except ValueError:
                    return '不明'

            per = _v('PER').replace('倍', '')
            if per not in ('－', '-', '', '不明'):
                info['per'] = per + '倍'
            pbr = _v('PBR').replace('倍', '')
            if pbr not in ('－', '-', '', '不明'):
                info['pbr'] = pbr + '倍'
            yld = _v('利回り').translate(_norm).replace('%', '')
            if yld not in ('－', '-', '', '不明'):
                info['dividend_yield'] = yld + '%'
            cap = _v('時価総額')
            if cap not in ('－', '-', '', '不明'):
                info['market_cap'] = cap.replace('円', '')
            break

    # 自己資本比率は決算ページから
    fin = _kabutan_finance_data(code)
    info['equity_ratio'] = fin['equity_ratio']
    return info


def search_code_yahoo(query: str) -> list[dict]:
    """Yahoo Finance検索版 search_code。IRBank検索が使えない環境向け。"""
    soup = _get(f"https://finance.yahoo.co.jp/search/?query={requests.utils.quote(query)}")
    if soup is None:
        return []
    candidates, seen = [], set()
    for a in soup.find_all('a', href=True):
        m = re.search(r'/quote/(\d{3}[0-9A-Z])\.T', a['href'])
        if not m:
            continue
        code = m.group(1)
        name = a.get_text(strip=True)
        # ナビリンク（チャート・時系列等）を除外し、社名らしいテキストのみ採用
        if code in seen or not name or name in ('チャート', '時系列', 'ニュース', '掲示板', '企業情報'):
            continue
        # 「5970東証PRM(株)ジーテクト2,091-11」のような連結テキストから社名を抽出
        m2 = re.search(r'(?:\(株\)|（株）)?([^\d(（]+)', name.replace(code, '').replace('東証PRM', '').replace('東証STD', '').replace('東証GRT', ''))
        clean = m2.group(1).strip() if m2 else name
        if clean:
            candidates.append({'code': code, 'name': clean})
            seen.add(code)
    return candidates
