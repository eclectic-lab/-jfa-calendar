#!/usr/bin/env python3
"""
Jリーグ公式データサイトからJ1 鹿島アントラーズの最新試合日程・結果を自動取得し、
RFC 5545準拠のiCalendar（.ics）ファイルを自動生成するスクリプト。

- 対象: 鹿島アントラーズ（J1リーグ、ルヴァンカップ、天皇杯など全公式戦）
- タイムゾーン: Asia/Tokyo
- 出力ファイル: j1_club.ics
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional
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
BASE_URL = "https://data.j-league.or.jp"
SEARCH_URL = f"{BASE_URL}/SFMS01/search"
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# サーバー負荷軽減のためのリクエスト間隔（秒）
REQUEST_INTERVAL_SECONDS = 1.0


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

    @property
    def is_home(self) -> bool:
        """鹿島がホームゲームかどうか"""
        return CLUB_SHORT_NAME in self.home_team or CLUB_NAME in self.home_team

    @property
    def opponent(self) -> str:
        """対戦相手チーム名"""
        if self.is_home:
            return self.away_team if self.away_team else "対戦相手未定"
        return self.home_team if self.home_team else "対戦相手未定"

    @property
    def uid(self) -> str:
        """カレンダーイベント用の一意なUID（重複防止・更新識別用）"""
        side = "home" if self.is_home else "away"
        raw_key = f"{self.date.strftime('%Y%m%d')}_{self.competition}_{self.opponent}_{side}"
        hash_digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        return f"jleague-antlers-{self.date.strftime('%Y%m%d')}-{hash_digest}@jleague.jp"

    @property
    def summary(self) -> str:
        """イベントのタイトル（ホームはvs、アウェイは@）"""
        mark = "vs" if self.is_home else "@"
        sec_info = f" {self.section}" if self.section else ""
        return f"【鹿島】{mark} {self.opponent}（{self.competition}{sec_info}）"

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

        # スコア判定（「vs」や空文字でなければ試合結果として表示）
        clean_score = self.score.strip()
        if clean_score and clean_score.lower() != "vs" and clean_score != "-":
            lines.append(f"試合結果: {clean_score}")

        if self.broadcast:
            # &nbsp; 等の特殊文字を除去
            clean_bc = self.broadcast.replace("\xa0", " ").replace("&nbsp;", " ").strip()
            if clean_bc:
                lines.append(f"TV中継・配信: {clean_bc}")

        if self.detail_url:
            lines.append(f"公式詳細ページ: {self.detail_url}")

        lines.append("")
        lines.append("※Jリーグ公式データサイトより自動取得した日程情報です。")
        return "\n".join(lines)


class J1ClubScraper:
    """Jリーグ公式データサイトから鹿島アントラーズの試合日程をスクレイピングするクラス"""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

    def fetch_html(self, url: str, params: Optional[dict] = None) -> Optional[str]:
        """指定URLのHTMLを取得（エラーハンドリング付き）"""
        try:
            logger.info(f"アクセス中: {url} (params={params})")
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
        # 1〜3月は前年のシーズン振り返りや残余日程も考慮して前年も含める
        if now.month <= 3:
            return [current_year - 1, current_year, current_year + 1]
        return [current_year, current_year + 1]

    def scrape_all_schedules(self) -> List[MatchEvent]:
        """対象年度の全スケジュールを取得して一覧化"""
        all_events: List[MatchEvent] = []
        target_years = self.get_target_years()
        logger.info(f"取得対象年度: {target_years}")

        for year in target_years:
            params = {
                "competition_years": str(year),
                "team_ids": CLUB_TEAM_ID,
                "home_away_select": "0",  # 0 = 全て（ホーム＆アウェイ）
            }
            events = self.scrape_year_schedule(SEARCH_URL, params)
            all_events.extend(events)
            time.sleep(REQUEST_INTERVAL_SECONDS)

        # 重複排除（UID基準）＆ 日付順ソート
        unique_events_dict = {event.uid: event for event in all_events}
        sorted_events = sorted(
            unique_events_dict.values(),
            key=lambda e: (e.date, e.kickoff_time or datetime.time.min),
        )
        logger.info(f"合計 {len(sorted_events)} 件の鹿島アントラーズ試合日程を抽出しました。")
        return sorted_events

    def scrape_year_schedule(self, url: str, params: dict) -> List[MatchEvent]:
        """特定年度の検索結果ページからテーブル行を解析"""
        html = self.fetch_html(url, params=params)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        events: List[MatchEvent] = []

        # 検索結果テーブルの特定: table.search-table
        table = soup.find("table", class_="search-table") or soup.find("table", class_="table-base00")
        if not table:
            logger.warning(f"試合結果テーブルが見つかりませんでした: params={params}")
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")

        for tr in rows:
            # ヘッダー行(thのみ)はスキップ
            if tr.find("th") and not tr.find("td"):
                continue

            tds = tr.find_all("td")
            # 必要なカラム数が揃っているかチェック（通常11列）
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
        """
        td要素リストからMatchEventを構築
        J.League Data Site SFMS01テーブルカラム構造:
        [0] シーズン (例: 2026/27, 2025)
        [1] 大会 (例: Ｊ１, ルヴァンカップ)
        [2] 節 (例: 第１節第２日)
        [3] 試合日 (例: 25/02/15(土), 26/08/07(金))
        [4] K/O時刻 (例: 15:03, 未定時は空文字)
        [5] ホーム (例: 湘南, 鹿島)
        [6] スコア (例: 1-0, vs) + 詳細リンク <a href="/SFMS02/?match_card_id=...">
        [7] アウェイ (例: 鹿島, 東京Ｖ)
        [8] スタジアム (例: レモンＳ, カシマ)
        [9] 入場者数
        [10] インターネット中継・TV放送 (例: DAZN)
        """
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

        # 日付パース
        parsed_date = self._parse_date(date_text)
        if not parsed_date:
            logger.debug(f"日付のパースに失敗したためスキップ: {date_text}")
            return None

        # キックオフ時間パース
        parsed_time = self._parse_kickoff_time(ko_text)

        # 詳細ページURLの取得（スコアセル内のリンク）
        detail_url = None
        if len(tds) > 6:
            link_a = tds[6].find("a")
            if link_a and link_a.get("href"):
                detail_url = urljoin(BASE_URL, link_a.get("href"))

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
        )

    def _parse_date(self, text: str) -> Optional[datetime.date]:
        """
        '25/02/15(土)', '2026/08/07(金)', '26/8/7' などの文字列から date オブジェクトを抽出
        2桁年は2000年代として処理
        """
        # 全角数字・スラッシュを半角に変換
        clean_text = text.translate(str.maketrans("０１２３４５６７８９／", "0123456789/"))
        m = re.search(r"(\d{2,4})[./年](\d{1,2})[./月](\d{1,2})", clean_text)
        if not m:
            return None

        raw_year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # 2桁年の場合は2000年代とみなす
        if raw_year < 100:
            year = 2000 + raw_year
        else:
            year = raw_year

        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None

    def _parse_kickoff_time(self, text: str) -> Optional[datetime.time]:
        """'15:03', '19:26' などの文字列から time オブジェクトを抽出"""
        if not text:
            return None

        clean_text = text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))
        m = re.search(r"(\d{1,2}):(\d{2})", clean_text)
        if not m:
            return None

        hour = int(m.group(1))
        minute = int(m.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return datetime.time(hour, minute)
        return None


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
            "X-WR-CALDESC:Jリーグ公式データサイトより自動取得した鹿島アントラーズの最新試合日程",
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
    scraper = J1ClubScraper()
    events = scraper.scrape_all_schedules()

    if not events:
        logger.warning("試合日程が1件も取得できませんでした。サイト構造が変更された可能性があります。")
        # 既存ファイル破壊防止のため正常終了
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
