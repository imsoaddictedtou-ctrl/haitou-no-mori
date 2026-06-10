"""
高配当株スクリーナー
====================
screening_config.py の基準に従って銘柄をスコアリングする。
資産管理アプリ（app.py）のスクリーニングタブから呼び出して使う。
"""

import sys
import time
from pathlib import Path

# scraper.py を参照
SCRAPER_PATH = str(Path(__file__).parent.parent / "秘書_コウ")
if SCRAPER_PATH not in sys.path:
    sys.path.insert(0, SCRAPER_PATH)

from scraper import get_results, get_dividend_streak, get_company_info, get_sector, get_dividend_from_yahoo_quote
from screening_config import (
    YIELD_THRESHOLD, SECTOR_TYPE_MAP,
    STREAK_A_YEARS, STREAK_B_YEARS, STREAK_C_YEARS,
    REV_CAGR_FULL, REV_CAGR_HALF,
    EPS_CAGR_FULL, EPS_CAGR_MID, EPS_CAGR_HALF,
    DIV_GROWTH_MIN,
    OP_MARGIN_FULL, OP_MARGIN_MID,
    EQUITY_RATIO_FULL, EQUITY_RATIO_MID,
    PAYOUT_IDEAL_MIN, PAYOUT_IDEAL_MAX, PAYOUT_OK_MAX,
    SCORE_YIELD, SCORE_STREAK_A, SCORE_STREAK_B, SCORE_STREAK_C,
    SCORE_DIV_GROWTH, SCORE_DIV_GROWTH_MID,
    SCORE_REV_CAGR_FULL, SCORE_REV_CAGR_HALF,
    SCORE_EPS_CAGR_FULL, SCORE_EPS_CAGR_MID, SCORE_EPS_CAGR_HALF,
    SCORE_OP_MARGIN_FULL, SCORE_OP_MARGIN_MID,
    SCORE_EQUITY_FULL, SCORE_EQUITY_MID,
    SCORE_PAYOUT_FULL, SCORE_PAYOUT_MID,
    RANK_S, RANK_A, RANK_B,
    COVID_EXCEPTION_YEARS,
)


def _parse(s) -> float | None:
    """文字列を float に変換。兆・億単位（IRBankの売上表記）も億円換算で対応。"""
    if not s or str(s) in ('不明', '-', '－', '*', ''):
        return None
    s = str(s).strip().replace(',', '').replace('▲', '-').replace('－', '-')
    # 兆・億単位を億円に統一（CAGR計算は相対値なので単位統一だけ必要）
    if '兆' in s:
        try:
            return float(s.replace('兆', '')) * 10000   # 1兆 = 10000億
        except ValueError:
            pass
    if '億' in s:
        try:
            return float(s.replace('億', ''))
        except ValueError:
            pass
    try:
        return float(s.replace('%', '').replace('倍', ''))
    except ValueError:
        return None


def _cagr(vals: list) -> float | None:
    clean = [v for v in vals if v is not None and v > 0]
    if len(clean) < 2:
        return None
    n = len(clean) - 1
    try:
        return ((clean[-1] / clean[0]) ** (1 / n) - 1) * 100
    except Exception:
        return None


def get_rank(score: int) -> str:
    if score >= RANK_S:
        return '🏆S'
    if score >= RANK_A:
        return '✅A'
    if score >= RANK_B:
        return '🥈B'
    return '⬇️C'


def score_one(code: str, yield_pct: float | None = None,
              sleep: float = 0.5) -> dict:
    """
    1銘柄をスコアリングして結果dictを返す。

    Parameters
    ----------
    code      : 証券コード（4桁）
    yield_pct : 配当利回り(%)。Noneの場合はIRBankから自動取得。
    sleep     : IRBankへのアクセス間隔（秒）

    Returns
    -------
    dict with keys:
        code, name, sector, sector_type, yield_pct, score, rank,
        streak, covid_exception, growth_rate,
        rev_cagr, eps_cagr, roe_avg, per,
        score_detail (dict of each item's points)
    """
    # ── 基本情報取得 ─────────────────────────────────────
    info        = get_company_info(code)
    sector_info = get_sector(code)
    time.sleep(sleep)

    name        = info.get('company_name', '不明')
    sector      = sector_info.get('sector', '不明')
    sector_type = SECTOR_TYPE_MAP.get(sector, '不明')

    # 利回り（IRBankから取得、「不明」の場合はフォールバック）
    yield_source = 'IRBank'
    if yield_pct is None:
        yield_pct = _parse(info.get('dividend_yield', '')) or 0.0

    if yield_pct == 0.0:
        # 株価を先にパース（フォールバック全体で使う）
        price_str = str(info.get('stock_price', '')).replace('円', '').replace(',', '').strip()
        price_val = _parse(price_str)

        # ── フォールバック①: Yahoo Finance / minkabu「会社予想配当」 ──
        # 会社が公式発表した来期予想配当が取得できるため最優先
        yahoo_div = get_dividend_from_yahoo_quote(code)
        if yahoo_div > 0 and price_val and price_val > 0:
            yield_pct = round(yahoo_div / price_val * 100, 2)
            yield_source = f'Yahoo予想(配当{yahoo_div}円÷株価{price_val:.0f}円)'
            _tmp_streak = get_dividend_streak(code)
        else:
            # ── フォールバック②: IRBank配当ページの直近実績 ÷ 株価 ──
            _tmp_streak = get_dividend_streak(code)
            latest_div = _tmp_streak.get('latest_div')
            if latest_div and price_val and price_val > 0:
                yield_pct = round(latest_div / price_val * 100, 2)
                yield_source = f'IRBank実績(配当{latest_div}円÷株価{price_val:.0f}円)'
            else:
                yield_source = 'データ取得失敗'

        # streak_data を再取得しないよう一時取得結果を再利用
        _pre_fetched_streak = _tmp_streak
    else:
        _pre_fetched_streak = None

    threshold = YIELD_THRESHOLD.get(sector_type, 3.5)

    # ── 連続増配・増配率 ──────────────────────────────────
    # 利回りフォールバック時に先取得済みの場合は再利用
    streak_data  = _pre_fetched_streak if _pre_fetched_streak is not None else get_dividend_streak(code)
    streak       = streak_data['streak']
    covid_exc    = streak_data['covid_exception']
    growth_rate  = streak_data['growth_rate']
    time.sleep(sleep)

    # ── 財務指標 ──────────────────────────────────────────
    try:
        results    = get_results(code)
        rev_vals   = [_parse(r.get('revenue'))    for r in results][-5:]
        eps_vals   = [_parse(r.get('eps'))         for r in results][-5:]
        op_p_vals  = [_parse(r.get('op_profit'))   for r in results][-3:]
        rev3_vals  = [_parse(r.get('revenue'))     for r in results][-3:]
    except Exception:
        rev_vals, eps_vals, op_p_vals, rev3_vals = [], [], [], []
    time.sleep(sleep)

    rev_cagr = _cagr(rev_vals)
    eps_cagr = _cagr(eps_vals)

    # 営業利益率：直近3期の平均（op_profit / revenue × 100）
    op_margins = []
    for op_p, rev in zip(op_p_vals, rev3_vals):
        if op_p is not None and rev is not None and rev > 0:
            op_margins.append(op_p / rev * 100)
    op_margin_avg = sum(op_margins) / len(op_margins) if op_margins else None

    # 自己資本比率：company_info から取得（例: '24.9%'）
    equity_ratio_val = _parse(info.get('equity_ratio', ''))

    # 配当性向：直近実績配当 / EPS × 100
    latest_div_for_payout = streak_data.get('latest_div')
    latest_eps = next(
        (_parse(r.get('eps')) for r in reversed(results) if _parse(r.get('eps'))),
        None
    ) if 'results' in dir() else None
    if latest_div_for_payout and latest_eps and latest_eps > 0:
        payout_ratio = round(latest_div_for_payout / latest_eps * 100, 1)
    else:
        payout_ratio = None

    # ── スコアリング ──────────────────────────────────────
    score  = 0
    detail = {}

    # 利回り（20点）
    if yield_pct >= threshold:
        pts = SCORE_YIELD
    elif yield_pct >= threshold - 0.3:
        pts = SCORE_YIELD // 2
    else:
        pts = 0
    score += pts
    detail['利回り'] = pts

    # 連続増配（15点）
    if streak >= STREAK_A_YEARS:
        pts = SCORE_STREAK_A
    elif covid_exc and streak >= STREAK_B_YEARS:
        pts = SCORE_STREAK_B
    elif streak >= STREAK_C_YEARS:
        pts = SCORE_STREAK_C
    else:
        pts = 0
    score += pts
    detail['連続増配'] = pts

    # 増配率（10点）
    if growth_rate is not None and growth_rate >= DIV_GROWTH_MIN:
        pts = SCORE_DIV_GROWTH
    elif growth_rate is not None and growth_rate > 0:
        pts = SCORE_DIV_GROWTH_MID
    else:
        pts = 0
    score += pts
    detail['増配率'] = pts

    # 売上CAGR（10点）
    if rev_cagr is not None:
        pts = SCORE_REV_CAGR_FULL if rev_cagr >= REV_CAGR_FULL else (SCORE_REV_CAGR_HALF if rev_cagr >= REV_CAGR_HALF else 0)
    else:
        pts = 0
    score += pts
    detail['売上CAGR'] = pts

    # EPS CAGR（15点）
    if eps_cagr is not None:
        if eps_cagr >= EPS_CAGR_FULL:
            pts = SCORE_EPS_CAGR_FULL
        elif eps_cagr >= EPS_CAGR_MID:
            pts = SCORE_EPS_CAGR_MID
        elif eps_cagr >= EPS_CAGR_HALF:
            pts = SCORE_EPS_CAGR_HALF
        else:
            pts = 0
    else:
        pts = 0
    score += pts
    detail['EPS_CAGR'] = pts

    # 営業利益率（10点）
    if op_margin_avg is not None:
        if op_margin_avg >= OP_MARGIN_FULL:
            pts = SCORE_OP_MARGIN_FULL
        elif op_margin_avg >= OP_MARGIN_MID:
            pts = SCORE_OP_MARGIN_MID
        else:
            pts = 0
    else:
        pts = 0
    score += pts
    detail['営業利益率'] = pts

    # 自己資本比率（10点）
    if equity_ratio_val is not None:
        if equity_ratio_val >= EQUITY_RATIO_FULL:
            pts = SCORE_EQUITY_FULL
        elif equity_ratio_val >= EQUITY_RATIO_MID:
            pts = SCORE_EQUITY_MID
        else:
            pts = 0
    else:
        pts = 0
    score += pts
    detail['自己資本比率'] = pts

    # 配当性向（10点）
    if payout_ratio is not None:
        if PAYOUT_IDEAL_MIN <= payout_ratio <= PAYOUT_IDEAL_MAX:
            pts = SCORE_PAYOUT_FULL   # 30〜50%：健全で満点
        elif payout_ratio <= PAYOUT_OK_MAX:
            pts = SCORE_PAYOUT_MID    # 50〜70%：やや高め
        else:
            pts = 0                   # 70%超（無理な配当）or 30%未満（吝嗇）
    else:
        pts = 0
    score += pts
    detail['配当性向'] = pts

    return {
        'code':            code,
        'name':            name,
        'sector':          sector,
        'sector_type':     sector_type,
        'yield_pct':       yield_pct,
        'yield_source':    yield_source,       # 利回りの取得元（IRBank / 計算 / Yahoo）
        'threshold':       threshold,
        'score':           score,
        'rank':            get_rank(score),
        'streak':          streak,
        'covid_exception': covid_exc,
        'growth_rate':     growth_rate,
        'rev_cagr':        rev_cagr,
        'eps_cagr':        eps_cagr,
        'op_margin':       op_margin_avg,      # 営業利益率（直近3期平均%）
        'equity_ratio':    equity_ratio_val,   # 自己資本比率（%）
        'payout_ratio':    payout_ratio,       # 配当性向（%）
        'score_detail':    detail,
    }


def score_list(codes_yields: list[tuple[str, float | None]],
               on_progress=None, sleep: float = 0.5) -> list[dict]:
    """
    複数銘柄をスコアリングしてスコア降順のリストを返す。

    Parameters
    ----------
    codes_yields : [(code, yield_pct), ...]  yield_pctはNoneでも可
    on_progress  : コールバック関数 fn(i, total, code, name, score)
    sleep        : IRBankアクセス間隔

    Returns
    -------
    スコア降順にソートされた result dict のリスト
    """
    results = []
    total   = len(codes_yields)
    for i, (code, yld) in enumerate(codes_yields):
        result = score_one(code, yld, sleep=sleep)
        results.append(result)
        if on_progress:
            on_progress(i + 1, total, code, result['name'], result['score'])
    return sorted(results, key=lambda x: x['score'], reverse=True)
