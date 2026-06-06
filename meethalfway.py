"""
MeetHalfway AI - v2.0

Algorithm : isochrone intersection (ORS/Mapbox) + exponential travel-time fairness penalty
AI        : LLM structured-JSON semantic extraction (with keyword fallback)
Engineering: asyncio + httpx concurrency, graceful degradation (Mapbox->OSM, LLM->keywords), logging
Demo      : folium interactive map, Surprise Me mode, fatigue parameter
Privacy   : zero-footprint design - user GPS is computed in memory only and discarded on return; never persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Optional heavy dependencies - degrade gracefully if missing, never crash.
# ---------------------------------------------------------------------------
try:
    from shapely.geometry import Point, Polygon, mapping, shape  # type: ignore
    from shapely.ops import unary_union  # type: ignore
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

try:
    import folium  # type: ignore
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logger = logging.getLogger("meethalfway")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAPBOX_GEOCODE_BASE = "https://api.mapbox.com/geocoding/v5/mapbox.places"
MAPBOX_ISOCHRONE_BASE = "https://api.mapbox.com/isochrone/v1/mapbox"
ORS_ISOCHRONE_BASE = "https://api.openrouteservice.org/v2/isochrones"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
# Foursquare Places API (2024 rebrand). Requires Bearer auth + X-Places-Api-Version.
FOURSQUARE_SEARCH_URL = "https://places-api.foursquare.com/places/search"
FOURSQUARE_API_VERSION = "2025-06-17"

# A descriptive User-Agent is REQUIRED by Nominatim's usage policy and helps
# avoid WAF blocks (HTTP 406/403) on the public Overpass instances.
HTTP_USER_AGENT = "MeetHalfwayAI/2.0 (SIGSPATIAL 2026 demo; +https://github.com/Perry-BIS/MeetHalfWay)"
OSM_HTTP_HEADERS: Dict[str, str] = {
    "User-Agent": HTTP_USER_AGENT,
    "Accept": "application/json",
}

# Public Overpass mirrors, tried in order. The primary endpoint is intermittently
# rate-limited / regionally blocked (returns 406), so the engine rotates through
# mirrors before degrading to Nominatim / offline sample data.
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_ENDPOINTS: List[str] = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# HTTP statuses worth retrying on a different mirror: rate limit, WAF block,
# forbidden, and gateway/timeout errors.
_OVERPASS_RETRYABLE_STATUS = {403, 406, 429, 500, 502, 503, 504}

# Mapbox isochrone profile mapping
_PROFILE_MAP: Dict[str, str] = {
    "drive": "driving",
    "walk": "walking",
    "transit": "driving",  # Mapbox has no transit isochrones; driving is used as an approximation
}

# Estimated speeds (km/min)
_SPEED_KM_MIN: Dict[str, float] = {
    "walk": 5.0 / 60,
    "drive": 40.0 / 60,
    "transit": 20.0 / 60,
}

_COMMUTE_BIAS_BASE: Dict[str, float] = {
    "walk": 1.35,
    "transit": 1.15,
    "drive": 0.85,
}


def normalize_transport_mode(mode: Optional[str]) -> str:
    raw = str(mode or "").strip().lower()
    aliases = {
        "walking": "walk",
        "walk": "walk",
        "on foot": "walk",
        "foot": "walk",
        "public transit": "transit",
        "transit": "transit",
        "bus": "transit",
        "train": "transit",
        "subway": "transit",
        "metro": "transit",
        "driving": "drive",
        "drive": "drive",
        "car": "drive",
    }
    return aliases.get(raw, "transit")


def compute_commute_bias_weights(
    transport_a: Optional[str],
    transport_b: Optional[str],
    tolerance_a_miles: Optional[float] = None,
    tolerance_b_miles: Optional[float] = None,
) -> Tuple[float, float]:
    def _weight(mode: Optional[str], miles: Optional[float]) -> float:
        normalized_mode = normalize_transport_mode(mode)
        base = _COMMUTE_BIAS_BASE.get(normalized_mode, _COMMUTE_BIAS_BASE["transit"])
        if miles is None:
            return base
        miles_value = max(1.0, float(miles))
        tolerance_factor = min(1.35, max(0.8, 15.0 / miles_value))
        return base * tolerance_factor

    return _weight(transport_a, tolerance_a_miles), _weight(transport_b, tolerance_b_miles)


# ---------------------------------------------------------------------------
# Venue types & scenario config (shared by CLI and Streamlit)
# ---------------------------------------------------------------------------
VENUE_TYPES: Dict[str, Dict[str, str]] = {
    "restaurant":  {"display": "Restaurant",         "query": "restaurant",                    "icon": "cutlery"},
    "cafe":        {"display": "Cafe",         "query": "cafe coffee",                   "icon": "coffee"},
    "park":        {"display": "Park",           "query": "park",                          "icon": "tree"},
    "mall":        {"display": "Mall",   "query": "shopping mall",                 "icon": "shopping-bag"},
    "clothing":    {"display": "Clothing store",         "query": "clothing store fashion apparel", "icon": "shopping-bag"},
    "department_store": {"display": "Department store",   "query": "department store",              "icon": "shopping-bag"},
    "cinema":      {"display": "Cinema",         "query": "cinema movie theater",          "icon": "film"},
    "bar":         {"display": "Bar / pub",       "query": "bar pub lounge",                "icon": "glass"},
    "bookstore":   {"display": "Bookstore",           "query": "bookstore library",             "icon": "book"},
    "gas_station": {"display": "Gas / convenience",   "query": "gas station convenience store", "icon": "road"},
    "sports":      {"display": "Sports / gym",       "query": "gym sports center stadium",     "icon": "futbol-o"},
    "museum":      {"display": "Museum / gallery",     "query": "museum gallery exhibition",     "icon": "university"},
    "parking":     {"display": "Parking",         "query": "parking parking lot",           "icon": "car"},
}

_OVERPASS_FILTERS: Dict[str, List[str]] = {
    "restaurant": ['nwr["amenity"="restaurant"]'],
    "cafe": ['nwr["amenity"="cafe"]'],
    "park": ['nwr["leisure"="park"]'],
    "mall": ['nwr["shop"="mall"]', 'nwr["building"="retail"]'],
    "clothing": ['nwr["shop"~"^(clothes|fashion|shoes|boutique|jewelry|sports)$"]'],
    "department_store": ['nwr["shop"="department_store"]'],
    "cinema": ['nwr["amenity"="cinema"]'],
    "bar": ['nwr["amenity"~"^(bar|pub)$"]'],
    "bookstore": ['nwr["shop"="books"]'],
    "gas_station": ['nwr["amenity"="fuel"]', 'nwr["shop"="convenience"]'],
    "sports": ['nwr["leisure"~"^(fitness_centre|sports_centre|stadium)$"]'],
    "museum": ['nwr["tourism"~"^(museum|gallery)$"]'],
    "parking": ['nwr["amenity"="parking"]', 'nwr["amenity"="parking_entrance"]'],
}

# ---------------------------------------------------------------------------
# Offline demo sample data (Riverside, CA - matches the paper's demo scenario)
# ---------------------------------------------------------------------------
# Final fallback when Mapbox / Overpass / Nominatim are all unavailable (venue network
# instability, dead key, regional blocking) so the demo always has results to show. These are
# approximate coords of real Riverside venues, but status/queue/crowd are illustrative estimates; the UI marks is_sample=True and never passes them off as live data.
_RIVERSIDE_DOWNTOWN = (33.9806, -117.3755)
# Fields: (name, lat, lon, rating_proxy, status, queue_level, crowd_index, wait_min)
_OFFLINE_SAMPLE_VENUES: Dict[str, List[Tuple[str, float, float, float, str, str, float, float]]] = {
    "restaurant": [
        ("The Old Spaghetti Factory", 33.98103, -117.37402, 0.78, "open", "medium", 0.62, 15.0),
        ("Mario's Place",             33.98052, -117.37448, 0.86, "open", "low",    0.45, 5.0),
        ("ProAbition Kitchen",        33.98089, -117.37381, 0.82, "open", "high",   0.78, 30.0),
        ("Tio's Tacos",              33.98170, -117.36862, 0.80, "open", "medium", 0.58, 12.0),
        ("Simple Simon's Bakery",     33.98121, -117.37479, 0.84, "open", "low",    0.40, 4.0),
        ("Las Campanas (Mission Inn)",33.98158, -117.37531, 0.81, "open", "medium", 0.55, 18.0),
        ("W. Wolfskill",              33.98061, -117.37419, 0.79, "uncertain", "low", 0.42, 6.0),
        ("The Salted Pig",            33.98074, -117.37402, 0.83, "open", "high",   0.74, 25.0),
    ],
    "cafe": [
        ("Augie's Coffee House",      33.98079, -117.37441, 0.85, "open", "medium", 0.60, 8.0),
        ("Back to the Grind",         33.98019, -117.37362, 0.80, "open", "low",    0.44, 5.0),
        ("Molino's Coffee",           33.97901, -117.37603, 0.77, "open", "low",    0.38, 3.0),
        ("Arcade Coffee Roasters",    33.97004, -117.39002, 0.82, "open", "medium", 0.52, 7.0),
    ],
    "bar": [
        ("ProAbition Gilded Age",     33.98091, -117.37383, 0.84, "open", "high",   0.80, 30.0),
        ("Brickwood",                 33.98104, -117.37461, 0.78, "open", "medium", 0.60, 15.0),
        ("Lake Alice Trading Co.",    33.98142, -117.37489, 0.76, "open", "medium", 0.58, 12.0),
    ],
    "park": [
        ("White Park",                33.97604, -117.37004, 0.74, "open", "low",    0.30, 0.0),
        ("Fairmount Park",            33.99304, -117.38205, 0.79, "open", "low",    0.28, 0.0),
        ("Mount Rubidoux Park",       33.98701, -117.39605, 0.83, "open", "medium", 0.40, 0.0),
    ],
}

# Scenario presets: drive the default privacy mode & venue-type ordering
MEET_SCENARIOS: Dict[str, Dict] = {
    "blind_date": {
        "display": "Blind date / first meeting",
        "desc": "When sharing exact locations is awkward; suggests public, safe, moderately busy venues.",
        "default_mode": "privacy-separated upload",
        "venue_types": ["cafe", "restaurant", "park", "bookstore"],
    },
    "couple": {
        "display": "Couple date",
        "desc": "Find romantic spots that are relaxing for two.",
        "default_mode": "address input",
        "venue_types": ["restaurant", "cinema", "park", "cafe", "bar"],
    },
    "friends": {
        "display": "Friends gathering",
        "desc": "Best meeting point for group dining or casual activities.",
        "default_mode": "address input",
        "venue_types": ["restaurant", "bar", "sports", "mall", "cinema"],
    },
    "business": {
        "display": "Business meeting",
        "desc": "Professional, quiet, neutral meeting venues.",
        "default_mode": "privacy-separated upload",
        "venue_types": ["cafe", "restaurant", "mall"],
    },
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Location:
    lat: float
    lon: float


@dataclass
class CandidateRestaurant:
    name: str
    lat: float
    lon: float
    place_name: str
    mapbox_relevance: float
    distance_to_center_km: float
    fairness_delta_km: float = 0.0
    fairness_delta_minutes: float = 0.0       # travel-time fairness gap (minutes)
    in_isochrone_intersection: bool = False    # whether inside the isochrone intersection
    rating_proxy: float = 0.5
    web_signals: Dict[str, Any] = field(default_factory=dict)
    final_score: float = 0.0
    venue_category: str = "restaurant"         # venue-type key (matches VENUE_TYPES)
    best_time_slot: str = ""
    availability_overlap: float = 0.0
    radius_tolerance_score: float = 0.0
    venue_popularity_score: float = 0.0
    mutual_vote_score: float = 0.0
    time_vote_score: float = 0.0
    search_area_mode: str = "intersection"
    time_conflict: bool = False
    closest_time_gap_minutes: float = 0.0
    severe_time_gap: bool = False
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    data_source: str = "mapbox"                # source: mapbox/overpass/nominatim/offline_sample
    is_sample: bool = False                    # True = offline demo sample (not a live result)


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------
class MeetHalfwayRecommender:
    def __init__(
        self,
        mapbox_token: str,
        ors_api_key: Optional[str],
        yelp_api_key: Optional[str],
        tavily_key: str,
        openai_key: Optional[str],
        openai_model: str,
        openai_base: Optional[str] = None,
        foursquare_api_key: Optional[str] = None,
        transport: str = "transit",
        isochrone_minutes: int = 20,
        low_cost_mode: bool = False,
        use_yelp: bool = True,
        use_foursquare: bool = True,
        use_llm_extraction: bool = True,
        use_llm_summary: bool = True,
        max_enriched_candidates: Optional[int] = None,
    ) -> None:
        self.mapbox_token = mapbox_token
        self.ors_api_key = ors_api_key
        self.yelp_api_key = yelp_api_key
        self.tavily_key = tavily_key
        self.openai_key = openai_key
        self.openai_model = openai_model
        self.openai_base = openai_base
        self.foursquare_api_key = (foursquare_api_key or "").strip() or None
        self.transport = transport
        self.isochrone_minutes = isochrone_minutes
        self.low_cost_mode = low_cost_mode
        self.use_yelp = use_yelp and bool(yelp_api_key)
        self.use_foursquare = use_foursquare and bool(self.foursquare_api_key)
        self.use_llm_extraction = use_llm_extraction
        self.use_llm_summary = use_llm_summary
        # Degradation flags: switch to fallbacks after the first failure
        self._mapbox_ok: bool = True
        self._openai_ok: bool = bool(openai_key)
        self._http_max_retries: int = 2 if low_cost_mode else 4
        self._retry_base_seconds: float = 0.35 if low_cost_mode else 0.6
        self._retry_jitter_seconds: float = 0.2 if low_cost_mode else 0.35
        self._yelp_sem = asyncio.Semaphore(2 if low_cost_mode else 3)
        self._tavily_search_depth = "basic" if low_cost_mode else "advanced"
        self._tavily_max_results = 3 if low_cost_mode else 5
        if max_enriched_candidates is not None:
            self.max_enriched_candidates = max(1, int(max_enriched_candidates))
        elif low_cost_mode:
            self.max_enriched_candidates = 3
        else:
            self.max_enriched_candidates = None
        self._async_openai_client: Optional[Any] = None
        self._sync_openai_client: Optional[Any] = None

    def recommend_search_limit(self, top_k: int) -> int:
        base = max(1, int(top_k))
        if self.low_cost_mode:
            return min(max(base + 1, 3), 4)
        return max(base * 2, 10)

    def _get_async_openai_client(self) -> Optional[Any]:
        if not self.openai_key or not self._openai_ok:
            return None
        if self._async_openai_client is None:
            import openai as _openai  # noqa: PLC0415

            self._async_openai_client = _openai.AsyncOpenAI(
                api_key=self.openai_key,
                base_url=self.openai_base,
            )
        return self._async_openai_client

    def _get_sync_openai_client(self) -> Optional[Any]:
        if not self.openai_key or not self._openai_ok:
            return None
        if self._sync_openai_client is None:
            from openai import OpenAI  # noqa: PLC0415

            self._sync_openai_client = OpenAI(
                api_key=self.openai_key,
                base_url=self.openai_base,
            )
        return self._sync_openai_client

    def _backoff_seconds(self, attempt: int) -> float:
        return self._retry_base_seconds * (2 ** attempt) + random.uniform(0.0, self._retry_jitter_seconds)

    def _post_overpass_with_retry(self, query: str, timeout: int = 25) -> Optional[Dict[str, Any]]:
        """Overpass request: multi-mirror rotation + exponential backoff.

        Public Overpass nodes intermittently return 429 (rate limit) or 406/403 (WAF/regional block).
        The old version hit a single node and didn't retry 406, failing outright. This now:
          1. sends a proper User-Agent (reduces WAF blocking);
          2. rotates across the OVERPASS_ENDPOINTS mirrors;
          3. treats 403/406/429/5xx as retryable (next mirror or backoff).
        """
        endpoints = OVERPASS_ENDPOINTS or [OVERPASS_API_URL]
        max_attempts = max(self._http_max_retries, len(endpoints))
        last_error = "unknown"
        for attempt in range(max_attempts):
            endpoint = endpoints[attempt % len(endpoints)]
            host = endpoint.split("//")[-1].split("/")[0]
            try:
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    headers=OSM_HTTP_HEADERS,
                    timeout=timeout,
                )
                if resp.status_code in _OVERPASS_RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code} @ {host}"
                    if attempt < max_attempts - 1:
                        delay = self._backoff_seconds(attempt)
                        logger.warning(
                            "Overpass %s (%s); switching mirror and retrying in %.2fs (%d/%d)",
                            resp.status_code, host, delay, attempt + 1, max_attempts,
                        )
                        time.sleep(delay)
                        continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_error = f"{type(exc).__name__} @ {host}"
                if attempt < max_attempts - 1:
                    delay = self._backoff_seconds(attempt)
                    logger.warning(
                        "Overpass request error: %s; switching mirror and retrying in %.2fs (%d/%d)",
                        exc, delay, attempt + 1, max_attempts,
                    )
                    time.sleep(delay)
                    continue
        logger.warning("All Overpass mirrors failed (last error: %s)", last_error)
        return None

    @staticmethod
    def _normalize_vote(v: Any) -> float:
        """Normalize a vote value to [0,1]. Accepts [-2,2], [0,2], or [0,1]."""
        try:
            fv = float(v)
        except Exception:
            return 0.5
        if fv < 0:
            return _clip01((fv + 2.0) / 4.0)
        if fv <= 1.0:
            return _clip01(fv)
        return _clip01(fv / 2.0)

    def _time_negotiation_for_candidate(
        self,
        candidate: CandidateRestaurant,
        a: Location,
        b: Location,
        time_slots: List[str],
        availability: Optional[Dict[str, List[str]]],
        time_votes: Optional[Dict[str, Dict[str, float]]],
        radius_tolerance: Optional[Dict[str, float]],
        a_fatigue: float,
        b_fatigue: float,
        time_conflict: bool = False,
    ) -> Tuple[str, float, float, float, float, bool]:
        """
        Score a single candidate's time negotiation:
        returns (best slot, availability-overlap score, radius-tolerance score, time-vote score).
        """
        if not time_slots:
            return "", 0.5, 0.5, 0.5, 0.0, False

        loc = Location(candidate.lat, candidate.lon)
        ta = self._travel_minutes(a, loc) * a_fatigue
        tb = self._travel_minutes(b, loc) * b_fatigue

        tol_a = float((radius_tolerance or {}).get("a", self.isochrone_minutes) or self.isochrone_minutes)
        tol_b = float((radius_tolerance or {}).get("b", self.isochrone_minutes) or self.isochrone_minutes)
        tol_a = max(tol_a, 5.0)
        tol_b = max(tol_b, 5.0)

        a_avail = set((availability or {}).get("a", time_slots))
        b_avail = set((availability or {}).get("b", time_slots))

        if time_conflict:
            def _slot_to_minutes(slot: str) -> Optional[int]:
                try:
                    h, m = slot.split(":", 1)
                    hh = int(h)
                    mm = int(m)
                    if hh < 0 or hh > 23 or mm not in (0, 30):
                        return None
                    return hh * 60 + mm
                except Exception:
                    return None

            radius_a = _clip01(1.0 - max(0.0, ta - tol_a) / tol_a)
            radius_b = _clip01(1.0 - max(0.0, tb - tol_b) / tol_b)
            radius_score = (radius_a + radius_b) / 2.0

            a_minutes = [(_slot_to_minutes(slot), slot) for slot in a_avail]
            b_minutes = [(_slot_to_minutes(slot), slot) for slot in b_avail]
            a_minutes = [(m, s) for m, s in a_minutes if m is not None]
            b_minutes = [(m, s) for m, s in b_minutes if m is not None]

            if not a_minutes or not b_minutes:
                return "No shared time available", 0.0, radius_score, 0.0, 0.0, False

            best_gap = 24 * 60
            best_pair = (a_minutes[0][1], b_minutes[0][1])
            best_votes = (0.5, 0.5)
            for min_a, slot_a in a_minutes:
                for min_b, slot_b in b_minutes:
                    gap = abs(min_a - min_b)
                    va = self._normalize_vote((time_votes or {}).get("a", {}).get(slot_a, 0.8))
                    vb = self._normalize_vote((time_votes or {}).get("b", {}).get(slot_b, 0.8))
                    # Prefer smaller schedule gaps, break ties by stronger combined preference.
                    if gap < best_gap or (gap == best_gap and (va + vb) > sum(best_votes)):
                        best_gap = gap
                        best_pair = (slot_a, slot_b)
                        best_votes = (va, vb)

            severe_gap = best_gap > 120
            proximity_score = _clip01(1.0 - best_gap / 240.0)
            vote_score = _clip01(0.5 * ((best_votes[0] + best_votes[1]) / 2.0) + 0.5 * proximity_score)
            if severe_gap:
                proximity_score *= 0.45
                vote_score *= 0.45
                best_slot = f"Hard to align: A {best_pair[0]} / B {best_pair[1]}"
            else:
                best_slot = f"Closest compromise: A {best_pair[0]} / B {best_pair[1]}"
            return best_slot, proximity_score, radius_score, vote_score, float(best_gap), severe_gap

        best_slot = time_slots[0]
        best_score = -1.0
        best_overlap = 0.0
        best_time_vote = 0.5

        for slot in time_slots:
            a_ok = slot in a_avail
            b_ok = slot in b_avail
            if a_ok and b_ok:
                overlap = 1.0
            elif a_ok or b_ok:
                overlap = 0.35
            else:
                overlap = 0.0

            va = self._normalize_vote((time_votes or {}).get("a", {}).get(slot, 1.0))
            vb = self._normalize_vote((time_votes or {}).get("b", {}).get(slot, 1.0))
            agreement_score = 1.0 - abs(va - vb)
            vote_score = 0.7 * ((va + vb) / 2.0) + 0.3 * agreement_score

            slot_score = 0.65 * overlap + 0.35 * vote_score
            if slot_score > best_score:
                best_score = slot_score
                best_slot = slot
                best_overlap = overlap
                best_time_vote = vote_score

        # Score drops linearly once travel time exceeds the personal tolerance (minutes)
        radius_a = _clip01(1.0 - max(0.0, ta - tol_a) / tol_a)
        radius_b = _clip01(1.0 - max(0.0, tb - tol_b) / tol_b)
        radius_score = (radius_a + radius_b) / 2.0
        return best_slot, best_overlap, radius_score, best_time_vote, 0.0, False

    def _place_vote_for_candidate(
        self,
        candidate: CandidateRestaurant,
        place_votes: Optional[Dict[str, Dict[str, float]]],
    ) -> float:
        """Compute the mutual venue-vote score (by type and by name)."""
        if not place_votes:
            return 0.5

        a_votes = place_votes.get("a", {})
        b_votes = place_votes.get("b", {})
        keys = [candidate.venue_category, candidate.name.lower()]

        def _pick(votes: Dict[str, float]) -> float:
            for k in keys:
                if k in votes:
                    return self._normalize_vote(votes[k])
            return 0.5

        a_score = _pick(a_votes)
        b_score = _pick(b_votes)
        mean_score = (a_score + b_score) / 2.0
        agreement_score = 1.0 - abs(a_score - b_score)
        return 0.65 * mean_score + 0.35 * agreement_score

    # -----------------------------------------------------------------------
    # Geometry helpers
    # -----------------------------------------------------------------------
    @staticmethod
    def haversine_km(a: Location, b: Location) -> float:
        """Great-circle distance between two points (km) via the Haversine formula."""
        r = 6371.0
        lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
        lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return r * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))

    def compute_weighted_midpoint(
        self, a: Location, b: Location, weight_a: float = 1.0, weight_b: float = 1.0
    ) -> Location:
        total = weight_a + weight_b
        return Location(
            lat=(a.lat * weight_a + b.lat * weight_b) / total,
            lon=(a.lon * weight_a + b.lon * weight_b) / total,
        )

    def _travel_minutes(self, a: Location, b: Location) -> float:
        """Estimate travel time (minutes) from the transport mode."""
        dist_km = self.haversine_km(a, b)
        speed = _SPEED_KM_MIN.get(self.transport, _SPEED_KM_MIN["transit"])
        return dist_km / speed

    # -----------------------------------------------------------------------
    # Isochrones - ORS/Mapbox API + circle approximation fallback
    # -----------------------------------------------------------------------
    def _fetch_isochrone(self, loc: Location, minutes: int, profile: str) -> Optional[Any]:
        """Call the Mapbox Isochrone API and return a shapely Polygon."""
        if not self.mapbox_token:
            return None
        if not HAS_SHAPELY:
            logger.warning("shapely not installed - isochrones degraded to radius approximation circles.")
            return None
        # Mapbox's per-profile cap is 60 minutes; >60 returns 422.
        minutes = min(60, max(1, int(minutes)))
        url = f"{MAPBOX_ISOCHRONE_BASE}/{profile}/{loc.lon},{loc.lat}"
        params = {
            "contours_minutes": str(minutes),
            "polygons": "true",
            "access_token": self.mapbox_token,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if not features:
                logger.warning("Isochrone API returned no features (%.4f, %.4f).", loc.lat, loc.lon)
                return None
            poly = shape(features[0]["geometry"])
            logger.info(
                "Isochrone fetched (%.4f, %.4f) | %d min | profile=%s",
                loc.lat, loc.lon, minutes, profile,
            )
            return poly
        except Exception as exc:
            logger.warning("Mapbox Isochrone request failed: %s - trying radius approximation", exc)
            self._mapbox_ok = False
            return None

    def _fetch_isochrone_ors(
        self,
        loc: Location,
        range_value: float,
        transport: str,
        range_type: str = "time",
    ) -> Optional[Any]:
        """Call the OpenRouteService Isochrone API and return a shapely Polygon.

        ``range_type="time"`` -> ``range_value`` is **minutes** (legacy path).
        ``range_type="distance"`` -> ``range_value`` is **miles**; reachable
        polygon follows the road network out to that many road-miles.  This is
        the path that matches the "Max commute distance: N mi" slider in the UI
        (UI promise == map geometry).
        """
        if not self.ors_api_key or not HAS_SHAPELY:
            return None

        profile_map = {
            "drive": "driving-car",
            "walk": "foot-walking",
            "transit": "driving-car",
        }
        profile = profile_map.get(transport, "driving-car")
        url = f"{ORS_ISOCHRONE_BASE}/{profile}"

        if range_type == "distance":
            # ORS free tier rejects driving distance ranges >120 km (~75 mi).
            miles = min(75.0, max(0.25, float(range_value)))
            range_meters = int(round(miles * 1609.34))
            payload = {
                "locations": [[loc.lon, loc.lat]],
                "range": [range_meters],
                "range_type": "distance",
            }
            log_unit = f"{miles:.1f} mi"
        else:
            # ORS free tier rejects ranges >3600 s (60 min) with HTTP 400.
            minutes = min(60, max(1, int(range_value)))
            payload = {
                "locations": [[loc.lon, loc.lat]],
                "range": [minutes * 60],
                "range_type": "time",
            }
            log_unit = f"{minutes} min"

        headers = {
            "Authorization": self.ors_api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if not features:
                logger.warning("ORS Isochrone returned no features (%.4f, %.4f).", loc.lat, loc.lon)
                return None
            poly = shape(features[0]["geometry"])
            logger.info(
                "ORS isochrone fetched (%.4f, %.4f) | %s | profile=%s",
                loc.lat,
                loc.lon,
                log_unit,
                profile,
            )
            return poly
        except Exception as exc:
            logger.warning("ORS Isochrone request failed: %s", exc)
            return None

    def _circle_fallback(
        self,
        loc: Location,
        range_value: float,
        range_type: str = "time",
    ) -> Optional[Any]:
        """Approximate an isochrone with a circular buffer when ORS/Mapbox are unavailable.

        In ``distance`` mode the radius is taken literally from miles; in
        ``time`` mode it's the legacy speed*minutes estimate.
        """
        if not HAS_SHAPELY:
            return None
        if range_type == "distance":
            radius_km = float(range_value) * 1.60934
        else:
            speed = _SPEED_KM_MIN.get(self.transport, _SPEED_KM_MIN["transit"])
            radius_km = speed * float(range_value)
        radius_deg = radius_km / 111.0  # 1° ≈ 111 km
        pt = Point(loc.lon, loc.lat)
        logger.info(
            "Circle-fallback isochrone (%.4f, %.4f) | radius %.2f km (%s)",
            loc.lat, loc.lon, radius_km, range_type,
        )
        return pt.buffer(radius_deg)

    def get_distance_circle(self, loc: Location, radius_km: float) -> Optional[Any]:
        """
        Build an ellipsoid-corrected circular polygon (radius in km) in lat/lon space.

        Because 1 deg of longitude shrinks with latitude (1 deg lon = 111*cos(lat) km),
        an affine scale makes the circle look round on the ground.

        Args:
            loc       : center location
            radius_km : radius (km)
        Returns:
            A Shapely Polygon, or None when Shapely is unavailable.
        """
        if not HAS_SHAPELY:
            return None
        lat_rad = math.radians(loc.lat)
        cos_lat = math.cos(lat_rad)
        lat_deg = radius_km / 111.0
        lon_deg = radius_km / (111.0 * cos_lat) if cos_lat > 1e-9 else lat_deg
        pt = Point(loc.lon, loc.lat)
        unit_circle = pt.buffer(lat_deg, resolution=64)
        try:
            from shapely.affinity import scale as _shapely_scale  # type: ignore
            ellipse = _shapely_scale(unit_circle, xfact=lon_deg / lat_deg, yfact=1.0, origin=pt)
        except Exception:
            ellipse = unit_circle  # conservative fallback if the affine scale fails
        logger.info(
            "Distance circle (%.5f, %.5f) radius %.2f km / lat_deg=%.5f lon_deg=%.5f",
            loc.lat, loc.lon, radius_km, lat_deg, lon_deg,
        )
        return ellipse

    def get_intersection_from_radii(
        self,
        a: Location,
        b: Location,
        radius_a_km: float,
        radius_b_km: float,
    ) -> Optional[Any]:
        """
        Build each person's reachable circle and return the intersection polygon.

        If empty (too far apart), fall back to the union of the circles (relaxed mode).
        Usable as the search_nearby_venues `intersection` argument to constrain the search area.
        """
        circle_a = self.get_distance_circle(a, radius_a_km)
        circle_b = self.get_distance_circle(b, radius_b_km)
        return self.compute_intersection(circle_a, circle_b)

    def get_search_area_from_radii(
        self,
        a: Location,
        b: Location,
        radius_a_km: float,
        radius_b_km: float,
    ) -> Dict[str, Any]:
        """
        Return the search area and its mode from the two radii.

        mode:
          - intersection: the two radii overlap
          - union_fallback: no overlap; relaxed union search
          - unknown: geometry computation failed
        """
        circle_a = self.get_distance_circle(a, radius_a_km)
        circle_b = self.get_distance_circle(b, radius_b_km)
        if circle_a is None or circle_b is None:
            return {"geometry": None, "overlap_exists": False, "mode": "unknown"}
        try:
            inter = circle_a.intersection(circle_b)
            if inter.is_empty:
                logger.warning("Radius intersection empty (too far apart) - using union fallback.")
                return {
                    "geometry": circle_a.union(circle_b),
                    "overlap_exists": False,
                    "mode": "union_fallback",
                }
            ratio = inter.area / min(circle_a.area, circle_b.area) * 100
            logger.info("Radius intersection covers %.1f%% of the smaller area", ratio)
            return {"geometry": inter, "overlap_exists": True, "mode": "intersection"}
        except Exception as exc:
            logger.error("Radius intersection computation failed: %s", exc)
            return {"geometry": None, "overlap_exists": False, "mode": "unknown"}

    def _isochrone(
        self,
        loc: Location,
        range_value: float,
        transport: str,
        range_type: str = "time",
    ) -> Optional[Any]:
        """Fetch one isochrone polygon (ORS first, then Mapbox if applicable, then circle fallback).

        ``range_type="distance"`` -> ``range_value`` is miles. Mapbox is skipped
        in distance mode because the Mapbox Isochrone API only supports time
        contours; we go straight to the distance-based circle fallback if ORS
        fails. ``range_type="time"`` keeps the legacy minutes-based path.
        """
        poly = self._fetch_isochrone_ors(loc, range_value, transport, range_type=range_type)
        if poly is None and range_type == "time":
            profile = _PROFILE_MAP.get(transport, "driving")
            poly = self._fetch_isochrone(loc, int(range_value), profile)
        if poly is None:
            poly = self._circle_fallback(loc, range_value, range_type=range_type)
        return poly

    def get_isochrone(self, loc: Location) -> Optional[Any]:
        """Travel-time isochrone polygon (ORS first, Mapbox, then circle fallback)."""
        return self._isochrone(loc, self.isochrone_minutes, self.transport, range_type="time")

    def get_isochrone_search_area(
        self,
        a: Location,
        b: Location,
        minutes_a: Optional[int] = None,
        minutes_b: Optional[int] = None,
        transport_a: Optional[str] = None,
        transport_b: Optional[str] = None,
        miles_a: Optional[float] = None,
        miles_b: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Shared feasible region from per-user reachable isochrones.

        If ``miles_a``/``miles_b`` are given, each region is the road-network
        polygon reachable within that many miles (ORS ``range_type=distance``);
        this matches the "Max commute distance" mile slider in the UI exactly.
        Otherwise falls back to time-budget isochrones from ``minutes_a/b``
        (legacy path).

        Returns ``{geometry, overlap_exists, mode, iso_a, iso_b}``. ``mode`` is
        ``isochrone_intersection`` when both reachable regions overlap, otherwise
        ``isochrone_union_fallback`` (relaxed) or ``unknown``. ``iso_a``/``iso_b``
        are the individual reachable polygons, used for map visualization.
        """
        ta = transport_a or self.transport
        tb = transport_b or self.transport
        if miles_a is not None or miles_b is not None:
            ra = float(miles_a) if miles_a is not None else 15.0
            rb = float(miles_b) if miles_b is not None else 15.0
            iso_a = self._isochrone(a, ra, ta, range_type="distance")
            iso_b = self._isochrone(b, rb, tb, range_type="distance")
        else:
            ma = int(minutes_a) if minutes_a else self.isochrone_minutes
            mb = int(minutes_b) if minutes_b else self.isochrone_minutes
            iso_a = self._isochrone(a, ma, ta, range_type="time")
            iso_b = self._isochrone(b, mb, tb, range_type="time")
        if iso_a is None or iso_b is None:
            return {"geometry": None, "overlap_exists": False, "mode": "unknown", "iso_a": iso_a, "iso_b": iso_b}
        try:
            inter = iso_a.intersection(iso_b)
            if inter is not None and not inter.is_empty:
                ratio = inter.area / min(iso_a.area, iso_b.area) * 100
                logger.info("Isochrone intersection covers %.1f%% of the smaller region.", ratio)
                return {"geometry": inter, "overlap_exists": True, "mode": "isochrone_intersection", "iso_a": iso_a, "iso_b": iso_b}
            logger.warning("Isochrones do not overlap — falling back to union (relaxed mode).")
            return {"geometry": iso_a.union(iso_b), "overlap_exists": False, "mode": "isochrone_union_fallback", "iso_a": iso_a, "iso_b": iso_b}
        except Exception as exc:
            logger.error("Isochrone search-area computation failed: %s", exc)
            return {"geometry": None, "overlap_exists": False, "mode": "unknown", "iso_a": iso_a, "iso_b": iso_b}

    def get_multi_isochrone_search_area(
        self,
        locations: List[Location],
        minutes_list: Optional[List[int]] = None,
        transport: Optional[str] = None,
        miles_list: Optional[List[float]] = None,
        transport_list: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """N-participant shared feasible region from per-user reachable isochrones.

        If ``miles_list`` is given, each region is the road-network polygon
        reachable within that many miles (matches the "Max distance" slider).
        Otherwise falls back to time-budget isochrones from ``minutes_list``
        (legacy path).

        ``transport_list`` (optional, aligned with ``locations``) lets each
        participant use a distinct transport mode (e.g. ``["drive","walk","transit"]``).
        If omitted, the single ``transport`` argument (or ``self.transport``)
        is applied uniformly. Backward-compatible.

        Returns ``{geometry, overlap_exists, mode, regions}`` where ``regions`` is
        the per-participant reachable polygon list (aligned with ``locations``).
        """
        t_default = transport or self.transport
        regions: List[Any] = []
        use_distance = miles_list is not None
        use_per_user_mode = (
            transport_list is not None
            and len(transport_list) == len(locations)
        )
        for i, loc in enumerate(locations):
            t_i = transport_list[i] if use_per_user_mode else t_default
            if use_distance:
                miles = (miles_list[i] if i < len(miles_list) else None) or 15.0
                regions.append(self._isochrone(loc, float(miles), t_i, range_type="distance"))
            else:
                minutes = (minutes_list[i] if minutes_list and i < len(minutes_list) else None) or self.isochrone_minutes
                regions.append(self._isochrone(loc, minutes, t_i, range_type="time"))
        valid = [r for r in regions if r is not None and not getattr(r, "is_empty", False)]
        if len(valid) < 2:
            return {"geometry": None, "overlap_exists": False, "mode": "unknown", "regions": regions}
        try:
            inter = valid[0]
            for r in valid[1:]:
                inter = inter.intersection(r)
            if inter is not None and not inter.is_empty:
                return {"geometry": inter, "overlap_exists": True, "mode": "isochrone_intersection", "regions": regions}
            union = valid[0]
            for r in valid[1:]:
                union = union.union(r)
            logger.warning("Isochrones do not overlap for all participants — union fallback.")
            return {"geometry": union, "overlap_exists": False, "mode": "isochrone_union_fallback", "regions": regions}
        except Exception as exc:
            logger.error("Multi-isochrone search-area computation failed: %s", exc)
            return {"geometry": None, "overlap_exists": False, "mode": "unknown", "regions": regions}

    def compute_intersection(self, iso_a: Any, iso_b: Any) -> Optional[Any]:
        """
        Compute the spatial intersection of two isochrone polygons.
        If empty (too far apart), fall back to their union (relaxed mode).
        """
        if iso_a is None or iso_b is None:
            return None
        try:
            inter = iso_a.intersection(iso_b)
            if inter.is_empty:
                logger.warning("Isochrone intersection empty (too far apart) - using union fallback.")
                return iso_a.union(iso_b)
            ratio = inter.area / min(iso_a.area, iso_b.area) * 100
            logger.info("Isochrone intersection covers %.1f%% of the smaller polygon", ratio)
            return inter
        except Exception as exc:
            logger.error("Isochrone intersection computation failed: %s", exc)
            return None

    # -----------------------------------------------------------------------
    # Natural-barrier subtraction (water / forest) - OSM Overpass API
    # -----------------------------------------------------------------------
    def _fetch_natural_barriers(self, poly: Any) -> List[Any]:
        """
        Fetch water and forest features within the polygon bbox via the Overpass API,
        returning a list of Shapely Polygons. On failure, return an empty list (conservative).

        Query targets:
          - natural=water  (lakes, ponds, river surfaces)
          - natural=wood   (woods)
          - landuse=forest (forest)
          - waterway=riverbank (enclosed riverbank water)
        """
        if not HAS_SHAPELY:
            return []

        # bounds: (lon_min, lat_min, lon_max, lat_max)
        minx, miny, maxx, maxy = poly.bounds
        # Overpass bbox format: south,west,north,east (i.e. lat_min,lon_min,lat_max,lon_max)
        query = (
            f"[out:json][timeout:20][bbox:{miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f}];\n"
            "(\n"
            '  way["natural"~"^(water|wood)$"];\n'
            '  way["landuse"="forest"];\n'
            '  way["waterway"="riverbank"];\n'
            ");\n"
            "out geom;"
        )
        result = self._post_overpass_with_retry(query, timeout=25)
        if result is None:
            logger.warning("Overpass natural-barrier query failed - skipping barrier removal (conservative)")
            return []
        elements = result.get("elements", [])

        barriers: List[Any] = []
        for elem in elements:
            if elem.get("type") != "way":
                continue
            geom_nodes = elem.get("geometry", [])
            if len(geom_nodes) < 4:  # need >= 3 vertices + closing point
                continue
            coords = [(pt["lon"], pt["lat"]) for pt in geom_nodes]
            try:
                barrier = Polygon(coords)
                if barrier.is_valid and not barrier.is_empty:
                    barriers.append(barrier)
            except Exception:
                pass

        logger.info(
            "Overpass barriers: found %d water/forest features in bbox(%.4f,%.4f,%.4f,%.4f)",
            miny, minx, maxy, maxx, len(barriers),
        )
        return barriers

    def subtract_natural_barriers(self, intersection: Any) -> Any:
        """
        Subtract water/forest and other unreachable natural features from the isochrone intersection.

        Fallback chain:
          Overpass query fails -> return the original intersection (remove nothing)
          empty difference     -> return the original intersection (conservative)
          Shapely missing      -> return the original intersection
        """
        if intersection is None or not HAS_SHAPELY:
            return intersection

        barriers = self._fetch_natural_barriers(intersection)
        if not barriers:
            return intersection

        original_area = intersection.area
        barrier_union = unary_union(barriers)
        try:
            result = intersection.difference(barrier_union)
            if result.is_empty:
                logger.warning("Intersection empty after barrier removal - keeping the original (conservative).")
                return intersection
            removed_pct = (original_area - result.area) / original_area * 100
            logger.info(
                "Barrier removal done: %d regions, %.1f%% of area removed",
                len(barriers),
                removed_pct,
            )
            return result
        except Exception as exc:
            logger.warning("Barrier removal error: %s - keeping the original polygon", exc)
            return intersection

    # -----------------------------------------------------------------------
    # POI-density hard filter (drop too-isolated areas with low POI density)
    # -----------------------------------------------------------------------
    def filter_by_poi_density(
        self,
        candidates: List[CandidateRestaurant],
        radius_m: float = 300.0,
        min_poi_count: int = 5,
    ) -> List[CandidateRestaurant]:
        """
        POI-density hard filter: drop candidates in too-isolated, low-POI areas.

        Strategy:
          1. one batched Overpass query for public POIs within the candidates' bbox
             （amenity / shop / tourism / leisure）。
          2. count POIs within radius_m meters of each candidate.
          3. drop candidates below min_poi_count (hard filter).
          4. conservative: if Overpass fails or all would be filtered -> keep the original list.

        Args:
          radius_m      : radius in meters (default 300m)
          min_poi_count : minimum POI threshold (default 5)
        """
        if not candidates:
            return candidates

        # Compute bbox with a 0.01 deg buffer (~1 km)
        lats = [c.lat for c in candidates]
        lons = [c.lon for c in candidates]
        lat_min = min(lats) - 0.01
        lat_max = max(lats) + 0.01
        lon_min = min(lons) - 0.01
        lon_max = max(lons) + 0.01

        query = (
            f"[out:json][timeout:25]"
            f"[bbox:{lat_min:.6f},{lon_min:.6f},{lat_max:.6f},{lon_max:.6f}];\n"
            "(\n"
            '  node["amenity"];\n'
            '  node["shop"];\n'
            '  node["tourism"];\n'
            '  node["leisure"];\n'
            ");\n"
            "out body;"
        )
        result = self._post_overpass_with_retry(query, timeout=25)
        if result is None:
            logger.warning("Overpass POI-density query failed - skipping density filter (conservative)")
            return candidates
        poi_nodes = result.get("elements", [])

        # keep only nodes that carry coordinates
        poi_points: List[Tuple[float, float]] = [
            (float(n["lat"]), float(n["lon"]))
            for n in poi_nodes
            if n.get("type") == "node" and "lat" in n and "lon" in n
        ]
        logger.info(
            "Overpass POI density: %d public POI nodes in bbox", len(poi_points)
        )

        radius_km = radius_m / 1000.0
        passed: List[CandidateRestaurant] = []
        for c in candidates:
            c_loc = Location(c.lat, c.lon)
            count = sum(
                1
                for (plat, plon) in poi_points
                if self.haversine_km(c_loc, Location(plat, plon)) <= radius_km
            )
            if count >= min_poi_count:
                passed.append(c)
                logger.debug(
                    "POI density pass  %-22s | %3d POI in %.0fm",
                    c.name[:22], count, radius_m,
                )
            else:
                logger.info(
                    "POI density drop  %-22s | only %d POI in %.0fm (threshold=%d)",
                    c.name[:22], count, radius_m, min_poi_count,
                )

        if not passed:
            logger.warning(
                "No candidates left after POI-density filter - keeping the original list (conservative)."
            )
            return candidates

        logger.info(
            "POI-density filter: %d -> %d candidates (dropped %d isolated venues)",
            len(candidates), len(passed), len(candidates) - len(passed),
        )
        return passed

    def filter_closed_candidates(
        self,
        candidates: List[CandidateRestaurant],
        drop_uncertain: bool = False,
        min_keep: int = 2,
    ) -> Tuple[List[CandidateRestaurant], Dict[str, int]]:
        """
        Drop candidates known to be closed based on the web-signal status.

        Default rule (legacy, lenient):
          - status=closed: drop
          - status=open / uncertain: keep

        ``drop_uncertain=True`` (strict): only ``status=="open"`` survives —
        anything we can't positively confirm as open is dropped. Safety net:
        if strict filter would leave fewer than ``min_keep`` candidates (e.g.
        Tavily was rate-limited and almost everything degraded to uncertain),
        we automatically fall back to the legacy rule so the user never sees
        an empty results page. The actual rule used is reflected in the log.
        """
        stats = {"open": 0, "closed": 0, "uncertain": 0}
        open_only: List[CandidateRestaurant] = []
        keep_loose: List[CandidateRestaurant] = []
        for c in candidates:
            status = str((c.web_signals or {}).get("status", "uncertain")).lower()
            if status not in stats:
                status = "uncertain"
            stats[status] += 1
            if status == "closed":
                continue
            keep_loose.append(c)
            if status == "open":
                open_only.append(c)

        if drop_uncertain and len(open_only) >= min_keep:
            filtered = open_only
            rule = "strict_open_only"
        elif drop_uncertain:
            filtered = keep_loose
            rule = "strict_degraded_to_legacy"  # not enough confirmed-open venues
        else:
            filtered = keep_loose
            rule = "legacy"

        logger.info(
            "Open-status filter [%s]: open=%d uncertain=%d closed=%d -> kept %d/%d",
            rule,
            stats["open"],
            stats["uncertain"],
            stats["closed"],
            len(filtered),
            len(candidates),
        )
        return filtered, stats

    def tag_with_isochrone(
        self,
        candidates: List[CandidateRestaurant],
        intersection: Optional[Any],
        area_mode: str = "intersection",
    ) -> None:
        """Tag each candidate with whether it falls inside the isochrone intersection."""
        if intersection is None or not HAS_SHAPELY:
            for c in candidates:
                c.search_area_mode = area_mode
                c.in_isochrone_intersection = area_mode == "intersection"
            return
        for c in candidates:
            pt = Point(c.lon, c.lat)
            c.search_area_mode = area_mode
            c.in_isochrone_intersection = area_mode == "intersection" and intersection.contains(pt)
            verdict = "inside" if c.in_isochrone_intersection else "outside"
            logger.debug("%s  %s", verdict, c.name)

    # -----------------------------------------------------------------------
    # Venue search (Mapbox POI -> OSM fallback)
    # -----------------------------------------------------------------------
    def search_nearby_venues(
        self,
        center: Location,
        venue_type: str = "restaurant",
        keyword: str = "",
        limit: int = 12,
        intersection: Optional[Any] = None,
    ) -> List[CandidateRestaurant]:
        """
        Search venues around the center (Mapbox POI -> OSM fallback).

        venue_type  : a VENUE_TYPES key (restaurant/cafe/park/mall/cinema/...)
        keyword     : custom search term (overrides the venue_type query when non-empty)
        intersection: shared polygon (Shapely). When provided:
                      1) use its centroid as the search center;
                      2) post-filter results to those inside the polygon.
                         If that leaves nothing, conservatively keep all results.
        """
        cfg = VENUE_TYPES.get(venue_type, VENUE_TYPES["restaurant"])
        q = keyword.strip() if keyword.strip() else cfg["query"]

        # If an intersection polygon is given, use its centroid as the search center.
        search_center = center
        if intersection is not None and HAS_SHAPELY:
            try:
                if not intersection.is_empty:
                    centroid = intersection.centroid
                    search_center = Location(lat=centroid.y, lon=centroid.x)
                    logger.info(
                        "Using intersection centroid as search center: (%.5f, %.5f)",
                        search_center.lat, search_center.lon,
                    )
            except Exception as exc:
                logger.warning("Could not extract intersection centroid, using default center: %s", exc)

        # Foursquare Places API is tried first when a key is configured: it returns
        # rating/hours/price metadata that Mapbox POI lacks, which feeds directly
        # into the fairness/preference scoring layer downstream.
        fsq_items = self._search_foursquare(
            search_center, center, venue_type, q, limit, intersection
        )
        if fsq_items:
            return fsq_items

        if not self.mapbox_token:
            logger.info("Mapbox not configured - going straight to the Overpass/OSM/sample fallback chain.")
            return self._fallback_venue_search(
                search_center, center, venue_type, q, limit, intersection
            )

        url = f"{MAPBOX_GEOCODE_BASE}/{requests.utils.quote(q)}.json"
        params = {
            "access_token": self.mapbox_token,
            "proximity": f"{search_center.lon},{search_center.lat}",
            "types": "poi",
            "language": "zh",
            "limit": min(max(limit, 1), 20),
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Mapbox venue search failed: %s - switching to Overpass/OSM/sample", exc)
            return self._fallback_venue_search(
                search_center, center, venue_type, q, limit, intersection
            )

        items: List[CandidateRestaurant] = []
        for ft in data.get("features", []):
            coords = ft.get("center", [None, None])
            if coords[0] is None:
                continue
            pos = Location(lat=float(coords[1]), lon=float(coords[0]))
            items.append(
                CandidateRestaurant(
                    name=ft.get("text", "Unknown"),
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=ft.get("place_name", ""),
                    mapbox_relevance=float(ft.get("relevance", 0.5)),
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=float(ft.get("relevance", 0.5)),
                    venue_category=venue_type,
                    data_source="mapbox",
                )
            )

        # Intersection filter: keep only venues inside the shared area.
        items = self._filter_by_intersection(items, intersection, center)
        if not items:
            logger.warning("Mapbox returned but no candidates - switching to Overpass/OSM/sample.")
            return self._fallback_venue_search(
                search_center, center, venue_type, q, limit, intersection
            )

        venue_display = cfg["display"]
        logger.info("Mapbox search returned %d candidates (type=%s)", len(items), venue_display)
        return items

    def _fallback_venue_search(
        self,
        search_center: Location,
        center: Location,
        venue_type: str,
        q: str,
        limit: int,
        intersection: Optional[Any],
    ) -> List[CandidateRestaurant]:
        """Fallback search chain: Overpass -> Nominatim -> offline sample (guaranteed non-empty).

        Unifies the three Mapbox-failure branches and guarantees a result to display -
        the sample data is the last line of defense for the paper's graceful-degradation claim.
        """
        overpass_items = self._search_overpass(
            search_center, venue_type=venue_type, limit=limit, intersection=intersection
        )
        if overpass_items:
            return overpass_items

        logger.warning("No Overpass results - switching to OSM Nominatim.")
        osm_items = self._search_osm(search_center, q, limit, venue_category=venue_type)
        if osm_items:
            return osm_items

        # Final fallback: clearly-labeled offline demo sample
        return self._offline_sample_venues(
            center, venue_type=venue_type, limit=limit, intersection=intersection
        )

    def _polygon_radius_m(self, intersection: Optional[Any]) -> int:
        """Derive a Foursquare-friendly search radius (meters) from an intersection polygon.

        Half the bounding-box diagonal + 500m buffer, clipped to [1000, 5000].
        Falls back to 3000m when no polygon is available.
        """
        if intersection is None or not HAS_SHAPELY:
            return 3000
        try:
            if intersection.is_empty:
                return 3000
            minx, miny, maxx, maxy = intersection.bounds
            diag_km = self.haversine_km(Location(miny, minx), Location(maxy, maxx))
            return int(min(max(diag_km * 1000.0 / 2.0 + 500.0, 1000.0), 5000.0))
        except Exception:
            return 3000

    def _search_foursquare(
        self,
        search_center: Location,
        center: Location,
        venue_type: str,
        q: str,
        limit: int,
        intersection: Optional[Any],
    ) -> List[CandidateRestaurant]:
        """Foursquare Places API search (first-class POI source with rating/hours/price).

        Tried before Mapbox when a Foursquare key is configured. Any failure (missing
        key, HTTP error, empty result after intersection filter) falls through silently
        so the Mapbox -> Overpass -> Nominatim -> offline_sample chain still runs.
        """
        if not (self.foursquare_api_key and self.use_foursquare):
            return []

        radius_m = self._polygon_radius_m(intersection)
        # Foursquare basic Service Key is free for the default fields
        # (name, lat/lon, categories, location, distance). Premium fields
        # like rating/price/hours are paywalled and return 429 + Remaining=0
        # for free keys, so we request only the basic set; rating/open-now
        # signals come from Tavily web enrichment downstream.
        params = {
            "ll": f"{search_center.lat},{search_center.lon}",
            "query": q,
            "radius": radius_m,
            "limit": min(max(limit, 1), 50),
            "sort": "RELEVANCE",
            "fields": "fsq_place_id,name,latitude,longitude,location,categories,distance",
        }
        headers = {
            "Authorization": f"Bearer {self.foursquare_api_key}",
            "X-Places-Api-Version": FOURSQUARE_API_VERSION,
            "Accept": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        }
        # Foursquare Places API exposes daily quota via X-RateLimit-Remaining but
        # also enforces an undocumented short-window burst cap that returns 429.
        # Retry a couple of times with exponential backoff before giving up.
        data: Optional[Dict[str, Any]] = None
        max_attempts = 3 if self.low_cost_mode else 4
        last_error: Optional[str] = None
        for attempt in range(max_attempts):
            try:
                resp = requests.get(
                    FOURSQUARE_SEARCH_URL, params=params, headers=headers, timeout=20
                )
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_s = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else self._backoff_seconds(attempt)
                    )
                    last_error = f"429 (retry-after={retry_after or 'n/a'})"
                    logger.info(
                        "Foursquare rate-limited (attempt %d/%d) - sleeping %.2fs",
                        attempt + 1, max_attempts, sleep_s,
                    )
                    time.sleep(min(sleep_s, 5.0))
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt < max_attempts - 1:
                    time.sleep(self._backoff_seconds(attempt))
                    continue
                logger.warning(
                    "Foursquare venue search failed (%s) - falling through to Mapbox/OSM",
                    last_error,
                )
                return []
        if data is None:
            logger.warning(
                "Foursquare venue search gave up after %d attempts (%s) - falling through",
                max_attempts, last_error or "unknown",
            )
            return []

        items: List[CandidateRestaurant] = []
        for row in data.get("results", []) or []:
            lat = row.get("latitude")
            lon = row.get("longitude")
            if lat is None or lon is None:
                continue
            try:
                pos = Location(lat=float(lat), lon=float(lon))
            except (TypeError, ValueError):
                continue
            rating = row.get("rating")
            try:
                rating_proxy = (
                    max(0.0, min(1.0, float(rating) / 10.0))
                    if rating is not None
                    else 0.5
                )
            except (TypeError, ValueError):
                rating_proxy = 0.5
            location = row.get("location") or {}
            place_name = location.get("formatted_address") or row.get("name") or ""
            web_signals: Dict[str, Any] = {}
            if rating is not None:
                try:
                    web_signals["foursquare_rating"] = float(rating)
                except (TypeError, ValueError):
                    pass
            price = row.get("price")
            if price is not None:
                try:
                    web_signals["price_tier"] = int(price)
                except (TypeError, ValueError):
                    pass
            hours = row.get("hours") or {}
            if isinstance(hours, dict) and "open_now" in hours:
                web_signals["open_now"] = bool(hours.get("open_now"))
            categories = row.get("categories") or []
            if categories:
                web_signals["foursquare_category"] = categories[0].get("name", "")
            items.append(
                CandidateRestaurant(
                    name=str(row.get("name") or "Unknown")[:80],
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=place_name,
                    mapbox_relevance=1.0,
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=rating_proxy,
                    venue_category=venue_type,
                    web_signals=web_signals,
                    data_source="foursquare",
                )
            )

        items = self._filter_by_intersection(items, intersection, center)
        if items:
            logger.info(
                "Foursquare search returned %d candidates (type=%s, radius=%dm)",
                len(items), venue_type, radius_m,
            )
        return items

    def _filter_by_intersection(
        self,
        items: List[CandidateRestaurant],
        intersection: Optional[Any],
        fallback_center: Location,
    ) -> List[CandidateRestaurant]:
        """
        Filter the candidate list to those inside the intersection polygon.
        If that leaves nothing (tiny intersection or sparse candidates), keep the original and warn.
        """
        if not items or intersection is None or not HAS_SHAPELY:
            return items
        try:
            if intersection.is_empty:
                return items
            inside = [c for c in items if intersection.contains(Point(c.lon, c.lat))]
            if inside:
                logger.info(
                    "Intersection filter: %d -> %d candidates (kept only those in the overlap)",
                    len(items), len(inside),
                )
                return inside
            logger.warning(
                "No candidates after intersection filter - keeping all %d and reranking by center distance (conservative)",
                len(items),
            )
            # conservative: rerank by distance to the intersection centroid
            centroid = intersection.centroid
            c_loc = Location(lat=centroid.y, lon=centroid.x)
            for c in items:
                c.distance_to_center_km = self.haversine_km(c_loc, Location(c.lat, c.lon))
            items.sort(key=lambda x: x.distance_to_center_km)
            return items
        except Exception as exc:
            logger.warning("Intersection filter error - keeping the original list: %s", exc)
            return items

    def _offline_sample_venues(
        self,
        center: Location,
        venue_type: str = "restaurant",
        limit: int = 6,
        intersection: Optional[Any] = None,
    ) -> List[CandidateRestaurant]:
        """Offline demo-sample fallback: return clearly-labeled sample venues when all live lookups fail.

        Called when venue-network issues, a dead Mapbox key, or regional Overpass blocking
        occur, so the demo always has results - the paper's 'graceful degradation, not silent crash'.

        To keep the map sensible at any center, the whole Riverside sample cluster is shifted by
        (center - Riverside anchor) to the user's actual center, preserving relative layout.
        status/queue/crowd are illustrative; candidate.is_sample=True and the UI must label it.
        """
        # Auxiliary categories (parking/gas) have no sample data; faking with restaurants would mislead -> return empty.
        if venue_type not in _OFFLINE_SAMPLE_VENUES and venue_type in ("parking", "gas_station"):
            return []
        table = _OFFLINE_SAMPLE_VENUES.get(venue_type) or _OFFLINE_SAMPLE_VENUES["restaurant"]
        anchor_lat, anchor_lon = _RIVERSIDE_DOWNTOWN
        # If the user's center is far from Riverside, shift the sample cluster near it (still sample data).
        far_from_anchor = self.haversine_km(center, Location(anchor_lat, anchor_lon)) > 40.0
        dlat = center.lat - anchor_lat if far_from_anchor else 0.0
        dlon = center.lon - anchor_lon if far_from_anchor else 0.0

        items: List[CandidateRestaurant] = []
        for (name, lat, lon, rating, status, queue, crowd, wait) in table:
            plat, plon = lat + dlat, lon + dlon
            pos = Location(plat, plon)
            c = CandidateRestaurant(
                name=name,
                lat=plat,
                lon=plon,
                place_name=f"{name} · Riverside, CA (demo sample)",
                mapbox_relevance=0.6,
                distance_to_center_km=self.haversine_km(center, pos),
                rating_proxy=rating,
                venue_category=venue_type,
                data_source="offline_sample",
                is_sample=True,
            )
            c.web_signals = {
                "status": status,
                "queue_level": queue,
                "crowd_index": crowd,
                "estimated_wait_minutes": wait,
                "promo_bonus": 0.0,
                "risk_penalty": 0.0,
                "confidence": "sample",
                "reason": "Offline demo sample: status/queue/crowd are illustrative estimates, not live results.",
                "source": "offline_sample",
            }
            items.append(c)

        items.sort(key=lambda x: x.distance_to_center_km)
        items = self._filter_by_intersection(items, intersection, center)
        result = items[: max(1, limit)]
        logger.warning(
            "All live lookups failed - using offline demo sample data (%d, type=%s).",
            len(result), venue_type,
        )
        return result

    def search_nearby_restaurants(
        self, center: Location, cuisine: str, limit: int = 12
    ) -> List[CandidateRestaurant]:
        """Backward-compatible wrapper that delegates to search_nearby_venues."""
        return self.search_nearby_venues(
            center=center, venue_type="restaurant", keyword=cuisine, limit=limit
        )

    def geocode_address(self, address: str, city_hint: str = "") -> Optional[Location]:
        """Geocode an address (Mapbox first, OSM fallback)."""
        query = address.strip()
        if city_hint and city_hint not in query:
            query = f"{city_hint} {query}"

        url = f"{MAPBOX_GEOCODE_BASE}/{requests.utils.quote(query)}.json"
        params = {
            "access_token": self.mapbox_token,
            "language": "zh",
            "limit": 1,
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if features:
                lon, lat = features[0]["center"]
                logger.info("Geocoded (Mapbox): %s -> (%.6f, %.6f)", address, lat, lon)
                return Location(lat=float(lat), lon=float(lon))
        except Exception as exc:
            logger.warning("Mapbox geocoding failed: %s", exc)

        return self._geocode_address_osm(address, city_hint)

    def _geocode_address_osm(self, address: str, city_hint: str = "") -> Optional[Location]:
        """OSM geocoding fallback."""
        query = address.strip()
        if city_hint and city_hint not in query:
            query = f"{city_hint} {query}"

        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": query,
            "format": "json",
            "limit": 1,
        }
        headers = {"User-Agent": "MeetHalfwayAI/2.0 (hackathon demo)"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                lat = float(rows[0]["lat"])
                lon = float(rows[0]["lon"])
                logger.info("Geocoded (OSM): %s -> (%.6f, %.6f)", address, lat, lon)
                return Location(lat=lat, lon=lon)
        except Exception as exc:
            logger.warning("OSM geocoding failed: %s", exc)
        return None

    def _search_osm(
        self, center: Location, query: str, limit: int, venue_category: str = "restaurant"
    ) -> List[CandidateRestaurant]:
        """OSM Nominatim fallback search (used when Mapbox is unavailable)."""
        logger.info("Using OSM Nominatim fallback search ...")
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": query,
            "format": "json",
            "limit": limit,
            "addressdetails": 1,
            "viewbox": (
                f"{center.lon - 0.05},{center.lat + 0.05},"
                f"{center.lon + 0.05},{center.lat - 0.05}"
            ),
            "bounded": 1,
        }
        headers = {"User-Agent": "MeetHalfwayAI/2.0 (hackathon demo)"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("OSM fallback search also failed: %s", exc)
            return []

        items = []
        for row in data:
            pos = Location(lat=float(row["lat"]), lon=float(row["lon"]))
            items.append(
                CandidateRestaurant(
                    name=row.get("display_name", "Unknown")[:50],
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=row.get("display_name", ""),
                    mapbox_relevance=0.5,
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=0.5,
                    venue_category=venue_category,
                    data_source="nominatim",
                )
            )
        venue_display = VENUE_TYPES.get(venue_category, {}).get("display", venue_category)
        logger.info("OSM returned %d candidates (type=%s)", len(items), venue_display)
        return items

    def _search_overpass(
        self,
        center: Location,
        venue_type: str = "restaurant",
        limit: int = 12,
        intersection: Optional[Any] = None,
    ) -> List[CandidateRestaurant]:
        """
        Overpass QL venue search (no key); single request to avoid hammering.

        intersection: if a polygon is given, use its bbox as the Overpass query bounds
                      and post-filter results to inside the polygon (shared reachable area only).
        """
        selectors = _OVERPASS_FILTERS.get(venue_type, _OVERPASS_FILTERS["restaurant"])

        # Prefer the intersection bbox over a fixed circular radius when available
        if intersection is not None and HAS_SHAPELY:
            try:
                minx, miny, maxx, maxy = intersection.bounds  # (lon_min,lat_min,lon_max,lat_max)
                # convert to Overpass bbox format: south,west,north,east
                bbox_str = f"{miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f}"
                selector_block = "\n".join(
                    f"  {s}({bbox_str});" for s in selectors
                )
                logger.info(
                    "Overpass bbox query: SW(%.5f,%.5f) NE(%.5f,%.5f)",
                    miny, minx, maxy, maxx,
                )
            except Exception as exc:
                logger.warning("Failed to extract intersection bbox, using fixed radius: %s", exc)
                intersection = None  # force fallback

        if intersection is None:
            radius_m = 4500
            selector_block = "\n".join(
                f"  {s}(around:{radius_m},{center.lat:.6f},{center.lon:.6f});" for s in selectors
            )

        query = (
            "[out:json][timeout:20];\n"
            "(\n"
            f"{selector_block}\n"
            ");\n"
            "out center tags;"
        )

        result_json = self._post_overpass_with_retry(query, timeout=25)
        if result_json is None:
            logger.warning("Overpass venue search failed")
            return []
        rows = result_json.get("elements", [])

        dedup = set()
        items: List[CandidateRestaurant] = []
        for row in rows:
            lat = row.get("lat")
            lon = row.get("lon")
            if lat is None or lon is None:
                center_obj = row.get("center") or {}
                lat = center_obj.get("lat")
                lon = center_obj.get("lon")
            if lat is None or lon is None:
                continue

            tags = row.get("tags") or {}
            name = str(tags.get("name") or "Unknown")
            key = (name.lower(), round(float(lat), 5), round(float(lon), 5))
            if key in dedup:
                continue
            dedup.add(key)

            address = tags.get("addr:full") or tags.get("addr:street") or tags.get("name", "")
            pos = Location(lat=float(lat), lon=float(lon))
            items.append(
                CandidateRestaurant(
                    name=name[:80],
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=str(address),
                    mapbox_relevance=0.5,
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=0.5,
                    venue_category=venue_type,
                    data_source="overpass",
                )
            )

        items.sort(key=lambda x: x.distance_to_center_km)
        # If an intersection is given, filter to venues inside the polygon
        items = self._filter_by_intersection(items, intersection, center)
        result = items[: max(1, limit)]
        logger.info("Overpass returned %d candidates (type=%s)", len(result), venue_type)
        return result

    # -----------------------------------------------------------------------
    # Async concurrent web-signal collection + LLM semantic extraction
    # -----------------------------------------------------------------------
    async def _fetch_tavily(
        self,
        client: httpx.AsyncClient,
        candidate: CandidateRestaurant,
        city_hint: str,
        year_hint: int,
        time_slot: str,
        party_size: int,
    ) -> Dict[str, Any]:
        """Per-venue web collection (Tavily first, DuckDuckGo fallback)."""
        venue_display = VENUE_TYPES.get(candidate.venue_category, {}).get("display", "venue")
        query = (
            f"{candidate.name} {city_hint} {time_slot} {party_size} people "
            f"{venue_display} open status crowd wait queue deals {year_hint}"
        )
        if not self.tavily_key:
            return await self._fetch_duckduckgo(
                query,
                candidate,
                time_slot,
                party_size,
                fallback_reason="missing_tavily_key",
            )

        payload = {
            "api_key": self.tavily_key,
            "query": query,
            "search_depth": self._tavily_search_depth,
            "include_answer": True,
            "max_results": self._tavily_max_results,
        }
        try:
            r = await client.post(TAVILY_SEARCH_URL, json=payload, timeout=30)
            r.raise_for_status()
            result = r.json()
        except Exception as exc:
            logger.warning("Tavily search failed [%s]: %s", candidate.name, exc)
            return await self._fetch_duckduckgo(
                query,
                candidate,
                time_slot,
                party_size,
                fallback_reason=f"tavily_failed: {type(exc).__name__}",
            )

        answer = (result.get("answer") or "").strip()
        snippets = [
            {
                "title": row.get("title", ""),
                "content": row.get("content", "")[:400],
                "url": row.get("url", ""),
            }
            for row in result.get("results", [])
        ]

        # LLM semantic extraction (preferred), keyword matching (fallback)
        if self.use_llm_extraction and self._openai_ok and self.openai_key:
            signals = await self._llm_extract(
                candidate.name,
                answer,
                snippets,
                time_slot=time_slot,
                party_size=party_size,
                venue_type=candidate.venue_category,
            )
        else:
            signals = self._keyword_extract(answer, snippets)

        signals.update(
            {
                "query": query,
                "answer": answer,
                "snippets": snippets,
                "web_source": "tavily",
                "fallback_reason": "",
                "fetch_error": "",
            }
        )
        return signals

    async def _fetch_duckduckgo(
        self,
        query: str,
        candidate: CandidateRestaurant,
        time_slot: str,
        party_size: int,
        fallback_reason: str = "",
    ) -> Dict[str, Any]:
        """DuckDuckGo search fallback (no API key needed)."""

        def _ddg_text(q: str) -> List[Dict[str, Any]]:
            from ddgs import DDGS  # noqa: PLC0415

            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=5))

        try:
            rows = await asyncio.to_thread(_ddg_text, query)
        except Exception as exc:
            logger.warning("DuckDuckGo search failed [%s]: %s", candidate.name, exc)
            out = self._default_signals(f"ddg_failed: {exc}")
            out.update(
                {
                    "web_source": "duckduckgo",
                    "fallback_reason": fallback_reason or "direct_duckduckgo",
                    "fetch_error": str(exc)[:120],
                }
            )
            return out

        snippets = [
            {
                "title": str(r.get("title") or "")[:120],
                "content": str(r.get("body") or "")[:400],
                "url": str(r.get("href") or r.get("url") or ""),
            }
            for r in rows
        ]
        answer = "\n".join(s["content"] for s in snippets if s.get("content"))[:1200]

        if self.use_llm_extraction and self._openai_ok and self.openai_key:
            signals = await self._llm_extract(
                candidate.name,
                answer,
                snippets,
                time_slot=time_slot,
                party_size=party_size,
                venue_type=candidate.venue_category,
            )
        else:
            signals = self._keyword_extract(answer, snippets)

        signals.update(
            {
                "query": query,
                "answer": answer,
                "snippets": snippets,
                "web_source": "duckduckgo",
                "fallback_reason": fallback_reason or "direct_duckduckgo",
                "fetch_error": "",
            }
        )
        return signals

    async def _llm_extract(
        self,
        restaurant_name: str,
        answer: str,
        snippets: List[Dict],
        time_slot: str,
        party_size: int,
        venue_type: str = "restaurant",
    ) -> Dict[str, Any]:
        """
        Feed the Tavily search results to the LLM and return structured JSON.

        Prompt goals:
        - detect euphemistic closures (e.g. 'closed for a month for a family event' -> closed)
        - extract a confidence field to avoid overconfidence
        - force JSON output so hallucinations don't pollute scoring
        """
        snippet_text = "\n".join(
            f"[{i + 1}] {s['title']}\n{s['content']}" for i, s in enumerate(snippets[:5])
        )
        venue_display = VENUE_TYPES.get(venue_type, {}).get("display", "venue")
        system_prompt = (
            f"You extract real-time info for {venue_display} venues. Read the search results carefully (including subtle phrasing), "
            "and return valid JSON with ONLY the following fields and no other text:\n"
            "  status        : 'open' | 'closed' | 'uncertain'\n"
            "  promo_bonus   : 0.0-1.0 (discount/coupon/deal -> 0.6, none -> 0.0)\n"
            "  queue_level   : 'low' | 'medium' | 'high' | 'unknown'\n"
            "  crowd_index   : 0.0-1.0 (how crowded at this time; higher = busier)\n"
            "  estimated_wait_minutes : 0-180 (estimated wait for this time and party size)\n"
            "  risk_penalty  : 0.0-1.0 (closure/renovation/hygiene complaints -> 0.8, none -> 0.0)\n"
            "  confidence    : 'high' | 'medium' | 'low'\n"
            "  reason        : a short English explanation (<= 20 words)\n"
            "Note: if the text implies closure (e.g. temporarily closed, under renovation, on hiatus) "
            "mark status=closed. When information is insufficient, set confidence=low."
        )
        user_msg = (
            f"{venue_display} name: {restaurant_name}\n\n"
            f"Scenario: {time_slot}, {party_size} people\n\n"
            f"Tavily summary:\n{answer}\n\n"
            f"Search snippets:\n{snippet_text}"
        )
        try:
            aclient = self._get_async_openai_client()
            if aclient is None:
                raise RuntimeError("openai_client_unavailable")
            resp = await aclient.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            logger.debug(
                "LLM extract [%s] -> status=%s reason=%s",
                restaurant_name,
                data.get("status"),
                data.get("reason", ""),
            )
            return {
                "status": data.get("status", "uncertain"),
                "risk_penalty": float(data.get("risk_penalty", 0.0)),
                "promo_bonus": float(data.get("promo_bonus", 0.0)),
                "queue_level": data.get("queue_level", "unknown"),
                "crowd_index": min(max(float(data.get("crowd_index", 0.5)), 0.0), 1.0),
                "estimated_wait_minutes": max(int(float(data.get("estimated_wait_minutes", 0) or 0)), 0),
                "confidence": data.get("confidence", "low"),
                "reason": data.get("reason", ""),
            }
        except Exception as exc:
            logger.warning(
                "LLM extraction failed [%s]: %s - falling back to keyword matching", restaurant_name, exc
            )
            self._openai_ok = False  # don't retry the LLM again this session
            return self._keyword_extract(answer, snippets)

    async def _fetch_yelp(
        self,
        client: httpx.AsyncClient,
        candidate: CandidateRestaurant,
    ) -> Dict[str, Any]:
        """Query Yelp Fusion rating and review count to reinforce rating_proxy."""
        if not self.yelp_api_key:
            return {"matched": False, "source": "yelp", "error": "missing_yelp_api_key"}

        params = {
            "term": candidate.name,
            "latitude": candidate.lat,
            "longitude": candidate.lon,
            "limit": 1,
            "radius": 500,
            "sort_by": "best_match",
        }
        headers = {"Authorization": f"Bearer {self.yelp_api_key}"}
        async with self._yelp_sem:
            for attempt in range(self._http_max_retries):
                try:
                    r = await client.get(YELP_SEARCH_URL, params=params, headers=headers, timeout=20)
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(
                            f"retryable status: {r.status_code}",
                            request=r.request,
                            response=r,
                        )

                    r.raise_for_status()
                    businesses = r.json().get("businesses", [])
                    if not businesses:
                        return {"matched": False, "source": "yelp", "error": "no_match"}

                    top = businesses[0]
                    rating = float(top.get("rating", 0.0) or 0.0)
                    review_count = int(top.get("review_count", 0) or 0)
                    rating_norm = _clip01(rating / 5.0)
                    # review-count confidence adjustment: avoid inflating scores from few reviews
                    review_conf = _clip01(math.log1p(review_count) / math.log(501))
                    blended = _clip01(0.8 * rating_norm + 0.2 * review_conf)

                    return {
                        "matched": True,
                        "source": "yelp",
                        "name": top.get("name", ""),
                        "rating": rating,
                        "review_count": review_count,
                        "rating_norm": rating_norm,
                        "blended_rating": blended,
                    }
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status in (429, 500, 502, 503, 504) and attempt < self._http_max_retries - 1:
                        delay = self._backoff_seconds(attempt)
                        logger.warning(
                            "Yelp throttled/error [%s] status=%s; retrying in %.2fs (%d/%d)",
                            candidate.name,
                            status,
                            delay,
                            attempt + 1,
                            self._http_max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Yelp query failed [%s]: %s", candidate.name, exc)
                    return {
                        "matched": False,
                        "source": "yelp",
                        "status_code": status,
                        "error": str(exc)[:120],
                    }
                except Exception as exc:
                    if attempt < self._http_max_retries - 1:
                        delay = self._backoff_seconds(attempt)
                        logger.warning(
                            "Yelp request error [%s]: %s; retrying in %.2fs (%d/%d)",
                            candidate.name,
                            exc,
                            delay,
                            attempt + 1,
                            self._http_max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Yelp query failed [%s]: %s", candidate.name, exc)
                    return {"matched": False, "source": "yelp", "error": str(exc)[:120]}

        return {"matched": False, "source": "yelp", "error": "retry_exhausted"}

    @staticmethod
    def _keyword_extract(answer: str, snippets: List[Dict]) -> Dict[str, Any]:
        """Keyword-matching fallback (when the LLM is unavailable)."""
        blob = answer + "\n" + "\n".join(s.get("content", "") for s in snippets)

        status = "uncertain"
        risk_penalty = 0.0
        if any(
            k in blob
            for k in ["permanently closed", "temporarily closed", "closed for renovation", "out of business", "shut down"]
        ):
            status = "closed"
            risk_penalty = 0.8
        elif any(k in blob for k in ["open now", "now open", "open today", "business hours"]):
            status = "open"

        promo_bonus = (
            0.6
            if any(k in blob for k in ["discount", "deal", "coupon", "promo", "happy hour", "special offer"])
            else 0.0
        )
        queue_level = (
            "high"
            if any(k in blob for k in ["wait", "line", "queue", "busy", "crowded"])
            else "unknown"
        )
        crowd_index = 0.75 if queue_level == "high" else 0.45
        estimated_wait_minutes = 35 if queue_level == "high" else 0
        return {
            "status": status,
            "risk_penalty": risk_penalty,
            "promo_bonus": promo_bonus,
            "queue_level": queue_level,
            "crowd_index": crowd_index,
            "estimated_wait_minutes": estimated_wait_minutes,
            "confidence": "low",
            "reason": "keyword_fallback",
        }

    @staticmethod
    def _default_signals(msg: str = "") -> Dict[str, Any]:
        return {
            "status": "uncertain",
            "risk_penalty": 0.0,
            "promo_bonus": 0.0,
            "queue_level": "unknown",
            "crowd_index": 0.5,
            "estimated_wait_minutes": 0,
            "confidence": "low",
            "reason": msg[:80],
            "answer": "",
            "snippets": [],
            "web_source": "unknown",
            "fallback_reason": "",
            "fetch_error": "",
        }

    def _seed_default_web_signals(self, candidates: List[CandidateRestaurant], reason: str = "") -> None:
        for c in candidates:
            if not c.web_signals:
                c.web_signals = self._default_signals(reason)

    def _pick_candidates_for_enrichment(
        self,
        candidates: List[CandidateRestaurant],
    ) -> List[CandidateRestaurant]:
        if not candidates:
            return []
        limit = self.max_enriched_candidates
        if limit is None or limit >= len(candidates):
            return candidates

        ranked = sorted(
            candidates,
            key=lambda c: (
                0 if c.in_isochrone_intersection else 1,
                c.distance_to_center_km,
                -float(c.mapbox_relevance),
                -float(c.rating_proxy),
            ),
        )
        selected = ranked[:limit]
        logger.info(
            "Low-cost mode: enriching only the top %d/%d candidates; others use default web signals.",
            len(selected),
            len(candidates),
        )
        return selected

    async def enrich_all_async(
        self,
        candidates: List[CandidateRestaurant],
        city_hint: str,
        year_hint: int = 2026,
        time_slot: str = "tonight 19:00",
        party_size: int = 2,
    ) -> None:
        """
        Concurrently fetch web signals for all candidates (asyncio.gather, 5-10x faster).

        Zero-footprint: all intermediate data lives only on this stack frame, never written to disk.
        """
        logger.info("Starting concurrent web-signal collection for %d venues ...", len(candidates))
        self._seed_default_web_signals(candidates, "not_enriched")
        # Offline samples already carry demo signals; skip live enrichment to save tokens and
        # avoid running real web lookups on shifted sample coordinates (misleading results).
        enrichable = [c for c in candidates if not c.is_sample]
        active_candidates = self._pick_candidates_for_enrichment(enrichable)
        if not active_candidates:
            return

        t0 = time.monotonic()
        async with httpx.AsyncClient() as client:
            tavily_tasks = [
                self._fetch_tavily(client, c, city_hint, year_hint, time_slot, party_size)
                for c in active_candidates
            ]
            tavily_results = await asyncio.gather(*tavily_tasks, return_exceptions=True)
            if self.use_yelp and self.yelp_api_key:
                yelp_tasks = [self._fetch_yelp(client, c) for c in active_candidates]
                yelp_results = await asyncio.gather(*yelp_tasks, return_exceptions=True)
            else:
                yelp_results = [{} for _ in active_candidates]

        for c, sig, yelp in zip(active_candidates, tavily_results, yelp_results):
            if isinstance(sig, dict):
                c.web_signals = sig
            else:
                c.web_signals = self._default_signals(str(sig))

            if isinstance(yelp, dict) and yelp:
                c.web_signals["yelp"] = yelp
                if yelp.get("matched") and "blended_rating" in yelp:
                    c.rating_proxy = _clip01(
                        0.5 * c.rating_proxy + 0.5 * float(yelp["blended_rating"])
                    )

        elapsed = time.monotonic() - t0
        logger.info(
            "Concurrent collection done in %.2fs (avg %.2fs/venue)",
            elapsed,
            elapsed / max(len(candidates), 1),
        )

    # -----------------------------------------------------------------------
    # Scoring (time fairness + isochrone + exponential penalty)
    # -----------------------------------------------------------------------
    def score_candidates(
        self,
        a: Location,
        b: Location,
        center: Location,
        candidates: List[CandidateRestaurant],
        w_dist: float,
        w_rating: float,
        w_pref: float,
        tired_person: Optional[str] = None,
        time_slots: Optional[List[str]] = None,
        availability: Optional[Dict[str, List[str]]] = None,
        place_votes: Optional[Dict[str, Dict[str, float]]] = None,
        time_votes: Optional[Dict[str, Dict[str, float]]] = None,
        radius_tolerance: Optional[Dict[str, float]] = None,
        time_conflict: bool = False,
    ) -> List[CandidateRestaurant]:
        """
        MCDM scoring v2:

        1. Time fairness instead of distance fairness
           - use travel minutes instead of km difference
           - exponential penalty: score drops sharply once the gap exceeds ~20 min
             penalty = exp(|ta - tb| / 10) - 1

        2. Isochrone-membership reward/penalty
           - inside the intersection: +0.4 bonus
           - outside the intersection: -0.6 penalty

        3. Fatigue parameter (tired_person='a'|'b')
           - the tired side's travel-time factor is scaled by 1.3 and the center leans toward them
        """
        max_center_dist = max((c.distance_to_center_km for c in candidates), default=1.0) or 1.0
        active_slots = time_slots or ["flexible"]

        a_fatigue = 1.3 if tired_person == "a" else 1.0
        b_fatigue = 1.3 if tired_person == "b" else 1.0

        for c in candidates:
            rest = Location(c.lat, c.lon)
            da_km = self.haversine_km(a, rest)
            db_km = self.haversine_km(b, rest)
            ta_min = self._travel_minutes(a, rest) * a_fatigue
            tb_min = self._travel_minutes(b, rest) * b_fatigue

            c.fairness_delta_km = abs(da_km - db_km)
            c.fairness_delta_minutes = abs(
                self._travel_minutes(a, rest) - self._travel_minutes(b, rest)
            )

            # Core: exponential time-gap penalty (avoid extreme unfairness like 5 min vs 50 min)
            time_gap = abs(ta_min - tb_min)
            fairness_penalty = math.exp(time_gap / 10.0) - 1.0  # 0 when perfectly fair
            fairness_balance = _clip01(1.0 - time_gap / max(ta_min + tb_min, 1.0))

            dist_component = 1.0 - min(c.distance_to_center_km / max_center_dist, 1.0)
            rating_component = min(max(c.rating_proxy, 0.0), 1.0)
            pref_component = _clip01(
                0.6 * max(0.0, 1.0 - fairness_penalty / 5.0)
                + 0.4 * fairness_balance
            )

            (
                c.best_time_slot,
                c.availability_overlap,
                c.radius_tolerance_score,
                c.time_vote_score,
                c.closest_time_gap_minutes,
                c.severe_time_gap,
            ) = self._time_negotiation_for_candidate(
                c,
                a,
                b,
                active_slots,
                availability,
                time_votes,
                radius_tolerance,
                a_fatigue,
                b_fatigue,
                time_conflict,
            )
            c.time_conflict = time_conflict
            c.mutual_vote_score = self._place_vote_for_candidate(c, place_votes)

            web_bonus = float(c.web_signals.get("promo_bonus", 0.0))
            web_penalty = float(c.web_signals.get("risk_penalty", 0.0))
            status = str(c.web_signals.get("status", "uncertain")).lower()
            status_penalty = {
                "open": 0.0,
                "uncertain": 0.12,
                "closed": 1.4,
            }.get(status, 0.12)
            queue_level = str(c.web_signals.get("queue_level", "unknown")).lower()
            queue_penalty = {
                "low": 0.00,
                "medium": 0.08,
                "high": 0.18,
                "unknown": 0.05,
            }.get(queue_level, 0.05)
            crowd_index = min(max(float(c.web_signals.get("crowd_index", 0.5)), 0.0), 1.0)
            wait_minutes = max(float(c.web_signals.get("estimated_wait_minutes", 0.0)), 0.0)
            crowd_penalty = max(queue_penalty, crowd_index * 0.22 + min(wait_minutes / 180.0, 0.25))
            # Mid-high popularity/density is best: too cold or too hot both lose points
            density_balance = _clip01(1.0 - abs(crowd_index - 0.55) / 0.55)
            c.venue_popularity_score = _clip01(0.55 * rating_component + 0.45 * density_balance)
            iso_bonus = 0.4 if c.in_isochrone_intersection else -0.6

            spatiotemporal_bonus = (
                0.23 * c.radius_tolerance_score
                + 0.23 * c.availability_overlap
                + 0.18 * c.venue_popularity_score
                + 0.20 * c.mutual_vote_score
                + 0.16 * c.time_vote_score
                - 0.5
            )

            raw = (
                w_dist * dist_component
                + w_rating * rating_component
                + w_pref * pref_component
                + 0.25 * web_bonus
                - 0.5 * web_penalty
                - status_penalty
                - crowd_penalty
                + iso_bonus
                + 0.9 * spatiotemporal_bonus
            )
            c.final_score = max(0.0, raw)
            c.score_breakdown = {
                "distance": round(dist_component, 4),
                "rating": round(rating_component, 4),
                "fairness": round(pref_component, 4),
                "radius_tolerance": round(c.radius_tolerance_score, 4),
                "availability_overlap": round(c.availability_overlap, 4),
                "venue_popularity": round(c.venue_popularity_score, 4),
                "mutual_vote": round(c.mutual_vote_score, 4),
                "time_vote": round(c.time_vote_score, 4),
                "closest_time_gap_minutes": round(c.closest_time_gap_minutes, 2),
                "severe_time_gap": 1.0 if c.severe_time_gap else 0.0,
            }

            logger.debug(
                "score %-20s | dist=%.2f rat=%.2f fair=%.2f radius=%.2f avail=%.2f "
                "vote=%.2f tVote=%.2f pop=%.2f iso=%+.1f web(%+.2f/-%0.2f) crowd=-%.2f delta=%.1fmin -> %.3f",
                c.name[:20],
                dist_component,
                rating_component,
                pref_component,
                c.radius_tolerance_score,
                c.availability_overlap,
                c.mutual_vote_score,
                c.time_vote_score,
                c.venue_popularity_score,
                iso_bonus,
                web_bonus,
                web_penalty,
                crowd_penalty,
                c.fairness_delta_minutes,
                c.final_score,
            )

        candidates.sort(key=lambda x: x.final_score, reverse=True)
        return candidates

    # -----------------------------------------------------------------------
    # AI recommendation text generation
    # -----------------------------------------------------------------------
    def generate_recommendation_text(
        self,
        a: Location,
        b: Location,
        center: Location,
        top_items: List[CandidateRestaurant],
        budget: float,
        cuisine: str,
        time_slot: str = "tonight 19:00",
        party_size: int = 2,
        venue_type: str = "restaurant",
    ) -> str:
        venue_display = VENUE_TYPES.get(venue_type, {}).get("display", "venue")
        if not top_items:
            return f"No suitable {venue_display} candidates found. Try widening the search or adjusting keywords."

        structured = [
            {
                "name": x.name,
                "address": x.place_name,
                "score": round(x.final_score, 4),
                "distance_to_center_km": round(x.distance_to_center_km, 2),
                "fairness_delta_km": round(x.fairness_delta_km, 2),
                "fairness_delta_minutes": round(x.fairness_delta_minutes, 1),
                "best_time_slot": x.best_time_slot,
                "availability_overlap": round(x.availability_overlap, 2),
                "radius_tolerance_score": round(x.radius_tolerance_score, 2),
                "venue_popularity_score": round(x.venue_popularity_score, 2),
                "mutual_vote_score": round(x.mutual_vote_score, 2),
                "time_vote_score": round(x.time_vote_score, 2),
                "in_isochrone_zone": x.in_isochrone_intersection,
                "status": x.web_signals.get("status"),
                "queue": x.web_signals.get("queue_level"),
                "crowd_index": x.web_signals.get("crowd_index", 0.5),
                "wait_minutes": x.web_signals.get("estimated_wait_minutes", 0),
                "promo": x.web_signals.get("promo_bonus", 0),
                "confidence": x.web_signals.get("confidence", "low"),
                "web_reason": x.web_signals.get("reason", ""),
            }
            for x in top_items
        ]

        def _build_local_summary() -> str:
            lines = [
                f"Type: {venue_display} ({cuisine}). Budget: ${budget} per person.",
                f"Scenario: {time_slot}, {party_size} people.",
                f"Fair center: ({center.lat:.6f}, {center.lon:.6f})",
                "Recommendations:",
            ]
            for i, item in enumerate(structured, start=1):
                iso_tag = "in shared area" if item["in_isochrone_zone"] else "edge"
                lines.append(
                    f"{i}. [{iso_tag}] {item['name']} | score {item['score']} "
                    f"| gap {item['fairness_delta_minutes']} min | status {item['status']} "
                    f"| time {item['best_time_slot']} | wait {item['wait_minutes']} min"
                )
            return "\n".join(lines)

        # When no LLM is configured / summary is off, build a local template summary
        if not self.use_llm_summary or not self._openai_ok or not self.openai_key:
            return _build_local_summary()

        prompt = {
            "task": f"From the structured {venue_display} candidate data, produce a concise, actionable meeting recommendation (in English).",
            "constraints": [
                f"Prefer {venue_display} with in_isochrone_zone=true (both within reasonable travel time)",
                "Drop candidates with status=closed or risk_penalty>0.5",
                "Emphasize travel-time fairness (smaller fairness_delta_minutes is better; warn if >20 min)",
                "Note any promotions and queue/wait risk",
                "State crowd level and wait risk for the time slot and party size (low/medium/high + estimated minutes)",
                "Give a final top 3, each with one-line reasoning and a concrete action suggestion",
                "Write all explanatory text in clear English",
                "Keep the output clean and consistent; avoid mixed-language phrasing",
                "Output complete content; do not cut off mid-sentence",
            ],
            "input": {
                "person_a": {"lat": a.lat, "lon": a.lon},
                "person_b": {"lat": b.lat, "lon": b.lon},
                "center": {"lat": center.lat, "lon": center.lon},
                "budget_per_person": budget,
                "cuisine": cuisine,
                "time_slot": time_slot,
                "party_size": party_size,
                "candidates": structured,
            },
        }
        try:
            client = self._get_sync_openai_client()
            if client is None:
                raise RuntimeError("openai_client_unavailable")
            resp = client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an urban meet-up advisor. Always reply in natural, fluent English. Produce web-ready recommendation copy, avoid mid-sentence truncation.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(prompt, ensure_ascii=False),
                    },
                ],
                temperature=0.2,
                max_tokens=380 if self.low_cost_mode else 700,
            )
            return resp.choices[0].message.content or "Model returned no text."
        except Exception as exc:
            logger.error("OpenAI recommendation generation failed: %s", exc)
            return _build_local_summary()

    def build_explanations(
        self,
        candidates: List[CandidateRestaurant],
        top_k: int = 5,
    ) -> Dict[str, str]:
        """
        Generate a natural-language "why this time + place" explanation for each top candidate.

        The explanation maps one-to-one onto the score components:
        - radius_tolerance_score
        - availability_overlap
        - venue_popularity_score
        - mutual_vote_score
        - time_vote_score
        """
        def _level(value: float, hi: float, mid: float) -> str:
            return "high" if value >= hi else "medium" if value >= mid else "low"

        explanations: Dict[str, str] = {}
        selected = candidates[: max(1, top_k)]

        for idx, c in enumerate(selected, start=1):
            iso_text = "inside the shared reachable area" if c.in_isochrone_intersection else "on the edge of the reachable area"

            radius_level = _level(c.radius_tolerance_score, 0.75, 0.45)
            overlap_level = _level(c.availability_overlap, 0.75, 0.35)
            popularity_level = _level(c.venue_popularity_score, 0.7, 0.45)
            vote_level = _level(c.mutual_vote_score, 0.7, 0.45)
            t_vote_level = _level(c.time_vote_score, 0.7, 0.45)

            slot_text = c.best_time_slot or "flexible time"
            wait_text = int(float(c.web_signals.get("estimated_wait_minutes", 0) or 0))
            queue_text = str(c.web_signals.get("queue_level", "unknown"))

            lines = [
                f"#{idx} recommendation: {c.name} (suggested time: {slot_text}).",
                f"Spatial fairness: {iso_text}; commute-tolerance match is {radius_level} ({c.radius_tolerance_score:.2f}); current time gap ~{c.fairness_delta_minutes:.1f} min.",
                f"Time negotiation: shared-availability overlap is {overlap_level} ({c.availability_overlap:.2f}); joint time-vote preference for this slot is {t_vote_level} ({c.time_vote_score:.2f}).",
                f"Preference alignment: mutual venue-vote preference is {vote_level} ({c.mutual_vote_score:.2f}).",
                f"Venue state: popularity/density fit is {popularity_level} ({c.venue_popularity_score:.2f}); queue {queue_text}; estimated wait {wait_text} min.",
                f"Overall: this candidate is more balanced across both place and time, so it ranks near the top (total score {c.final_score:.3f}).",
            ]
            explanations[c.name] = "\n".join(lines)

        return explanations

    # -----------------------------------------------------------------------
    # Surprise Me mode
    # -----------------------------------------------------------------------
    def pick_surprise(
        self, candidates: List[CandidateRestaurant]
    ) -> Optional[CandidateRestaurant]:
        """
        Randomly pick a high-scoring candidate (skipping Top 1) to break the filter bubble.
        Conditions: score > 0.5, inside the isochrone, not closed.
        """
        eligible = [
            c
            for c in candidates
            if c.final_score > 0.5
            and c.in_isochrone_intersection
            and c.web_signals.get("status") not in ("closed",)
        ]
        if not eligible:
            eligible = [c for c in candidates if c.final_score > 0.3]
        return random.choice(eligible) if eligible else None

    # -----------------------------------------------------------------------
    # Interactive map (folium)
    # -----------------------------------------------------------------------
    def generate_map(
        self,
        a: Location,
        b: Location,
        center: Location,
        candidates: List[CandidateRestaurant],
        intersection: Optional[Any] = None,
        output_path: str = "meethalfway_map.html",
        surprise: Optional[CandidateRestaurant] = None,
        top_k: int = 5,
        show_user_points: bool = True,
        iso_a: Optional[Any] = None,
        iso_b: Optional[Any] = None,
        baseline_midpoint: Optional[Tuple[float, float]] = None,
        panel_label: Optional[str] = None,
        candidate_rank_labels: bool = True,
        extra_origins: Optional[List[Location]] = None,
        extra_origin_labels: Optional[List[str]] = None,
        extra_isos: Optional[List[Any]] = None,
        per_user_modes: Optional[List[str]] = None,
    ) -> str:
        """Render the interactive folium map for a recommendation.

        Extension kwargs (all optional and backward-compatible):
          * ``baseline_midpoint``: ``(lat, lon)`` tuple. Renders an orange CSS
            diamond marker labelled "Geometric midpoint (baseline)" for the
            paper's N=2 panel.
          * ``panel_label``: caption such as ``"(a) N=2 — driving 15 mi each"``
            pinned to the top-left of the map via a small HTML overlay.
          * ``candidate_rank_labels``: if True (default), top-K venue markers
            also show a ``#i`` rank chip via a DivIcon next to the cutlery pin.
          * ``extra_origins`` / ``extra_origin_labels`` / ``extra_isos``: extra
            participants beyond ``a`` and ``b`` (e.g. participant ``c`` for
            N=3). ``extra_origin_labels`` is aligned with ``extra_origins``
            and ``extra_isos`` is aligned the same way.
          * ``per_user_modes``: mode strings (``"drive"``/``"walk"``/``"transit"``)
            aligned with ``[a, b] + extra_origins``. When provided, each
            participant's isochrone polygon is styled with a mode-specific
            color and labelled accordingly.
        """
        if not HAS_FOLIUM:
            logger.warning("folium not installed - skipping map generation.")
            return ""

        m = folium.Map(location=[center.lat, center.lon], zoom_start=12)

        # Build the unified participants list (a, b, *extra_origins) plus their
        # isochrones / labels / modes so per-user-mode rendering is uniform.
        origins: List[Location] = [a, b]
        if extra_origins:
            origins.extend(extra_origins)
        default_origin_labels = ["Start A", "Start B"]
        if extra_origins:
            for i in range(len(extra_origins)):
                if extra_origin_labels and i < len(extra_origin_labels):
                    default_origin_labels.append(extra_origin_labels[i])
                else:
                    default_origin_labels.append(f"Start {chr(ord('C') + i)}")
        isos: List[Any] = [iso_a, iso_b]
        if extra_isos:
            isos.extend(extra_isos)

        # Mode-specific palette used for both iso polygons and origin pins when
        # per_user_modes is supplied. Falls back to the legacy blue/red scheme
        # for the first two participants when no per-user list is given.
        _MODE_COLORS = {
            "drive": "#2563eb",    # blue
            "walk": "#16a34a",     # green
            "transit": "#a855f7",  # purple
        }
        _MODE_PIN_COLORS = {
            "drive": "blue",
            "walk": "green",
            "transit": "purple",
        }
        legacy_iso_colors = ["#2563eb", "#dc2626"]
        legacy_pin_colors = ["blue", "red"]

        def _iso_color_for(idx: int) -> str:
            if per_user_modes and idx < len(per_user_modes):
                return _MODE_COLORS.get(per_user_modes[idx], "#6b7280")
            if idx < len(legacy_iso_colors):
                return legacy_iso_colors[idx]
            # Cycle through a palette for additional participants without modes
            extra_palette = ["#16a34a", "#a855f7", "#f59e0b", "#0ea5e9"]
            return extra_palette[(idx - len(legacy_iso_colors)) % len(extra_palette)]

        def _pin_color_for(idx: int) -> str:
            if per_user_modes and idx < len(per_user_modes):
                return _MODE_PIN_COLORS.get(per_user_modes[idx], "darkblue")
            if idx < len(legacy_pin_colors):
                return legacy_pin_colors[idx]
            extra_pins = ["green", "purple", "orange", "cadetblue"]
            return extra_pins[(idx - len(legacy_pin_colors)) % len(extra_pins)]

        # Each person's reachable isochrone (R_i), visualizing "meet halfway" fairness
        if HAS_SHAPELY:
            for idx, iso in enumerate(isos):
                if iso is None or getattr(iso, "is_empty", True):
                    continue
                color = _iso_color_for(idx)
                mode_suffix = (
                    f" ({per_user_modes[idx]})"
                    if per_user_modes and idx < len(per_user_modes)
                    else ""
                )
                label = f"{default_origin_labels[idx]} reachable isochrone{mode_suffix}"
                try:
                    folium.GeoJson(
                        mapping(iso),
                        style_function=(lambda col: (lambda _f: {
                            "fillColor": col, "color": col, "weight": 2,
                            "fillOpacity": 0.08, "dashArray": "4,4",
                        }))(color),
                        tooltip=label,
                    ).add_to(m)
                except Exception as exc:
                    logger.warning("Failed to render an individual isochrone: %s", exc)

        # Isochrone intersection layer (shared reachable area)
        if HAS_SHAPELY and intersection is not None and not intersection.is_empty:
            try:
                folium.GeoJson(
                    mapping(intersection),
                    style_function=lambda _: {
                        "fillColor": "#10b981",
                        "color": "#0f9d76",
                        "weight": 3,
                        "fillOpacity": 0.28,
                    },
                    tooltip="Shared reachable area (intersection of all participants)",
                ).add_to(m)
            except Exception as exc:
                logger.warning("Failed to render the isochrone: %s", exc)

        # Start points for each participant
        if show_user_points:
            for idx, loc in enumerate(origins):
                folium.Marker(
                    [loc.lat, loc.lon],
                    tooltip=default_origin_labels[idx],
                    icon=folium.Icon(
                        color=_pin_color_for(idx), icon="user", prefix="fa"
                    ),
                ).add_to(m)

        # Fair center (pipeline weighted midpoint, "fair" not geometric)
        folium.Marker(
            [center.lat, center.lon],
            tooltip="Fair center",
            icon=folium.Icon(color="purple", icon="map-marker", prefix="fa"),
        ).add_to(m)

        # Optional geometric-midpoint baseline marker (orange CSS diamond)
        if baseline_midpoint is not None:
            try:
                bm_lat, bm_lon = baseline_midpoint
                diamond_html = (
                    '<div style="transform: rotate(45deg); width: 16px; '
                    'height: 16px; background:#f59e0b; '
                    'border:2px solid #b45309; box-shadow:0 0 4px rgba(0,0,0,0.4);">'
                    '</div>'
                )
                folium.Marker(
                    [bm_lat, bm_lon],
                    tooltip="Geometric midpoint (baseline)",
                    icon=folium.DivIcon(
                        icon_size=(20, 20),
                        icon_anchor=(10, 10),
                        html=diamond_html,
                    ),
                ).add_to(m)
            except Exception as exc:
                logger.warning("Failed to render baseline midpoint marker: %s", exc)

        # Candidate venues
        top_names = {c.name for c in candidates[:top_k]}
        # Map name -> rank for the top-K, so labels stay stable across redraws.
        top_rank_by_name = {
            c.name: i + 1 for i, c in enumerate(candidates[:top_k])
        }
        surprise_name = surprise.name if surprise else ""
        for c in candidates:
            if c.name == surprise_name:
                color, icon_name = "orange", "star"
            elif c.name in top_names:
                color, icon_name = "green", "cutlery"
            else:
                color, icon_name = "gray", "cutlery"

            iso_tag = "✓" if c.in_isochrone_intersection else "△"
            rank = top_rank_by_name.get(c.name)
            rank_prefix = f"#{rank} " if (candidate_rank_labels and rank) else ""
            tip = (
                f"{iso_tag} {rank_prefix}{c.name}<br>"
                f"score: {c.final_score:.3f}<br>"
                f"gap: {c.fairness_delta_minutes:.1f} min<br>"
                f"status: {c.web_signals.get('status', '?')}<br>"
                f"{c.web_signals.get('reason', '')}"
            )
            if c.name == surprise_name:
                tip = "Surprise Pick!<br>" + tip

            folium.Marker(
                [c.lat, c.lon],
                tooltip=folium.Tooltip(tip),
                icon=folium.Icon(color=color, icon=icon_name, prefix="fa"),
            ).add_to(m)

            # Optional rank chip rendered next to the icon as a DivIcon overlay,
            # so the static screenshot reads "#1 ... #5" without the user
            # hovering to see the tooltip.
            if candidate_rank_labels and rank:
                chip_html = (
                    f'<div style="background:#0f9d76;color:white;'
                    f'border-radius:9px;padding:1px 6px;font-size:11px;'
                    f'font-weight:700;font-family:Arial,sans-serif;'
                    f'box-shadow:0 1px 2px rgba(0,0,0,0.3);'
                    f'white-space:nowrap;">#{rank}</div>'
                )
                folium.Marker(
                    [c.lat, c.lon],
                    icon=folium.DivIcon(
                        icon_size=(36, 18),
                        icon_anchor=(-6, 36),  # offset to the lower-right of the pin
                        html=chip_html,
                    ),
                ).add_to(m)

        # Top-left panel caption overlay (e.g. "(a) N=2 — driving 15 mi each")
        if panel_label:
            try:
                from branca.element import Element  # type: ignore
                safe_label = (
                    panel_label.replace("&", "&amp;")
                    .replace("<", "&lt;").replace(">", "&gt;")
                )
                overlay_html = (
                    '<div style="position: fixed; top: 12px; left: 12px; '
                    'z-index: 9999; background: rgba(255,255,255,0.92); '
                    'padding: 6px 12px; border: 1px solid #d1d5db; '
                    'border-radius: 6px; font-family: Arial, sans-serif; '
                    'font-size: 14px; font-weight: 600; color: #111827; '
                    'box-shadow: 0 1px 3px rgba(0,0,0,0.15);">'
                    f'{safe_label}</div>'
                )
                m.get_root().html.add_child(Element(overlay_html))
            except Exception as exc:
                logger.warning("Failed to render panel label overlay: %s", exc)

        m.save(output_path)
        logger.info("Interactive map saved: %s", output_path)
        return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MeetHalfway AI v2 - isochrone intersection, LLM semantic extraction, zero-footprint",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--a-lat", type=float, help="Start A latitude")
    parser.add_argument("--a-lon", type=float, help="Start A longitude")
    parser.add_argument("--b-lat", type=float, help="Start B latitude")
    parser.add_argument("--b-lon", type=float, help="Start B longitude")
    parser.add_argument("--a-address", type=str, default="", help="Start A address (alternative to coordinates)")
    parser.add_argument("--b-address", type=str, default="", help="Start B address (alternative to coordinates)")
    parser.add_argument("--cuisine", type=str, default="hotpot", help="Cuisine keyword")
    parser.add_argument("--budget", type=float, default=80, help="Budget per person")
    parser.add_argument(
        "--venue-type",
        type=str,
        default="restaurant",
        choices=sorted(VENUE_TYPES.keys()),
        help="Venue type (restaurant/cafe/park/mall/cinema/...)",
    )
    parser.add_argument(
        "--transport",
        type=str,
        default="transit",
        choices=["drive", "walk", "transit"],
        help="Travel mode",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Number of final recommendations")
    parser.add_argument(
        "--weight-a", type=float, default=1.0, help="A priority weight (higher pulls center toward A)"
    )
    parser.add_argument("--weight-b", type=float, default=1.0, help="B priority weight")
    parser.add_argument(
        "--tired",
        type=str,
        default="",
        choices=["", "a", "b"],
        help="Tired participant (auto-raises their weight so the center leans toward them)",
    )
    parser.add_argument(
        "--isochrone-minutes", type=int, default=20, help="Isochrone time budget (minutes)"
    )
    parser.add_argument("--city", type=str, default="", help="City name (search context)")
    parser.add_argument("--time-slot", type=str, default="tonight 19:00", help="Target time slot")
    parser.add_argument("--party-size", type=int, default=2, help="Party size")
    parser.add_argument("--low-cost", action="store_true", help="Low-cost test mode: fewer candidates and model calls")
    parser.add_argument("--enable-yelp", action="store_true", help="Enable Yelp enrichment (slower)")
    parser.add_argument("--enable-llm-summary", action="store_true", help="Enable the final LLM summary")
    parser.add_argument(
        "--max-enriched-candidates",
        type=int,
        default=0,
        help="Enrich only the top N candidates (0 = auto by mode)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--map", action="store_true", help="Generate an interactive folium map (HTML)")
    parser.add_argument(
        "--map-output", type=str, default="meethalfway_map.html", help="Map output path"
    )
    parser.add_argument(
        "--surprise", action="store_true", help="Surprise mode: suggest a high-scoring under-the-radar venue"
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG-level logging")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main entry (async)
# ---------------------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> None:
    # Resolve .env next to this module so CLI runs work from any directory.
    load_dotenv(Path(__file__).with_name(".env"))

    if args.verbose:
        logging.getLogger("meethalfway").setLevel(logging.DEBUG)

    mapbox_token = os.getenv("MAPBOX_ACCESS_TOKEN", "")
    ors_api_key = (
        os.getenv("OPENROUTESERVICE_API_KEY")
        or os.getenv("ORS_API_KEY")
        or ""
    )
    yelp_api_key = os.getenv("YELP_API_KEY", "").strip() or None
    tavily_key = os.getenv("TAVILY_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY")
    openai_base = os.getenv("OPENAI_API_BASE", "").strip() or None
    openai_model = (
        os.getenv("MODEL_NAME")
        or os.getenv("OPENAI_MODEL")
        or "gpt-4o-mini"
    )

    if not tavily_key:
        logger.info("TAVILY_API_KEY not set: falling back to keyless DuckDuckGo search.")

    foursquare_api_key = os.getenv("FOURSQUARE_API_KEY", "").strip() or None

    engine = MeetHalfwayRecommender(
        mapbox_token=mapbox_token,
        ors_api_key=ors_api_key,
        yelp_api_key=yelp_api_key,
        tavily_key=tavily_key,
        openai_key=openai_key,
        openai_model=openai_model,
        openai_base=openai_base,
        foursquare_api_key=foursquare_api_key,
        transport=args.transport,
        isochrone_minutes=args.isochrone_minutes,
        low_cost_mode=args.low_cost,
        use_yelp=args.enable_yelp,
        use_llm_extraction=True,
        use_llm_summary=args.enable_llm_summary,
        max_enriched_candidates=(args.max_enriched_candidates or None),
    )

    def _resolve_location(
        lat: Optional[float],
        lon: Optional[float],
        address: str,
        person_name: str,
    ) -> Location:
        if lat is not None and lon is not None:
            return Location(lat, lon)

        if address.strip():
            loc = engine.geocode_address(address=address, city_hint=args.city.strip())
            if loc is not None:
                return loc
            raise RuntimeError(f"{person_name}: address lookup failed; provide a more specific address or use coordinates.")

        raise RuntimeError(
            f"{person_name}: missing location. Provide coordinates (--{person_name.lower()}-lat/--{person_name.lower()}-lon)"
            f" or an address (--{person_name.lower()}-address)."
        )

    a = _resolve_location(args.a_lat, args.a_lon, args.a_address, "A")
    b = _resolve_location(args.b_lat, args.b_lon, args.b_address, "B")

    # Step 1: weighted midpoint (initial seed)
    center = engine.compute_weighted_midpoint(a, b, args.weight_a, args.weight_b)
    logger.info("Weighted center: (%.6f, %.6f)", center.lat, center.lon)

    # Step 2: isochrones (one polygon each for A and B)
    logger.info("Fetching isochrone polygons (%d min, %s) ...", args.isochrone_minutes, args.transport)
    iso_a = engine.get_isochrone(a)
    iso_b = engine.get_isochrone(b)
    intersection = engine.compute_intersection(iso_a, iso_b)

    # Step 2b: subtract water/forest and other unreachable natural features from the intersection
    intersection = engine.subtract_natural_barriers(intersection)

    # Step 3: search nearby venues
    limit = engine.recommend_search_limit(args.top_k)
    raw_candidates = engine.search_nearby_venues(
        center=center,
        venue_type=args.venue_type,
        keyword=args.cuisine,
        limit=limit,
    )

    if not raw_candidates:
        venue_display = VENUE_TYPES.get(args.venue_type, {}).get("display", "venue")
        print(f"No {venue_display} candidates found. Check the coordinates or keywords.")
        return

    # Step 4: tag isochrone-intersection membership
    engine.tag_with_isochrone(raw_candidates, intersection)

    # Step 4b: POI-density hard filter - drop isolated, low-density candidates
    raw_candidates = engine.filter_by_poi_density(raw_candidates)
    if not raw_candidates:
        venue_display = VENUE_TYPES.get(args.venue_type, {}).get("display", "venue")
        print(f"No {venue_display} candidates after POI-density filtering. Adjust the location or keywords.")
        return

    # Step 5: concurrent web collection + LLM extraction (zero-footprint: nothing written to disk)
    city_hint = args.city.strip() or "local area"
    await engine.enrich_all_async(
        raw_candidates,
        city_hint=city_hint,
        year_hint=2026,
        time_slot=args.time_slot,
        party_size=max(1, args.party_size),
    )

    # Step 6: score and rank
    tired_person = args.tired.lower() if args.tired else None
    scored = engine.score_candidates(
        a, b, center, raw_candidates,
        w_dist=0.35, w_rating=0.30, w_pref=0.35,
        tired_person=tired_person,
    )
    top_items = scored[: args.top_k]

    # Step 7: surprise pick
    surprise_pick = engine.pick_surprise(scored) if args.surprise else None

    # Step 8: AI recommendation text
    summary = engine.generate_recommendation_text(
        a,
        b,
        center,
        top_items,
        args.budget,
        args.cuisine,
        time_slot=args.time_slot,
        party_size=max(1, args.party_size),
        venue_type=args.venue_type,
    )

    # Step 9: map
    map_path = ""
    if args.map:
        map_path = engine.generate_map(
            a, b, center, scored, intersection,
            output_path=args.map_output,
            surprise=surprise_pick,
            top_k=args.top_k,
            iso_a=iso_a,
            iso_b=iso_b,
        )

    # Build the result dict
    result = {
        "meta": {
            "version": "2.0",
            "algorithm": "Mapbox Isochrone Intersection + GPT-4o-mini semantic extraction + exponential fairness penalty",
            "privacy": "Zero-footprint design: user GPS coordinates live only on the in-memory call stack and are discarded on return; never persisted.",
        },
        "inputs": {
            "a": {"lat": a.lat, "lon": a.lon},
            "b": {"lat": b.lat, "lon": b.lon},
            "cuisine": args.cuisine,
            "venue_type": args.venue_type,
            "budget": args.budget,
            "transport": args.transport,
            "isochrone_minutes": args.isochrone_minutes,
            "time_slot": args.time_slot,
            "party_size": max(1, args.party_size),
            "tired_person": tired_person,
        },
        "center": {"lat": center.lat, "lon": center.lon},
        "isochrone_intersection_active": intersection is not None,
        "top_candidates": [
            {
                "name": x.name,
                "place_name": x.place_name,
                "lat": x.lat,
                "lon": x.lon,
                "final_score": round(x.final_score, 4),
                "distance_to_center_km": round(x.distance_to_center_km, 3),
                "fairness_delta_km": round(x.fairness_delta_km, 3),
                "fairness_delta_minutes": round(x.fairness_delta_minutes, 1),
                "best_time_slot": x.best_time_slot,
                "availability_overlap": round(x.availability_overlap, 3),
                "radius_tolerance_score": round(x.radius_tolerance_score, 3),
                "venue_popularity_score": round(x.venue_popularity_score, 3),
                "mutual_vote_score": round(x.mutual_vote_score, 3),
                "time_vote_score": round(x.time_vote_score, 3),
                "score_breakdown": x.score_breakdown,
                "in_isochrone_zone": x.in_isochrone_intersection,
                "web_signals": x.web_signals,
            }
            for x in top_items
        ],
        "surprise_pick": (
            {
                "name": surprise_pick.name,
                "place_name": surprise_pick.place_name,
                "score": round(surprise_pick.final_score, 4),
            }
            if surprise_pick
            else None
        ),
        "summary": summary,
        "map_file": map_path,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Human-readable output
    print("=" * 72)
    print("MeetHalfway AI v2 - Recommendations")
    print("=" * 72)
    venue_display = VENUE_TYPES.get(args.venue_type, {}).get("display", "venue")
    print(f"Venue type   : {venue_display}")
    print(f"Fair center  : ({center.lat:.6f}, {center.lon:.6f})")
    print(
        f"Shared area  : {'enabled (isochrone intersection)' if intersection is not None else 'radius approximation fallback'}"
    )
    print(f"Web signals  : {'LLM extraction' if engine._openai_ok else 'keyword fallback'}")
    print(f"Candidates   : {len(scored)}, showing top {len(top_items)}")
    print("-" * 72)
    for i, item in enumerate(result["top_candidates"], start=1):
        iso_tag = "in shared area" if item["in_isochrone_zone"] else "edge"
        ws = item["web_signals"]
        print(
            f"{i}. [{iso_tag}] {item['name']}\n"
            f"   score={item['final_score']}  gap={item['fairness_delta_minutes']}min  "
            f"status={ws.get('status')}  confidence={ws.get('confidence', '?')}  "
            f"reason={ws.get('reason', '-')}"
        )
    if surprise_pick:
        print("-" * 72)
        print(
            f"[Surprise Pick] {surprise_pick.name}  (score={round(surprise_pick.final_score, 4)})"
        )
    print("-" * 72)
    print("AI recommendation summary:")
    print(result["summary"])
    if map_path:
        print(f"\nInteractive map saved: {map_path}")
    print("\n[Zero-footprint] Computation complete; no user location data was persisted.")


def main() -> None:
    # Windows consoles default to GBK; printing JSON with emoji/special chars raises UnicodeEncodeError.
    # Force stdout/stderr to UTF-8 so the CLI doesn't crash on Chinese-locale Windows.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
