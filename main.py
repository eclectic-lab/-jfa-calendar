#!/usr/bin/env python3
"""
JFA（日本サッカー協会）公式サイトからサッカー日本代表（SAMURAI BLUE）の最新試合日程を取得し、
RFC 5545準拠のiCalendar（.ics）ファイルを自動生成するスクリプト。

- 対象: SAMURAI BLUE 最新試合日程（今年および翌年）
- タイムゾーン: Asia/Tokyo
- 出力ファイル: japan_national_team.ics
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:
    JST = datetime.timezone(datetime.timedelta(hours=9), name="JST")

import requests
from bs4 import BeautifulSoup

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 定数
BASE_URL = "https://www.jfa.jp"
SCHEDULE_BASE_URL = "https://www.jfa.jp/samuraiblue/schedule_result/"
OUTPUT_ICS_PATH = "japan_national_team.ics"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 (JFA-Calendar-Sync-Bot/1.0; +https://github.com/)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# 相手サーバー負荷軽減のためのリクエスト間隔（秒）
REQUEST_INTERVAL_SECONDS = 0.5

# 日本国内判定用キーワード
JAPAN_KEYWORDS = [
    "東京", "大阪", "横浜", "名古屋", "福岡", "札幌", "仙台", "神戸",
    "埼玉", "千葉", "石川", "静岡", "広島", "宮城", "新潟", "松本",
    "沖縄", "北海道", "青森", "岩手", "秋田", "山形",
    "茨城", "栃木", "群馬", "神奈川", "富山",
    "福井", "山梨", "長野", "岐阜", "愛知", "三重",
    "滋賀", "京都", "兵庫", "奈良", "和歌山", "鳥取", "島根",
    "岡山", "山口", "徳島", "香川", "愛媛", "高知",
    "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島",
    "国立競技場", "日産スタジアム", "パナソニックスタジアム", "パナソニック スタジアム",
    "エディオンピースウイング広島", "キューアンドエースタジアムみやぎ",
    "Japan", "JAPAN",
]

# 海外会場・国別の日本時間との時差（JST - 現地時間、単位: 時間）
VENUE_TIME_DIFF = {
    # アメリカ・メキシコ・カナダ (W杯 2026 JFA公式サイト定義準拠)
    "バンクーバー(カナダ)／BCプレイスバンクーバー": 16,
    "シアトル(アメリカ)／シアトルスタジアム": 16,
    "サンフランシスコ・ベイエリア(アメリカ)／サンフランシスコベイエリアスタジアム": 16,
    "ロサンゼルス(アメリカ)／ロサンゼルススタジアム": 16,
    "グアダラハラ(メキシコ)／グアダラハラスタジアム": 15,
    "メキシコシティ(メキシコ)／メキシコシティスタジアム": 15,
    "モンテレイ(メキシコ)／モンテレイスタジアム": 15,
    "ヒューストン(アメリカ)／ヒューストンスタジアム": 14,
    "ダラス(アメリカ)／ダラススタジアム": 14,
    "カンザスシティ(アメリカ)／カンザスシティスタジアム": 14,
    "アトランタ(アメリカ)／アトランタスタジアム": 13,
    "マイアミ(アメリカ)／マイアミスタジアム": 13,
    "トロント(カナダ)／トロントスタジアム": 13,
    "ボストン(アメリカ)／ボストンスタジアム": 13,
    "フィラデルフィア(アメリカ)／フィラデルフィアスタジアム": 13,
    "ニューヨーク(アメリカ)／ニューヨークニュージャージースタジアム": 13,
    # サウジアラビア (AFC アジアカップ 2027)
    "Prince Abdullah Al Faisal Sports City Stadium": 6,
    "Al Shabab Stadium": 6,
    "King Saud University Stadium": 6,
    "サウジアラビア": 6,
    "ジッダ": 6,
    "ジェッダ": 6,
    "リヤド": 6,
    # シンガポール
    "シンガポールナショナルスタジアム": 1,
    "シンガポール": 1,
    # イギリス
    "グラスゴー(スコットランド)／ハムデン・パーク": 8,
    "ロンドン(イングランド)／ウェンブリー・スタジアム": 8,
    "スコットランド": 8,
    "イングランド": 8,
}


def to_halfwidth(text: str) -> str:
    """全角英数字・記号を半角に変換する"""
    if not text:
        return text
    return text.translate(str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９"
        "　！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
        " !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    ))


@dataclass
class MatchEvent:
    """試合日程情報を保持するデータクラス"""
    date: datetime.date
    competition: str
    opponent: str
    venue: str
    score: str = ""
    kickoff_time: Optional[datetime.time] = None
    detail_url: Optional[str] = None

    @property
    def uid(self) -> str:
        """カレンダーイベント用の一意なUID（重複防止・更新識別用）"""
        raw_key = f"{self.date.strftime('%Y%m%d')}_{self.competition}_{self.opponent}"
        hash_digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"jfa-match-{self.date.strftime('%Y%m%d')}-{hash_digest}@jfa.jp"

    @property
    def is_home(self) -> bool:
        """会場名から日本国内開催か判定（ホーム試合か否か）"""
        if not self.venue:
            return True
        return any(kw in self.venue for kw in JAPAN_KEYWORDS)

    @property
    def summary(self) -> str:
        """イベントのタイトル（例: 代表[H]ウルグアイ【キリンチャレンジカップ2026】）"""
        ha = "H" if self.is_home else "A"
        opponent_text = to_halfwidth(self.opponent) if self.opponent and self.opponent != "-" else "未定"
        comp_info = f"【{to_halfwidth(self.competition)}】" if self.competition else ""
        return f"代表[{ha}]{opponent_text}{comp_info}"

    @property
    def description(self) -> str:
        """イベントの詳細説明"""
        lines = [
            f"大会名: {self.competition}",
            f"対戦カード: 日本代表 vs {self.opponent}",
        ]
        if self.kickoff_time:
            lines.append(f"キックオフ: {self.kickoff_time.strftime('%H:%M')} (JST)")
        else:
            lines.append("キックオフ: 時間未定（または公式発表待ち）")

        if self.venue:
            lines.append(f"会場: {self.venue}")

        if self.score and self.score != "-":
            lines.append(f"試合結果: {self.score}")

        if self.detail_url:
            lines.append(f"詳細ページ: {self.detail_url}")

        lines.append("")
        lines.append("※JFA公式サイトより自動取得した日程情報です。")
        return "\n".join(lines)


class JFAScraper:
    """JFA公式サイトからSAMURAI BLUEの試合日程をスクレイピングするクラス"""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.cached_page_html: Dict[str, str] = {}
        self.json_matches_by_year: Dict[int, List[dict]] = {}

    def fetch_html(self, url: str) -> Optional[str]:
        """指定URLのHTMLを取得（エラーハンドリング・エンコーディング自動補正付き）"""
        if url in self.cached_page_html:
            return self.cached_page_html[url]

        try:
            logger.info(f"アクセス中: {url}")
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"HTTPステータス {resp.status_code}: {url}")
                self.cached_page_html[url] = ""
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            html_text = resp.text
            self.cached_page_html[url] = html_text
            return html_text
        except requests.RequestException as e:
            logger.error(f"リクエストエラー [{url}]: {e}")
            self.cached_page_html[url] = ""
            return None

    def get_target_years(self) -> List[int]:
        """取得対象となる年度リスト（今年、翌年）"""
        now = datetime.datetime.now(JST)
        current_year = now.year
        return [current_year, current_year + 1]

    def scrape_all_schedules(self) -> List[MatchEvent]:
        """対象年度のスケジュールをすべて取得して一覧化"""
        all_events: List[MatchEvent] = []
        target_years = self.get_target_years()

        available_years = self._discover_available_years()
        years_to_scrape = sorted(list(set(target_years + available_years)))
        now_year = datetime.datetime.now(JST).year
        years_to_scrape = [y for y in years_to_scrape if y >= now_year - 1 and y <= now_year + 2]

        logger.info(f"取得対象年度: {years_to_scrape}")

        for year in years_to_scrape:
            year_url = f"{SCHEDULE_BASE_URL}{year}.html"
            events = self.scrape_year_page(year_url, default_year=year)
            all_events.extend(events)
            time.sleep(REQUEST_INTERVAL_SECONDS)

        # 重複排除（UID基準）＆ 日付順ソート
        unique_events_dict = {event.uid: event for event in all_events}
        sorted_events = sorted(unique_events_dict.values(), key=lambda e: (e.date, e.kickoff_time or datetime.time.min))
        logger.info(f"合計 {len(sorted_events)} 件の試合日程を抽出しました。")
        return sorted_events

    def _discover_available_years(self) -> List[int]:
        """スケジュールページ内の年度セレクトボックスから存在する年を取得"""
        html = self.fetch_html(SCHEDULE_BASE_URL)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        select_elem = soup.find("select", id=re.compile(r"select-year", re.I)) or soup.find("select", attrs={"name": "year"})
        years = []
        if select_elem:
            for opt in select_elem.find_all("option"):
                val = opt.get("value", "")
                text = opt.get_text()
                match = re.search(r"(\d{4})\.html", val) or re.search(r"(\d{4})年", text)
                if match:
                    years.append(int(match.group(1)))
        return years

    def scrape_year_page(self, url: str, default_year: int) -> List[MatchEvent]:
        """特定の年度スケジュールページからテーブル行を解析"""
        html = self.fetch_html(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        events: List[MatchEvent] = []

        tables = soup.find_all("table")
        schedule_table = None
        for table in tables:
            header_text = table.get_text()
            if "開催日" in header_text and ("試合名" in header_text or "対戦チーム" in header_text):
                schedule_table = table
                break

        if not schedule_table:
            logger.warning(f"試合日程テーブルが見つかりませんでした: {url}")
            return []

        tbody = schedule_table.find("tbody") or schedule_table
        rows = tbody.find_all("tr")

        for tr in rows:
            if tr.find("th") or not tr.find_all("td"):
                continue

            tds = tr.find_all("td")
            if len(tds) < 4:
                continue

            try:
                event = self._parse_table_row(tds, default_year)
                if event:
                    resolved_date, kickoff = self._resolve_kickoff_datetime(
                        target_date=event.date,
                        opponent=event.opponent,
                        comp=event.competition,
                        venue=event.venue,
                        detail_url=event.detail_url,
                    )
                    event.date = resolved_date
                    event.kickoff_time = kickoff
                    events.append(event)
            except Exception as e:
                logger.error(f"行の解析中にエラーが発生しました: {e}", exc_info=True)

        return events

    def _parse_table_row(self, tds: list, default_year: int) -> Optional[MatchEvent]:
        """td要素リストからMatchEventを構築"""
        date_td = None
        comp_td = None
        score_td = None
        team_td = None
        place_td = None

        for td in tds:
            classes = td.get("class", [])
            class_str = " ".join(classes)
            if "date" in class_str:
                date_td = td
            elif "comp_name" in class_str:
                comp_td = td
            elif "score" in class_str:
                score_td = td
            elif "team" in class_str:
                team_td = td
            elif "place" in class_str:
                place_td = td

        if not date_td and len(tds) >= 1:
            date_td = tds[0]
        if not comp_td and len(tds) >= 2:
            comp_td = tds[1]
        if not score_td and len(tds) >= 3:
            score_td = tds[2]
        if not team_td and len(tds) >= 4:
            team_td = tds[3]
        if not place_td and len(tds) >= 5:
            place_td = tds[4]

        date_text = date_td.get_text(strip=True) if date_td else ""
        comp_text = comp_td.get_text(strip=True) if comp_td else ""
        score_text = score_td.get_text(strip=True) if score_td else ""
        team_text = team_td.get_text(strip=True) if team_td else ""
        place_text = place_td.get_text(strip=True) if place_td else ""

        if not date_text or not comp_text:
            return None

        parsed_date = self._parse_date(date_text, default_year)
        if not parsed_date:
            logger.debug(f"日付のパースに失敗したためスキップ: {date_text}")
            return None

        detail_url = None
        if comp_td:
            link_a = comp_td.find("a")
            if link_a and link_a.get("href"):
                detail_url = urljoin(BASE_URL, link_a.get("href"))

        return MatchEvent(
            date=parsed_date,
            competition=comp_text,
            opponent=team_text,
            venue=place_text,
            score=score_text,
            detail_url=detail_url,
        )

    def _parse_date(self, text: str, default_year: int) -> Optional[datetime.date]:
        """文字列から date オブジェクトを抽出"""
        clean_text = text.translate(str.maketrans("０１２３４５６７８９／", "0123456789/"))

        m = re.search(r"(\d{4})[./年](\d{1,2})[./月](\d{1,2})", clean_text)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime.date(year, month, day)

        m2 = re.search(r"(\d{1,2})[./月](\d{1,2})", clean_text)
        if m2:
            month, day = int(m2.group(1)), int(m2.group(2))
            return datetime.date(default_year, month, day)

        return None

    def _resolve_kickoff_datetime(
        self,
        target_date: datetime.date,
        opponent: str,
        comp: str,
        venue: str,
        detail_url: Optional[str],
    ) -> Tuple[datetime.date, Optional[datetime.time]]:
        """
        公式JSON、詳細ページ、時差計算を統合して正確な日本時間（JST）の日時を解決する。
        """
        resolved_date = target_date
        resolved_time: Optional[datetime.time] = None

        # 1. 公式JSONからのマッチング・時差算出
        matched_json = self._find_json_match(target_date, opponent, comp, venue)
        if matched_json:
            jst_dt = self._calculate_jst_from_json(matched_json, venue)
            if jst_dt:
                resolved_date = jst_dt.date()
                resolved_time = jst_dt.time()
                logger.debug(f"JSONより取得: {opponent} -> {resolved_date} {resolved_time} (JST)")

        # 2. 詳細ページがある場合、ページ内の特定表記を解析
        if detail_url:
            time.sleep(REQUEST_INTERVAL_SECONDS)
            html = self.fetch_html(detail_url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text()

                # 未定・調整中の確認
                if "キックオフ時間調整中" in text or "キックオフ未定" in text:
                    if opponent in ["未定", "-"] or "対戦国調整中" in text or opponent in text:
                        logger.info(f"キックオフ未定と確認: {opponent} ({detail_url})")
                        return (target_date, None)

                # 最優先: 「日本時間 H:MM」の明記（アウェイ戦での表記）
                # 例: "日本時間 3/29(日) 2:00", "日本時間 4/1(水) 3:45"
                m_jst = re.search(
                    r'日本時間(?:[^\d\n\r]*\d{1,2}/\d{1,2}(?:\([^\)]+\))?)?[^\d\n\r]*(\d{1,2})[:：](\d{2})',
                    text,
                )
                if m_jst:
                    h, mi = int(m_jst.group(1)), int(m_jst.group(2))
                    if 0 <= h < 24 and 0 <= mi < 60:
                        resolved_time = datetime.time(h, mi)
                        logger.info(f"詳細ページより日本時間取得成功 [{detail_url}]: {resolved_time.strftime('%H:%M')}")

                # JSONで取得できなかった場合のフォールバック（日本国内開催）
                elif not resolved_time:
                    is_japan = any(k in venue for k in JAPAN_KEYWORDS)
                    if is_japan:
                        # 複数時間表記 (例: 15:10/19:10キックオフ予定 -> 第2試合が代表戦)
                        m_multi = re.search(r'(\d{1,2}:\d{2})\s*/\s*(\d{1,2}:\d{2})\s*(?:キックオフ|KickOff)', text, re.I)
                        if m_multi:
                            h, mi = map(int, m_multi.group(2).split(':'))
                            resolved_time = datetime.time(h, mi)
                            logger.info(f"詳細ページ複数時間表記より取得 [{detail_url}]: {resolved_time.strftime('%H:%M')}")
                        else:
                            patterns = [
                                r'(\d{1,2})[:：](\d{2})\s*(?:KickOff|キックオフ|KICK\s*OFF)',
                                r'(\d{1,2})時(\d{2})分\s*(?:KickOff|キックオフ|KICK\s*OFF)?',
                                r'(?:KickOff|キックオフ)\s*[:：]?\s*(\d{1,2})[:：](\d{2})',
                            ]
                            for pat in patterns:
                                m = re.search(pat, text, re.I)
                                if m:
                                    h, mi = int(m.group(1)), int(m.group(2))
                                    if 0 <= h < 24 and 0 <= mi < 60:
                                        resolved_time = datetime.time(h, mi)
                                        logger.info(f"詳細ページキックオフ時刻取得 [{detail_url}]: {resolved_time.strftime('%H:%M')}")
                                        break

        if resolved_time:
            logger.info(f"試合時間決定: {resolved_date} {resolved_time.strftime('%H:%M')} JST | vs {opponent}")
        else:
            logger.info(f"試合時間未定: {resolved_date} | vs {opponent}")

        return (resolved_date, resolved_time)

    def _load_json_matches(self, year: int) -> List[dict]:
        """JFA公式の年度別全代表戦JSONを取得してキャッシュ"""
        if year in self.json_matches_by_year:
            return self.json_matches_by_year[year]

        url = f"https://www.jfa.jp/national_team/inc/schedule_result/samuraiblue_{year}.json"
        try:
            logger.info(f"JSONデータ取得中: {url}")
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                text = resp.text
                # JFA公式JSONの文法エラー（カンマ欠落・末尾余分カンマ）を補正
                cleaned = re.sub(r'("|\d|true|false|null)\s*\n(\s*")', r'\1,\n\2', text)
                cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
                data = json.loads(cleaned)
                matches = data.get("match", [])
                jp_matches = [
                    m for m in matches
                    if "日本" in m.get("homeTeamName", "") or "日本" in m.get("awayTeamName", "")
                ]
                self.json_matches_by_year[year] = jp_matches
                return jp_matches
        except Exception as e:
            logger.warning(f"公式JSON取得・解析スキップ [{url}]: {e}")

        self.json_matches_by_year[year] = []
        return []

    def _find_json_match(
        self,
        target_date: datetime.date,
        opponent: str,
        comp: str,
        venue: str,
    ) -> Optional[dict]:
        """公式JSON内の日本代表戦リストから、対象試合を照合して特定"""
        years = [target_date.year, target_date.year - 1, target_date.year + 1]
        clean_opp = re.sub(r'\s+', '', opponent).lower()
        clean_comp = re.sub(r'\s+', '', comp).lower()

        candidates = []
        for y in years:
            matches = self._load_json_matches(y)
            for m in matches:
                home = m.get("homeTeamName", "")
                away = m.get("awayTeamName", "")
                m_opp = home if "日本" not in home else away
                clean_m_opp = re.sub(r'\s+', '', m_opp).lower()

                m_date_str = m.get("matchDate", "")
                if not m_date_str:
                    continue
                try:
                    my, mm, md = map(int, m_date_str.split('/'))
                    m_date = datetime.date(my, mm, md)
                except Exception:
                    continue

                opp_match = bool(clean_opp and clean_m_opp and (clean_opp in clean_m_opp or clean_m_opp in clean_opp))
                date_diff = abs((m_date - target_date).days)

                if date_diff <= 1:
                    score = 0
                    if opp_match:
                        score += 10
                    clean_m_comp = re.sub(r'\s+', '', m.get("competitionName", "")).lower()
                    if clean_comp in clean_m_comp or clean_m_comp in clean_comp:
                        score += 5
                    if date_diff == 0:
                        score += 3
                    if score >= 8:
                        candidates.append((score, m))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return None

    def _calculate_jst_from_json(self, m: dict, fallback_venue: str) -> Optional[datetime.datetime]:
        """公式JSONの現地日時から時差を適用して日本時間（JST）を算出"""
        m_date_str = m.get("matchDate", "")
        m_time_str = m.get("matchTime", "")
        if not m_date_str or not m_time_str:
            return None

        try:
            my, mm, md = map(int, m_date_str.split('/'))
            mh, mmi = map(int, m_time_str.split(':'))
            local_dt = datetime.datetime(my, mm, md, mh, mmi)
        except Exception:
            return None

        venue_text = m.get("venue", "") + " " + m.get("venueFullName", "") + " " + fallback_venue
        diff = 0
        if not any(k in venue_text for k in JAPAN_KEYWORDS):
            for v_key, d_val in VENUE_TIME_DIFF.items():
                if v_key in venue_text:
                    diff = d_val
                    break

        jst_dt = (local_dt + datetime.timedelta(hours=diff)).replace(tzinfo=JST)
        return jst_dt


class ICalGenerator:
    """RFC 5545規格に準拠したiCalendarファイルを生成するクラス"""

    @staticmethod
    def escape_text(text: str) -> str:
        """iCalテキストエスケープ処理 (RFC 5545 §3.3.11)"""
        if not text:
            return ""
        text = text.replace("\\", "\\\\")
        text = text.replace(";", "\\;")
        text = text.replace(",", "\\,")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", "\\n")
        return text

    @staticmethod
    def fold_line(line: str) -> str:
        """1行の長さをRFC 5545規定の75バイト以内で折り返し（フォールディング）"""
        encoded = line.encode("utf-8")
        if len(encoded) <= 75:
            return line

        folded_parts = []
        current_part = bytearray()
        for b in encoded:
            current_part.append(b)
            if len(current_part) >= 74:
                folded_parts.append(current_part.decode("utf-8", errors="ignore"))
                current_part = bytearray()

        if current_part:
            folded_parts.append(current_part.decode("utf-8", errors="ignore"))

        return "\r\n ".join(folded_parts)

    @classmethod
    def generate(cls, events: List[MatchEvent]) -> str:
        """イベント一覧から完全なiCalendarフォーマット文字列を生成"""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//JFA National Team Calendar Bot//JA//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:サッカー日本代表 試合日程",
            "X-WR-CALDESC:JFA公式サイトより自動取得したサッカー日本代表（SAMURAI BLUE）の最新試合日程",
            "X-WR-TIMEZONE:Asia/Tokyo",
            "REFRESH-INTERVAL;VALUE=DURATION:P1D",
            "X-PUBLISHED-TTL:P1D",
            # VTIMEZONE定義（Asia/Tokyo）
            "BEGIN:VTIMEZONE",
            "TZID:Asia/Tokyo",
            "X-LIC-LOCATION:Asia/Tokyo",
            "BEGIN:STANDARD",
            "TZOFFSETFROM:+0900",
            "TZOFFSETTO:+0900",
            "TZNAME:JST",
            "DTSTART:19700101T000000",
            "END:STANDARD",
            "END:VTIMEZONE",
        ]

        for ev in events:
            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{ev.uid}")
            lines.append(f"DTSTAMP:{dtstamp}")

            if ev.kickoff_time:
                # キックオフ時間が判明している場合（Asia/Tokyo時間帯指定）
                start_dt = datetime.datetime.combine(ev.date, ev.kickoff_time, tzinfo=JST)
                end_dt = start_dt + datetime.timedelta(hours=2)
                lines.append(f"DTSTART;TZID=Asia/Tokyo:{start_dt.strftime('%Y%m%dT%H%M%S')}")
                lines.append(f"DTEND;TZID=Asia/Tokyo:{end_dt.strftime('%Y%m%dT%H%M%S')}")
            else:
                # キックオフ未定時は終日イベント（VALUE=DATE）
                next_day = ev.date + datetime.timedelta(days=1)
                lines.append(f"DTSTART;VALUE=DATE:{ev.date.strftime('%Y%m%d')}")
                lines.append(f"DTEND;VALUE=DATE:{next_day.strftime('%Y%m%d')}")

            lines.append(f"SUMMARY:{cls.escape_text(ev.summary)}")
            lines.append(f"DESCRIPTION:{cls.escape_text(ev.description)}")

            if ev.venue:
                lines.append(f"LOCATION:{cls.escape_text(ev.venue)}")

            if ev.detail_url:
                lines.append(f"URL:{ev.detail_url}")

            lines.append("STATUS:CONFIRMED")
            lines.append("TRANSP:OPAQUE")
            lines.append("END:VEVENT")

        lines.append("END:VCALENDAR")

        # RFC 5545に従いCRLF改行で結合
        formatted_lines = [cls.fold_line(line) for line in lines]
        return "\r\n".join(formatted_lines) + "\r\n"


def main() -> int:
    """メイン実行処理"""
    logger.info("=== サッカー日本代表 試合日程取得スクリプト 開始 ===")
    scraper = JFAScraper()
    events = scraper.scrape_all_schedules()

    if not events:
        logger.warning("試合日程が1件も取得できませんでした。サイト構造が変更された可能性があります。")
        return 0

    logger.info(f"{len(events)} 件の試合日程からiCalendarデータを生成中...")
    ics_content = ICalGenerator.generate(events)

    with open(OUTPUT_ICS_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(ics_content)

    logger.info(f"保存完了: {OUTPUT_ICS_PATH} ({len(ics_content.encode('utf-8'))} bytes)")
    logger.info("=== 処理が正常に完了しました ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
