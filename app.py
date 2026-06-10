"""
高配当株 分析ツール（公開版）
================================
・ポートフォリオ分析
・銘柄スクリーニング（100点スコアリング）
・月次レポート

データはすべてセッション内のみで処理。サーバーには何も保存しない。
"""

import socket
socket.setdefaulttimeout(7)   # 全ソケット操作（DNS含む）に7秒の強制タイムアウト

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sys
import io
import time
import re
import csv as csv_module
import json
from pathlib import Path
from datetime import datetime

# ── パス設定（ローカル実行時は親ディレクトリのモジュールを参照） ──
_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent

# screener.py / screening_config.py は 資産管理/ から参照
_ASSET_PATH = _REPO_ROOT / "資産管理"
if str(_ASSET_PATH) not in sys.path:
    sys.path.insert(0, str(_ASSET_PATH))

# scraper.py は 秘書_コウ/ から参照
_SCRAPER_PATH = _REPO_ROOT / "秘書_コウ"
if str(_SCRAPER_PATH) not in sys.path:
    sys.path.insert(0, str(_SCRAPER_PATH))

from screener import score_list, get_rank
from screening_config import (
    YIELD_THRESHOLD, RANK_S, RANK_A, RANK_B, SECTOR_TYPE_MAP,
    STREAK_A_YEARS, STREAK_B_YEARS, STREAK_C_YEARS,
    REV_CAGR_FULL, REV_CAGR_HALF,
    EPS_CAGR_FULL, EPS_CAGR_MID, EPS_CAGR_HALF,
    DIV_GROWTH_MIN,
    OP_MARGIN_FULL, OP_MARGIN_MID,
    EQUITY_RATIO_FULL, EQUITY_RATIO_MID,
    PAYOUT_IDEAL_MIN, PAYOUT_IDEAL_MAX, PAYOUT_OK_MAX,
)
from scraper import (
    get_dividends_from_results, get_sector,
    get_dividend_from_yahoo_quote, _parse_value,
    get_company_info, search_code,
    get_price_yield_from_kabutan,
    get_category_stocks_kabutan, KABUTAN_INDUSTRY_NUM,
)

# ── セクタータイプ色定義 ──────────────────────────────────
SECTOR_TYPE_COLOR = {
    'ディフェンシブ': '#4C9BE8',
    '景気敏感':       '#E05252',
    '金融':           '#F5A623',
    '中間':           '#4CAF7D',
    '不明':           '#AAAAAA',
}

# ── ページ設定 ────────────────────────────────────────────
from PIL import Image
_icon_path   = _HERE / "icon.png"
_illust_path = _HERE / "kou_illust.png"
_page_icon   = Image.open(_icon_path) if _icon_path.exists() else "💎"

st.set_page_config(
    page_title="配当の森 〜育てる高配当株ダッシュボード〜",
    page_icon=_page_icon,
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
[data-testid="stMetricValue"]  { font-size: 1.7rem !important; font-weight: 700; }
[data-testid="stMetricLabel"]  { font-size: 0.85rem !important; color: #888; }
[data-testid="stMetricDelta"]  { font-size: 0.95rem !important; }
div[data-testid="stFileUploaderDropzone"] { border: 2px dashed #1a6b9e !important; border-radius: 12px !important; }
button[kind="primary"] { background-color: #1a6b9e !important; border-color: #1a6b9e !important; }
button[kind="primary"]:hover { background-color: #155a87 !important; border-color: #155a87 !important; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
#  CSV パーサー（楽天証券 & 独自フォーマット対応）
# ═══════════════════════════════════════════════════════
def _to_float(s: str) -> float:
    if not s:
        return 0.0
    s = str(s).strip().replace('－', '-').replace('▲', '-')
    s = re.sub(r'[,円%株\s]', '', s)
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_rakuten_csv(file) -> pd.DataFrame:
    """楽天証券の保有商品明細CSVを解析してDataFrameを返す"""
    raw = file.read()
    content = None
    for enc in ['shift_jis', 'cp932', 'utf-8-sig', 'utf-8']:
        try:
            content = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if content is None:
        st.error("CSVの文字コードを認識できませんでした")
        return pd.DataFrame()

    lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    def _section_to_account(text: str) -> str:
        """セクション見出し（●NISA成長投資枠 など）を口座名に変換"""
        if '成長投資枠' in text:
            return 'NISA成長'
        if 'つみたて' in text or '積立' in text:
            return 'NISAつみたて'
        if 'NISA' in text.upper():
            return 'NISA'
        if '特定' in text:
            return '特定'
        if '一般' in text:
            return '一般'
        return '不明'

    # 楽天CSVは「●NISA成長投資枠」のようなセクション見出しの下に
    # ヘッダー＋銘柄行が続く構造。セクションごとに口座区分を付与する。
    headers = None
    current_account = '不明'
    rows = []

    for line in lines:
        if not line.strip():
            continue

        # セクション見出し行（■や●で始まる行。楽天CSVは「■NISA成長投資枠」形式）
        stripped = line.strip().strip('"')
        if stripped.startswith(('■', '●', '【')):
            detected = _section_to_account(stripped)
            if detected != '不明':   # 「■現在の評価額合計」等の見出しでは口座を変えない
                current_account = detected
            continue

        # ヘッダー行（セクションごとに繰り返し出現する）
        if '銘柄コード' in line:
            try:
                headers = [h.strip() for h in next(csv_module.reader([line]))]
            except Exception:
                headers = line.split(',')
            continue

        if headers is None:
            continue

        try:
            cols = next(csv_module.reader([line]))
        except Exception:
            cols = line.split(',')

        if len(cols) < 5:
            continue

        row = {headers[j]: cols[j].strip() for j in range(min(len(headers), len(cols)))}

        raw_code = (
            row.get('銘柄コード・ティッカー') or
            row.get('銘柄コード') or
            cols[0]
        ).strip().strip('"')
        code = raw_code.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        if re.fullmatch(r'\d{4}', code):
            row['_code'] = code
            row['_account'] = current_account
            rows.append(row)

    if headers is None:
        st.error("CSVの形式が認識できませんでした。楽天証券のCSVか確認してください。")
        return pd.DataFrame()

    if not rows:
        st.error("国内株式（4桁コード）のデータが見つかりませんでした。")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df['code']    = df['_code']
    df['account'] = df['_account']   # セクション見出しから判定した口座区分

    def find_col(df_cols: list, keywords: list) -> str | None:
        for kw in keywords:
            for col in df_cols:
                if kw in col:
                    return col
        return None

    df_cols = list(df.columns)
    mapping = {
        'name':          find_col(df_cols, ['銘柄名', '銘柄']),
        'shares':        find_col(df_cols, ['保有数量', '保有株数']),
        'cost_price':    find_col(df_cols, ['平均取得価額', '平均取得単価']),
        'current_price': find_col(df_cols, ['現在値']),
        'market_value':  find_col(df_cols, ['時価評価額']),
        'profit':        find_col(df_cols, ['評価損益']),
        'purchase_raw':  find_col(df_cols, ['取得総額', '購入額']),
    }

    for dst, src in mapping.items():
        if src and dst not in df.columns:
            df[dst] = df[src]

    for col in ['name', 'account', 'shares', 'cost_price', 'current_price',
                'market_value', 'profit', 'profit_pct']:
        if col not in df.columns:
            df[col] = '0'

    for col in ['shares', 'cost_price', 'current_price', 'market_value', 'profit']:
        df[col] = df[col].apply(_to_float)

    if 'purchase_raw' in df.columns:
        df['purchase_value'] = df['purchase_raw'].apply(_to_float)
        mask = df['purchase_value'] == 0
        df.loc[mask, 'purchase_value'] = df.loc[mask, 'shares'] * df.loc[mask, 'cost_price']
    else:
        df['purchase_value'] = df['shares'] * df['cost_price']

    if 'account' not in df.columns or df['account'].eq('0').all():
        df['account'] = '不明'

    keep = ['code', 'name', 'account', 'shares', 'cost_price', 'purchase_value',
            'current_price', 'market_value', 'profit', 'profit_pct']
    return df[keep].copy()


def parse_simple_csv(file) -> pd.DataFrame:
    """独自保存フォーマット（コード,銘柄名,口座,保有数,取得単価,...）を読み込む"""
    raw = file.read()
    content = None
    for enc in ['utf-8-sig', 'utf-8', 'shift_jis', 'cp932']:
        try:
            content = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if content is None:
        return pd.DataFrame()

    try:
        df = pd.read_csv(io.StringIO(content))
        # コード列が4桁数字かチェック
        if 'code' in df.columns:
            df['code'] = df['code'].astype(str).str.strip()
            df = df[df['code'].str.fullmatch(r'\d{4}')]
            if not df.empty:
                return df
    except Exception:
        pass

    return pd.DataFrame()


# ═══════════════════════════════════════════════════════
#  IRBank データ取得（セッションキャッシュのみ・ファイル保存なし）
# ═══════════════════════════════════════════════════════
def fetch_stock_info(code: str, force: bool = False) -> dict:
    """IRBankから配当金単価・業種を取得。セッションキャッシュのみ使用。
    socket.setdefaulttimeout(7) により全ソケット操作に7秒の上限がある。
    """
    key      = f'_info_{code}'
    fallback = {'sector': '不明', 'sector_type': '不明', 'dividend_per_share': 0.0}

    if not force and key in st.session_state:
        return st.session_state[key]

    try:
        time.sleep(0.3)
        sector_info = get_sector(code)
        time.sleep(0.3)
        divs = get_dividends_from_results(code)
        dividend_per_share = 0.0
        if divs:
            for d in reversed(divs):
                val = _parse_value(d.get('total', ''))
                if val is not None and val > 0:
                    dividend_per_share = float(val)
                    break

        # フォールバック①: Yahoo Finance / minkabu
        if dividend_per_share == 0.0:
            time.sleep(0.3)
            dividend_per_share = get_dividend_from_yahoo_quote(code)

        # フォールバック②: 株探（株価×利回りで逆算）
        # IRBank/Yahooがクラウド環境からブロックされる場合もkabutanは通る
        if dividend_per_share == 0.0:
            time.sleep(0.3)
            kabutan = get_price_yield_from_kabutan(code)
            dividend_per_share = kabutan['dividend']

        info = {
            'sector':             sector_info.get('sector', '不明'),
            'sector_type':        sector_info.get('sector_type', '不明'),
            'dividend_per_share': dividend_per_share,
        }
    except Exception:
        info = fallback

    st.session_state[key] = info
    return info


# ═══════════════════════════════════════════════════════
#  ポートフォリオ構築（配当・業種をIRBankから付与）
# ═══════════════════════════════════════════════════════
def build_portfolio(df: pd.DataFrame, progress_bar=None) -> pd.DataFrame:
    total = len(df)
    sectors, sector_types, dividends = [], [], []

    for i, (_, row) in enumerate(df.iterrows()):
        code = row['code']
        if progress_bar:
            progress_bar.progress(i / total, text=f"{code} 取得中… ({i+1}/{total})")
        info = fetch_stock_info(code)
        sectors.append(info['sector'])
        sector_types.append(info['sector_type'])
        dividends.append(info['dividend_per_share'])

    df = df.copy()
    df['sector']             = sectors
    df['sector_type']        = sector_types
    df['dividend_per_share'] = dividends
    df['annual_dividend']    = df['dividend_per_share'] * df['shares']
    df['yield_current']      = df.apply(
        lambda r: r['annual_dividend'] / r['market_value'] * 100 if r['market_value'] > 0 else 0, axis=1)
    df['yield_cost']         = df.apply(
        lambda r: r['annual_dividend'] / r['purchase_value'] * 100 if r['purchase_value'] > 0 else 0, axis=1)
    return df


# ═══════════════════════════════════════════════════════
#  高配当戦略アドバイス
# ═══════════════════════════════════════════════════════
def _render_advice(df: pd.DataFrame, avg_yield: float) -> None:
    total_market  = df['market_value'].sum()
    advisories    = []

    type_pct = (
        df.groupby('sector_type')['market_value'].sum() / total_market * 100
        if total_market else pd.Series(dtype=float)
    )
    def pct(label): return type_pct.get(label, 0.0)

    defensive_pct = pct('ディフェンシブ')
    cyclical_pct  = pct('景気敏感')

    high_yield_stocks = df[df['yield_current'] > 5.0][['name', 'yield_current']]
    sector_counts     = df.groupby('sector')['code'].count()
    dup_sectors       = sector_counts[sector_counts >= 2].index.tolist()
    dup_sectors       = [s for s in dup_sectors if s not in ('不明', '')]
    no_div            = df[df['dividend_per_share'] == 0][['name', 'code']]

    if defensive_pct < 30:
        advisories.append(("warning",
            f"🔴 ディフェンシブ比率が低め（{defensive_pct:.1f}%）",
            "高配当戦略の安定性を高めるには、ディフェンシブ銘柄（電気・ガス、通信、食料品、医薬品など）を"
            "**40%以上**に引き上げるのが理想です。景気後退期でも配当が維持されやすくなります。"
        ))
    elif defensive_pct >= 40:
        advisories.append(("success",
            f"✅ ディフェンシブ比率が良好（{defensive_pct:.1f}%）",
            "景気変動に強いポートフォリオ構成です。この比率を維持しましょう。"
        ))

    if cyclical_pct > 35:
        advisories.append(("warning",
            f"⚠️ 景気敏感セクターが多め（{cyclical_pct:.1f}%）",
            "景気敏感銘柄は好景気では恩恵を受けますが、**不況期に減配リスク**が高まります。"
            "化学・鉱業・不動産など同じ景気敏感内でも分散できているか確認しましょう。"
        ))

    if not high_yield_stocks.empty:
        names = "・".join(high_yield_stocks['name'].str[:8].tolist())
        advisories.append(("warning",
            f"⚠️ 利回り5%超の銘柄に注意（{len(high_yield_stocks)}銘柄）",
            f"**{names}** が時価利回り5%を超えています。\n\n"
            "高利回りは魅力ですが、「株価下落で利回りが上昇しているだけ」や"
            "「減配リスクを市場が織り込んでいる」可能性もあります。"
            "IRBankで配当の継続性・業績トレンドを必ず確認してください。"
        ))

    if dup_sectors:
        advisories.append(("info",
            f"📌 同業種の銘柄が重複しています（{len(dup_sectors)}業種）",
            f"**{' / '.join(dup_sectors)}** に2銘柄以上保有しています。\n\n"
            "同業種は景気・規制・原材料価格の影響を同時に受けるため、"
            "リスクが集中しやすくなります。代わりに未保有業種への分散も検討してみましょう。"
        ))

    if not no_div.empty:
        names = "・".join(no_div['name'].str[:8].tolist())
        advisories.append(("info",
            f"📌 配当データが未取得の銘柄（{len(no_div)}銘柄）",
            f"**{names}** の配当データをIRBankから取得できていません。\n\n"
            "「🔄 配当・業種データを再取得」ボタンを押して再試行するか、IRBankで手動確認してください。"
        ))

    if avg_yield >= 3.5:
        advisories.append(("success",
            f"✅ 平均配当利回りが目標水準（{avg_yield:.2f}%）",
            "一般的な高配当ポートフォリオの目安である **3.5%以上** を達成しています。"
        ))
    elif avg_yield > 0:
        advisories.append(("info",
            f"💬 平均配当利回り {avg_yield:.2f}%（目標：3.5%以上）",
            "高配当ポートフォリオの目安は **利回り3.5〜5%** です。"
        ))

    COLOR  = {"warning": "#fff3cd", "success": "#d4edda", "info": "#d1ecf1"}
    BORDER = {"warning": "#ffc107", "success": "#28a745", "info": "#17a2b8"}

    for kind, title, body in advisories:
        bg = COLOR.get(kind, "#f8f9fa")
        border = BORDER.get(kind, "#6c757d")
        st.markdown(
            f'<div style="background:{bg}; border-left:5px solid {border}; '
            f'border-radius:6px; padding:14px 18px; margin-bottom:12px;">'
            f'<strong>{title}</strong></div>',
            unsafe_allow_html=True,
        )
        st.markdown(body)
        st.markdown("")


# ═══════════════════════════════════════════════════════
#  ポートフォリオタブ
# ═══════════════════════════════════════════════════════
def render_portfolio_tab() -> None:

    df: pd.DataFrame | None = st.session_state.get('portfolio_df')

    # ── アップロードエリア（データがない or 入れ替えたいとき） ──
    with st.expander("📂 保有銘柄データの入力・読み込み", expanded=(df is None)):

        upload_col, hint_col = st.columns([2, 3])

        with upload_col:
            uploaded = st.file_uploader(
                "楽天証券CSVまたは保存済みCSVをアップロード",
                type=['csv'],
                key="portfolio_upload",
            )

        with hint_col:
            st.markdown("""
**対応フォーマット**
- **楽天証券** › 国内株式 › 保有商品明細ダウンロード（Shift-JIS CSV）
- **このアプリで保存したCSV**（下の「💾 保存」ボタンでダウンロードしたもの）

📌 アップロードしたデータはこのブラウザセッション内のみで処理され、サーバーには保存されません。
            """)

        if uploaded is not None:
            # ファイルIDで二重処理を防止（st.rerunで同じファイルが再処理されるのを防ぐ）
            file_id = f"{uploaded.name}_{uploaded.size}"
            if st.session_state.get('_last_file_id') != file_id:
                st.session_state['_last_file_id'] = file_id

                uploaded.seek(0)
                raw_peek = uploaded.read(200)
                uploaded.seek(0)
                is_rakuten = '銘柄コード' in (raw_peek.decode('shift_jis', errors='ignore') +
                                               raw_peek.decode('utf-8', errors='ignore'))

                if is_rakuten:
                    base_df = parse_rakuten_csv(uploaded)
                else:
                    base_df = parse_simple_csv(uploaded)

                if base_df.empty:
                    st.error("データの読み込みに失敗しました。フォーマットを確認してください。")
                else:
                    progress = st.progress(0, text="配当・業種データを取得中…")
                    built_df = build_portfolio(base_df, progress_bar=progress)
                    progress.progress(1.0, text="取得完了！")
                    st.session_state['portfolio_df'] = built_df
                    df = built_df   # 同一スクリプト実行内でそのまま表示へ進む
                    st.success(f"✅ {len(df)}銘柄のデータを読み込みました")
            else:
                st.info("このファイルは読み込み済みです。再読み込みするには「🗑️ データをクリア」してください。")

    # ── データがなければ終了 ──────────────────────────────
    if df is None or df.empty:
        st.markdown(
            '<div class="upload-hint">⬆️ CSVをアップロードするとポートフォリオが表示されます</div>',
            unsafe_allow_html=True,
        )
        return

    # ── エクスポート（保存）ボタン ────────────────────────
    export_cols = ['code', 'name', 'account', 'shares', 'cost_price',
                   'purchase_value', 'current_price', 'market_value', 'profit']
    export_df = df[[c for c in export_cols if c in df.columns]].copy()
    csv_bytes = export_df.to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')

    col_save, col_reset, _ = st.columns([2, 2, 6])
    with col_save:
        st.download_button(
            "💾 保有データを保存（CSV）",
            data=csv_bytes,
            file_name=f"portfolio_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            help="次回アップロードすればデータを復元できます",
        )
    with col_reset:
        if st.button("🗑️ データをクリア"):
            del st.session_state['portfolio_df']
            for k in [k for k in st.session_state if k.startswith('_info_')]:
                del st.session_state[k]
            st.rerun()

    st.divider()

    # ── KPI ──────────────────────────────────────────────
    total_market   = df['market_value'].sum()
    total_purchase = df['purchase_value'].sum()
    total_profit   = total_market - total_purchase
    profit_pct     = total_profit / total_purchase * 100 if total_purchase else 0
    total_dividend = df['annual_dividend'].sum()
    avg_yield      = total_dividend / total_market * 100 if total_market else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("保有資産額",       f"¥{total_market:,.0f}")
    c2.metric("評価損益",         f"¥{total_profit:+,.0f}", f"{profit_pct:+.2f}%")
    c3.metric("銘柄数",           f"{len(df)}銘柄")
    c4.metric("配当利回り（時価）", f"{avg_yield:.2f}%")
    c5.metric("年間配当金（予）",  f"¥{total_dividend:,.0f}")

    st.divider()

    # ── グラフ ────────────────────────────────────────────
    col_l, col_m, col_r = st.columns(3)

    with col_l:
        sec_df = df[df['annual_dividend'] > 0].groupby('sector')['annual_dividend'].sum().reset_index()
        if not sec_df.empty:
            fig = px.pie(sec_df, values='annual_dividend', names='sector',
                         title='業種別 配当金割合', hole=0.42,
                         color_discrete_sequence=px.colors.qualitative.Set3)
            fig.update_traces(textposition='inside', textinfo='percent+label')
            fig.update_layout(showlegend=False, margin=dict(t=50, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("配当データが取得できませんでした")

    with col_m:
        type_df = df.groupby('sector_type')['market_value'].sum().reset_index()
        type_df.columns = ['タイプ', '評価額']
        type_df = type_df[type_df['評価額'] > 0]
        if not type_df.empty:
            fig = px.pie(type_df, values='評価額', names='タイプ',
                         title='景気タイプ別 割合（評価額）', hole=0.42,
                         color='タイプ', color_discrete_map=SECTOR_TYPE_COLOR)
            fig.update_traces(textposition='inside', textinfo='percent+label')
            fig.update_layout(showlegend=False, margin=dict(t=50, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

    with col_r:
        top = df.nlargest(10, 'annual_dividend')
        top = top[top['annual_dividend'] > 0]
        if not top.empty:
            top = top.copy()
            top['label'] = top['code'] + ' ' + top['name'].str[:10]
            fig = go.Figure(go.Bar(
                x=top['annual_dividend'], y=top['label'],
                orientation='h', marker_color='#1a6b9e',
                text=top['annual_dividend'].apply(lambda v: f'¥{v:,.0f}'),
                textposition='inside', insidetextanchor='end',
                textfont=dict(color='white', size=12),
            ))
            fig.update_layout(
                title='配当金 Top10',
                xaxis=dict(title='年間配当金（円）', range=[0, top['annual_dividend'].max() * 1.05]),
                yaxis={'autorange': 'reversed'},
                height=360, margin=dict(t=50, b=20, l=140, r=20),
            )
            st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ── 保有銘柄一覧テーブル ──────────────────────────────
    st.subheader("📋 保有銘柄一覧")
    disp = df[['code', 'name', 'sector', 'account', 'shares',
               'cost_price', 'purchase_value', 'current_price', 'market_value',
               'profit', 'dividend_per_share', 'annual_dividend',
               'yield_current', 'yield_cost']].copy()
    disp.columns = ['コード', '銘柄名', '業種', '口座', '保有数',
                    '取得単価', '購入額', '株価', '評価額',
                    '損益（円）', '配当金単価', '年間配当金',
                    '利回り(時価)%', '利回り(簿価)%']
    disp['保有数']     = disp['保有数'].apply(lambda v: f'{int(v):,}')
    disp['取得単価']   = disp['取得単価'].apply(lambda v: f'{v:,.1f}')
    disp['株価']       = disp['株価'].apply(lambda v: f'{v:,.1f}')
    disp['配当金単価'] = disp['配当金単価'].apply(lambda v: f'{v:,.1f}')
    for col in ['購入額', '評価額', '年間配当金']:
        disp[col] = disp[col].apply(lambda v: f'{v:,.0f}')
    disp['損益（円）'] = disp['損益（円）'].apply(lambda v: f'{v:+,.0f}')
    for col in ['利回り(時価)%', '利回り(簿価)%']:
        disp[col] = disp[col].apply(lambda v: f'{v:.2f}%')
    st.dataframe(disp, use_container_width=True, hide_index=True)

    # ── 業種タイプ凡例 ────────────────────────────────────
    with st.expander("📖 景気タイプ 業種分類一覧"):
        l1, l2, l3, l4 = st.columns(4)
        with l1:
            st.markdown("🔵 **ディフェンシブ**")
            st.caption("景気の影響を受けにくい安定需要業種")
            st.markdown("食料品 / 医薬品 / 電気・ガス業 / 陸運業 / 通信業 / 水産・農林業 / 小売業 / 医療・福祉")
        with l2:
            st.markdown("🔴 **景気敏感**")
            st.caption("景気拡大期に恩恵を受けやすい業種")
            st.markdown("輸送用機器 / 鉄鋼 / 化学 / 機械 / 電気機器 / 非鉄金属 / 鉱業 / 海運業 / 空運業 / 建設業 / 不動産業など")
        with l3:
            st.markdown("🟡 **金融**")
            st.caption("金利・景気双方の影響を受ける業種")
            st.markdown("銀行業 / 証券業 / 保険業 / その他金融業")
        with l4:
            st.markdown("🟢 **中間**")
            st.caption("ディフェンシブと景気敏感の中間的性質")
            st.markdown("卸売業 / 情報・通信業 / サービス業 / その他製品 / 精密機器 / 倉庫・運輸関連業")

    # ── アドバイス ────────────────────────────────────────
    st.divider()
    st.subheader("💡 高配当戦略アドバイス")
    _render_advice(df, avg_yield)

    # ── データ再取得 ──────────────────────────────────────
    st.divider()
    if st.button("🔄 配当・業種データを再取得（キャッシュクリア）"):
        for k in [k for k in st.session_state if k.startswith('_info_')]:
            del st.session_state[k]
        st.rerun()


# ═══════════════════════════════════════════════════════
#  カスタム配点（5点刻み・合計100点）
# ═══════════════════════════════════════════════════════
_DEFAULT_WEIGHTS = {
    '配当利回り':   20,
    '連続増配':     15,
    'EPS成長':      15,
    '営業利益率':   10,
    '自己資本比率': 10,
    '配当性向':     10,
    '売上CAGR':     10,
    '増配率':       10,
}


def _load_weights_from_url() -> None:
    """URLクエリパラメータ（?w=20,15,...）から配点を復元する。
    ブックマークしておけば次回アクセス時も同じ配点で使える（サーバー保存なし）。
    """
    if 'score_weights' in st.session_state:
        return
    qp = st.query_params.get('w', '')
    try:
        vals = [int(x) for x in qp.split(',')]
        if (len(vals) == len(_DEFAULT_WEIGHTS)
                and sum(vals) == 100
                and all(v >= 0 and v % 5 == 0 for v in vals)):
            st.session_state['score_weights'] = dict(zip(_DEFAULT_WEIGHTS.keys(), vals))
    except (ValueError, AttributeError):
        pass


def _sync_weights_to_url(w: dict) -> None:
    """現在の配点をURLクエリパラメータに反映する（ブックマーク用）"""
    vals = ','.join(str(w[k]) for k in _DEFAULT_WEIGHTS.keys())
    if st.query_params.get('w', '') != vals:
        st.query_params['w'] = vals


def get_weights() -> dict:
    """現在有効な配点を返す（未設定ならデフォルト）"""
    return st.session_state.get('score_weights', dict(_DEFAULT_WEIGHTS))


def recompute_results(results: list[dict], w: dict) -> list[dict]:
    """スキャン結果の生データから、カスタム配点でスコア・ランクを再計算する。
    各項目の部分点はデフォルト配点と同じ比率で配分する。
    """
    out = []
    for r in results:
        score, detail = 0, {}

        # 配当利回り（満点 / 閾値-0.3%以内で半分）
        wt  = w['配当利回り']
        y   = r.get('yield_pct', 0) or 0
        th  = r.get('threshold', 3.5)
        pts = wt if y >= th else (round(wt / 2) if y >= th - 0.3 else 0)
        score += pts; detail['利回り'] = pts

        # 連続増配（10年:満点 / コロナ例外7年:8/15 / 5年:1/3）
        wt     = w['連続増配']
        streak = r.get('streak', 0) or 0
        covid  = r.get('covid_exception', False)
        if streak >= STREAK_A_YEARS:
            pts = wt
        elif covid and streak >= STREAK_B_YEARS:
            pts = round(wt * 8 / 15)
        elif streak >= STREAK_C_YEARS:
            pts = round(wt / 3)
        else:
            pts = 0
        score += pts; detail['連続増配'] = pts

        # EPS成長CAGR（満点 / 2/3 / 1/3）
        wt  = w['EPS成長']
        eps = r.get('eps_cagr')
        if eps is not None:
            if eps >= EPS_CAGR_FULL:
                pts = wt
            elif eps >= EPS_CAGR_MID:
                pts = round(wt * 2 / 3)
            elif eps >= EPS_CAGR_HALF:
                pts = round(wt / 3)
            else:
                pts = 0
        else:
            pts = 0
        score += pts; detail['EPS_CAGR'] = pts

        # 営業利益率（満点 / 半分）
        wt = w['営業利益率']
        om = r.get('op_margin')
        pts = (wt if om >= OP_MARGIN_FULL else (round(wt / 2) if om >= OP_MARGIN_MID else 0)) if om is not None else 0
        score += pts; detail['営業利益率'] = pts

        # 自己資本比率（満点 / 半分）
        wt = w['自己資本比率']
        eq = r.get('equity_ratio')
        pts = (wt if eq >= EQUITY_RATIO_FULL else (round(wt / 2) if eq >= EQUITY_RATIO_MID else 0)) if eq is not None else 0
        score += pts; detail['自己資本比率'] = pts

        # 配当性向（30〜50%:満点 / 〜70%:半分）
        wt = w['配当性向']
        po = r.get('payout_ratio')
        if po is not None:
            if PAYOUT_IDEAL_MIN <= po <= PAYOUT_IDEAL_MAX:
                pts = wt
            elif po <= PAYOUT_OK_MAX:
                pts = round(wt / 2)
            else:
                pts = 0
        else:
            pts = 0
        score += pts; detail['配当性向'] = pts

        # 売上CAGR（満点 / 半分）
        wt  = w['売上CAGR']
        rev = r.get('rev_cagr')
        pts = (wt if rev >= REV_CAGR_FULL else (round(wt / 2) if rev >= REV_CAGR_HALF else 0)) if rev is not None else 0
        score += pts; detail['売上CAGR'] = pts

        # 増配率（満点 / 半分）
        wt = w['増配率']
        gr = r.get('growth_rate')
        pts = (wt if gr >= DIV_GROWTH_MIN else (round(wt / 2) if gr > 0 else 0)) if gr is not None else 0
        score += pts; detail['増配率'] = pts

        new_r = dict(r)
        new_r['score']        = score
        new_r['rank']         = get_rank(score)
        new_r['score_detail'] = detail
        out.append(new_r)

    return sorted(out, key=lambda x: x['score'], reverse=True)


# ═══════════════════════════════════════════════════════
#  スクリーニングタブ
# ═══════════════════════════════════════════════════════
_DEFAULT_WATCHLIST = ""   # 初期状態は空欄（各自で銘柄を追加してもらう）


def render_screening_tab() -> None:

    # ── スコア定義・配点カスタマイズ ──────────────────────
    _load_weights_from_url()   # URLに配点があれば復元（初回のみ）

    with st.expander("📖 スコア定義・配点カスタマイズ", expanded=False):
        cur_w = get_weights()

        st.markdown("**合計100点満点。80点以上🏆S・65点以上✅A・50点以上🥈B・50点未満⬇️C**")
        st.markdown("⚙️ **配点は5点刻みで自由に変更できます**（合計100点になるように調整してください）")

        # 配点入力（4列×2行）
        items = list(_DEFAULT_WEIGHTS.keys())
        new_w = {}
        for row_start in (0, 4):
            cols = st.columns(4)
            for i, item in enumerate(items[row_start:row_start + 4]):
                with cols[i]:
                    new_w[item] = st.number_input(
                        item, min_value=0, max_value=50, step=5,
                        value=int(cur_w.get(item, _DEFAULT_WEIGHTS[item])),
                        key=f"w_{item}",
                    )

        total = sum(new_w.values())
        col_total, col_reset = st.columns([3, 2])
        with col_total:
            if total == 100:
                st.success(f"✅ 合計 {total}点 — この配点でスコア計算します")
                st.session_state['score_weights'] = new_w
                _sync_weights_to_url(new_w)   # URLに記録（ブックマークで次回復元）
            else:
                st.error(f"⚠️ 合計 {total}点（100点になるよう調整してください。それまで前回の有効な配点を使います）")
        with col_reset:
            if st.button("↩️ デフォルトに戻す", key="reset_weights"):
                st.session_state['score_weights'] = dict(_DEFAULT_WEIGHTS)
                for item in items:
                    st.session_state.pop(f"w_{item}", None)
                st.query_params.pop('w', None)
                st.rerun()

        st.caption("🔖 配点を変えるとURLに反映されます。**そのURLをブックマーク**しておけば、次回アクセス時も同じ配点で使えます（サーバーには保存されません）")

        # 採点基準表（現在の配点で動的表示）
        w = get_weights()
        st.markdown(f"""
| 項目 | 配点 | 基準 |
|---|---|---|
| **配当利回り** | {w['配当利回り']}点 | セクタータイプの閾値以上で満点、▲0.3%以内で半分 |
| **連続増配** | {w['連続増配']}点 | 10年以上で満点 / コロナ例外込み7年以上で約半分 / 5年以上で1/3 |
| **EPS成長CAGR** | {w['EPS成長']}点 | 年平均5%以上で満点 / 2%以上で2/3 / プラスなら1/3 |
| **営業利益率** | {w['営業利益率']}点 | 直近3期平均が10%以上で満点 / 5%以上で半分 |
| **自己資本比率** | {w['自己資本比率']}点 | 60%以上で満点 / 40%以上で半分 |
| **配当性向** | {w['配当性向']}点 | 30〜50%（健全）で満点 / 50〜70%で半分 / 30%未満・70%超は0点 |
| **売上CAGR** | {w['売上CAGR']}点 | 年平均3%以上で満点 / プラスなら半分 |
| **増配率(CAGR)** | {w['増配率']}点 | 年平均3%以上で満点 / プラスなら半分 |

**配当利回りの閾値（セクタータイプ別）**

| タイプ | 閾値 |
|---|---|
| ディフェンシブ（食料品・医薬品・電力・通信など） | 3.5% |
| 景気敏感（化学・建設・機械・不動産など） | 3.8% |
| 金融（銀行・保険・証券） | 3.5% |
| 中間（卸売・情報通信・サービスなど） | 3.5% |

> 配点を変えると、スキャン済みの結果も**再スキャンなしで**新しい配点で並び替わります。
> ⚠️ の利回りはIRBankで取得できず、Yahoo Finance予想配当÷株価で補完した値です。購入前に実際の利回りを確認してください。
        """)

        # 各指標のやさしい解説
        st.markdown("---")
        st.markdown("#### 📚 各指標の意味（やさしい解説）")
        st.markdown("""
**💰 配当利回り**
株価に対して年間いくら配当がもらえるかの割合。「100万円分買ったら年3.5万円もらえる」なら3.5%。
高いほどお得に見えるけど、高すぎる（6%超など）は減配の前兆のこともあるので注意。

**📈 連続増配**
配当を何年連続で増やし続けているか。長いほど「株主への還元を大事にする会社」の証拠。
途中でコロナ禍（2020〜21年）だけ減配した会社は例外として救済してます。

**🚀 EPS成長CAGR**
EPS＝1株あたり利益。会社が「1株あたりいくら稼いだか」。CAGRは年平均の成長率。
これが伸びてる会社は、将来の増配の原資（稼ぐ力）が育ってるってこと。

**🏭 営業利益率**
売上のうち、本業の儲けが何%残るか。「100円売って10円儲かる」なら10%。
高いほど「強いビジネス」。価格競争に巻き込まれにくい会社は利益率が高い傾向。

**🏦 自己資本比率**
会社の全財産のうち、借金じゃない自前のお金の割合。簿記でいう「純資産÷総資産」。
高いほど倒産しにくい。不況が来ても配当を維持する体力がある。
※銀行・保険は業態的に低くて普通なので、低くても気にしすぎないでOK。

**⚖️ 配当性向**
利益のうち何%を配当に回しているか。「100円稼いで40円配当」なら40%。
30〜50%が健全ゾーン。**70%超は要注意**——利益のほとんどを配当に回してて、業績が少し悪化しただけで減配リスクが高まる。低すぎ（30%未満）は株主還元に消極的。

**📊 売上CAGR**
売上高の年平均成長率。会社の事業そのものが大きくなってるかどうか。
利益は一時的な操作ができるけど、売上はごまかしにくいので「素の成長力」が見える。

**📈 増配率(CAGR)**
配当金そのものの年平均成長率。年3%以上増配が続くと、10年で配当は約1.3倍に。
「今の利回り」より「将来の利回り」を重視する人はここを重く配点するのがおすすめ。
        """)

    # ── ウォッチリスト編集 ────────────────────────────────
    # 状態は widget key 'watchlist_input' に一本化。
    # 検索・自動候補からの追加は '_pending_watchlist' 経由で
    # 次回実行の widget 生成前に反映する（Streamlitの制約回避）。
    if '_pending_watchlist' in st.session_state:
        st.session_state['watchlist_input'] = st.session_state.pop('_pending_watchlist')
    if 'watchlist_input' not in st.session_state:
        st.session_state['watchlist_input'] = _DEFAULT_WATCHLIST

    with st.expander("📋 ウォッチリスト編集（証券コードをカンマ・改行区切りで入力）", expanded=False):

        codes_text = st.text_area(
            "証券コード一覧（4桁）",
            height=140,
            key="watchlist_input",
            placeholder="例：7203,8306,9432\n気になる銘柄の証券コードを入力してください",
            help="カンマ・改行・スペース区切りで入力。編集内容は即座にスキャン対象に反映されます",
        )

        # 銘柄検索して追加
        st.markdown("**🔍 銘柄を検索してウォッチリストに追加**")
        sc1, sc2 = st.columns([3, 1])
        with sc1:
            search_query = st.text_input("銘柄コード or 会社名", key="search_query_pub",
                                         placeholder="例：8591 または オリックス")
        with sc2:
            st.markdown("<br>", unsafe_allow_html=True)
            do_search = st.button("🔍 検索", key="do_search_pub")

        if do_search and search_query.strip():
            q = search_query.strip()
            with st.spinner("検索中…"):
                if re.fullmatch(r'\d{4}', q):
                    info = get_company_info(q)
                    found = [{'code': q, 'name': info.get('company_name', '不明')}]
                else:
                    found = search_code(q)
            st.session_state['search_results_pub'] = found or []
            if not found:
                st.warning("銘柄が見つかりませんでした")

        # 検索結果はセッションに保持（追加ボタンのrerunで消えないように）
        for item in st.session_state.get('search_results_pub', [])[:5]:
            c, n = item['code'], item['name']
            existing = re.findall(r'\d{4}', codes_text)
            if c not in existing:
                if st.button(f"➕ {c} {n[:20]} をウォッチリストに追加", key=f"add_search_{c}"):
                    st.session_state['_pending_watchlist'] = codes_text.rstrip() + f"\n{c}"
                    st.session_state['search_results_pub'] = []
                    st.rerun()
            else:
                st.caption(f"✅ {c} {n[:20]} は既に登録済み")

        st.download_button(
            "📥 ウォッチリストをダウンロード",
            data=codes_text.encode('utf-8'),
            file_name="watchlist.txt",
            mime="text/plain",
            help="次回このテキストを貼り付ければウォッチリストを復元できます",
        )

    # ── ポートフォリオバランスから候補を自動追加 ─────────────
    portfolio_df: pd.DataFrame | None = st.session_state.get('portfolio_df')

    with st.expander("🤖 ポートフォリオバランスから候補を自動追加", expanded=False):
        if portfolio_df is None or portfolio_df.empty:
            st.info("ポートフォリオタブでCSVを読み込むと、不足セクターから自動で候補を提示します")
        else:
            # セクタータイプ別配当比率
            if 'sector_type' in portfolio_df.columns and 'annual_dividend' in portfolio_df.columns:
                type_div   = portfolio_df[portfolio_df['annual_dividend'] > 0].groupby('sector_type')['annual_dividend'].sum()
                total_div  = type_div.sum()
                type_ratio = (type_div / total_div * 100).to_dict() if total_div > 0 else {}
            else:
                type_ratio = {}

            all_types = ['ディフェンシブ', '景気敏感', '金融', '中間']
            for t in all_types:
                if t not in type_ratio:
                    type_ratio[t] = 0.0

            ideal = {'ディフェンシブ': 35.0, '景気敏感': 25.0, '金融': 20.0, '中間': 20.0}

            st.markdown("**現在のセクターバランス**")
            cols_t = st.columns(4)
            for i, t in enumerate(all_types):
                cur  = type_ratio.get(t, 0)
                idl  = ideal.get(t, 25)
                diff = cur - idl
                color = SECTOR_TYPE_COLOR.get(t, '#888')
                cols_t[i].markdown(
                    f"<div style='border-left:4px solid {color};padding:6px 10px;'>"
                    f"<b style='color:{color}'>{t}</b><br>"
                    f"<span style='font-size:1.3em;font-weight:bold'>{cur:.0f}%</span>"
                    f"<span style='font-size:0.85em;color:{'red' if diff < -10 else 'gray'}'>"
                    f" （理想{idl}%、{diff:+.0f}%）</span></div>",
                    unsafe_allow_html=True
                )
            st.markdown("")

            lacking_types = [t for t in all_types if type_ratio.get(t, 0) < ideal.get(t, 25) - 5]
            if not lacking_types:
                lacking_types = sorted(all_types, key=lambda t: type_ratio.get(t, 0))[:2]

            st.markdown(f"**不足気味のタイプ：{' / '.join(lacking_types)}**")

            col_opt1, col_opt2, col_opt3 = st.columns(3)
            with col_opt1:
                n_per_sector   = st.number_input("業種ごとの取得上限", min_value=3, max_value=20, value=5, key="auto_n_pub")
            with col_opt2:
                min_yield_auto = st.number_input("最低利回り（%）", min_value=2.0, max_value=6.0, value=3.5, step=0.1, key="auto_yield_pub")
            with col_opt3:
                st.markdown("<br>", unsafe_allow_html=True)
                prime_only = st.checkbox("プライム市場のみ（大型株中心）", value=True, key="auto_prime_pub")

            owned_codes_auto = set(portfolio_df['code'].tolist()) if portfolio_df is not None else set()
            current_wl_codes = set(re.findall(r'\d{4}', st.session_state.get('watchlist_input', _DEFAULT_WATCHLIST)))

            if st.button("🔍 候補を自動取得", key="auto_fetch_pub"):
                # 不足タイプに属する業種を抽出（株探の業種番号で重複排除）
                target_sectors = []
                seen_nums = set()
                for s, t in SECTOR_TYPE_MAP.items():
                    num = KABUTAN_INDUSTRY_NUM.get(s)
                    if t in lacking_types and num is not None and num not in seen_nums:
                        target_sectors.append(s)
                        seen_nums.add(num)

                fetch_bar      = st.progress(0, text="株探から業種別銘柄を取得中...")
                all_candidates = []
                seen_codes     = set(current_wl_codes)

                for idx, sector in enumerate(target_sectors):
                    fetch_bar.progress((idx + 1) / max(len(target_sectors), 1),
                                       text=f"取得中: {sector}")
                    # 株探の業種ページは利回り付きの一覧（1業種2〜5リクエストで完結）
                    stocks = get_category_stocks_kabutan(
                        sector, min_yield=min_yield_auto,
                        prime_only=prime_only, max_pages=5,
                    )
                    added = 0
                    for s in stocks:   # 利回り降順で来る
                        if added >= n_per_sector:
                            break
                        code = s['code']
                        if code in owned_codes_auto or code in seen_codes:
                            continue
                        all_candidates.append({**s, 'yield_str': f"{s['yield_pct']:.2f}%"})
                        seen_codes.add(code)
                        added += 1

                fetch_bar.empty()

                if all_candidates:
                    st.success(f"✅ {len(all_candidates)}銘柄が候補として見つかりました")
                    st.session_state['auto_candidates_pub'] = all_candidates
                else:
                    st.info("条件に合う候補が見つかりませんでした。条件を緩めてみてください。")
                    st.session_state['auto_candidates_pub'] = []

            # 候補リスト表示
            if st.session_state.get('auto_candidates_pub'):
                all_candidates = st.session_state['auto_candidates_pub']
                st.markdown("**追加したい銘柄を選んでください**")
                selected_codes = []
                for c in all_candidates:
                    label = f"{c['code']} {c['name'][:14]}　｜　{c['sector']}　｜　利回り {c['yield_str']}"
                    if st.checkbox(label, key=f"cand_pub_{c['code']}"):
                        selected_codes.append(c['code'])

                if selected_codes:
                    if st.button(f"📋 選択した{len(selected_codes)}銘柄をウォッチリストに追加", key="add_selected_pub"):
                        current = st.session_state.get('watchlist_input', _DEFAULT_WATCHLIST)
                        merged  = current.rstrip() + '\n' + ','.join(selected_codes)
                        st.session_state['_pending_watchlist'] = merged
                        st.session_state['auto_candidates_pub'] = []
                        st.success(f"✅ {len(selected_codes)}銘柄をウォッチリストに追加しました！")
                        st.rerun()
                else:
                    st.caption("☝️ 追加したい銘柄にチェックを入れてください")

    # ── スキャン実行 ──────────────────────────────────────
    raw_codes = re.findall(r'\d{4}', st.session_state.get('watchlist_input', _DEFAULT_WATCHLIST))
    unique_codes = list(dict.fromkeys(raw_codes))

    st.markdown(f"**ウォッチリスト：{len(unique_codes)} 銘柄**")

    scan_col1, scan_col2 = st.columns([2, 8])
    with scan_col1:
        do_scan = st.button("▶️ スキャン実行", type="primary", key="do_scan_pub")
    with scan_col2:
        st.caption("1銘柄あたり約3秒かかります。銘柄数が多いほど時間がかかります。")

    if do_scan:
        if not unique_codes:
            st.warning("ウォッチリストが空です")
        else:
            prog  = st.progress(0, text="スキャン開始…")
            status_placeholder = st.empty()

            completed = []
            total = len(unique_codes)

            for i, code in enumerate(unique_codes):
                prog.progress(i / total, text=f"({i+1}/{total}) {code} 分析中…")
                try:
                    result = score_list([(code, None)], sleep=0.5)[0]
                    completed.append(result)
                    status_placeholder.caption(
                        f"✅ {code} {result['name'][:14]}  スコア {result['score']}点 {result['rank']}"
                    )
                except Exception as e:
                    status_placeholder.caption(f"⚠️ {code} エラー → スキップ ({e})")

            results = sorted(completed, key=lambda x: x['score'], reverse=True)

            prog.progress(1.0, text="スキャン完了！")
            status_placeholder.empty()

            # スキャン時刻を付与してセッションに保存
            scanned_at = datetime.now().strftime('%Y年%m月%d日 %H:%M')
            for r in results:
                r['scanned_at'] = scanned_at
            st.session_state['screening_results'] = results
            st.success(f"✅ スキャン完了（{len(results)}銘柄）")
            st.rerun()

    # ── 結果表示 ──────────────────────────────────────────
    results: list[dict] = st.session_state.get('screening_results', [])

    if not results:
        st.info("「▶️ スキャン実行」でスクリーニングを開始してください")
        return

    # カスタム配点でスコア・ランクを再計算（再スキャン不要）
    results = recompute_results(results, get_weights())

    scanned_at = results[0].get('scanned_at', '') if results else ''
    if scanned_at:
        st.caption(f"🕐 最終スキャン：{scanned_at}")

    # スキャン結果ダウンロード
    result_rows = []
    for r in results:
        result_rows.append({
            'コード':      r.get('code',''),
            '銘柄名':      r.get('name',''),
            'ランク':      r.get('rank',''),
            'スコア':      r.get('score',0),
            '業種':        r.get('sector',''),
            'タイプ':      r.get('sector_type',''),
            '利回り%':     r.get('yield_pct',0),
            '連続増配年':  r.get('streak',0),
            '増配率%':     r.get('growth_rate'),
            '売上CAGR%':   r.get('rev_cagr'),
            'EPS_CAGR%':   r.get('eps_cagr'),
            '営業利益率%': r.get('op_margin'),
            '自己資本比率%': r.get('equity_ratio'),
            '配当性向%':   r.get('payout_ratio'),
        })
    result_csv = pd.DataFrame(result_rows).to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')
    st.download_button("📥 スキャン結果をCSVで保存", data=result_csv,
                       file_name=f"screening_{datetime.now().strftime('%Y%m%d')}.csv",
                       mime="text/csv")

    # 保有済み銘柄コード
    df = st.session_state.get('portfolio_df')
    owned_codes = set(df['code'].tolist()) if df is not None else set()

    # 表示行データ生成
    rows = []
    for r in results:
        code   = r.get('code', '')
        score  = r.get('score', 0)
        streak = r.get('streak', 0)
        covid  = r.get('covid_exception', False)
        gr     = r.get('growth_rate')
        ypct   = r.get('yield_pct', 0)
        ysrc   = r.get('yield_source', 'IRBank')
        ylabel = f"{ypct:.2f}%" + (' ⚠️' if ysrc != 'IRBank' else '')
        rows.append({
            'ランク':      r.get('rank', ''),
            'コード':      code,
            '銘柄名':      r.get('name', '')[:15],
            '業種':        r.get('sector', '')[:10],
            'タイプ':      r.get('sector_type', ''),
            '利回り%':     ylabel,
            'スコア':      score,
            '連続増配':    f"{streak}年{'※' if covid else ''}",
            '増配率':      f"+{gr:.1f}%" if gr else '-',
            'EPS成長':     f"+{r['eps_cagr']:.1f}%" if r.get('eps_cagr') is not None else '-',
            '営業利益率':  f"{r['op_margin']:.1f}%" if r.get('op_margin') is not None else '-',
            '自己資本比率': f"{r['equity_ratio']:.1f}%" if r.get('equity_ratio') is not None else '-',
            '配当性向':    f"{r['payout_ratio']:.1f}%" if r.get('payout_ratio') is not None else '-',
            '利回り元':    ysrc,
            '保有':        '✅' if code in owned_codes else '',
        })

    result_df = pd.DataFrame(rows)

    # フィルター
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        show_rank = st.multiselect("ランク絞り込み", ['🏆S', '✅A', '🥈B', '⬇️C'],
                                   default=['🏆S', '✅A'], key="rank_filter_pub")
    with col_f2:
        hide_owned = st.checkbox("保有済みを除外", value=True, key="hide_owned_pub")
    with col_f3:
        show_type = st.multiselect("セクタータイプ",
                                   ['ディフェンシブ', '景気敏感', '金融', '中間', '不明'],
                                   default=['ディフェンシブ', '景気敏感', '金融', '中間'],
                                   key="type_filter_pub")

    display_df = result_df.copy()
    if show_rank:
        display_df = display_df[display_df['ランク'].isin(show_rank)]
    if hide_owned:
        display_df = display_df[display_df['保有'] != '✅']
    if show_type:
        display_df = display_df[display_df['タイプ'].isin(show_type)]

    display_df = display_df.drop(columns=['利回り元'])

    # タイプ列に色付け
    def _style_type(val):
        color = SECTOR_TYPE_COLOR.get(val, '#AAAAAA')
        return f'background-color: {color}28; color: {color}; font-weight: 600;'

    styler = display_df.style.map(_style_type, subset=['タイプ'])
    st.dataframe(styler, use_container_width=True, hide_index=True)

    if any(r.get('yield_source', 'IRBank') != 'IRBank' for r in results):
        st.caption("⚠️ 印の利回りはIRBankで取得できず補完値を使用。購入前に実際の利回りを確認してください。")

    # JS タグ色付け
    _tag_colors = {
        **SECTOR_TYPE_COLOR,
        '🏆S': '#B8860B', '✅A': '#2E7D52', '🥈B': '#5B7FA6', '⬇️C': '#888888',
    }
    tag_color_js = json.dumps(_tag_colors)
    components.html(f"""
<script>
const TAG_COLORS = {tag_color_js};
function colorTags() {{
    const spans = window.parent.document.querySelectorAll('[data-baseweb="tag"] span');
    spans.forEach(span => {{
        const text = span.textContent.trim();
        if (TAG_COLORS[text]) {{
            const tag = span.closest('[data-baseweb="tag"]');
            if (tag) tag.style.backgroundColor = TAG_COLORS[text] + '33';
        }}
    }});
}}
colorTags();
const obs = new MutationObserver(colorTags);
obs.observe(window.parent.document.body, {{ childList: true, subtree: true }});
</script>
""", height=0)


# ═══════════════════════════════════════════════════════
#  月次レポートタブ
# ═══════════════════════════════════════════════════════
def render_report_tab() -> None:
    st.subheader("📋 月次レポート")

    results: list[dict] = st.session_state.get('screening_results', [])
    if results:
        results = recompute_results(results, get_weights())   # カスタム配点を反映
    scanned_at = results[0].get('scanned_at', '') if results else ''

    col_date, col_hint = st.columns([2, 5])
    with col_date:
        st.caption(f"📅 レポート日：{datetime.now().strftime('%Y年%m月%d日')}")
        if scanned_at:
            st.caption(f"🔍 スキャン日：{scanned_at}")
    with col_hint:
        st.caption("💡 スクリーニングタブで「▶️ スキャン実行」すると購入候補が更新されます")

    st.divider()

    # ── Section 1: ポートフォリオ現状 ────────────────────
    st.markdown("### 📊 ポートフォリオ現状")
    df: pd.DataFrame | None = st.session_state.get('portfolio_df')

    if df is None or df.empty:
        st.info("CSVをアップロードするとポートフォリオサマリーが表示されます")
    else:
        total_div    = df['annual_dividend'].sum()
        total_market = df['market_value'].sum()
        avg_yield    = total_div / total_market * 100 if total_market > 0 else 0
        num_owned    = len(df)
        remaining    = max(0, 30 - num_owned)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("保有銘柄数",  f"{num_owned}銘柄",  f"目標まで残り{remaining}銘柄")
        c2.metric("年間配当（予）", f"¥{total_div:,.0f}")
        c3.metric("配当利回り",  f"{avg_yield:.2f}%")
        c4.metric("目標達成率",  f"{num_owned/30*100:.0f}%", f"{num_owned}/30銘柄")

        if 'sector_type' in df.columns and 'annual_dividend' in df.columns:
            type_div  = df[df['annual_dividend'] > 0].groupby('sector_type')['annual_dividend'].sum()
            cyc_div   = type_div.get('景気敏感', 0)
            cyc_ratio = cyc_div / total_div * 100 if total_div > 0 else 0

            col_bar, col_warn = st.columns([3, 2])
            with col_bar:
                bar_data = type_div.reset_index()
                bar_data.columns = ['タイプ', '配当合計']
                bar_data['割合%'] = (bar_data['配当合計'] / total_div * 100).round(1)
                st.dataframe(bar_data.sort_values('配当合計', ascending=False),
                             use_container_width=True, hide_index=True)
            with col_warn:
                color = 'red' if cyc_ratio > 50 else 'green'
                st.markdown(
                    f"景気敏感比率：<span style='color:{color};font-size:1.4em;font-weight:bold'>"
                    f"{cyc_ratio:.1f}%</span>（上限50%）",
                    unsafe_allow_html=True)
                if cyc_ratio > 50:
                    st.error("⚠️ 景気敏感が50%超え。次はディフェンシブ・金融・中間を優先して。")
                else:
                    st.success("✅ セクターバランスOK")

    st.divider()

    # ── Section 2: 購入候補トップ10 ──────────────────────
    st.markdown("### 🏆 購入候補トップ10")

    if not results:
        st.info("「🔍 購入候補スクリーニング」タブでスキャンを実行してください")
    else:
        owned_codes = set(df['code'].tolist()) if df is not None else set()
        candidates = [
            r for r in results
            if r.get('rank', '') in ('🏆S', '✅A') and r.get('code', '') not in owned_codes
        ]
        candidates = sorted(candidates, key=lambda x: x.get('score', 0), reverse=True)[:10]

        if not candidates:
            st.info("S/Aランクの未保有銘柄がありません。スクリーニング条件を見直してみてください。")
        else:
            rows = []
            for rank_i, r in enumerate(candidates, 1):
                streak = r.get('streak', 0)
                covid  = r.get('covid_exception', False)
                gr     = r.get('growth_rate')
                ypct   = r.get('yield_pct', 0)
                ysrc   = r.get('yield_source', 'IRBank')
                ylabel = f"{ypct:.2f}%" + (' ⚠️' if ysrc != 'IRBank' else '')
                rows.append({
                    '順位':      rank_i,
                    'ランク':    r.get('rank', ''),
                    'コード':    r.get('code', ''),
                    '銘柄名':    r.get('name', '')[:14],
                    '業種タイプ': r.get('sector_type', ''),
                    '利回り':    ylabel,
                    'スコア':    r.get('score', 0),
                    '連続増配':  f"{streak}年{'※' if covid else ''}",
                    '増配率':    f"+{gr:.1f}%" if gr else '-',
                    'EPS成長':   f"+{r['eps_cagr']:.1f}%" if r.get('eps_cagr') is not None else '-',
                    '営業利益率': f"{r['op_margin']:.1f}%" if r.get('op_margin') is not None else '-',
                    '配当性向':  f"{r['payout_ratio']:.1f}%" if r.get('payout_ratio') is not None else '-',
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            if any(r.get('yield_source', 'IRBank') != 'IRBank' for r in candidates):
                st.caption("⚠️ = IRBankで利回り取得できず補完値を使用。購入前に実際の利回りを確認してね")

    st.divider()

    # ── Section 3: 次の購入タイミング提案 ────────────────
    st.markdown("### 💡 次の購入タイミング提案")

    if df is None or df.empty or not results:
        st.info("ポートフォリオCSVとスクリーニング結果の両方が揃うと提案が表示されます")
    else:
        owned_codes  = set(df['code'].tolist())
        num_owned    = len(df)
        remaining    = max(0, 30 - num_owned)

        month        = datetime.now().month
        bonus_months = {6, 7, 12, 1}
        near_bonus   = month in bonus_months

        type_div     = df[df['annual_dividend'] > 0].groupby('sector_type')['annual_dividend'].sum() if 'sector_type' in df.columns else pd.Series()
        total_div    = type_div.sum()
        cyc_ratio    = type_div.get('景気敏感', 0) / total_div * 100 if total_div > 0 else 0
        avoid_cyc    = cyc_ratio > 45

        top3 = [
            r for r in results
            if r.get('rank', '') in ('🏆S', '✅A')
            and r.get('code', '') not in owned_codes
            and (not avoid_cyc or r.get('sector_type', '') != '景気敏感')
        ][:3]

        timing_msgs = []
        if near_bonus:
            timing_msgs.append(f"🎉 **ボーナス月（{month}月）**です！購入の好機です。")
        else:
            next_b = min((b for b in bonus_months if b > month), default=min(bonus_months))
            diff   = (next_b - month) % 12
            timing_msgs.append(f"📆 次のボーナス月まで約 **{diff}ヶ月**（{next_b}月）")

        if remaining <= 0:
            timing_msgs.append("🎯 目標30銘柄に達しています。定期的な見直し（入れ替え）を検討しましょう。")
        elif remaining <= 5:
            timing_msgs.append(f"📌 目標まで残り **{remaining}銘柄**。完成が見えてきました！")
        else:
            timing_msgs.append(f"📌 目標まで残り **{remaining}銘柄**。着実に積み上げましょう。")

        if avoid_cyc:
            timing_msgs.append("⚠️ 景気敏感比率が高め（45%超）のため、次はディフェンシブ・金融・中間を優先するのがおすすめ。")

        for msg in timing_msgs:
            st.markdown(msg)

        if top3:
            st.markdown("\n**今すぐ検討できる候補トップ3：**")
            for i, r in enumerate(top3, 1):
                gr    = r.get('growth_rate')
                ypct  = r.get('yield_pct', 0)
                ysrc  = r.get('yield_source', 'IRBank')
                yield_str = f"{ypct:.2f}%" + (' ⚠️' if ysrc != 'IRBank' else '')
                growth_str = f"・増配率+{gr:.1f}%" if gr else ''
                st.markdown(
                    f"**{i}. {r.get('rank','')} {r.get('code','')} {r.get('name','')}**  "
                    f"（{r.get('sector_type','')}）  利回り{yield_str}  スコア{r.get('score',0)}点{growth_str}"
                )


# ═══════════════════════════════════════════════════════
#  使い方タブ（取扱説明書）
# ═══════════════════════════════════════════════════════
def render_manual_tab() -> None:
    st.markdown("""
## 📘 使い方ガイド

### このアプリでできること

| 機能 | 内容 |
|---|---|
| 📊 ポートフォリオ | 保有株のCSVを読み込んで、配当・セクターバランスを自動分析 |
| 🔍 スクリーニング | 気になる銘柄を100点満点でスコアリングして、買い候補をランク付け |
| 📋 月次レポート | ポートフォリオの現状と購入候補トップ10をまとめて表示 |

---

### 🚀 はじめかた（3ステップ）

**Step 1：楽天証券からCSVをダウンロード**

1. 楽天証券にログイン
2. **マイメニュー → 保有商品一覧（国内株式）** を開く
3. ページ内の **「CSVで保存」** ボタンを押してダウンロード

**Step 2：このアプリにアップロード**

1. 「📊 ポートフォリオ」タブを開く
2. 「📂 保有銘柄データの入力・読み込み」にCSVをドラッグ＆ドロップ
3. 自動で配当・業種データを取得して分析開始（1銘柄あたり1〜2秒）

**Step 3：分析結果を見る**

- 保有資産額・年間配当金・利回りが自動計算される
- 業種バランスの円グラフと、改善アドバイスが表示される

---

### 🔍 スクリーニングの使い方

1. 「🔍 購入候補スクリーニング」タブを開く
2. **ウォッチリスト**に気になる銘柄の証券コード（4桁）を入力
   - 会社名で検索して追加もできる
   - ポートフォリオ読込済みなら「🤖 自動追加」で不足セクターから候補を自動提案
3. **「▶️ スキャン実行」** を押す（1銘柄あたり約3秒）
4. 結果がスコア順に表示される

**ランクの見方**

| ランク | スコア | 意味 |
|---|---|---|
| 🏆S | 80点以上 | 即検討レベルの優良銘柄 |
| ✅A | 65点以上 | 有力候補 |
| 🥈B | 50点以上 | ウォッチ継続 |
| ⬇️C | 50点未満 | 見送り |

**配点のカスタマイズ**

- 「📖 スコア定義・配点カスタマイズ」を開くと、8項目の配点を**5点刻み**で変更できる（合計100点）
- 例：利回り重視派なら「配当利回り」を40点に上げる、など
- 配点を変えるとURLに反映される。**そのURLをブックマーク**すれば次回も同じ配点で使える

---

### 💾 データの保存と復元

このアプリは**サーバーに何も保存しない**設計です。データは自分のPCで管理します。

| データ | 保存方法 | 復元方法 |
|---|---|---|
| 保有銘柄 | 「💾 保有データを保存（CSV）」でダウンロード | 次回そのCSVをアップロード |
| ウォッチリスト | 「📥 ウォッチリストをダウンロード」 | 中身をコピーして貼り付け |
| 配点設定 | URLをブックマーク | ブックマークから開く |

⚠️ **ブラウザのタブを閉じるとデータは消えます。** 終わる前に保存を忘れずに！

---

### 🔒 プライバシーについて

- アップロードしたCSVは**ブラウザセッション内のみ**で処理され、サーバーに保存されません
- 外部に送信されるのは銘柄コード（4桁）だけ。**保有数・金額・損益は一切送信されません**
- 他の利用者があなたのデータを見ることはできません

---

### ❓ よくある質問

**Q. データの取得が途中で止まった**
→ ページを再読み込み（F5）してもう一度アップロードしてみてください。

**Q. 利回りに ⚠️ マークがついている**
→ IRBankで取得できず、Yahoo Financeの予想配当から補完した値です。購入前に証券会社で実際の利回りを確認してください。

**Q. 配当データが「0」になる銘柄がある**
→ 「🔄 配当・業種データを再取得」ボタンで再試行してください。ETF・REITは取得できない場合があります。

**Q. 楽天証券以外のCSVは使える？**
→ 現在は楽天証券の保有商品明細CSVと、このアプリで保存したCSVに対応しています。

---

### ⚠️ 免責事項

- 本アプリは投資判断の**参考情報**を提供するものであり、特定銘柄の購入を推奨するものではありません
- データはIRBank・Yahoo Finance等から取得していますが、正確性・最新性は保証されません
- **投資の最終判断はご自身の責任**でお願いします
    """)


# ═══════════════════════════════════════════════════════
#  メイン
# ═══════════════════════════════════════════════════════
def main():
    # ── タイトルエリア（ロゴ／テキスト／キャラクター）──
    col_logo, col_title, col_illust = st.columns([1, 8, 2])
    with col_logo:
        if _icon_path.exists():
            st.image(str(_icon_path), width=56)
    with col_title:
        st.markdown("## 🌳 配当の森 〜育てる高配当株ダッシュボード〜")
        st.caption(
            "1銘柄ずつコツコツ植えて、配当をすくすく育てる。 "
            "データはこのブラウザセッション内のみで処理され、サーバーには保存されません。"
        )
    with col_illust:
        st.markdown(
            '<div style="text-align:right; margin-bottom:6px;">'
            '<a href="https://www.rakuten-sec.co.jp/" target="_blank" '
            'style="background:#bf0000; color:white; padding:6px 12px; '
            'border-radius:6px; font-size:13px; text-decoration:none; font-weight:bold;">'
            '🏦 楽天証券</a></div>',
            unsafe_allow_html=True,
        )
        if _illust_path.exists():
            st.image(str(_illust_path), width=220)
            st.caption("by 秘書コウ ｜ データソース：IRバンク")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 ポートフォリオ", "🔍 購入候補スクリーニング", "📋 月次レポート", "📘 使い方",
    ])

    with tab1:
        render_portfolio_tab()

    with tab2:
        render_screening_tab()

    with tab3:
        render_report_tab()

    with tab4:
        render_manual_tab()


if __name__ == "__main__":
    main()
