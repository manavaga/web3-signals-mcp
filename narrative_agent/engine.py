from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time as _time_module
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

import yaml

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold, is_source_enabled


class NarrativeAgent(BaseAgent):
    """
    Scores narrative momentum from 6 data sources with authority weighting,
    influencer tracking, and LLM-based sentiment analysis.

    Sources (each independently enabled/disabled in YAML):
    1. Reddit (via PRAW) — subreddit + r/all search with authority weighting
    2. Twitter/X (via twikit) — free scraping, no API key
    3. Farcaster (via Neynar) — web3-native social layer
    4. CryptoPanic — community-voted news aggregator
    5. Google News RSS — unlimited free news headlines
    6. CoinGecko Trending — trending coin boost
    7. LunarCrush — social metrics (galaxy_score, sentiment, social volume)

    LLM Sentiment: 12-hour batched analysis via Claude Haiku.
    All config driven by profiles/default.yaml.
    """

    def __init__(self, profile_path: str | None = None, db_path: str = "signals.db") -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))
        self.db_path = db_path
        self.keywords: Dict[str, List[str]] = self.profile.get("asset_keywords", {})

        # Load influencer list
        self.influencers = self._load_influencers()

        super().__init__(
            agent_name="narrative_agent",
            profile_name=self.profile.get("name", "narrative_default"),
        )

    def _load_influencers(self) -> List[Dict[str, Any]]:
        """Load influencer list from YAML file."""
        inf_path = self.profile.get("influencer_file", "")
        if not inf_path:
            inf_path = str(Path(__file__).resolve().parent / "data" / "influencers.yaml")
        try:
            with open(inf_path, "r") as f:
                data = yaml.safe_load(f) or {}
            return data.get("influencers", [])
        except Exception:
            return []

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
            "reddit_weighted_mentions": 0,
            "twitter_mentions": 0,
            "farcaster_mentions": 0,
            "cryptopanic_mentions": 0,
            "google_news_mentions": 0,
            "trending_coingecko": False,
            "total_mentions": 0,
            "total_weighted_mentions": 0,
            "normalised_score": 0.0,
            "narrative_condition": False,
            "narrative_status": "unknown",
            "top_headlines": [],
            "keyword_sentiment": 0.0,
            "llm_sentiment": None,
            "llm_events": [],
            "community_sentiment": None,
            "influencer_mentions": 0,
            "top_influencers_active": [],
            "sources_with_data": 0,
            # LunarCrush social metrics
            "lunarcrush_galaxy_score": None,
            "lunarcrush_alt_rank": None,
            "lunarcrush_social_volume": None,
            "lunarcrush_sentiment": None,
            "lunarcrush_social_dominance": None,
            # Composite narrative score
            "narrative_score": 50.0,
            "data_coverage": 0.0,
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []

        # Per-asset accumulators
        reddit_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        reddit_weighted: Dict[str, float] = {sym: 0.0 for sym in self.assets}
        twitter_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        farcaster_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        cryptopanic_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        google_news_counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        community_sentiment: Dict[str, Dict[str, int]] = {
            sym: {"bullish": 0, "bearish": 0, "important": 0} for sym in self.assets
        }
        influencer_hits: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        trending: List[str] = []

        # --- Fetch all 7 sources concurrently ---
        # These fetches are independent and I/O-bound. Running them serially
        # (each with its own per-request urlopen timeout) could exceed the
        # orchestrator's 120s ceiling and cause the whole agent to be killed.
        # Run them in parallel with a per-source wall clock; the total time
        # becomes ~= the slowest source instead of the sum of all sources.
        lunarcrush_data: Dict[str, Dict[str, Any]] = {}

        source_jobs: List[Tuple[str, Any]] = []
        if is_source_enabled(self.profile, "reddit"):
            source_jobs.append(("reddit", self._fetch_reddit))
        if is_source_enabled(self.profile, "twitter"):
            source_jobs.append(("twitter", self._fetch_twitter))
        if is_source_enabled(self.profile, "farcaster"):
            source_jobs.append(("farcaster", self._fetch_farcaster))
        if is_source_enabled(self.profile, "cryptopanic"):
            source_jobs.append(("cryptopanic", self._fetch_cryptopanic))
        if is_source_enabled(self.profile, "google_news"):
            source_jobs.append(("google_news", self._fetch_google_news))
        if is_source_enabled(self.profile, "coingecko_trending"):
            source_jobs.append(("coingecko_trending", self._fetch_trending))
        if self.profile.get("lunarcrush", {}).get("enabled", False):
            source_jobs.append(("lunarcrush", self._fetch_lunarcrush))

        per_source_timeout = int(self.profile.get("source_timeout_sec", 45))
        source_results: Dict[str, Any] = {}
        if source_jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(source_jobs)) as pool:
                future_to_name = {pool.submit(fn): name for name, fn in source_jobs}
                for future, name in future_to_name.items():
                    try:
                        source_results[name] = future.result(timeout=per_source_timeout)
                    except concurrent.futures.TimeoutError:
                        errors.append(f"{name}: timeout after {per_source_timeout}s")
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")

        # --- Assemble results (cheap, in-memory; preserves source ordering) ---
        if "reddit" in source_results:
            reddit_counts, reddit_weighted, reddit_headlines = source_results["reddit"]
            for sym in self.assets:
                headlines[sym].extend(reddit_headlines.get(sym, []))
            data["sources_used"].append("reddit")

        if "twitter" in source_results:
            twitter_counts, twitter_headlines, tw_influencers = source_results["twitter"]
            for sym in self.assets:
                headlines[sym].extend(twitter_headlines.get(sym, []))
                influencer_hits[sym].extend(tw_influencers.get(sym, []))
            data["sources_used"].append("twitter")

        if "farcaster" in source_results:
            farcaster_counts, fc_headlines, fc_influencers = source_results["farcaster"]
            for sym in self.assets:
                headlines[sym].extend(fc_headlines.get(sym, []))
                influencer_hits[sym].extend(fc_influencers.get(sym, []))
            data["sources_used"].append("farcaster")

        if "cryptopanic" in source_results:
            cryptopanic_counts, cp_headlines, community_sentiment = source_results["cryptopanic"]
            for sym in self.assets:
                headlines[sym].extend(cp_headlines.get(sym, []))
            data["sources_used"].append("cryptopanic")

        if "google_news" in source_results:
            google_news_counts, gn_headlines = source_results["google_news"]
            for sym in self.assets:
                headlines[sym].extend(gn_headlines.get(sym, []))
            data["sources_used"].append("google_news")

        if "coingecko_trending" in source_results:
            trending = source_results["coingecko_trending"]
            data["trending_on_coingecko"] = trending
            data["sources_used"].append("coingecko_trending")

        if "lunarcrush" in source_results:
            lunarcrush_data = source_results["lunarcrush"]
            if lunarcrush_data:
                data["sources_used"].append("lunarcrush")

        # --- Score each asset ---
        score_min = float(get_threshold(self.profile, "thresholds", "narrative_score_min", default=0.40))
        score_max = float(get_threshold(self.profile, "thresholds", "narrative_score_max", default=0.70))
        peak_days = int(get_threshold(self.profile, "thresholds", "peak_window_days", default=30))
        trending_boost = int(get_threshold(self.profile, "coingecko_trending", "trending_boost", default=20))

        early, too_early, crowded, no_data = [], [], [], []
        sentiment_cfg = self.profile.get("sentiment", {})

        for sym in self.assets:
            rd = reddit_counts.get(sym, 0)
            rd_w = reddit_weighted.get(sym, 0.0)
            tw = twitter_counts.get(sym, 0)
            fc = farcaster_counts.get(sym, 0)
            cp = cryptopanic_counts.get(sym, 0)
            gn = google_news_counts.get(sym, 0)
            is_trending = sym in trending
            boost = trending_boost if is_trending else 0

            total = rd + tw + fc + cp + gn + boost
            total_weighted = rd_w + tw + fc + cp + gn + boost

            # Count sources with data
            sources_with_data = sum(1 for c in [rd, tw, fc, cp, gn] if c > 0)
            if is_trending:
                sources_with_data += 1

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

            keyword_sent = self._score_sentiment(headlines.get(sym, []), sentiment_cfg)

            # Load cached LLM sentiment and events (from 12h cycle)
            llm_sent = self._load_cached_llm_sentiment(sym)
            llm_events = self._load_cached_llm_events(sym)

            # Community sentiment from CryptoPanic
            cs = community_sentiment.get(sym, {})
            cs_total = cs.get("bullish", 0) + cs.get("bearish", 0)
            cs_score = None
            if cs_total > 0:
                cs_score = round((cs["bullish"] - cs["bearish"]) / cs_total, 4)

            # Influencer mentions
            inf_list = influencer_hits.get(sym, [])

            # LunarCrush data for this asset
            lc = lunarcrush_data.get(sym, {})
            lc_galaxy = lc.get("galaxy_score")
            lc_alt_rank = lc.get("alt_rank")
            lc_social_vol = lc.get("social_volume")
            lc_sentiment = lc.get("sentiment")
            lc_social_dom = lc.get("social_dominance")
            has_lunarcrush = lc_galaxy is not None

            # --- Compute composite narrative_score (0-100) ---
            narrative_score = self._compute_narrative_score(
                normalised=normalised,
                status=status,
                keyword_sent=keyword_sent,
                lc_galaxy=lc_galaxy,
                cs_score=cs_score,
                llm_sent=llm_sent,
                total_mentions=total,
                has_lunarcrush=has_lunarcrush,
            )

            # Data coverage: count sources with actual data / total possible sources
            # Possible sources: reddit, twitter, farcaster, cryptopanic, google_news, coingecko_trending, lunarcrush
            total_possible_sources = 7
            coverage_count = sources_with_data + (1 if has_lunarcrush else 0)
            data_coverage = round(coverage_count / total_possible_sources, 4)

            data["by_asset"][sym] = {
                "reddit_mentions": rd,
                "reddit_weighted_mentions": round(rd_w, 1),
                "twitter_mentions": tw,
                "farcaster_mentions": fc,
                "cryptopanic_mentions": cp,
                "google_news_mentions": gn,
                "trending_coingecko": is_trending,
                "total_mentions": total,
                "total_weighted_mentions": round(total_weighted, 1),
                "normalised_score": normalised,
                "narrative_condition": status == "early_pickup",
                "narrative_status": status,
                "top_headlines": headlines.get(sym, [])[:8],
                "keyword_sentiment": keyword_sent,
                "llm_sentiment": llm_sent,
                "llm_events": llm_events or [],
                "community_sentiment": {
                    "bullish": cs.get("bullish", 0),
                    "bearish": cs.get("bearish", 0),
                    "important": cs.get("important", 0),
                    "score": cs_score,
                } if cs_total > 0 else None,
                "influencer_mentions": len(inf_list),
                "top_influencers_active": inf_list[:5],
                "sources_with_data": sources_with_data,
                # LunarCrush social metrics
                "lunarcrush_galaxy_score": lc_galaxy,
                "lunarcrush_alt_rank": lc_alt_rank,
                "lunarcrush_social_volume": lc_social_vol,
                "lunarcrush_sentiment": lc_sentiment,
                "lunarcrush_social_dominance": lc_social_dom,
                # Composite narrative score
                "narrative_score": narrative_score,
                "data_coverage": data_coverage,
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
    # Source 1: Reddit (via PRAW) — with authority weighting
    # ------------------------------------------------------------------ #

    def _fetch_reddit(self) -> Tuple[Dict[str, int], Dict[str, float], Dict[str, List[str]]]:
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
        weighted: Dict[str, float] = {sym: 0.0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        min_score = int(cfg.get("min_score", 5))
        posts_per_search = int(cfg.get("posts_per_search", 250))
        time_filter = cfg.get("time_filter", "day")
        sort = cfg.get("sort", "new")
        seen_ids: set = set()

        # Authority weighting config
        auth_cfg = cfg.get("authority", {})
        auth_enabled = auth_cfg.get("enabled", True)
        min_account_age_days = int(auth_cfg.get("min_account_age_days", 30))
        karma_tiers = auth_cfg.get("karma_tiers", [
            {"min_karma": 0, "weight": 1.0},
            {"min_karma": 1000, "weight": 1.5},
            {"min_karma": 10000, "weight": 2.0},
            {"min_karma": 50000, "weight": 3.0},
        ])
        karma_tiers = sorted(karma_tiers, key=lambda t: t.get("min_karma", 0), reverse=True)
        mod_bonus = float(auth_cfg.get("mod_bonus", 1.5))
        verified_bonus = float(auth_cfg.get("verified_bonus", 1.2))

        def _author_weight(post) -> float:
            """Calculate authority weight for a Reddit author."""
            if not auth_enabled:
                return float(post.score) if cfg.get("weight_by_score", True) else 1.0

            base = 1.0
            try:
                author = post.author
                if author is None:
                    return base

                # Account age filter
                created = getattr(author, "created_utc", 0)
                if created:
                    age_days = (datetime.now(timezone.utc) - datetime.fromtimestamp(created, tz=timezone.utc)).days
                    if age_days < min_account_age_days:
                        return 0.0  # Too new, skip

                # Karma-based tier
                total_karma = getattr(author, "comment_karma", 0) + getattr(author, "link_karma", 0)
                for tier in karma_tiers:
                    if total_karma >= int(tier.get("min_karma", 0)):
                        base = float(tier.get("weight", 1.0))
                        break

                # Mod bonus
                if getattr(author, "is_mod", False):
                    base *= mod_bonus

                # Verified email bonus
                if getattr(author, "has_verified_email", False):
                    base *= verified_bonus

            except Exception:
                pass

            # Post engagement multiplier
            engagement = 1.0 + (post.score / 100.0)  # Mild engagement boost
            return base * min(engagement, 5.0)  # Cap at 5x

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
                    weight = _author_weight(post)

                    if weight <= 0:
                        continue

                    for sym in self.assets:
                        kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                        if any(kw in text for kw in kws):
                            counts[sym] += 1
                            weighted[sym] += weight
                            title = post.title[:100]
                            if title and title not in headlines[sym]:
                                headlines[sym].append(title)
            except Exception:
                continue

        return counts, weighted, headlines

    # ------------------------------------------------------------------ #
    # Source 2: Twitter/X (via twikit) — free, no API key
    # ------------------------------------------------------------------ #

    def _fetch_twitter(self) -> Tuple[Dict[str, int], Dict[str, List[str]], Dict[str, List[str]]]:
        """
        Twitter via twikit (free scraper using Twitter internal API).
        Requires TWITTER_USERNAME, TWITTER_EMAIL, TWITTER_PASSWORD env vars.
        """
        cfg = self.profile.get("twitter", {})
        username = os.getenv("TWITTER_USERNAME", "").strip()
        email = os.getenv("TWITTER_EMAIL", "").strip()
        password = os.getenv("TWITTER_PASSWORD", "").strip()

        if not username or not password:
            raise RuntimeError("TWITTER_USERNAME or TWITTER_PASSWORD not set")

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        influencer_hits: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        seen_ids: set = set()

        tweets_per_query = int(cfg.get("tweets_per_query", 20))
        queries = cfg.get("search_queries", [])
        cookie_path = cfg.get("cookie_path", "/tmp/twikit_cookies.json")

        # Build influencer lookup for Twitter
        tw_influencers = {
            inf["handle"].lower(): inf
            for inf in self.influencers
            if inf.get("platform") == "twitter"
        }

        try:
            from twikit import Client as TwikitClient

            # Create a dedicated event loop for this thread (handles non-main threads)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            client = TwikitClient("en-US")

            # Try to load cookies first
            try:
                client.load_cookies(cookie_path)
            except Exception:
                # Login fresh
                loop.run_until_complete(
                    client.login(auth_info_1=username, auth_info_2=email, password=password)
                )
                try:
                    client.save_cookies(cookie_path)
                except Exception:
                    pass

            for query in queries:
                try:
                    result = loop.run_until_complete(
                        client.search_tweet(query, "Latest", count=tweets_per_query)
                    )

                    if not result:
                        continue

                    for tweet in result:
                        tweet_id = getattr(tweet, "id", "")
                        if not tweet_id or tweet_id in seen_ids:
                            continue
                        seen_ids.add(tweet_id)

                        text = str(getattr(tweet, "text", "")).lower()
                        author = getattr(tweet, "user", None)
                        screen_name = ""
                        if author:
                            screen_name = str(getattr(author, "screen_name", "")).lower()

                        for sym in self.assets:
                            kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                            if any(kw in text for kw in kws):
                                counts[sym] += 1
                                snippet = str(getattr(tweet, "text", ""))[:100]
                                if snippet and snippet not in headlines[sym]:
                                    headlines[sym].append(snippet)

                                # Check if author is a known influencer
                                if screen_name in tw_influencers:
                                    inf = tw_influencers[screen_name]
                                    handle = f"@{inf['handle']}"
                                    if handle not in influencer_hits[sym]:
                                        influencer_hits[sym].append(handle)

                except Exception:
                    continue

            loop.close()

        except ImportError:
            raise RuntimeError("twikit not installed — pip install twikit")

        return counts, headlines, influencer_hits

    # ------------------------------------------------------------------ #
    # Source 3: Farcaster (via Neynar)
    # ------------------------------------------------------------------ #

    def _fetch_farcaster(self) -> Tuple[Dict[str, int], Dict[str, List[str]], Dict[str, List[str]]]:
        cfg = self.profile.get("farcaster", {})
        api_key = os.getenv("NEYNAR_API_KEY", "").strip()

        if not api_key:
            raise RuntimeError("NEYNAR_API_KEY not set")

        base_url = cfg.get("base_url", "https://api.neynar.com/v2/farcaster/cast/search")
        limit = int(cfg.get("results_per_query", 25))

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        influencer_hits: Dict[str, List[str]] = {sym: [] for sym in self.assets}

        # Build Farcaster influencer lookup
        fc_influencers = {
            inf["handle"].lower(): inf
            for inf in self.influencers
            if inf.get("platform") == "farcaster"
        }

        queries = cfg.get("search_queries", [])
        if not queries:
            # Default: search each asset
            queries = [sym.lower() for sym in self.assets[:10]]

        for query in queries:
            try:
                url = f"{base_url}?q={quote_plus(query)}&limit={limit}"
                req = Request(url, headers={
                    "accept": "application/json",
                    "x-api-key": api_key,
                })
                with urlopen(req, timeout=self.timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))

                casts = result.get("result", {}).get("casts", [])
                for cast in casts:
                    text = str(cast.get("text", "")).lower()
                    author = cast.get("author", {})
                    fc_username = str(author.get("username", "")).lower()

                    for sym in self.assets:
                        kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                        if any(kw in text for kw in kws):
                            counts[sym] += 1
                            snippet = str(cast.get("text", ""))[:100]
                            if snippet and snippet not in headlines[sym]:
                                headlines[sym].append(snippet)

                            if fc_username in fc_influencers:
                                inf = fc_influencers[fc_username]
                                handle = f"@{inf['handle']}"
                                if handle not in influencer_hits[sym]:
                                    influencer_hits[sym].append(handle)

            except Exception:
                continue

        return counts, headlines, influencer_hits

    # ------------------------------------------------------------------ #
    # Source 4: CryptoPanic — community-voted news
    # ------------------------------------------------------------------ #

    def _fetch_cryptopanic(self) -> Tuple[Dict[str, int], Dict[str, List[str]], Dict[str, Dict[str, int]]]:
        cfg = self.profile.get("cryptopanic", {})
        api_key = os.getenv("CRYPTOPANIC_API_KEY", "").strip()

        if not api_key:
            raise RuntimeError("CRYPTOPANIC_API_KEY not set")

        base_url = cfg.get("base_url", "https://cryptopanic.com/api/v1/posts/")
        filter_type = cfg.get("filter", "hot")

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}
        community: Dict[str, Dict[str, int]] = {
            sym: {"bullish": 0, "bearish": 0, "important": 0} for sym in self.assets
        }

        # CryptoPanic uses currency codes directly
        currency_map = cfg.get("currency_map", {})

        try:
            url = f"{base_url}?auth_token={api_key}&filter={filter_type}&public=true"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            posts = result.get("results", [])
            for post in posts:
                title = str(post.get("title", ""))
                text = title.lower()

                # Get currencies this post is tagged with
                currencies = post.get("currencies", []) or []
                tagged_syms = [str(c.get("code", "")).upper() for c in currencies]

                # Votes
                votes = post.get("votes", {})
                bullish = int(votes.get("positive", 0) or 0)
                bearish = int(votes.get("negative", 0) or 0)
                important = int(votes.get("important", 0) or 0)

                for sym in self.assets:
                    # Match by CryptoPanic currency tag or keyword search
                    cp_code = currency_map.get(sym, sym)
                    matched = cp_code in tagged_syms

                    if not matched:
                        kws = [k.lower() for k in self.keywords.get(sym, [sym.lower()])]
                        matched = any(kw in text for kw in kws)

                    if matched:
                        counts[sym] += 1
                        if title[:100] not in headlines[sym]:
                            headlines[sym].append(title[:100])
                        community[sym]["bullish"] += bullish
                        community[sym]["bearish"] += bearish
                        community[sym]["important"] += important

        except Exception:
            pass

        return counts, headlines, community

    # ------------------------------------------------------------------ #
    # Source 5: Google News RSS — free, unlimited
    # ------------------------------------------------------------------ #

    def _fetch_google_news(self) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
        cfg = self.profile.get("google_news", {})
        base_url = cfg.get("base_url", "https://news.google.com/rss/search")
        asset_names = cfg.get("asset_search_names", {})

        counts: Dict[str, int] = {sym: 0 for sym in self.assets}
        headlines: Dict[str, List[str]] = {sym: [] for sym in self.assets}

        for sym in self.assets:
            search_name = asset_names.get(sym, sym)
            query = f"{search_name} crypto"
            url = f"{base_url}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"

            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=self.timeout) as resp:
                    xml_data = resp.read().decode("utf-8")

                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                max_items = int(cfg.get("max_items_per_asset", 20))

                for item in items[:max_items]:
                    title_el = item.find("title")
                    if title_el is None or not title_el.text:
                        continue
                    title = title_el.text.strip()[:120]
                    counts[sym] += 1
                    if title not in headlines[sym]:
                        headlines[sym].append(title)

            except Exception:
                continue

        return counts, headlines

    # ------------------------------------------------------------------ #
    # Source 6: CoinGecko Trending
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
    # Keyword-based sentiment (fast, every cycle)
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
    # LLM Sentiment — 12-hour batch cycle (called from orchestrator)
    # ------------------------------------------------------------------ #

    def _filter_headlines(self, headlines: List[str]) -> List[str]:
        """Score and filter headlines by news-worthiness before LLM processing.

        Prioritizes material news (regulatory, hacks, partnerships) over
        generic discussion posts ("should I buy BTC?"). Config-driven from
        YAML ``llm_sentiment.headline_filter``.
        """
        filter_cfg = self.profile.get("llm_sentiment", {}).get("headline_filter", {})
        if not filter_cfg.get("enabled", False) or not headlines:
            return headlines

        max_count = int(filter_cfg.get("max_per_asset", 10))
        min_score = float(filter_cfg.get("min_relevance_score", -3.0))
        boost_keywords = filter_cfg.get("boost_keywords", [])
        noise_patterns = filter_cfg.get("noise_patterns", [])
        question_penalty = float(filter_cfg.get("question_penalty", -3.0))
        short_penalty = float(filter_cfg.get("short_headline_penalty", -2.0))
        short_threshold = int(filter_cfg.get("short_headline_threshold", 20))
        number_bonus = float(filter_cfg.get("number_bonus", 1.0))

        scored: List[Tuple[float, str]] = []
        for h in headlines:
            text = h.lower()
            score = 0.0

            # Boost for event-signal keywords
            for kw in boost_keywords:
                if kw in text:
                    score += 2.0

            # Penalize noise patterns
            for pattern in noise_patterns:
                if pattern in text:
                    score -= 5.0

            # Penalize questions (usually discussion, not news)
            if text.rstrip().endswith("?"):
                score += question_penalty

            # Penalize very short headlines
            if len(text) < short_threshold:
                score += short_penalty

            # Bonus for containing numbers (specific data points)
            if number_bonus > 0 and any(c.isdigit() for c in text):
                score += number_bonus

            scored.append((score, h))

        # Sort by relevance score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Drop below minimum threshold, take top N
        return [h for s, h in scored if s >= min_score][:max_count]

    def run_llm_sentiment(self, store) -> Dict[str, Any]:
        """
        Run LLM-based sentiment analysis on all collected headlines.
        Called by the orchestrator every 12 hours (not every 15-min cycle).
        Saves results to storage for caching.
        """
        llm_cfg = self.profile.get("llm_sentiment", {})
        if not llm_cfg.get("enabled", True):
            return {"skipped": True, "reason": "disabled"}

        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return {"skipped": True, "reason": "ANTHROPIC_API_KEY not set"}

        model = llm_cfg.get("model", "claude-haiku-4-5-20251001")
        max_tokens = int(llm_cfg.get("max_tokens", 2048))

        # Collect the latest narrative data
        latest = store.load_latest("narrative_agent")
        if not latest:
            return {"skipped": True, "reason": "no narrative data"}

        by_asset = latest.get("data", {}).get("by_asset", {})
        results = {}

        # Batch all assets into one LLM call for cost efficiency
        # Pre-filter headlines to remove noise before sending to LLM
        batch_input = {}
        total_raw = 0
        total_filtered = 0
        for sym in self.assets:
            asset_data = by_asset.get(sym, {})
            all_headlines = asset_data.get("top_headlines", [])
            if all_headlines:
                total_raw += len(all_headlines)
                filtered = self._filter_headlines(all_headlines)
                total_filtered += len(filtered)
                if filtered:
                    batch_input[sym] = filtered

        if not batch_input:
            return {"skipped": True, "reason": "no headlines to analyze"}

        system_prompt = llm_cfg.get("system_prompt", (
            "You are a crypto market sentiment analyst. Analyze headlines for each "
            "cryptocurrency and provide structured sentiment analysis."
        ))

        # Event types for classification
        event_types = llm_cfg.get("event_types", [
            "regulatory", "hack_exploit", "partnership", "listing",
            "upgrade", "adoption", "market_event", "general_sentiment",
        ])

        user_prompt = (
            "Analyze the following crypto headlines per asset and return a JSON object. "
            "For each asset, provide:\n"
            "- sentiment: float from -1.0 (very bearish) to 1.0 (very bullish)\n"
            "- confidence: float from 0.0 to 1.0\n"
            "- dominant_narrative: string (1-3 words describing the main narrative)\n"
            "- narrative_topics: list of string tags (e.g. ['etf', 'regulation', 'defi'])\n"
            "- tone: 'bullish', 'bearish', or 'neutral'\n"
            "- events: list of significant events extracted from headlines. "
            "Each event is an object with:\n"
            "  - type: one of " + json.dumps(event_types) + "\n"
            "  - headline: the original headline (shortened)\n"
            "  - impact: 'bullish' or 'bearish'\n"
            "  - magnitude: 'critical', 'high', 'medium', or 'low'\n"
            "  - confidence: float 0.0-1.0 (how confident you are in the classification)\n"
            "\nOnly include events that represent MATERIAL news (not generic sentiment). "
            "A regulatory ruling, ETF approval, major hack, or institutional adoption "
            "is an event. Generic 'BTC price analysis' is NOT an event.\n\n"
            "Headlines by asset:\n"
            f"{json.dumps(batch_input, indent=1)}\n\n"
            "Return ONLY valid JSON like: {\"BTC\": {\"sentiment\": 0.5, \"events\": [...], ...}, ...}"
        )

        try:
            url = "https://api.anthropic.com/v1/messages"
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            data = json.dumps(payload).encode()
            req = Request(url, data=data, headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            })
            with urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode())

            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""

            # Parse JSON from response
            # Try to extract JSON from response (handle markdown code blocks)
            json_text = text
            if "```" in json_text:
                # Extract content between code blocks
                parts = json_text.split("```")
                for part in parts:
                    stripped = part.strip()
                    if stripped.startswith("json"):
                        stripped = stripped[4:].strip()
                    if stripped.startswith("{"):
                        json_text = stripped
                        break

            results = json.loads(json_text)

            # Save to storage with timestamp
            store.save_kv_json("llm_sentiment", "latest", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "results": results,
            })

            return {
                "success": True,
                "assets_analyzed": len(results),
                "headlines_raw": total_raw,
                "headlines_filtered": total_filtered,
            }

        except Exception as exc:
            return {"error": str(exc)}

    def _load_cached_llm_sentiment(self, asset: str) -> Optional[Dict[str, Any]]:
        """Load cached LLM sentiment for an asset from storage."""
        try:
            from shared.storage import Storage
            store = Storage()
            cached = store.load_kv_json("llm_sentiment", "latest")
            if not cached:
                return None

            # Check staleness (default 24h max)
            ts = cached.get("timestamp", "")
            if ts:
                cached_time = datetime.fromisoformat(ts)
                max_age_hours = int(self.profile.get("llm_sentiment", {}).get("max_age_hours", 24))
                if (datetime.now(timezone.utc) - cached_time).total_seconds() > max_age_hours * 3600:
                    return None

            results = cached.get("results", {})
            return results.get(asset)
        except Exception:
            return None

    def _load_cached_llm_events(self, asset: str) -> Optional[List[Dict[str, Any]]]:
        """Load cached LLM events for an asset from storage."""
        try:
            from shared.storage import Storage
            store = Storage()
            cached = store.load_kv_json("llm_sentiment", "latest")
            if not cached:
                return None

            # Check staleness (same as sentiment)
            ts = cached.get("timestamp", "")
            if ts:
                cached_time = datetime.fromisoformat(ts)
                max_age_hours = int(self.profile.get("llm_sentiment", {}).get("max_age_hours", 24))
                if (datetime.now(timezone.utc) - cached_time).total_seconds() > max_age_hours * 3600:
                    return None

            results = cached.get("results", {})
            asset_data = results.get(asset)
            if asset_data and isinstance(asset_data, dict):
                events = asset_data.get("events", [])
                if isinstance(events, list):
                    return events
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Source 7: LunarCrush Social Metrics
    # ------------------------------------------------------------------ #

    def _fetch_lunarcrush(self) -> Dict[str, Dict[str, Any]]:
        """Fetch social metrics from LunarCrush v2 API for all tracked assets.

        Returns a dict keyed by asset symbol with galaxy_score, alt_rank,
        social_volume, sentiment, social_dominance, and social_contributors.
        Skips gracefully if API key is not set or calls fail.
        """
        lc_cfg = self.profile.get("lunarcrush", {})
        api_key = os.getenv(lc_cfg.get("api_key_env", "LUNARCRUSH_API_KEY"), "").strip()

        if not api_key:
            raise RuntimeError("LUNARCRUSH_API_KEY not set — skipping LunarCrush")

        base_url = lc_cfg.get("base_url", "https://lunarcrush.com/api/v2")
        rate_limit_sleep = float(lc_cfg.get("rate_limit_sleep", 0.5))

        results: Dict[str, Dict[str, Any]] = {}

        # Try to batch all symbols in one call first
        symbols_str = ",".join(self.assets)
        batch_url = f"{base_url}/assets?data=assets&symbol={symbols_str}"

        try:
            req = Request(batch_url, headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "web3-signal-bot/1.0",
            })
            with urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))

            assets_data = raw.get("data", [])
            if isinstance(assets_data, list):
                for item in assets_data:
                    sym = str(item.get("symbol", "")).upper()
                    if sym in self.assets:
                        results[sym] = {
                            "galaxy_score": item.get("galaxy_score"),
                            "alt_rank": item.get("alt_rank"),
                            "social_volume": item.get("social_volume"),
                            "social_score": item.get("social_score"),
                            "social_contributors": item.get("social_contributors"),
                            "sentiment": item.get("sentiment"),
                            "social_dominance": item.get("social_dominance"),
                        }

            # If batch worked, return
            if results:
                return results

        except Exception:
            pass  # Fall through to per-asset calls

        # Fallback: per-asset calls with rate limiting
        for sym in self.assets:
            try:
                url = f"{base_url}/assets?data=assets&symbol={sym}"
                req = Request(url, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "User-Agent": "web3-signal-bot/1.0",
                })
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))

                assets_data = raw.get("data", [])
                if isinstance(assets_data, list) and assets_data:
                    item = assets_data[0]
                    results[sym] = {
                        "galaxy_score": item.get("galaxy_score"),
                        "alt_rank": item.get("alt_rank"),
                        "social_volume": item.get("social_volume"),
                        "social_score": item.get("social_score"),
                        "social_contributors": item.get("social_contributors"),
                        "sentiment": item.get("sentiment"),
                        "social_dominance": item.get("social_dominance"),
                    }

                _time_module.sleep(rate_limit_sleep)

            except Exception:
                continue  # Skip this asset, continue with others

        return results

    # ------------------------------------------------------------------ #
    # Composite narrative_score (0-100)
    # ------------------------------------------------------------------ #

    def _compute_narrative_score(
        self,
        normalised: float,
        status: str,
        keyword_sent: float,
        lc_galaxy: Optional[float],
        cs_score: Optional[float],
        llm_sent: Optional[Dict[str, Any]],
        total_mentions: int,
        has_lunarcrush: bool,
    ) -> float:
        """Compute a composite narrative score (0-100) from all available signals.

        When LunarCrush is unavailable, its weight is redistributed to
        mentions and keyword_sentiment. When there is no data at all,
        returns 50 (true neutral).
        """
        scoring_cfg = self.profile.get("scoring", {})
        weights_cfg = scoring_cfg.get("weights", {})
        no_lc_cfg = scoring_cfg.get("no_lunarcrush_redistribution", {})

        # --- Sub-score 1: Mention score based on normalised_score & status ---
        if status == "early_pickup":
            # Sweet spot: growing attention (normalised 0.4-0.7)
            # Map 0.4-0.7 range to 60-75
            t = max(0.0, min(1.0, (normalised - 0.4) / 0.3))
            mention_score = 60.0 + t * 15.0
        elif status == "peak_crowded":
            # Already priced in (normalised > 0.7)
            # Map 0.7-1.0 range to 55-40 (higher normalised = lower score)
            t = max(0.0, min(1.0, (normalised - 0.7) / 0.3))
            mention_score = 55.0 - t * 15.0
        elif status == "too_early":
            # Not enough signal (normalised < 0.4)
            # Map 0.0-0.4 range to 45-55
            t = max(0.0, min(1.0, normalised / 0.4))
            mention_score = 45.0 + t * 10.0
        else:
            # unknown / no data
            mention_score = 50.0

        # --- Sub-score 2: Keyword sentiment (-1.0 to +1.0 -> 20-80) ---
        # +1.0 -> 80, 0.0 -> 50, -1.0 -> 20
        keyword_sentiment_score = 50.0 + keyword_sent * 30.0
        keyword_sentiment_score = max(20.0, min(80.0, keyword_sentiment_score))

        # --- Sub-score 3: LunarCrush galaxy_score (already 0-100) ---
        lunarcrush_score = 50.0
        if has_lunarcrush and lc_galaxy is not None:
            lunarcrush_score = max(0.0, min(100.0, float(lc_galaxy)))

        # --- Sub-score 4: Community sentiment from CryptoPanic ---
        community_score = 50.0
        if cs_score is not None:
            # Map -1.0 to +1.0 -> 20-80
            community_score = 50.0 + float(cs_score) * 30.0
            community_score = max(20.0, min(80.0, community_score))

        # --- Sub-score 5: LLM sentiment ---
        llm_sentiment_score = 50.0
        if llm_sent is not None and isinstance(llm_sent, dict):
            llm_val = llm_sent.get("sentiment")
            if llm_val is not None:
                # Map -1.0 to +1.0 -> 20-80
                llm_sentiment_score = 50.0 + float(llm_val) * 30.0
                llm_sentiment_score = max(20.0, min(80.0, llm_sentiment_score))

        # --- Weighted average ---
        if has_lunarcrush:
            w_mention = float(weights_cfg.get("mentions", 0.25))
            w_keyword = float(weights_cfg.get("keyword_sentiment", 0.20))
            w_lunar = float(weights_cfg.get("lunarcrush", 0.25))
            w_community = float(weights_cfg.get("community", 0.10))
            w_llm = float(weights_cfg.get("llm_sentiment", 0.20))
        else:
            # Redistribute LunarCrush weight to mentions and keyword_sentiment
            w_mention = float(no_lc_cfg.get("mentions", 0.35))
            w_keyword = float(no_lc_cfg.get("keyword_sentiment", 0.30))
            w_lunar = 0.0
            w_community = float(no_lc_cfg.get("community", 0.15))
            w_llm = float(no_lc_cfg.get("llm_sentiment", 0.20))

        composite = (
            w_mention * mention_score
            + w_keyword * keyword_sentiment_score
            + w_lunar * lunarcrush_score
            + w_community * community_score
            + w_llm * llm_sentiment_score
        )

        # If no data at all (no mentions AND no LunarCrush), return true neutral
        if total_mentions == 0 and not has_lunarcrush:
            return 50.0

        return round(max(0.0, min(100.0, composite)), 1)

    # ------------------------------------------------------------------ #
    # Rolling peak storage
    # ------------------------------------------------------------------ #

    def _load_peak(self, symbol: str, days: int) -> Optional[int]:
        """Load rolling peak with time decay (5% per day) from shared storage."""
        try:
            import time as _time
            from shared.storage import Storage
            store = Storage()
            val = store.load_kv("narrative_peaks", f"{symbol}_peak")
            ts = store.load_kv("narrative_peaks", f"{symbol}_peak_ts")
            if val is not None and ts is not None:
                days_elapsed = (_time.time() - ts) / 86400
                decayed = val * (0.95 ** days_elapsed)  # 5% decay per day
                return max(1, int(decayed))
            return int(val) if val is not None else None
        except Exception:
            return None

    def _store_count(self, symbol: str, count: int) -> None:
        """Store mention count with decaying peak tracking."""
        try:
            import time as _time
            from shared.storage import Storage
            store = Storage()
            current_peak = store.load_kv("narrative_peaks", f"{symbol}_peak")
            peak_ts = store.load_kv("narrative_peaks", f"{symbol}_peak_ts")

            # Apply decay to stored peak before comparing
            if current_peak is not None and peak_ts is not None:
                days_elapsed = (_time.time() - peak_ts) / 86400
                decayed_peak = current_peak * (0.95 ** days_elapsed)
            else:
                decayed_peak = 0

            # New peak = max of decayed old peak and current count
            effective_peak = max(decayed_peak, float(count))
            store.save_kv("narrative_peaks", f"{symbol}_peak", effective_peak)
            store.save_kv("narrative_peaks", f"{symbol}_peak_ts", float(_time.time()))
            store.save_kv("narrative_peaks", f"{symbol}_latest", float(count))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # HTTP helper
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
