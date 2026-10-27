#!/usr/bin/env python3
"""
Jリーグ公式データサイトおよびJFA（日本サッカー協会）公式サイトから
J1 鹿島アントラーズの最新試合日程・結果を自動取得し、
RFC 5545準拠のiCalendar（.ics）ファイルを自動生成・マージするスクリプト。

- 対象: 鹿島アントラーズ（J1リーグ、ルヴァンカップ、天皇杯、ACLなど全公式戦）
- データソース:
  1. J.League Data Site (J1リーグ、ルヴァンカップ、一部天皇杯など)
  2. JFA公式 天皇杯スケジュール (最新ラウンド・トーナメント詳細)
  3. JFA公式 ACL (AFC Champions League Elite / Two) スケジュール
- タイムゾーン: Asia/Tokyo
- 出力ファイル: j1_club.ics
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
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 定数設定
JST = ZoneInfo("Asia/Tokyo")
JLEAGUE_DATA_BASE_URL = "https://data.j-league.or.jp"
JLEAGUE_SEARCH_URL = f"{JLEAGUE_DATA_BASE_URL}/SFMS01/search"
JFA_BASE_URL = "https://www.jfa.jp"
OUTPUT_ICS_PATH = "j1_club.ics"

# 鹿島アントラーズ設定
CLUB_NAME = "鹿島アントラーズ"
CLUB_SHORT_NAME = "鹿島"
CLUB_TEAM_ID = "1"  # J.League Data Siteにおける鹿島の固有ID

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 (J1-Club-Calendar-Sync-Bot/1.0; +https://github.com/)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# サーバー負荷軽減のためのリクエスト間隔（秒）
REQUEST_INTERVAL_SECONDS = 0.5


def is_kashima_team(team_name: str) -> bool:
    """チーム名が鹿島アントラーズに該当するか判定"""
    if not team_name:
        return False
    clean = re.sub(r"[\s　・]", "", team_name).lower()
    return "鹿島" in clean or "アントラーズ" in clean or "antlers" in clean or "kashima" in clean


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

# チーム名の短縮表記マッピング（キーの部分文字列が一致した場合に短縮表記を返す）
# 優先度順に記載（長い・より具体的なものを先に）
TEAM_SHORT_NAMES: list[tuple[str, str]] = [
    ("東京Ｖ", "東京V"),
    ("川崎Ｆ", "川崎F"),
    ("ヴァンフォーレ甲府", "甲府"),
    ("Ｇ大阪", "G大阪"),
    ("Ｃ大阪", "C大阪"),
    ("セレッソ大阪", "C大阪"),
    ("アトレチコ鈴鹿", "鈴鹿")
]


def normalize_team_name(team_name: str) -> str:
    """チーム名を短縮表記に変換する（辞書に登録のないチームはそのまま返す）"""
    for keyword, short in TEAM_SHORT_NAMES:
        if keyword in team_name:
            return short
    return team_name


def normalize_competition_category(comp_name: str) -> str:
    """大会名を正規化して大分類キー（重複判定・UID用）を返す"""
    if not comp_name:
        return "other"
    # 全角→半角変換してから比較することで「Ｊ１」「ｊ１」どちらも正しく判定
    c = to_halfwidth(comp_name).lower()
    if "天皇杯" in comp_name or "emperors" in c:
        return "emperorscup"
    if "ルヴァン" in comp_name or "levain" in c or "ナビスコ" in comp_name:
        return "levain"
    if "acl" in c or "afc" in c or "champions league" in c or "チャンピオンズリーグ" in comp_name:
        return "acl"
    if "j1" in c or "jリーグ" in comp_name:
        return "j1"
    return "other"


@dataclass
class MatchEvent:
    """試合日程情報を保持するデータクラス"""
    date: datetime.date
    competition: str
    section: str
    home_team: str
    away_team: str
    venue: str
    score: str = ""
    kickoff_time: Optional[datetime.time] = None
    detail_url: Optional[str] = None
    broadcast: str = ""
    source: str = "J.League Data Site"

    @property
    def is_home(self) -> bool:
        """鹿島がホームゲームかどうか"""
        return is_kashima_team(self.home_team)

    @property
    def opponent(self) -> str:
        """対戦相手チーム名"""
        if self.is_home:
            return self.away_team if self.away_team else "対戦相手未定"
        return self.home_team if self.home_team else "対戦相手未定"

    @property
    def short_competition(self) -> str:
        """カレンダータイトル用に簡潔に短縮した大会名（略称）"""
        comp = self.competition
        cat = normalize_competition_category(comp)
        if cat == "emperorscup":
            return "天"
        if cat == "acl":
            return "AC"
        if cat == "levain":
            return "LC"
        if cat == "j1":
            return "J1"
        # 不要なプレフィックスや長い文言を切り落とし
        clean = re.sub(r"明治安田(生命)?", "", comp).strip()
        return clean if clean else comp

    @property
    def uid(self) -> str:
        """
        カレンダーイベント用の一意なUID（重複防止・更新識別用）
        大会の大分類と日付、ホーム/アウェイをベースにハッシュ生成することで
        表記揺れがあっても同一試合で安定したUIDを維持
        """
        side = "home" if self.is_home else "away"
        cat = normalize_competition_category(self.competition)
        raw_key = f"{self.date.strftime('%Y%m%d')}_{cat}_{side}"
        hash_digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"j1-club-antlers-{self.date.strftime('%Y%m%d')}-{hash_digest}@kashima.antlers"

    @property
    def summary(self) -> str:
        """イベントのタイトル（例: J1[H]C大阪【第5節】）"""
        ha = "H" if self.is_home else "A"
        sec_info = f"【{to_halfwidth(self.section)}】" if self.section else ""
        opponent_display = normalize_team_name(self.opponent)
        return f"{self.short_competition}[{ha}]{opponent_display}{sec_info}"

    @property
    def description(self) -> str:
        """イベントの詳細説明"""
        lines = [
            f"大会名: {self.competition}",
        ]
        if self.section:
            lines.append(f"節 / ラウンド: {self.section}")

        lines.append(f"対戦カード: {self.home_team} vs {self.away_team}")

        if self.kickoff_time:
            lines.append(f"キックオフ: {self.kickoff_time.strftime('%H:%M')} (JST)")
        else:
            lines.append("キックオフ: 時間未定（または公式発表待ち）")

        if self.venue:
            lines.append(f"会場: {self.venue}")

        # スコア判定（「vs」や空文字、ハイフンのみでなければ試合結果として表示）
        clean_score = self.score.strip()
        if clean_score and clean_score.lower() != "vs" and clean_score != "-":
            lines.append(f"試合結果: {clean_score}")

        if self.broadcast:
            clean_bc = self.broadcast.replace("\xa0", " ").replace("&nbsp;", " ").strip()
            if clean_bc:
                lines.append(f"TV中継・配信: {clean_bc}")

        if self.detail_url:
            lines.append(f"公式詳細ページ: {self.detail_url}")

        lines.append("")
        lines.append(f"※情報ソース: {self.source}")
        return "\n".join(lines)

    def merge_with(self, other: MatchEvent) -> MatchEvent:
        """
        同一試合の2つのMatchEventを統合し、より充実したMatchEventを生成。
        JFAの正式情報（詳細なラウンド名、正式会場名、PKスコア等）と
        Jリーグデータサイトの情報（放送配信、詳細リンク等）を相互補完。
        """
        # キックオフ時間: 判明している方を優先
        ko = self.kickoff_time or other.kickoff_time

        # 大会名: 正式名称（より詳しい方）を優先
        comp = self.competition if len(self.competition) >= len(other.competition) else other.competition

        # 節 / ラウンド: より具体的なラウンド名（「ラウンド16」「準々決勝」「第1節」等）を優先
        sec = self.section or other.section
        for candidate in [other.section, self.section]:
            if candidate and any(k in candidate for k in ["ラウンド", "回戦", "節", "準", "決勝"]):
                sec = candidate
                break

        # ホーム / アウェイ チーム名: 正式名称・詳細名称を優先（例: セレッソ大阪 > Ｃ大阪）
        home = self.home_team if len(self.home_team) >= len(other.home_team) else other.home_team
        away = self.away_team if len(self.away_team) >= len(other.away_team) else other.away_team

        # 会場名: より詳しいスタジアム名称を優先（例: メルカリスタジアム > メルスタ）
        venue = self.venue if len(self.venue) >= len(other.venue) else other.venue

        # スコア: 具体的なスコアが入っている方を優先
        score = self.score
        if not score or score.lower() in ("vs", "-"):
            score = other.score
        elif other.score and "pk" in other.score.lower() and "pk" not in score.lower():
            score = other.score

        # 中継・放送情報
        broadcast = self.broadcast or other.broadcast

        # 詳細URL
        detail_url = self.detail_url or other.detail_url

        # ソース表記
        src_set = set()
        for s in (self.source, other.source):
            if s:
                src_set.update(s.split(" & "))
        combined_source = " & ".join(sorted(src_set))

        return MatchEvent(
            date=self.date,
            competition=comp,
            section=sec,
            home_team=home,
            away_team=away,
            venue=venue,
            score=score,
            kickoff_time=ko,
            detail_url=detail_url,
            broadcast=broadcast,
            source=combined_source,
        )


class J1ClubScraper:
    """Jリーグ公式データサイトから鹿島アントラーズの試合日程をスクレイピングするクラス"""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

    def fetch_html(self, url: str, params: Optional[dict] = None) -> Optional[str]:
        """指定URLのHTMLを取得（エラーハンドリング付き）"""
        try:
            logger.info(f"[J.League Data Site] アクセス中: {url} (params={params})")
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"HTTPステータス {resp.status_code}: {url}")
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.RequestException as e:
            logger.error(f"リクエストエラー [{url}]: {e}")
            return None

    def get_target_years(self) -> List[int]:
        """
        取得対象となる年度リスト（今年、翌年、シーズン初頭は前年も含む）
        Jリーグの秋春制移行や年度跨ぎに対応
        """
        now = datetime.datetime.now(JST)
        current_year = now.year
        if now.month <= 3:
            return [current_year - 1, current_year, current_year + 1]
        return [current_year, current_year + 1]

    def scrape_all_schedules(self) -> List[MatchEvent]:
        """対象年度の全スケジュールを取得して一覧化"""
        all_events: List[MatchEvent] = []
        target_years = self.get_target_years()
        logger.info(f"[J.League Data Site] 取得対象年度: {target_years}")

        for year in target_years:
            params = {
                "competition_years": str(year),
                "team_ids": CLUB_TEAM_ID,
                "home_away_select": "0",  # 0 = 全て（ホーム＆アウェイ）
            }
            events = self.scrape_year_schedule(JLEAGUE_SEARCH_URL, params)
            all_events.extend(events)
            time.sleep(REQUEST_INTERVAL_SECONDS)

        logger.info(f"[J.League Data Site] 合計 {len(all_events)} 件の日程を抽出しました。")
        return all_events

    def scrape_year_schedule(self, url: str, params: dict) -> List[MatchEvent]:
        """特定年度の検索結果ページからテーブル行を解析"""
        html = self.fetch_html(url, params=params)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        events: List[MatchEvent] = []

        table = soup.find("table", class_="search-table") or soup.find("table", class_="table-base00")
        if not table:
            logger.warning(f"試合結果テーブルが見つかりませんでした: params={params}")
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")

        for tr in rows:
            if tr.find("th") and not tr.find("td"):
                continue

            tds = tr.find_all("td")
            if len(tds) < 8:
                continue

            try:
                event = self._parse_table_row(tds)
                if event:
                    events.append(event)
            except Exception as e:
                logger.error(f"行の解析中にエラーが発生しました: {e}", exc_info=True)

        return events

    def _parse_table_row(self, tds: list) -> Optional[MatchEvent]:
        comp_text = tds[1].get_text(strip=True) if len(tds) > 1 else ""
        sec_text = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        date_text = tds[3].get_text(strip=True) if len(tds) > 3 else ""
        ko_text = tds[4].get_text(strip=True) if len(tds) > 4 else ""
        home_text = tds[5].get_text(strip=True) if len(tds) > 5 else ""
        score_text = tds[6].get_text(strip=True) if len(tds) > 6 else ""
        away_text = tds[7].get_text(strip=True) if len(tds) > 7 else ""
        venue_text = tds[8].get_text(strip=True) if len(tds) > 8 else ""
        broadcast_text = tds[10].get_text(strip=True) if len(tds) > 10 else ""

        if not date_text or not (home_text or away_text):
            return None

        parsed_date = self._parse_date(date_text)
        if not parsed_date:
            return None

        parsed_time = self._parse_kickoff_time(ko_text)

        detail_url = None
        if len(tds) > 6:
            link_a = tds[6].find("a")
            if link_a and link_a.get("href"):
                detail_url = urljoin(JLEAGUE_DATA_BASE_URL, link_a.get("href"))

        return MatchEvent(
            date=parsed_date,
            competition=comp_text,
            section=sec_text,
            home_team=home_text,
            away_team=away_text,
            venue=venue_text,
            score=score_text,
            kickoff_time=parsed_time,
            detail_url=detail_url,
            broadcast=broadcast_text,
            source="J.League Data Site",
        )

    def _parse_date(self, text: str) -> Optional[datetime.date]:
        clean_text = text.translate(str.maketrans("０１２３４５６７８９／", "0123456789/"))
        m = re.search(r"(\d{2,4})[./年](\d{1,2})[./月](\d{1,2})", clean_text)
        if not m:
            return None

        raw_year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + raw_year if raw_year < 100 else raw_year
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None

    def _parse_kickoff_time(self, text: str) -> Optional[datetime.time]:
        if not text:
            return None
        clean_text = text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))
        m = re.search(r"(\d{1,2}):(\d{2})", clean_text)
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return datetime.time(hour, minute)
        return None


class JFAClubScraper:
    """
    JFA（日本サッカー協会）公式サイトから鹿島アントラーズの
    天皇杯およびACL（AFC Champions League Elite / Two）の日程を取得するクラス。
    JFAの標準データAPI（schedule.json）およびHTMLフォールバックにより自動取得。
    """

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

    def fetch_json(self, url: str) -> Optional[dict]:
        """指定URLのJSONを取得"""
        try:
            logger.info(f"[JFA] JSON取得中: {url}")
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.debug(f"[JFA] HTTP {resp.status_code}: {url}")
                return None
            resp.encoding = "utf-8"
            return resp.json()
        except Exception as e:
            logger.debug(f"[JFA] JSON取得スキップ [{url}]: {e}")
            return None

    def fetch_html(self, url: str) -> Optional[str]:
        """指定URLのHTMLを取得"""
        try:
            logger.info(f"[JFA] HTML取得中: {url}")
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.debug(f"[JFA] HTTP {resp.status_code}: {url}")
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as e:
            logger.debug(f"[JFA] HTML取得エラー [{url}]: {e}")
            return None

    def scrape_all_schedules(self, target_years: List[int]) -> List[MatchEvent]:
        """天皇杯およびACLから鹿島アントラーズの試合日程をすべて抽出"""
        events: List[MatchEvent] = []

        logger.info(f"[JFA] 天皇杯・ACL 取得開始 (対象年度: {target_years})")
        # 1. 天皇杯の取得
        emp_events = self.scrape_emperors_cup(target_years)
        events.extend(emp_events)

        # 2. ACL (ACLE, ACL Two, ACL) の取得
        acl_events = self.scrape_acl(target_years)
        events.extend(acl_events)

        logger.info(f"[JFA] 天皇杯 {len(emp_events)} 件, ACL {len(acl_events)} 件, 合計 {len(events)} 件を抽出しました。")
        return events

    # -------------------------------------------------------------
    # 天皇杯 (Emperor's Cup) 取得処理
    # -------------------------------------------------------------
    def scrape_emperors_cup(self, target_years: List[int]) -> List[MatchEvent]:
        """JFA天皇杯スケジュールページおよびJSONから鹿島の試合日程を取得"""
        events: List[MatchEvent] = []
        found_match_keys: Set[str] = set()

        # 大会トップから年度リストを発見
        available_years = self._discover_tournament_years(f"{JFA_BASE_URL}/match/emperorscup/")
        years_to_check = sorted(list(set(target_years + available_years)))

        for year in years_to_check:
            # 1. 公式JSONエンドポイント
            json_url = f"{JFA_BASE_URL}/match/emperorscup_{year}/match/schedule.json"
            data = self.fetch_json(json_url)
            if data:
                parsed = self._parse_jfa_schedule_json(
                    data,
                    default_competition=f"天皇杯 JFA 第{year}回全日本サッカー選手権大会",
                    detail_base_url=f"{JFA_BASE_URL}/match/emperorscup_{year}/",
                    source_label="JFA (天皇杯)",
                )
                for ev in parsed:
                    key = f"{ev.date}_{ev.opponent}"
                    if key not in found_match_keys:
                        found_match_keys.add(key)
                        events.append(ev)
            else:
                # 2. JSONが取得できない場合のHTMLフォールバック
                html_url = f"{JFA_BASE_URL}/match/emperorscup_{year}/schedule_result/"
                html_events = self._parse_jfa_schedule_html(
                    html_url,
                    default_competition="天皇杯",
                    source_label="JFA (天皇杯)",
                )
                for ev in html_events:
                    key = f"{ev.date}_{ev.opponent}"
                    if key not in found_match_keys:
                        found_match_keys.add(key)
                        events.append(ev)

            time.sleep(REQUEST_INTERVAL_SECONDS)

        return events

    # -------------------------------------------------------------
    # ACL (AFC Champions League Elite & Two) 取得処理
    # -------------------------------------------------------------
    def scrape_acl(self, target_years: List[int]) -> List[MatchEvent]:
        """JFA ACLページ群から鹿島の試合日程を取得"""
        events: List[MatchEvent] = []
        found_match_keys: Set[str] = set()

        acl_competitions = [
            ("acl_elite", "AFCチャンピオンズリーグ Elite"),
            ("acl_two", "AFCチャンピオンズリーグ Two"),
            ("acl", "AFCチャンピオンズリーグ"),
        ]

        # 代表的なステージディレクトリ
        common_stages = [
            "east",
            "round16",
            "quarterfinals",
            "semifinals",
            "final",
            "playoff",
            "",
            "groupA",
            "groupB",
            "groupC",
            "groupD",
            "groupE",
            "groupF",
            "groupG",
            "groupH",
        ]

        for comp_slug, comp_title in acl_competitions:
            # トップページから対象年度を発見
            top_url = f"{JFA_BASE_URL}/match/{comp_slug}/"
            available_years = self._discover_tournament_years(top_url)
            years_to_check = sorted(list(set(target_years + available_years)))

            for year in years_to_check:
                # HTMLからステージ一覧（jsonPath）を自動検出
                base_page_url = f"{JFA_BASE_URL}/match/{comp_slug}_{year}/schedule_result/"
                discovered_stages = self._discover_acl_stages(base_page_url)
                stages_to_query = list(dict.fromkeys(discovered_stages + common_stages))

                for stage in stages_to_query:
                    stage_sub = f"{stage}/" if stage else ""
                    json_url = f"{JFA_BASE_URL}/match/{comp_slug}_{year}/{stage_sub}match/schedule.json"
                    data = self.fetch_json(json_url)
                    if data:
                        parsed = self._parse_jfa_schedule_json(
                            data,
                            default_competition=f"{comp_title} {year}/{year+1}",
                            detail_base_url=f"{JFA_BASE_URL}/match/{comp_slug}_{year}/",
                            source_label=f"JFA ({comp_title})",
                        )
                        for ev in parsed:
                            key = f"{ev.date}_{ev.opponent}"
                            if key not in found_match_keys:
                                found_match_keys.add(key)
                                events.append(ev)

                time.sleep(REQUEST_INTERVAL_SECONDS)

        return events

    def _discover_tournament_years(self, url: str) -> List[int]:
        """ページ内のセレクトボックス等から大会年度一覧を検出"""
        html = self.fetch_html(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        years: List[int] = []
        for opt in soup.find_all("option"):
            val = opt.get("value", "")
            text = opt.get_text()
            m = re.search(r"(\d{4})", val) or re.search(r"(\d{4})年", text)
            if m:
                years.append(int(m.group(1)))
        return list(set(years))

    def _discover_acl_stages(self, url: str) -> List[str]:
        """ACL日程ページHTMLのJavaScriptコードからjsonPathステージ名を抽出"""
        html = self.fetch_html(url)
        if not html:
            return []
        stages = []
        # 例: ['east',''], ['round16',''], ['final','']
        matches = re.findall(r"\['([a-zA-Z0-9_-]+)',\s*['\"]", html)
        for stage in matches:
            if stage and stage not in stages:
                stages.append(stage)
        return stages

    def _parse_jfa_schedule_json(
        self,
        data: dict,
        default_competition: str,
        detail_base_url: str,
        source_label: str,
    ) -> List[MatchEvent]:
        """JFAのmatch/schedule.jsonを解析し、鹿島アントラーズの試合を抽出"""
        events: List[MatchEvent] = []
        schedule_list = data.get("matchScheduleList", {})
        comp_name = schedule_list.get("competitionName") or default_competition
        matches = schedule_list.get("matchSchedule", [])

        for item in matches:
            home_name = item.get("homeTeamName", "").strip()
            away_name = item.get("awayTeamName", "").strip()

            # 鹿島アントラーズが参加している試合か判定
            if not (is_kashima_team(home_name) or is_kashima_team(away_name)):
                continue

            date_str = item.get("matchDate", "").strip()
            if not date_str or date_str == "未定":
                continue

            # 日付パース
            clean_date_str = date_str.replace("-", "/").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", clean_date_str)
            if not m:
                continue
            try:
                parsed_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue

            # キックオフ時間パース
            time_str = item.get("matchTime", "").strip()
            parsed_time = None
            if time_str and time_str != "未定":
                tm = re.search(r"(\d{1,2}):(\d{2})", time_str)
                if tm:
                    hour, minute = int(tm.group(1)), int(tm.group(2))
                    if 0 <= hour < 24 and 0 <= minute < 60:
                        parsed_time = datetime.time(hour, minute)

            # 会場
            venue = item.get("venueFullName") or item.get("venue") or ""

            # ラウンド・節
            section = item.get("matchTypeName", "").strip()

            # スコア
            score_dict = item.get("score") or {}
            home_score = str(score_dict.get("homeScore", "")).strip()
            away_score = str(score_dict.get("awayScore", "")).strip()
            score_text = ""
            if home_score and away_score:
                score_text = f"{home_score}-{away_score}"
                home_pk = str(score_dict.get("homePKScore", "")).strip()
                away_pk = str(score_dict.get("awayPKScore", "")).strip()
                if home_pk and away_pk:
                    score_text += f" (PK {home_pk}-{away_pk})"

            # 詳細URL
            match_no = item.get("matchNumber", "")
            official_report = item.get("officialReportURL", "")
            detail_url = None
            if official_report:
                detail_url = urljoin(detail_base_url, official_report)
            elif match_no:
                detail_url = urljoin(detail_base_url, f"match_page/m{match_no}.html")
            else:
                detail_url = urljoin(detail_base_url, "schedule_result/")

            event = MatchEvent(
                date=parsed_date,
                competition=comp_name,
                section=section,
                home_team=home_name,
                away_team=away_name,
                venue=venue,
                score=score_text,
                kickoff_time=parsed_time,
                detail_url=detail_url,
                broadcast="",
                source=source_label,
            )
            events.append(event)

        return events

    def _parse_jfa_schedule_html(
        self,
        url: str,
        default_competition: str,
        source_label: str,
    ) -> List[MatchEvent]:
        """HTMLテーブル構造から鹿島の試合をパースするフォールバック処理"""
        html = self.fetch_html(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        events: List[MatchEvent] = []

        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for tr in rows:
                row_text = tr.get_text()
                if not is_kashima_team(row_text):
                    continue

                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue

                # JFA標準の試合行構造
                try:
                    date_match = re.search(r"(\d{4})[./年](\d{1,2})[./月](\d{1,2})", row_text)
                    if not date_match:
                        continue
                    p_date = datetime.date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))

                    time_match = re.search(r"(\d{1,2}):(\d{2})", row_text)
                    p_time = None
                    if time_match:
                        p_time = datetime.time(int(time_match.group(1)), int(time_match.group(2)))

                    home_team = ""
                    away_team = ""
                    teams = re.findall(r"([^\s\d\-–—vsVS:：/／()（）]+(?:\s[^\s\d\-–—vsVS:：/／()（）]+)*)", row_text)
                    for t in teams:
                        if is_kashima_team(t):
                            home_team = t
                            break

                    events.append(MatchEvent(
                        date=p_date,
                        competition=default_competition,
                        section="",
                        home_team=home_team or CLUB_NAME,
                        away_team=away_team,
                        venue="",
                        score="",
                        kickoff_time=p_time,
                        detail_url=url,
                        broadcast="",
                        source=source_label,
                    ))
                except Exception as e:
                    logger.debug(f"HTML行パーススキップ: {e}")

        return events


# -------------------------------------------------------------
# イベント統合・マージ処理
# -------------------------------------------------------------
def is_same_match(e1: MatchEvent, e2: MatchEvent) -> bool:
    """
    2つのMatchEventが同一試合かどうかを判定
    同日に行われ、かつホーム/アウェイ関係や大会種別・対戦相手が合致するか検証
    """
    if e1.date != e2.date:
        return False

    # 同じ日であれば、鹿島が同じ日に2試合することはないため原則同一試合
    # ホーム/アウェイ関係が一致していれば確実
    if e1.is_home == e2.is_home:
        return True

    # 大会カテゴリ（天皇杯、ACLなど）が一致していれば同一試合
    c1 = normalize_competition_category(e1.competition)
    c2 = normalize_competition_category(e2.competition)
    if c1 != "other" and c1 == c2:
        return True

    return False


def merge_schedule_events(
    jleague_events: List[MatchEvent],
    jfa_events: List[MatchEvent],
) -> List[MatchEvent]:
    """
    Jリーグ公式データサイトのイベント群とJFA公式サイト（天皇杯・ACL）のイベント群をスマートに統合。
    重複する試合は互いの詳細情報を合成して1つにし、新規試合はそのまま追加。
    """
    merged_list: List[MatchEvent] = []
    used_jfa_indices: Set[int] = set()

    # 1. Jリーグイベントをベースに、JFA側に同一試合があればマージ
    for j_ev in jleague_events:
        matched = False
        for idx, jfa_ev in enumerate(jfa_events):
            if idx not in used_jfa_indices and is_same_match(j_ev, jfa_ev):
                merged_ev = j_ev.merge_with(jfa_ev)
                merged_list.append(merged_ev)
                used_jfa_indices.add(idx)
                matched = True
                logger.info(f"試合日程を統合しました: {merged_ev.date} {merged_ev.summary}")
                break
        if not matched:
            merged_list.append(j_ev)

    # 2. Jリーグ側にはなくJFA側にのみ存在する試合（天皇杯の先の日程やACLなど）を追加
    for idx, jfa_ev in enumerate(jfa_events):
        if idx not in used_jfa_indices:
            merged_list.append(jfa_ev)
            logger.info(f"JFAから新規日程を追加しました: {jfa_ev.date} {jfa_ev.summary}")

    # 3. UIDによる最終重複排除（万が一の同日同条件）
    unique_events_dict: Dict[str, MatchEvent] = {}
    for ev in merged_list:
        if ev.uid in unique_events_dict:
            unique_events_dict[ev.uid] = unique_events_dict[ev.uid].merge_with(ev)
        else:
            unique_events_dict[ev.uid] = ev

    # 4. 日付・キックオフ時間順にソート
    sorted_events = sorted(
        unique_events_dict.values(),
        key=lambda e: (e.date, e.kickoff_time or datetime.time.min),
    )
    return sorted_events


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
            "PRODID:-//J1 Kashima Antlers Calendar Bot//JA//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:鹿島アントラーズ 試合日程",
            "X-WR-CALDESC:Jリーグ公式データサイトおよびJFA公式サイトより自動取得した鹿島アントラーズの最新試合日程",
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
                # キックオフ時間が判明している場合（Asia/Tokyo時間帯指定、標準試合時間2時間を設定）
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
    logger.info("=== J1 鹿島アントラーズ 試合日程取得スクリプト 開始 ===")

    # 1. Jリーグ公式データサイトからスクレイピング
    logger.info("--- [1/3] Jリーグ公式データサイトからの日程取得 ---")
    jleague_scraper = J1ClubScraper()
    target_years = jleague_scraper.get_target_years()
    jleague_events = jleague_scraper.scrape_all_schedules()
    logger.info(f"Jリーグ公式データサイト取得結果: {len(jleague_events)} 件")

    # 2. JFA公式サイト（天皇杯およびACL）からスクレイピング
    logger.info("--- [2/3] JFA公式サイト（天皇杯・ACL）からの日程取得 ---")
    jfa_scraper = JFAClubScraper(session=jleague_scraper.session)
    jfa_events = jfa_scraper.scrape_all_schedules(target_years)
    logger.info(f"JFA公式取得結果: {len(jfa_events)} 件")

    # 3. 日程のマージと重複排除
    logger.info("--- [3/3] 日程データのマージ・重複排除処理 ---")
    merged_events = merge_schedule_events(jleague_events, jfa_events)

    if not merged_events:
        logger.warning("試合日程が1件も取得できませんでした。サイト構造が変更された可能性があります。")
        return 0

    logger.info(f"合計 {len(merged_events)} 件の試合日程（Jリーグ: {len(jleague_events)}, JFA: {len(jfa_events)}）からiCalendarデータを生成中...")
    ics_content = ICalGenerator.generate(merged_events)

    with open(OUTPUT_ICS_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(ics_content)

    logger.info(f"保存完了: {OUTPUT_ICS_PATH} ({len(ics_content.encode('utf-8'))} bytes)")
    logger.info("=== 処理が正常に完了しました ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
