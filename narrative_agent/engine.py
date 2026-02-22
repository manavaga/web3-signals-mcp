from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold, is_source_enabled


class NarrativeAgent(BaseAgent):
    """
    Scores narrative momentum from Reddit, Twitter, and crypto news.
    Everything is driven by profiles/default.yaml — no hardcoded values.

    Sources (each independently enabled/disabled in YAML):
    1. Reddit (via PRAW) — subreddit + r/all search
    2. Twitter (via Apify) — tweet scraping
    3. CryptoCompare News — headline mentions
    4. CoinGecko Trending — trending coin boost

    Signal: normalised score vs 30D peak. Sweet spot = 0.40–0.70 (from YAML).
    """

    def __init__(self, profile_path: str | None = None, db_path: str = "signals.db") -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))
        self.db_path = db_path
        self.keywords: Dict[str, List[str]] = self.profile.get("asset_keywords", {})

        super().__init__(
            agent_name="narrative_agent",
            profile_name=self.profile.get("name", "narrative_default"),
        )

    def empty_data(self) -> Dict[str, Any]:
        return {
            "by_asset": {sym: self._empty_asset() for sym in self.assets},
            "trending_on_coingecko": [],
            "sources_used": [],
            "summary": {
                "early_pickup": [],
                "too_early": [],
                "peak_crowded": [],
                "no_data": [],
            },
        }

    @staticmethod
    def _empty_asset() -> Dict[str, Any]:
        return {
            "reddit_mentions": 0,
            "twitter_mentions": 0,
            "news_mentions": 0,
            "trending_coingecko": False,
            "total_mentions": 0,
            "normalised_score": 0.0,
            "narrative_condition": False,
            "narrative_status": "unknown",
            "top_headlines": [],
            "sentiment_score": 0.0,
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []

        # Per-asset accumulators
        reddit_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        twitter_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        news_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        trending: List[str] = []

        # --- Source 1: Reddit ---
        if is_source_enabled(self.profile, "reddit"):
            try:
                reddit_counts, reddit_headlines = self._fetch_reddit()
                for sym in self.assets:
                    headlines[sym].extend(reddit_headlines.get(sym, []))
                data["sources_used"].append("reddit")
            except Exception as exc:
                errors.append(f"reddit: {exc}")

        # --- Source 2: Twitter ---
        if is_source_enabled(self.profile, "twitter"):
            try:
                twitter_counts, twitter_headlines = self._fetch_twitter()
                for sym in self.assets:
                    headlines[sym].extend(twitter_headlines.get(sym, []))
                data["sources_used"].append("twitter")
            except Exception as exc:
                errors.append(f"twitter: {exc}")

        # --- Source 3: CryptoCompare News ---
        if is_source_enabled(self.profile, "news"):
            try:
                news_counts, news_headlines = self._fetch_news()
                for sym in self.assets:
                    headlines[sym].extend(news_headlines.get(sym, []))
                data["sources_used"].append("news")
            except Exception as exc:
                errors.append(f"news: {exc}")

        # --- Source 4: CoinGecko Trending ---
        if is_source_enabled(self.profile, "coingecko_trending"):
            try:
                trending = self._fetch_trending()
                data["trending_on_coingecko"] = trending
                data["sources_used"].append("coingecko_trending")
            except Exception as exc:
                errors.append(f"coingecko_trending: {exc}")

        # --- Score each asset ---
        score_min = float(get_threshold(self.profile, "thresholds", "narrative_score_min", default=0.40))
        score_max = float(get_threshold(self.profile, "thresholds", "narrative_score_max", default=0.70))
        peak_days = int(get_threshold(self.profile, "thresholds", "peak_window_days", default=30))
        trending_boost = int(get_threshold(self.profile, "coingecko_trending", "trending_boost", default=20))

        early, too_early, crowded, no_data = [], [], [], []
        sentiment_cfg = self.profile.get("sentiment", {})

        for sym in self.assets:
            rd = reddit_counts.get(sym, 0)
            tw = twitter_counts.get(sym, 0)
            nw = news_counts.get(sym, 0)
            is_trending = sym in trending
            boost = trending_boost if is_trending else 0
            total = rd + tw + nw + boost

            # Compare to rolling peak
            peak = self._load_peak(sym, peak_days)
            if peak is None or peak == 0:
                self._store_count(sym, total)
                peak = max(total, 1)

            normalised = round(min(total / peak, 1.0), 4)

            if total == 0:
                status = "unknown"
                no_data.append(sym)
            elif normalised < score_min:
                status = "too_early"
                too_early.append(sym)
            elif normalised <= score_max:
                status = "early_pickup"
                early.append(sym)
            else:
                status = "peak_crowded"
                crowded.append(sym)

            sentiment = self._score_sentiment(headlines.get(sym, []), sentiment_cfg)

            data["by_asset"][sym] = {
                "reddit_mentions": rd,
                "twitter_mentions": tw,
                "news_mentions": nw,
                "trending_coingecko": is_trending,
                "total_mentions": total,
                "normalised_score": normalised,
                "narrative_condition": status == "early_pickup",
                "narrative_status": status,
                "top_headlines": headlines.get(sym, [])[:5],
                "sentiment_score": sentiment,
            }

            self._store_count(sym, total)

        data["summary"] = {
            "early_pickup": early,
            "too_early": too_early,
            "peak_crowded": crowded,
            "no_data": no_data,
        }

        return data, errors

    # ------------------------------------------------------------------ #
    # Source 1: Reddit (via PRAW)
    # ------------------------------------------------------------------ #

    def _fetch_reddit(self) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        import praw

        cfg = self.profile["reddit"]
        client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
        client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()

        if not client_id or not client_secret:
            raise RuntimeError("REDDIT_CLIENT_ID or REDDIT_CLIENT_SECRET not set")

        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=cfg.get("user_agent", "web3-signal-bot:v1.0"),
        )

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        min_score = int(cfg.get("min_score", 5))
        weight_by_score = bool(cfg.get("weight_by_score", True))
        posts_per_search = int(cfg.get("posts_per_search", 250))
        time_filter = cfg.get("time_filter", "day")
        sort = cfg.get("sort", "new")
        seen_ids: set = set()

        # Search r/all with each keyword
        for keyword in cfg.get("search_keywords", []):
            try:
                for post in reddit.subreddit("all").search(
                    keyword, time_filter=time_filter, sort=sort, limit=posts_per_search
                ):
                    if post.id in seen_ids:
                        continue
                    seen_ids.add(post.id)

                    if post.score < min_score:
                        continue

                    text = f"{post.title} {post.selftext}".lower()
                    weight = post.score if weight_by_score else 1

                    for sym in self.assets:
                        kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                        if any(kw in text for kw in kws):
                            counts[sym] += weight
                            title = post.title[:100]
                            if title and title not in headlines[sym]:
                                headlines[sym].append(title)
            except Exception:
                continue

        return counts, headlines

    # ------------------------------------------------------------------ #
    # Source 2: Twitter (via Apify) — placeholder until cookies configured
    # ------------------------------------------------------------------ #

    def _fetch_twitter(self) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        """
        Twitter via Apify (kaitoeasyapi pay-per-result actor).
        No cookies needed. Requires APIFY_API_KEY env var.
        All config from YAML: actor_id, search_queries, weight tiers, etc.
        """
        cfg = self.profile.get("twitter", {})
        apify_key = os.getenv("APIFY_API_KEY", "").strip()
        if not apify_key:
            raise RuntimeError("APIFY_API_KEY not set")

        actor_id = cfg.get("actor_id", "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest")
        run_timeout = int(cfg.get("run_timeout_sec", 60))
        tweets_per_search = int(cfg.get("tweets_per_search", 20))
        min_likes = int(cfg.get("min_likes", 0))
        weight_by_likes = bool(cfg.get("weight_by_likes", True))
        weight_tiers = cfg.get("weight_tiers", [{"min_likes": 0, "weight": 1}])
        queries = cfg.get("search_queries", [])

        # Sort tiers descending by min_likes for matching
        weight_tiers = sorted(weight_tiers, key=lambda t: t.get("min_likes", 0), reverse=True)

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        seen_ids: set = set()

        url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items?token={apify_key}&timeout={run_timeout}"

        for query in queries:
            try:
                payload = json.dumps({
                    "searchTerms": [query],
                    "maxItems": tweets_per_search,
                    "searchMode": "live",
                }).encode()

                req = Request(url, data=payload, headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                })

                with urlopen(req, timeout=run_timeout + 30) as resp:
                    items = json.loads(resp.read().decode("utf-8"))

                for tweet in items:
                    tweet_id = tweet.get("id", "")
                    if not tweet_id or tweet_id in seen_ids:
                        continue
                    seen_ids.add(tweet_id)

                    # Skip demo/placeholder items
                    if "demo" in tweet and len(tweet) == 1:
                        continue

                    text = str(tweet.get("text", "")).lower()
                    likes = int(tweet.get("likeCount", 0) or 0)

                    if likes < min_likes:
                        continue

                    # Determine weight from YAML tiers
                    weight = 1
                    if weight_by_likes:
                        for tier in weight_tiers:
                            if likes >= int(tier.get("min_likes", 0)):
                                weight = int(tier.get("weight", 1))
                                break

                    # Match assets
                    for sym in self.assets:
                        kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                        if any(kw in text for kw in kws):
                            counts[sym] += weight
                            snippet = str(tweet.get("text", ""))[:100]
                            if snippet and snippet not in headlines[sym]:
                                headlines[sym].append(snippet)

            except Exception:
                continue  # per-query failure is non-fatal

        return counts, headlines

    # ------------------------------------------------------------------ #
    # Source 3: CryptoCompare News
    # ------------------------------------------------------------------ #

    def _fetch_news(self) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        cfg = self.profile["news"]
        url = cfg.get("base_url", "https://min-api.cryptocompare.com/data/v2/news/?lang=EN")
        lookback_hours = int(cfg.get("lookback_hours", 24))

        raw = self._get_json(url)
        articles = raw.get("Data", [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}

        for article in articles:
            pub_ts = int(article.get("published_on", 0))
            if datetime.fromtimestamp(pub_ts, tz=timezone.utc) < cutoff:
                continue

            combined = f"{article.get('title', '')} {article.get('body', '')} {article.get('tags', '')}".lower()

            for sym in self.assets:
                kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                if any(kw in combined for kw in kws):
                    counts[sym] += 1
                    title = str(article.get("title", ""))[:100]
                    if title and title not in headlines[sym]:
                        headlines[sym].append(title)

        return counts, headlines

    # ------------------------------------------------------------------ #
    # Source 4: CoinGecko Trending
    # ------------------------------------------------------------------ #

    def _fetch_trending(self) -> List[str]:
        cfg = self.profile.get("coingecko_trending", {})
        url = cfg.get("base_url", "https://api.coingecko.com/api/v3/search/trending")
        raw = self._get_json(url)
        return [
            str(item.get("item", {}).get("symbol", "")).upper()
            for item in raw.get("coins", [])
            if str(item.get("item", {}).get("symbol", "")).upper() in self.assets
        ]

    # ------------------------------------------------------------------ #
    # Sentiment — keywords from YAML
    # ------------------------------------------------------------------ #

    @staticmethod
    def _score_sentiment(headlines: List[str], cfg: Dict[str, Any]) -> float:
        positive = cfg.get("positive", [])
        negative = cfg.get("negative", [])
        if not headlines:
            return 0.0
        pos = neg = 0
        for h in headlines:
            t = h.lower()
            pos += sum(1 for w in positive if w in t)
            neg += sum(1 for w in negative if w in t)
        total = pos + neg
        return round((pos - neg) / total, 4) if total else 0.0

    # ------------------------------------------------------------------ #
    # Rolling peak storage
    # ------------------------------------------------------------------ #

    def _load_peak(self, symbol: str, days: int) -> Optional[int]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS narrative_peaks "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, "
                    "timestamp TEXT NOT NULL, mention_count INTEGER NOT NULL)"
                )
                row = conn.execute(
                    "SELECT MAX(mention_count) FROM narrative_peaks WHERE symbol=? AND timestamp>=?",
                    (symbol, since),
                ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except Exception:
            return None

    def _store_count(self, symbol: str, count: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS narrative_peaks "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, "
                    "timestamp TEXT NOT NULL, mention_count INTEGER NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO narrative_peaks (symbol, timestamp, mention_count) VALUES (?,?,?)",
                    (symbol, now, count),
                )
                conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # HTTP helper
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
