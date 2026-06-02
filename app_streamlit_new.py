import asyncio
import html
import json
import math
import os
import re
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import folium
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from streamlit_folium import st_folium
from streamlit_js_eval import get_geolocation
from streamlit_js_eval import streamlit_js_eval

from meethalfway import (
    Location,
    MEET_SCENARIOS,
    VENUE_TYPES,
    MeetHalfwayRecommender,
    compute_commute_bias_weights,
    normalize_transport_mode,
)


load_dotenv()
st.set_page_config(page_title="MeetHalfway AI", layout="wide")


# ============================================================================
# Initialize Session State
# ============================================================================
def init_session_state():
    """Initialize all session state variables."""
    if "current_page" not in st.session_state:
        st.session_state.current_page = "home"  # home, action_select, generate_link, join_link, check_result, know_position
    if "selected_action" not in st.session_state:
        st.session_state.selected_action = None
    if "private_checkins" not in st.session_state:
        st.session_state.private_checkins = {}
    if "map_center" not in st.session_state:
        st.session_state.map_center = {"lat": 39.0997, "lon": -94.5786}  # Kansas City
    if "map_zoom" not in st.session_state:
        st.session_state.map_zoom = 13
    if "a_point" not in st.session_state:
        st.session_state.a_point = None
    if "b_point" not in st.session_state:
        st.session_state.b_point = None
    if "room_id" not in st.session_state:
        st.session_state.room_id = ""
    if "user_role" not in st.session_state:
        st.session_state.user_role = ""
    if "user_name" not in st.session_state:
        st.session_state.user_name = ""
    if "participant_count" not in st.session_state:
        st.session_state.participant_count = 2
    if "link_generated" not in st.session_state:
        st.session_state.link_generated = False
    if "generated_room_id" not in st.session_state:
        st.session_state.generated_room_id = ""
    if "generated_link" not in st.session_state:
        st.session_state.generated_link = ""
    if "creator_name" not in st.session_state:
        st.session_state.creator_name = ""
    if "joiner_name" not in st.session_state:
        st.session_state.joiner_name = ""
    if "direct_flow_active" not in st.session_state:
        st.session_state.direct_flow_active = False
    if "direct_preferences" not in st.session_state:
        st.session_state.direct_preferences = {"A": {}, "B": {}}
    if "direct_candidates" not in st.session_state:
        st.session_state.direct_candidates = []
    if "direct_votes" not in st.session_state:
        st.session_state.direct_votes = {"A": [], "B": []}
    if "direct_recommendation_meta" not in st.session_state:
        st.session_state.direct_recommendation_meta = {}
    if "direct_recommendation_text" not in st.session_state:
        st.session_state.direct_recommendation_text = ""


init_session_state()



# ============================================================================
# Shared Room Storage
# ============================================================================
MAX_PARTICIPANTS = 5  # 支持最多5人
ROOM_STATE_PATH = Path(__file__).with_name("room_state.json")
STREAMLIT_CONFIG_PATH = Path(__file__).with_name(".streamlit").joinpath("config.toml")


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _normalize_user_role(user_role: str) -> str:
    # 支持P1~P5编号，兼容旧A/B
    role = str(user_role or "").strip().upper()
    if role in {"A", "B"}:
        return role
    if role.startswith("P") and role[1:].isdigit():
        idx = int(role[1:])
        if 1 <= idx <= 5:
            return f"P{idx}"
    # 兼容数字
    if role.isdigit() and 1 <= int(role) <= 5:
        return f"P{int(role)}"
    # 默认A/B映射
    if role.endswith("A"):
        return "P1"
    if role.endswith("B"):
        return "P2"
    return role


def _role_index(role_key: str) -> Optional[int]:
    role = _normalize_user_role(role_key)
    if role in {"A", "B"}:
        return 1 if role == "A" else 2
    if role.startswith("P") and role[1:].isdigit():
        idx = int(role[1:])
        if 1 <= idx <= MAX_PARTICIPANTS:
            return idx
    return None


def _role_label(role_key: str) -> str:
    idx = _role_index(role_key)
    return f"Person {idx}" if idx else f"Person {role_key}"


def _role_options(total: int) -> list[str]:
    total = max(2, min(MAX_PARTICIPANTS, int(total or 2)))
    return [f"Person {i}" for i in range(1, total + 1)]


def _canonical_participant_key(raw_key: str) -> Optional[str]:
    idx = _role_index(raw_key)
    return f"P{idx}" if idx else None


def _set_room_participant_count(room_id: str, count: int) -> None:
    room_key = str(room_id or "").strip()
    if not room_key:
        return
    count = max(2, min(MAX_PARTICIPANTS, int(count or 2)))
    state = _load_room_state()
    room_record = state.setdefault(room_key, {"participants": {}, "updated_at": _utc_timestamp()})
    if int(room_record.get("participant_count") or 2) != count:
        room_record["participant_count"] = count
        room_record["updated_at"] = _utc_timestamp()
        _invalidate_room_outputs(room_record, "participant count changed")
    else:
        room_record["participant_count"] = count
        room_record["updated_at"] = _utc_timestamp()
    _save_room_state(state)


def _room_participant_count(room_record: Dict[str, Any]) -> int:
    raw_count = room_record.get("participant_count")
    if raw_count:
        try:
            return max(2, min(MAX_PARTICIPANTS, int(raw_count)))
        except Exception:
            pass
    participants = room_record.get("participants", {}) or {}
    discovered = [
        idx for idx in (_role_index(key) for key in participants.keys())
        if idx is not None
    ]
    return max(2, min(MAX_PARTICIPANTS, max(discovered, default=2)))


def _canonical_room_participants(room_record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    canonical: Dict[str, Dict[str, Any]] = {}
    for raw_key, value in (room_record.get("participants", {}) or {}).items():
        key = _canonical_participant_key(raw_key)
        if key and isinstance(value, dict):
            canonical[key] = value
    return canonical


def _load_room_state() -> Dict[str, Any]:
    if not ROOM_STATE_PATH.exists():
        return {}
    try:
        return json.loads(ROOM_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_room_state(data: Dict[str, Any]) -> None:
    ROOM_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_room_record(room_id: str) -> Dict[str, Any]:
    state = _load_room_state()
    room_key = str(room_id or "").strip()
    return state.get(room_key, {"participants": {}})


def _payload_without_timestamps(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: _payload_without_timestamps(value)
            for key, value in payload.items()
            if key not in {"submitted_at", "updated_at"}
        }
    if isinstance(payload, list):
        return [_payload_without_timestamps(item) for item in payload]
    return payload


def _invalidate_room_outputs(room_record: Dict[str, Any], reason: str) -> None:
    room_record.pop("recommendation", None)
    room_record["recommendation_invalidated_reason"] = reason
    room_record["recommendation_invalidated_at"] = _utc_timestamp()
    participants = room_record.get("participants", {}) or {}
    for participant in participants.values():
        if isinstance(participant, dict):
            participant.pop("vote", None)


def _upsert_room_participant(room_id: str, participant_key: str, section: str, payload: Dict[str, Any]) -> None:
    room_key = str(room_id or "").strip()
    if not room_key:
        return
    state = _load_room_state()
    room_record = state.setdefault(room_key, {"participants": {}, "updated_at": _utc_timestamp()})
    participants = room_record.setdefault("participants", {})
    participant = participants.setdefault(participant_key, {})
    previous_payload = participant.get(section)
    participant[section] = payload
    participant["updated_at"] = _utc_timestamp()
    if section in {"location", "preferences"}:
        before = _payload_without_timestamps(previous_payload or {})
        after = _payload_without_timestamps(payload or {})
        if before != after:
            _invalidate_room_outputs(
                room_record,
                f"{participant_key} updated {section}",
            )
    room_record["updated_at"] = _utc_timestamp()
    _save_room_state(state)


def _participant_record(room_id: str, user_role: str) -> Dict[str, Any]:
    room_record = _get_room_record(room_id)
    participant_key = _normalize_user_role(user_role)
    return room_record.get("participants", {}).get(participant_key, {})


def _partner_roles(user_role: str, total: int = None) -> list:
    # 返回除自己外的所有角色key
    me = _normalize_user_role(user_role)
    n = total if total is not None else MAX_PARTICIPANTS
    keys = [f"P{i}" for i in range(1, n+1)]
    return [k for k in keys if k != me]


def _persist_user_location(room_id: str, user_role: str, location: Location, source: str) -> None:
    _upsert_room_participant(
        room_id,
        _normalize_user_role(user_role),
        "location",
        {
            "lat": float(location.lat),
            "lon": float(location.lon),
            "source": source,
            "submitted_at": _utc_timestamp(),
        },
    )


def _persist_user_preferences(room_id: str, user_role: str, payload: Dict[str, Any]) -> None:
    payload = dict(payload)
    payload["submitted_at"] = _utc_timestamp()
    _upsert_room_participant(room_id, _normalize_user_role(user_role), "preferences", payload)


def _persist_user_profile(room_id: str, user_role: str, name: str) -> None:
    _upsert_room_participant(
        room_id,
        _normalize_user_role(user_role),
        "profile",
        {
            "name": str(name or "").strip(),
            "submitted_at": _utc_timestamp(),
        },
    )


def _load_saved_profile_name(room_id: str, user_role: str) -> str:
    raw = _participant_record(room_id, user_role).get("profile") or {}
    return str(raw.get("name", "")).strip()


def _load_saved_preferences(room_id: str, user_role: str) -> Dict[str, Any]:
    return _participant_record(room_id, user_role).get("preferences", {}) or {}


def _load_saved_location(room_id: str, user_role: str) -> Optional[Location]:
    raw = _participant_record(room_id, user_role).get("location")
    if not raw:
        return None
    try:
        return Location(float(raw["lat"]), float(raw["lon"]))
    except Exception:
        return None


def _resume_page_for_participant(room_id: str, user_role: str) -> str:
    """Return the most appropriate page when someone rejoins an existing room."""
    role_key = _normalize_user_role(user_role)
    saved_location = _load_saved_location(room_id, role_key)
    saved_preferences = _load_saved_preferences(room_id, role_key)
    recommendation = _load_room_recommendation(room_id)

    if recommendation and recommendation.get("status") == "ready":
        return "check_result"
    if saved_location or saved_preferences:
        return "user_info_step2"
    return "user_info_step1"


def _render_distance_tolerance_preview(
    location: Optional[Location],
    distance_miles: float,
    map_key: str,
) -> None:
    """Visual preview of the user's acceptable travel radius."""
    if location is None:
        st.info("Set your location on the previous step to see a travel-radius preview here.")
        return

    radius_meters = max(1.0, float(distance_miles)) * 1609.34
    lat = float(location.lat)
    lon = float(location.lon)

    preview_map = folium.Map(location=[lat, lon], zoom_start=11)
    folium.Circle(
        location=[lat, lon],
        radius=radius_meters,
        color="#ff5a5f",
        weight=3,
        fill=True,
        fill_color="#ff5a5f",
        fill_opacity=0.12,
        tooltip=f"Travel radius: {float(distance_miles):.0f} miles",
    ).add_to(preview_map)
    folium.CircleMarker(
        location=[lat, lon],
        radius=7,
        color="#143a5c",
        fill=True,
        fill_color="#143a5c",
        fill_opacity=1,
        tooltip="Your selected location",
    ).add_to(preview_map)

    lat_offset = radius_meters / 111320.0
    lon_scale = max(math.cos(math.radians(lat)), 0.2)
    lon_offset = radius_meters / (111320.0 * lon_scale)
    preview_map.fit_bounds(
        [
            [lat - lat_offset, lon - lon_offset],
            [lat + lat_offset, lon + lon_offset],
        ]
    )

    st.caption(
        f"Range preview: centered on your saved location, showing an approximate {float(distance_miles):.0f}-mile travel radius."
    )
    st_folium(
        preview_map,
        width=None,
        height=300,
        key=map_key,
        use_container_width=True,
    )


def _render_radius_selector_block(
    location: Optional[Location],
    distance_miles: int,
    slider_key: str,
    map_key: str,
    *,
    label: str = "Max travel distance (miles)",
    min_value: int = 1,
    max_value: int = 50,
    help_text: str = "Drag the radius slider, then review the circle map below.",
) -> int:
    st.markdown("**Travel Radius**")
    distance = st.slider(
        label,
        min_value=min_value,
        max_value=max_value,
        value=int(distance_miles),
        key=slider_key,
    )
    st.caption(help_text)
    _render_distance_tolerance_preview(
        location,
        float(distance),
        map_key=map_key,
    )
    return int(distance)


def _preference_summary(room_id: str) -> Dict[str, Any]:
    """
    汇总所有参与者的location和preferences，支持2-5人。
    返回：
      - participants: {P1: {...}, P2: {...}, ...}
      - all_prefs: [dict, ...]
      - all_locs: [dict, ...]
      - all_ready: bool
      - weighted_center: {lat, lon} or None
      - weights: [float, ...]
    """
    room_record = _get_room_record(room_id)
    participants = _canonical_room_participants(room_record)
    participant_count = _room_participant_count(room_record)
    keys = [f"P{i}" for i in range(1, participant_count + 1)]
    all_prefs = [participants.get(k, {}).get("preferences") or {} for k in keys]
    all_locs = [participants.get(k, {}).get("location") or {} for k in keys]
    both_preferences_ready = all(bool(p) for p in all_prefs) and len(all_prefs) >= 2
    both_locations_ready = all(bool(loc) for loc in all_locs) and len(all_locs) >= 2

    weighted_center = None
    weights = None
    if both_preferences_ready and both_locations_ready:
        weights = []
        for pref in all_prefs:
            try:
                weight, _ = compute_commute_bias_weights(
                    pref.get("travel_mode"),
                    pref.get("travel_mode"),
                    pref.get("distance_miles"),
                    pref.get("distance_miles"),
                )
            except Exception:
                weight = 1.0
            weights.append(float(weight))
        total = sum(weights)
        weighted_center = {
            "lat": sum(float(loc["lat"]) * w for loc, w in zip(all_locs, weights)) / total,
            "lon": sum(float(loc["lon"]) * w for loc, w in zip(all_locs, weights)) / total,
        }

    return {
        "participants": participants,
        "participant_count": participant_count,
        "all_prefs": all_prefs,
        "all_locs": all_locs,
        "both_preferences_ready": both_preferences_ready,
        "both_locations_ready": both_locations_ready,
        "weighted_center": weighted_center,
        "weights": weights,
        "keys": keys,
        "prefs_a": all_prefs[0] if len(all_prefs) > 0 else {},
        "prefs_b": all_prefs[1] if len(all_prefs) > 1 else {},
        "loc_a": all_locs[0] if len(all_locs) > 0 else {},
        "loc_b": all_locs[1] if len(all_locs) > 1 else {},
        "weight_a": weights[0] if weights and len(weights) > 0 else None,
        "weight_b": weights[1] if weights and len(weights) > 1 else None,
        "preferences_ready_count": sum(1 for p in all_prefs if p),
        "locations_ready_count": sum(1 for loc in all_locs if loc),
    }


def _load_room_recommendation(room_id: str) -> Optional[Dict[str, Any]]:
    room_record = _get_room_record(room_id)
    rec = room_record.get("recommendation")
    return rec if isinstance(rec, dict) else None


def _load_llm_settings() -> Tuple[Optional[str], str, Optional[str]]:
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    model_name = (os.getenv("MODEL_NAME") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    openai_base = (os.getenv("OPENAI_API_BASE") or "").strip() or None
    return openai_key, model_name, openai_base


def _summarize_preference_context(prefs_a: Dict[str, Any], prefs_b: Dict[str, Any]) -> Tuple[float, str, str]:
    budgets = [
        float(value)
        for value in (prefs_a.get("budget"), prefs_b.get("budget"))
        if value not in (None, "")
    ]
    budget = sum(budgets) / len(budgets) if budgets else 50.0

    cuisines: list[str] = []
    for raw in (prefs_a.get("cuisine"), prefs_b.get("cuisine")):
        cuisine = str(raw or "").strip()
        if cuisine and cuisine.lower() not in {item.lower() for item in cuisines}:
            cuisines.append(cuisine)
    cuisine_label = " / ".join(cuisines) if cuisines else "flexible"

    venue_types = _combine_venue_preferences(prefs_a, prefs_b)
    primary_venue_type = venue_types[0] if venue_types else "restaurant"
    return budget, cuisine_label, primary_venue_type


def _render_recommendation_summary(text: str) -> None:
    summary = str(text or "").strip()
    if not summary:
        return
    safe_summary = html.escape(summary)
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,rgba(255,247,240,0.98),rgba(240,247,255,0.98));
        border:1px solid rgba(80,130,200,0.18);border-radius:20px;padding:18px 22px;margin:10px 0 18px;
        box-shadow:0 10px 26px rgba(40,80,130,0.08);">
        <div style="font-size:1.02rem;font-weight:800;color:#18344f;margin-bottom:8px;">AI Recommendation Summary</div>
        <div style="color:#3f5870;line-height:1.7;white-space:pre-wrap;">{safe_summary}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _save_room_recommendation(room_id: str, payload: Dict[str, Any]) -> None:
    """Save the 5-candidate list (status=ready) generated by _compute_room_recommendations."""
    room_key = str(room_id or "").strip()
    if not room_key:
        return
    state = _load_room_state()
    room_record = state.setdefault(room_key, {"participants": {}, "updated_at": _utc_timestamp()})
    room_record["recommendation"] = payload
    room_record["updated_at"] = _utc_timestamp()
    _save_room_state(state)


# ---- Vote helpers -----------------------------------------------------------

def _save_vote(room_id: str, user_role: str, ranking: list[str]) -> None:
    """Persist a user's ranked list (up to 3 venue names) for the room."""
    _upsert_room_participant(
        room_id,
        _normalize_user_role(user_role),
        "vote",
        {"ranking": ranking[:3], "submitted_at": _utc_timestamp()},
    )


def _load_vote(room_id: str, user_role: str) -> list[str]:
    """Return the saved ranking list for a user, or []."""
    raw = _participant_record(room_id, user_role).get("vote") or {}
    return list(raw.get("ranking", []))


def _compute_combined_ranking(*rankings: list[str]) -> list[str]:
    """
    Borda-count merge: rank 1 = 3 pts, rank 2 = 2 pts, rank 3 = 1 pt.
    Returns venues sorted by descending combined score.
    """
    scores: Dict[str, int] = {}
    for rank_list in rankings:
        borda = len(rank_list)
        for name in rank_list:
            scores[name] = scores.get(name, 0) + borda
            borda -= 1
    return sorted(scores.keys(), key=lambda n: -scores[n])


def _reset_direct_flow_results() -> None:
    st.session_state.direct_candidates = []
    st.session_state.direct_votes = {"A": [], "B": []}
    st.session_state.direct_recommendation_meta = {}
    st.session_state.direct_recommendation_text = ""


def _load_direct_vote(user_role: str) -> list[str]:
    return list((st.session_state.get("direct_votes", {}) or {}).get(_normalize_user_role(user_role), []))


def _save_direct_vote(user_role: str, ranking: list[str]) -> None:
    votes = dict(st.session_state.get("direct_votes", {"A": [], "B": []}) or {"A": [], "B": []})
    votes[_normalize_user_role(user_role)] = ranking[:3]
    st.session_state.direct_votes = votes


def _build_direct_summary() -> Dict[str, Any]:
    prefs = st.session_state.get("direct_preferences", {}) or {"A": {}, "B": {}}
    prefs_a = prefs.get("A", {}) or {}
    prefs_b = prefs.get("B", {}) or {}
    loc_a_obj = st.session_state.get("location_A")
    loc_b_obj = st.session_state.get("location_B")
    loc_a = {"lat": float(loc_a_obj.lat), "lon": float(loc_a_obj.lon)} if isinstance(loc_a_obj, Location) else {}
    loc_b = {"lat": float(loc_b_obj.lat), "lon": float(loc_b_obj.lon)} if isinstance(loc_b_obj, Location) else {}
    both_preferences_ready = bool(prefs_a and prefs_b)
    both_locations_ready = bool(loc_a and loc_b)

    weighted_center = None
    weight_a = weight_b = None
    if both_preferences_ready and both_locations_ready:
        weight_a, weight_b = compute_commute_bias_weights(
            prefs_a.get("travel_mode"),
            prefs_b.get("travel_mode"),
            prefs_a.get("distance_miles"),
            prefs_b.get("distance_miles"),
        )
        total = weight_a + weight_b
        weighted_center = {
            "lat": (float(loc_a["lat"]) * weight_a + float(loc_b["lat"]) * weight_b) / total,
            "lon": (float(loc_a["lon"]) * weight_a + float(loc_b["lon"]) * weight_b) / total,
        }

    return {
        "prefs_a": prefs_a,
        "prefs_b": prefs_b,
        "loc_a": loc_a,
        "loc_b": loc_b,
        "both_preferences_ready": both_preferences_ready,
        "both_locations_ready": both_locations_ready,
        "weighted_center": weighted_center,
        "weight_a": weight_a,
        "weight_b": weight_b,
    }


def _normalize_venue_key(raw: str) -> str:
    v = str(raw or "").strip().lower()
    mapping = {
        "restaurant": "restaurant",
        "cafe": "cafe",
        "bar": "bar",
        "park": "park",
        "mall": "mall",
        "shopping": "mall",
        "clothing": "clothing",
        "clothing store": "clothing",
        "department store": "department_store",
        "museum": "museum",
        "theater": "cinema",
        "cinema": "cinema",
        "mall": "mall",
    }
    return mapping.get(v, "restaurant")


def _room_preferred_venue(summary: Dict[str, Any]) -> str:
    venues_a = [_normalize_venue_key(x) for x in (summary.get("prefs_a", {}).get("venue_type") or [])]
    venues_b = [_normalize_venue_key(x) for x in (summary.get("prefs_b", {}).get("venue_type") or [])]
    common = [v for v in venues_a if v in venues_b]
    if common:
        return common[0]
    if venues_a:
        return venues_a[0]
    if venues_b:
        return venues_b[0]
    return "restaurant"


def _build_room_recommendation(room_id: str, force: bool = False) -> Optional[Dict[str, Any]]:
    room_key = str(room_id or "").strip()
    if not room_key:
        return None

    if not force:
        cached = _load_room_recommendation(room_key)
        if cached and cached.get("status") == "ready":
            return cached

    summary = _preference_summary(room_key)
    if not (summary.get("both_preferences_ready") and summary.get("both_locations_ready") and summary.get("weighted_center")):
        return None

    try:
        mapbox_token = (os.getenv("MAPBOX_ACCESS_TOKEN") or "").strip()
        ors_key = (os.getenv("OPENROUTESERVICE_API_KEY") or os.getenv("ORS_API_KEY") or "").strip() or None
        yelp_key = (os.getenv("YELP_API_KEY") or "").strip() or None
        tavily_key = (os.getenv("TAVILY_API_KEY") or "").strip()
        openai_key, model_name, openai_base = _load_llm_settings()

        # 多人支持：聚合所有人的location和preferences
        all_prefs = summary.get("all_prefs", [])
        all_locs = summary.get("all_locs", [])
        n = len(all_prefs)
        if n < 2:
            return None
        # venue_type和cuisine聚合（简单取第一个/合并）
        venue_type = None
        for p in all_prefs:
            if p.get("venue_type"):
                venue_type = p.get("venue_type")
                break
        if not venue_type:
            venue_type = "restaurant"
        cuisine_keyword = ""
        for p in all_prefs:
            if p.get("cuisine"):
                cuisine_keyword = p.get("cuisine")
                break

        # 距离半径，取最大/平均
        distances = [float(p.get("distance_miles", 15)) for p in all_prefs]
        radii_km = [max(1.0, d) * 1.60934 for d in distances]
        # 位置对象
        loc_objs = [Location(float(l["lat"]), float(l["lon"])) for l in all_locs]
        center = Location(float(summary["weighted_center"]["lat"]), float(summary["weighted_center"]["lon"]))

        engine = MeetHalfwayRecommender(
            mapbox_token=mapbox_token,
            ors_api_key=ors_key,
            yelp_api_key=yelp_key,
            tavily_key=tavily_key,
            openai_key=openai_key,
            openai_model=model_name,
            openai_base=openai_base,
            transport="transit",
            low_cost_mode=True,
            use_yelp=False,
            use_llm_extraction=bool(openai_key),
            use_llm_summary=bool(openai_key),
            max_enriched_candidates=2,
        )

        # 多人交集区域（需扩展engine，暂用所有人半径的交集/中心）
        # 这里只做简单聚合，实际可扩展为多圆交集
        intersection = None
        if hasattr(engine, "get_intersection_from_radii"):
            # 如果engine支持多点
            try:
                intersection = engine.get_intersection_from_radii(*loc_objs, *radii_km)
            except Exception:
                intersection = None
        candidates = engine.search_nearby_venues(
            center=center,
            venue_type=venue_type,
            keyword=cuisine_keyword,
            limit=engine.recommend_search_limit(5),
            intersection=intersection,
        )
        if not candidates and venue_type != "restaurant":
            candidates = engine.search_nearby_venues(
                center=center,
                venue_type="restaurant",
                keyword=cuisine_keyword,
                limit=engine.recommend_search_limit(5),
                intersection=intersection,
            )

        if not candidates:
            failed = {
                "status": "failed",
                "generated_at": _utc_timestamp(),
                "message": "No nearby places found yet. Try widening distance preferences or changing venue type.",
            }
            _save_room_recommendation(room_key, failed)
            return failed

        scored = []
        for c in candidates:
            place_loc = Location(float(c.lat), float(c.lon))
            # 多人公平性：最大-最小距离
            dists = [engine.haversine_km(loc, place_loc) for loc in loc_objs]
            fairness_gap = max(dists) - min(dists) if len(dists) > 1 else 0
            scored.append(
                {
                    "name": c.name,
                    "lat": float(c.lat),
                    "lon": float(c.lon),
                    "place_name": c.place_name,
                    "distance_to_center_km": float(c.distance_to_center_km),
                    "fairness_gap_km": fairness_gap,
                }
            )

        scored.sort(key=lambda x: (x["fairness_gap_km"], x["distance_to_center_km"]))
        top_items = scored[:5]

        payload = {
            "status": "ready",
            "generated_at": _utc_timestamp(),
            "room_id": room_key,
            "venue_type": venue_type,
            "keyword": cuisine_keyword,
            "weighted_center": {"lat": center.lat, "lon": center.lon},
            "items": top_items,
        }
        _save_room_recommendation(room_key, payload)
        return payload
    except Exception as exc:
        failed = {
            "status": "failed",
            "generated_at": _utc_timestamp(),
            "message": f"Recommendation failed: {exc}",
        }
        _save_room_recommendation(room_key, failed)
        return failed


def _build_half_hour_slots() -> list[str]:
    slots = []
    for hour in range(24):
        for minute in (0, 30):
            slots.append(f"{hour:02d}:{minute:02d}")
    return slots


TIME_SLOT_OPTIONS = _build_half_hour_slots()
UI_TO_ENGINE_VENUE = {
    "Restaurant": "restaurant",
    "Cafe": "cafe",
    "Bar": "bar",
    "Park": "park",
    "Mall": "mall",
    "Shopping": "mall",
    "Clothing Store": "clothing",
    "Department Store": "department_store",
    "Museum": "museum",
    "Theater": "cinema",
    "Parking": "parking",
}
DIRECT_MEETING_TYPE_OPTIONS = [
    "Breakfast Date",
    "Dinner Date",
    "Coffee Chat",
    "Casual Hangout",
    "Business Meeting",
    "Shopping Trip",
]
ROOM_MEETING_TYPE_OPTIONS = [
    "Breakfast Date",
    "Dinner Date",
    "Lunch Date",
    "Coffee",
    "Business Meeting",
    "Shopping Trip",
    "Mall Hangout",
    "Drinks",
    "Activity",
    "Other",
]
TIME_BAND_LABELS = {
    "morning": "morning",
    "midday": "midday",
    "afternoon": "afternoon",
    "evening": "evening",
    "night": "night",
}

BUSINESS_MEETING_TYPES = {"business meeting", "client meeting", "team meeting"}
BUSINESS_VENUE_PRIORITY = ["cafe", "restaurant", "mall"]
BUSINESS_KEYWORDS = [
    "quiet business cafe",
    "hotel lobby cafe",
    "business lunch restaurant",
    "quiet meeting cafe",
]
SHOPPING_MEETING_TYPES = {"shopping trip", "mall hangout", "shopping", "clothes shopping"}
SHOPPING_VENUE_PRIORITY = ["mall", "clothing", "department_store"]
SHOPPING_KEYWORDS = [
    "shopping mall",
    "clothing store",
    "fashion apparel",
    "department store",
]


def _is_business_meeting_type(value: Any) -> bool:
    return str(value or "").strip().lower() in BUSINESS_MEETING_TYPES


def _is_business_context_from_prefs(prefs: list[Dict[str, Any]]) -> bool:
    return any(_is_business_meeting_type(pref.get("meeting_type")) for pref in prefs if isinstance(pref, dict))


def _business_mode_label(is_business: bool) -> str:
    return "Business meeting" if is_business else "Social meetup"


def _is_shopping_meeting_type(value: Any) -> bool:
    return str(value or "").strip().lower() in SHOPPING_MEETING_TYPES


def _is_shopping_context_from_prefs(prefs: list[Dict[str, Any]]) -> bool:
    for pref in prefs:
        if not isinstance(pref, dict):
            continue
        if _is_shopping_meeting_type(pref.get("meeting_type")):
            return True
        raw_venues = pref.get("venue_type") or []
        if isinstance(raw_venues, str):
            raw_venues = [raw_venues]
        engine_venues = {UI_TO_ENGINE_VENUE.get(str(v), _normalize_venue_key(str(v))) for v in raw_venues}
        if engine_venues.intersection(SHOPPING_VENUE_PRIORITY):
            return True
    return False


def _meeting_mode_label(is_business: bool, is_shopping: bool) -> str:
    if is_business:
        return "Business meeting"
    if is_shopping:
        return "Shopping"
    return "Social meetup"


def _format_time_slot_label(slot: str) -> str:
    try:
        hour, minute = slot.split(":")
        h = int(hour)
        m = int(minute)
        suffix = "AM" if h < 12 else "PM"
        display_hour = h % 12
        if display_hour == 0:
            display_hour = 12
        return f"{display_hour}:{m:02d} {suffix}"
    except Exception:
        return slot


def _slot_to_minutes(slot: str) -> Optional[int]:
    try:
        hour, minute = slot.split(":", 1)
        hh = int(hour)
        mm = int(minute)
        if hh < 0 or hh > 23 or mm not in (0, 30):
            return None
        return hh * 60 + mm
    except Exception:
        return None


def _slot_to_time_band(slot: str) -> Optional[str]:
    minutes = _slot_to_minutes(slot)
    if minutes is None:
        return None
    if minutes < 11 * 60:
        return "morning"
    if minutes < 15 * 60:
        return "midday"
    if minutes < 18 * 60:
        return "afternoon"
    if minutes < 22 * 60:
        return "evening"
    return "night"


def _meeting_type_expected_bands(meeting_type: Any) -> set[str]:
    key = str(meeting_type or "").strip().lower()
    mapping = {
        "breakfast": {"morning"},
        "breakfast date": {"morning"},
        "brunch": {"morning", "midday"},
        "lunch": {"midday"},
        "lunch date": {"midday"},
        "coffee": {"morning", "midday", "afternoon"},
        "coffee chat": {"morning", "midday", "afternoon"},
        "drinks": {"evening", "night"},
        "dinner": {"evening", "night"},
        "dinner date": {"evening", "night"},
        "business meeting": {"morning", "midday", "afternoon"},
    }
    return set(mapping.get(key, set()))


def _analyze_meeting_time_alignment(
    prefs_a: Dict[str, Any],
    prefs_b: Dict[str, Any],
    overlap_slots: list[str],
) -> Dict[str, Any]:
    def _analyze_person(label: str, prefs: Dict[str, Any]) -> Dict[str, Any]:
        meeting_type = str(prefs.get("meeting_type", "") or "")
        availability_slots = list(prefs.get("availability_slots", []) or [])
        expected_bands = _meeting_type_expected_bands(meeting_type)
        slot_bands = {
            band for band in (_slot_to_time_band(slot) for slot in availability_slots) if band
        }
        self_mismatch = bool(expected_bands and slot_bands and expected_bands.isdisjoint(slot_bands))
        return {
            "person": label,
            "meeting_type": meeting_type,
            "availability_slots": availability_slots,
            "expected_bands": sorted(expected_bands),
            "slot_bands": sorted(slot_bands),
            "self_mismatch": self_mismatch,
        }

    person_a = _analyze_person("A", prefs_a)
    person_b = _analyze_person("B", prefs_b)
    overlap_bands = {
        band for band in (_slot_to_time_band(slot) for slot in overlap_slots) if band
    }
    hard_conflict = (
        not overlap_slots
        and not person_a["self_mismatch"]
        and not person_b["self_mismatch"]
        and bool(person_a["expected_bands"])
        and bool(person_b["expected_bands"])
        and set(person_a["expected_bands"]).isdisjoint(set(person_b["expected_bands"]))
    )
    return {
        "person_a": person_a,
        "person_b": person_b,
        "overlap_bands": sorted(overlap_bands),
        "self_mismatch_people": [
            person["person"] for person in (person_a, person_b) if person.get("self_mismatch")
        ],
        "hard_conflict": hard_conflict,
    }


def _compute_shared_time_overlap(a_slots: list[str], b_slots: list[str]) -> list[str]:
    order = {slot: idx for idx, slot in enumerate(TIME_SLOT_OPTIONS)}
    overlap = sorted(set(a_slots or []).intersection(set(b_slots or [])), key=lambda x: order.get(x, 999))
    return overlap


def _build_negotiation_time_slots(a_slots: list[str], b_slots: list[str]) -> list[str]:
    """Build ordered candidate slots for conflict-time negotiation.

    Prefer the union of both users' selected slots so scoring stays aligned with
    actual user intent instead of fixed default evening slots.
    """
    order = {slot: idx for idx, slot in enumerate(TIME_SLOT_OPTIONS)}
    merged = sorted(set(a_slots or []).union(set(b_slots or [])), key=lambda x: order.get(x, 999))
    return merged


def _analyze_time_conflict(a_slots: list[str], b_slots: list[str]) -> Dict[str, Any]:
    overlap = _compute_shared_time_overlap(a_slots, b_slots)
    if overlap:
        return {"has_overlap": True, "closest_gap_minutes": 0.0, "severe_gap": False}

    a_pairs = [(_slot_to_minutes(slot), slot) for slot in list(a_slots or [])]
    b_pairs = [(_slot_to_minutes(slot), slot) for slot in list(b_slots or [])]
    a_pairs = [(m, s) for m, s in a_pairs if m is not None]
    b_pairs = [(m, s) for m, s in b_pairs if m is not None]
    if not a_pairs or not b_pairs:
        return {"has_overlap": False, "closest_gap_minutes": None, "severe_gap": False}

    best_gap = 24 * 60
    best_pair = (a_pairs[0][1], b_pairs[0][1])
    for min_a, slot_a in a_pairs:
        for min_b, slot_b in b_pairs:
            gap = abs(min_a - min_b)
            if gap < best_gap:
                best_gap = gap
                best_pair = (slot_a, slot_b)

    return {
        "has_overlap": False,
        "closest_gap_minutes": float(best_gap),
        "closest_pair": best_pair,
        "severe_gap": best_gap > 120,
    }


def _build_recommendation_meta(
    area_mode: str,
    overlap_slots: list[str],
    prefs_a: Dict[str, Any],
    prefs_b: Dict[str, Any],
    open_status_stats: Optional[Dict[str, int]] = None,
    radius_negotiation: Optional[Dict[str, Any]] = None,
    time_negotiation: Optional[Dict[str, Any]] = None,
    meeting_time_alignment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    time_overlap_exists = bool(overlap_slots)
    stats = open_status_stats or {}
    radius_negotiation = radius_negotiation or {}
    time_negotiation = time_negotiation or {}
    meeting_time_alignment = meeting_time_alignment or {}
    return {
        "search_area_mode": area_mode,
        "radius_overlap_exists": area_mode == "intersection",
        "time_overlap_exists": time_overlap_exists,
        "shared_slots": overlap_slots,
        "person_a_slots": list(prefs_a.get("availability_slots", []) or []),
        "person_b_slots": list(prefs_b.get("availability_slots", []) or []),
        "opening_hours_checked": bool(stats),
        "open_count": int(stats.get("open", 0) or 0),
        "uncertain_count": int(stats.get("uncertain", 0) or 0),
        "closed_filtered_count": int(stats.get("closed", 0) or 0),
        "radius_negotiation": radius_negotiation,
        "time_negotiation": time_negotiation,
        "meeting_time_alignment": meeting_time_alignment,
    }


def _render_recommendation_warnings(meta: Dict[str, Any]) -> None:
    if not meta:
        return

    if str(meta.get("meeting_mode", "")).lower().startswith("business"):
        st.info(
            "Business mode: venues are ranked for a quieter professional setting, neutral location, arrival reliability, and nearby parking."
        )
    elif str(meta.get("meeting_mode", "")).lower().startswith("shopping"):
        st.info(
            "Shopping mode: recommendations favor malls, clothing stores, and department stores near the fair meeting area, with parking shown when available."
        )

    if not meta.get("radius_overlap_exists", True):
        st.warning(
            "System warning: the two commute-radius areas do not overlap. "
            "Recommendations are still shown, but they come from the combined reachable area and may lean closer to one person."
        )

    radius_negotiation = meta.get("radius_negotiation", {}) or {}
    if radius_negotiation.get("negotiation_applied"):
        st.info(
            "Radius negotiation applied: "
            f"Person A +{float(radius_negotiation.get('extra_a_km', 0.0)):.2f} km, "
            f"Person B +{float(radius_negotiation.get('extra_b_km', 0.0)):.2f} km "
            "to recover overlap."
        )
    elif radius_negotiation.get("too_far"):
        st.error(
            "Distance check: these two locations are currently too far apart for a fair overlap-based recommendation. "
            "Try selecting closer locations or much larger commute ranges."
        )

    time_negotiation = meta.get("time_negotiation", {}) or {}
    meeting_time_alignment = meta.get("meeting_time_alignment", {}) or {}
    for person_key in ("person_a", "person_b"):
        person = meeting_time_alignment.get(person_key, {}) or {}
        if person.get("self_mismatch"):
            slot_text = ", ".join(
                _format_time_slot_label(slot) for slot in (person.get("availability_slots") or [])[:6]
            ) or "none selected"
            band_text = ", ".join(
                TIME_BAND_LABELS.get(str(band), str(band)) for band in (person.get("expected_bands") or [])
            ) or "flexible times"
            st.warning(
                f"Preference check: Person {person.get('person', '?')} selected '{person.get('meeting_type', 'Unknown')}', "
                f"but their available times look more like {slot_text}. This meeting type usually fits {band_text}, "
                "so please confirm whether the meetup type or the time selection was chosen incorrectly."
            )

    if meeting_time_alignment.get("hard_conflict"):
        person_a = meeting_time_alignment.get("person_a", {}) or {}
        person_b = meeting_time_alignment.get("person_b", {}) or {}
        st.error(
            "Meal-time mismatch: the two people are available in different parts of the day, and their selected meetup types also point to different meal windows. "
            "Please align both the time selection and the meetup type before relying on the recommendations below."
        )
        st.caption(
            f"Person A: {person_a.get('meeting_type', 'Unknown')} | Person B: {person_b.get('meeting_type', 'Unknown')}"
        )

    if time_negotiation.get("severe_gap"):
        gap_minutes = float(time_negotiation.get("closest_gap_minutes", 0.0) or 0.0)
        gap_hours = gap_minutes / 60.0
        closest_pair = time_negotiation.get("closest_pair") or ("", "")
        st.warning(
            f"Time coordination warning: the closest available slots are still {gap_hours:.1f} hours apart. "
            "Recommendations are shown at reduced confidence, so both people should negotiate the final meeting time manually."
        )
        if closest_pair[0] and closest_pair[1]:
            st.caption(
                "Closest attempted compromise: "
                f"Person A {_format_time_slot_label(str(closest_pair[0]))}, "
                f"Person B {_format_time_slot_label(str(closest_pair[1]))}."
            )

    if not meta.get("time_overlap_exists", True):
        a_slots = meta.get("person_a_slots", [])
        b_slots = meta.get("person_b_slots", [])
        a_text = ", ".join(_format_time_slot_label(slot) for slot in a_slots[:6]) or "none selected"
        b_text = ", ".join(_format_time_slot_label(slot) for slot in b_slots[:6]) or "none selected"
        st.warning(
            "System warning: the two schedules do not overlap. Recommendations below are venue-only suggestions, "
            "so you still need to coordinate a meeting time separately."
        )
        st.caption(f"Person A availability: {a_text}")
        st.caption(f"Person B availability: {b_text}")

    if meta.get("opening_hours_checked"):
        closed_filtered = int(meta.get("closed_filtered_count", 0) or 0)
        uncertain_count = int(meta.get("uncertain_count", 0) or 0)
        if closed_filtered > 0:
            st.info(
                f"Opening-hours check: filtered out {closed_filtered} venue(s) that appear closed for the selected time."
            )
        if uncertain_count > 0:
            st.warning(
                f"Opening-hours check: {uncertain_count} venue(s) could not be fully verified for the selected time, so please confirm before booking."
            )


def _preferred_engine_transport(mode_a: Optional[str], mode_b: Optional[str]) -> str:
    normalized = {normalize_transport_mode(mode_a), normalize_transport_mode(mode_b)}
    if "walk" in normalized:
        return "walk"
    if "transit" in normalized:
        return "transit"
    return "drive"


def _distance_miles_to_minutes(distance_miles: float, mode: Optional[str]) -> float:
    mode_key = normalize_transport_mode(mode)
    speed_km_per_min = {
        "walk": 5.0 / 60.0,
        "transit": 20.0 / 60.0,
        "drive": 40.0 / 60.0,
    }.get(mode_key, 20.0 / 60.0)
    return max(5.0, float(distance_miles) * 1.60934 / speed_km_per_min)


def _resolve_search_area_with_radius_negotiation(
    engine: MeetHalfwayRecommender,
    loc_a: Location,
    loc_b: Location,
    prefs_a: Dict[str, Any],
    prefs_b: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute search area and, when needed, negotiate small mode-aware radius extensions."""
    radius_a_km = max(1.0, float(prefs_a.get("distance_miles", 15)) * 1.60934)
    radius_b_km = max(1.0, float(prefs_b.get("distance_miles", 15)) * 1.60934)
    first_area = engine.get_search_area_from_radii(loc_a, loc_b, radius_a_km, radius_b_km)
    distance_km = engine.haversine_km(loc_a, loc_b)
    required_extra_total = max(0.0, distance_km - (radius_a_km + radius_b_km))

    detail: Dict[str, Any] = {
        "distance_km": round(distance_km, 2),
        "base_radius_a_km": round(radius_a_km, 2),
        "base_radius_b_km": round(radius_b_km, 2),
        "required_extra_total_km": round(required_extra_total, 2),
        "negotiation_applied": False,
        "too_far": False,
        "extra_a_km": 0.0,
        "extra_b_km": 0.0,
    }

    if first_area.get("overlap_exists"):
        return {"search_area": first_area, "detail": detail}

    ABSOLUTE_DISTANCE_LIMIT_KM = 120.0
    MAX_NEGOTIABLE_EXTRA_TOTAL_KM = 18.0
    MAX_NEGOTIABLE_EXTRA_PER_PERSON_KM = 12.0

    if distance_km > ABSOLUTE_DISTANCE_LIMIT_KM or required_extra_total > MAX_NEGOTIABLE_EXTRA_TOTAL_KM:
        detail["too_far"] = True
        detail["too_far_reason"] = "distance_too_large"
        return {"search_area": first_area, "detail": detail}

    mode_a = normalize_transport_mode(prefs_a.get("travel_mode"))
    mode_b = normalize_transport_mode(prefs_b.get("travel_mode"))
    mode_flex = {"walk": 0.75, "transit": 1.0, "drive": 1.35}

    flex_a = mode_flex.get(mode_a, 1.0) * min(1.4, max(0.8, radius_a_km / 12.0))
    flex_b = mode_flex.get(mode_b, 1.0) * min(1.4, max(0.8, radius_b_km / 12.0))
    flex_sum = max(0.01, flex_a + flex_b)

    extra_a = min(MAX_NEGOTIABLE_EXTRA_PER_PERSON_KM, required_extra_total * flex_a / flex_sum)
    extra_b = min(MAX_NEGOTIABLE_EXTRA_PER_PERSON_KM, required_extra_total * flex_b / flex_sum)
    still_missing = max(0.0, required_extra_total - (extra_a + extra_b))

    if still_missing > 0.1:
        detail["too_far"] = True
        detail["too_far_reason"] = "extra_cap_exceeded"
        detail["extra_a_km"] = round(extra_a, 2)
        detail["extra_b_km"] = round(extra_b, 2)
        return {"search_area": first_area, "detail": detail}

    expanded_area = engine.get_search_area_from_radii(
        loc_a,
        loc_b,
        radius_a_km + extra_a,
        radius_b_km + extra_b,
    )
    detail.update(
        {
            "negotiation_applied": True,
            "mode_a": mode_a,
            "mode_b": mode_b,
            "extra_a_km": round(extra_a, 2),
            "extra_b_km": round(extra_b, 2),
            "expanded_radius_a_km": round(radius_a_km + extra_a, 2),
            "expanded_radius_b_km": round(radius_b_km + extra_b, 2),
            "negotiated_overlap_exists": bool(expanded_area.get("overlap_exists")),
        }
    )
    return {"search_area": expanded_area, "detail": detail}


def _combine_venue_preferences(prefs_a: Dict[str, Any], prefs_b: Dict[str, Any]) -> list[str]:
    if _is_business_context_from_prefs([prefs_a, prefs_b]):
        selected: list[str] = []
        raw_values = list(prefs_a.get("venue_type", []) or []) + list(prefs_b.get("venue_type", []) or [])
        for raw in raw_values:
            venue = UI_TO_ENGINE_VENUE.get(str(raw), _normalize_venue_key(str(raw)))
            if venue in BUSINESS_VENUE_PRIORITY and venue not in selected:
                selected.append(venue)
        return selected + [venue for venue in BUSINESS_VENUE_PRIORITY if venue not in selected]

    if _is_shopping_context_from_prefs([prefs_a, prefs_b]):
        selected: list[str] = []
        raw_values = list(prefs_a.get("venue_type", []) or []) + list(prefs_b.get("venue_type", []) or [])
        for raw in raw_values:
            venue = UI_TO_ENGINE_VENUE.get(str(raw), _normalize_venue_key(str(raw)))
            if venue in SHOPPING_VENUE_PRIORITY and venue not in selected:
                selected.append(venue)
        return selected + [venue for venue in SHOPPING_VENUE_PRIORITY if venue not in selected]

    set_a = {UI_TO_ENGINE_VENUE.get(v, "restaurant") for v in prefs_a.get("venue_type", [])}
    set_b = {UI_TO_ENGINE_VENUE.get(v, "restaurant") for v in prefs_b.get("venue_type", [])}
    overlap = [v for v in set_a.intersection(set_b) if v]
    if overlap:
        return sorted(overlap)
    combined = [v for v in set_a.union(set_b) if v]
    return sorted(combined) or ["restaurant"]


def _build_cuisine_keyword_plan(prefs_a: Dict[str, Any], prefs_b: Dict[str, Any]) -> list[str]:
    if _is_business_context_from_prefs([prefs_a, prefs_b]):
        cuisine_terms = [
            str(p.get("cuisine", "")).strip()
            for p in (prefs_a, prefs_b)
            if str(p.get("cuisine", "")).strip()
        ]
        if cuisine_terms:
            return [f"{term} business lunch quiet" for term in cuisine_terms[:2]] + BUSINESS_KEYWORDS
        return BUSINESS_KEYWORDS.copy()

    if _is_shopping_context_from_prefs([prefs_a, prefs_b]):
        shopping_terms = [
            str(p.get("cuisine", "")).strip()
            for p in (prefs_a, prefs_b)
            if str(p.get("cuisine", "")).strip()
        ]
        if shopping_terms:
            return shopping_terms[:3] + [f"{term} clothing store" for term in shopping_terms[:2]] + SHOPPING_KEYWORDS
        return SHOPPING_KEYWORDS.copy()

    raw = [str(prefs_a.get("cuisine", "")).strip(), str(prefs_b.get("cuisine", "")).strip()]
    cuisines: list[str] = []
    for cuisine in raw:
        normalized = cuisine.lower()
        if cuisine and normalized not in {item.lower() for item in cuisines}:
            cuisines.append(cuisine)

    if not cuisines:
        return []
    if len(cuisines) == 1:
        return cuisines

    combined_phrase = " ".join(cuisines)
    return [f"{combined_phrase} fusion", combined_phrase, *cuisines]


def _search_with_cuisine_fallback(
    engine: MeetHalfwayRecommender,
    center: Location,
    venue_type: str,
    search_limit: int,
    intersection: Any,
    cuisine_plan: list[str],
) -> list[Any]:
    if venue_type != "restaurant":
        if cuisine_plan:
            dedupe: Dict[Tuple[str, float, float], Any] = {}
            for keyword in cuisine_plan[:2]:
                found = engine.search_nearby_venues(
                    center,
                    venue_type=venue_type,
                    keyword=keyword,
                    limit=search_limit,
                    intersection=intersection,
                )
                for item in found:
                    dedupe[(item.name.lower(), round(item.lat, 4), round(item.lon, 4))] = item
            if dedupe:
                return list(dedupe.values())
        return engine.search_nearby_venues(
            center,
            venue_type=venue_type,
            keyword="",
            limit=search_limit,
            intersection=intersection,
        )

    dedupe: Dict[Tuple[str, float, float], Any] = {}
    combined_keywords = cuisine_plan[:2] if len(cuisine_plan) > 1 else cuisine_plan
    single_keywords = cuisine_plan[2:] if len(cuisine_plan) > 2 else []

    for keyword in combined_keywords:
        found = engine.search_nearby_venues(
            center,
            venue_type=venue_type,
            keyword=keyword,
            limit=search_limit,
            intersection=intersection,
        )
        for item in found:
            dedupe[(item.name.lower(), round(item.lat, 4), round(item.lon, 4))] = item
        if dedupe:
            return list(dedupe.values())

    for keyword in single_keywords:
        found = engine.search_nearby_venues(
            center,
            venue_type=venue_type,
            keyword=keyword,
            limit=search_limit,
            intersection=intersection,
        )
        for item in found:
            dedupe[(item.name.lower(), round(item.lat, 4), round(item.lon, 4))] = item

    if dedupe:
        return list(dedupe.values())

    return engine.search_nearby_venues(
        center,
        venue_type=venue_type,
        keyword="",
        limit=search_limit,
        intersection=intersection,
    )


def _apply_ambiance_preference(candidates: list, pref_a: str, pref_b: str) -> list:
    targets = {"quiet": 0.22, "balanced": 0.55, "lively": 0.82}
    target = (targets.get(pref_a, 0.55) + targets.get(pref_b, 0.55)) / 2.0
    for candidate in candidates:
        crowd_index = float(candidate.web_signals.get("crowd_index", 0.5))
        ambiance_score = max(0.0, 1.0 - abs(crowd_index - target) / 0.85)
        candidate.score_breakdown["ambiance_fit"] = round(ambiance_score, 4)
        candidate.final_score += 0.18 * ambiance_score
    candidates.sort(key=lambda x: x.final_score, reverse=True)
    return candidates


def _apply_business_meeting_fit(candidates: list, is_business: bool) -> list:
    if not is_business:
        return candidates
    for candidate in candidates:
        crowd_index = float((getattr(candidate, "web_signals", {}) or {}).get("crowd_index", 0.5))
        quiet_score = max(0.0, 1.0 - crowd_index)
        category = str(getattr(candidate, "venue_category", "restaurant") or "restaurant")
        venue_score = {"cafe": 1.0, "restaurant": 0.78, "mall": 0.62}.get(category, 0.35)
        parking_score = 1.0 if getattr(candidate, "parking_recommendations", None) else 0.35
        business_fit = 0.45 * quiet_score + 0.35 * venue_score + 0.20 * parking_score
        candidate.score_breakdown["business_fit"] = round(business_fit, 4)
        candidate.final_score += 0.22 * business_fit
    candidates.sort(key=lambda x: x.final_score, reverse=True)
    return candidates


def _compute_room_recommendations(room_id: str) -> Dict[str, Any]:
    """Generate recommendations for a room with 2-5 participants."""
    summary = _preference_summary(room_id)
    if not (summary["both_preferences_ready"] and summary["both_locations_ready"] and summary["weighted_center"]):
        return {"status": "incomplete", "summary": summary}

    all_prefs = summary["all_prefs"]
    all_locs = summary["all_locs"]
    n = len(all_prefs)
    business_context = _is_business_context_from_prefs(all_prefs)
    shopping_context = _is_shopping_context_from_prefs(all_prefs)
    if n < 2:
        return {"status": "incomplete", "summary": summary}

    transport_counts: Dict[str, int] = {}
    for pref in all_prefs:
        mode = normalize_transport_mode(pref.get("travel_mode"))
        transport_counts[mode] = transport_counts.get(mode, 0) + 1
    transport = max(transport_counts, key=transport_counts.get)

    openai_key, model_name, openai_base = _load_llm_settings()
    engine = MeetHalfwayRecommender(
        mapbox_token=os.getenv("MAPBOX_ACCESS_TOKEN", "").strip(),
        ors_api_key=os.getenv("OPENROUTESERVICE_API_KEY", "").strip() or None,
        yelp_api_key=os.getenv("YELP_API_KEY", "").strip() or None,
        tavily_key=os.getenv("TAVILY_API_KEY", "").strip(),
        openai_key=openai_key,
        openai_model=model_name,
        openai_base=openai_base,
        transport=transport,
        isochrone_minutes=20,
        use_yelp=True,
        use_llm_extraction=bool(openai_key),
        use_llm_summary=bool(openai_key),
        low_cost_mode=True,
    )

    loc_objs = [Location(float(l["lat"]), float(l["lon"])) for l in all_locs]
    center = Location(float(summary["weighted_center"]["lat"]), float(summary["weighted_center"]["lon"]))
    radii_km = [max(1.0, float(p.get("distance_miles", 15))) * 1.60934 for p in all_prefs]
    intersection = None
    area_mode = "unknown"
    try:
        circles = [engine.get_distance_circle(loc, radius_km) for loc, radius_km in zip(loc_objs, radii_km)]
        circles = [circle for circle in circles if circle is not None]
        if circles:
            intersection = circles[0]
            for circle in circles[1:]:
                intersection = intersection.intersection(circle)
            if getattr(intersection, "is_empty", False):
                intersection = circles[0]
                for circle in circles[1:]:
                    intersection = intersection.union(circle)
                area_mode = "union_fallback"
            else:
                area_mode = "intersection"
    except Exception:
        intersection = None
        area_mode = "unknown"

    cuisine_plan = [str(p.get("cuisine", "")).strip() for p in all_prefs if str(p.get("cuisine", "")).strip()][:3]
    if business_context:
        cuisine_plan = (cuisine_plan + BUSINESS_KEYWORDS)[:5] if cuisine_plan else BUSINESS_KEYWORDS.copy()
    elif shopping_context:
        cuisine_plan = (cuisine_plan + [f"{term} clothing store" for term in cuisine_plan] + SHOPPING_KEYWORDS)[:7] if cuisine_plan else SHOPPING_KEYWORDS.copy()
    venue_types: list[str] = []
    for pref in all_prefs:
        raw_venues = pref.get("venue_type") or ["Restaurant"]
        if isinstance(raw_venues, str):
            raw_venues = [raw_venues]
        for raw in raw_venues:
            venue = UI_TO_ENGINE_VENUE.get(str(raw), _normalize_venue_key(str(raw)))
            if venue not in venue_types:
                venue_types.append(venue)
    if business_context:
        venue_types = [v for v in BUSINESS_VENUE_PRIORITY if v in venue_types] + [v for v in BUSINESS_VENUE_PRIORITY if v not in venue_types]
    elif shopping_context:
        venue_types = [v for v in SHOPPING_VENUE_PRIORITY if v in venue_types] + [v for v in SHOPPING_VENUE_PRIORITY if v not in venue_types]
    venue_types = venue_types[:3] if venue_types else ["restaurant"]

    dedupe: Dict[Tuple[str, float, float], Any] = {}
    for venue_type in venue_types:
        for item in _search_with_cuisine_fallback(engine, center, venue_type, 8, intersection, cuisine_plan):
            dedupe[(item.name.lower(), round(item.lat, 4), round(item.lon, 4))] = item
    candidates = list(dedupe.values())
    if not candidates:
        return {"status": "no_candidates", "summary": summary}

    engine.tag_with_isochrone(candidates, intersection, area_mode=area_mode)
    all_slots = [set(p.get("availability_slots", []) or []) for p in all_prefs]
    if not all(all_slots):
        return {
            "status": "missing_time_selection",
            "summary": summary,
            "recommendation_meta": {"area_mode": area_mode, "participant_count": n},
        }
    overlap_slots = sorted(set.intersection(*all_slots))
    time_conflict = not bool(overlap_slots)
    selected_slots = overlap_slots or sorted(set.union(*all_slots))
    time_label = _format_time_slot_label(selected_slots[0]) if selected_slots else "Flexible"

    try:
        asyncio.run(engine.enrich_all_async(candidates, city_hint="", year_hint=2026, time_slot=time_label, party_size=n))
    except Exception:
        pass

    candidates, open_status_stats = engine.filter_closed_candidates(candidates)
    if not candidates:
        return {
            "status": "no_open_candidates",
            "summary": summary,
            "recommendation_meta": {"area_mode": area_mode, "participant_count": n, "open_status_stats": open_status_stats},
        }

    if business_context:
        _attach_parking_recommendations(candidates, engine, limit_per_venue=2)

    max_center_dist = max((float(getattr(c, "distance_to_center_km", 0.0) or 0.0) for c in candidates), default=1.0) or 1.0
    ambiance_targets = {"quiet": 0.22, "balanced": 0.55, "lively": 0.82}
    target_ambiance = 0.22 if business_context else sum(ambiance_targets.get(p.get("ambiance_preference", "balanced"), 0.55) for p in all_prefs) / n
    for c in candidates:
        place_loc = Location(float(c.lat), float(c.lon))
        dists = [engine.haversine_km(loc, place_loc) for loc in loc_objs]
        fairness_gap_km = max(dists) - min(dists) if len(dists) > 1 else 0.0
        distance_score = max(0.0, 1.0 - (float(getattr(c, "distance_to_center_km", 0.0) or 0.0) / max_center_dist))
        fairness_score = max(0.0, 1.0 - fairness_gap_km / 20.0)
        rating_score = float(getattr(c, "rating_proxy", 0.5) or 0.5)
        crowd_index = float((getattr(c, "web_signals", {}) or {}).get("crowd_index", 0.5))
        crowd_score = max(0.0, 1.0 - crowd_index)
        ambiance_score = max(0.0, 1.0 - abs(crowd_index - target_ambiance) / 0.85)
        c.fairness_delta_minutes = fairness_gap_km / 35.0 * 60.0
        c.best_time_slot = _format_time_slot_label(selected_slots[0]) if (selected_slots and not time_conflict) else ""
        c.time_conflict = time_conflict
        c.group_distance_km = [round(d, 3) for d in dists]
        c.group_fairness_gap_km = round(fairness_gap_km, 3)
        c.score_breakdown = {
            "distance": round(distance_score, 4),
            "group_fairness": round(fairness_score, 4),
            "rating": round(rating_score, 4),
            "crowd": round(crowd_score, 4),
            "ambiance_fit": round(ambiance_score, 4),
        }
        c.final_score = 0.30 * fairness_score + 0.20 * distance_score + 0.22 * rating_score + 0.16 * crowd_score + 0.12 * ambiance_score
    candidates.sort(key=lambda x: x.final_score, reverse=True)
    candidates = _apply_business_meeting_fit(candidates, business_context)
    _attach_parking_recommendations(candidates[:5], engine)

    budget_values = [float(p.get("budget")) for p in all_prefs if p.get("budget") not in (None, "")]
    budget = sum(budget_values) / len(budget_values) if budget_values else 50.0
    cuisine_label = " / ".join(cuisine_plan) if cuisine_plan else "flexible"
    if business_context:
        recommendation_text = (
            f"Business meeting recommendation for {n} people. I prioritized neutral, quiet, professional venues, "
            f"reasonable commute balance, reliable arrival logistics, and nearby parking. Shared time: {time_label}. "
            f"Average budget: ${budget:.0f} per person."
        )
    elif shopping_context:
        shopping_goal = " / ".join([term for term in cuisine_plan[:3] if term not in SHOPPING_KEYWORDS]) or "shopping"
        recommendation_text = (
            f"Shopping recommendation for {n} people. I searched for malls, clothing stores, and department stores "
            f"near the fair meeting area, using the shopping goal: {shopping_goal}. Shared time: {time_label}. "
            "Parking options are included for each destination when available."
        )
    else:
        recommendation_text = (
            f"Group recommendation for {n} people. I balanced every participant's location, commute limit, "
            f"shared availability, vibe, and venue preferences. Shared time: {time_label}. "
            f"Cuisine: {cuisine_label}. Average budget: ${budget:.0f} per person."
        )

    return {
        "status": "ok",
        "summary": summary,
        "center": center,
        "transport": transport,
        "shared_slots": overlap_slots,
        "recommendation_meta": {
            "area_mode": area_mode,
            "participant_count": n,
            "open_status_stats": open_status_stats,
            "shared_slots": overlap_slots,
            "meeting_mode": _meeting_mode_label(business_context, shopping_context),
        },
        "recommendation_text": recommendation_text,
        "recommendations": candidates[:5],
    }


def _compute_direct_recommendations() -> Dict[str, Any]:
    # 合并饮食禁忌
    def _collect_dietary_restrictions(prefs):
        restrictions = set(prefs.get("dietary_restrictions", []) or [])
        custom = prefs.get("dietary_restrictions_custom", "").strip()
        if custom:
            for part in re.split(r"[,;，；\s]+", custom):
                if part:
                    restrictions.add(part)
        return {r.lower() for r in restrictions if r}

    # 定义简单的食物关键词映射
    RESTRICTION_KEYWORDS = {
        "不吃猪肉": ["猪肉", "pork", "回锅肉", "红烧肉", "叉烧", "腊肉", "bacon", "ham"],
        "不吃牛肉": ["牛肉", "beef", "牛排", "牛腩", "brisket", "steak"],
        "清真": ["猪肉", "pork", "酒精", "alcohol", "ham", "bacon"],
        "犹太洁食": ["猪肉", "pork", "贝类", "shellfish", "虾", "蟹", "lobster", "bacon", "ham"],
        "素食": ["肉", "鸡", "牛", "猪", "羊", "鱼", "虾", "蟹", "beef", "pork", "chicken", "lamb", "fish", "shrimp", "crab"],
        "纯素": ["蛋", "奶", "cheese", "milk", "butter", "yogurt"] + ["肉", "鸡", "牛", "猪", "羊", "鱼", "虾", "蟹", "beef", "pork", "chicken", "lamb", "fish", "shrimp", "crab"],
        "无海鲜": ["鱼", "虾", "蟹", "贝", "海鲜", "seafood", "fish", "shrimp", "crab", "lobster", "clam", "oyster"],
        "无坚果": ["坚果", "花生", "核桃", "杏仁", "腰果", "nut", "peanut", "walnut", "almond", "cashew"],
    }

    def _is_venue_violating_restriction(venue, restrictions):
        # 检查餐厅名、类型、菜系等是否包含禁忌关键词
        text = (getattr(venue, "name", "") or "") + " " + (getattr(venue, "place_name", "") or "")
        text = text.lower()
        for r in restrictions:
            for key, keywords in RESTRICTION_KEYWORDS.items():
                if r in key or key in r:
                    for kw in keywords:
                        if kw in text:
                            return True
            # 直接用自定义禁忌词模糊匹配
            if r and r in text:
                return True
        return False

    summary = _build_direct_summary()
    if not (summary["both_preferences_ready"] and summary["both_locations_ready"] and summary["weighted_center"]):
        return {"status": "incomplete", "summary": summary}

    prefs_a = summary["prefs_a"]
    prefs_b = summary["prefs_b"]
    business_context = _is_business_context_from_prefs([prefs_a, prefs_b])
    shopping_context = _is_shopping_context_from_prefs([prefs_a, prefs_b])
    loc_a = Location(float(summary["loc_a"]["lat"]), float(summary["loc_a"]["lon"]))
    loc_b = Location(float(summary["loc_b"]["lat"]), float(summary["loc_b"]["lon"]))
    center = Location(float(summary["weighted_center"]["lat"]), float(summary["weighted_center"]["lon"]))

    restrictions_a = _collect_dietary_restrictions(prefs_a)
    restrictions_b = _collect_dietary_restrictions(prefs_b)
    all_restrictions = restrictions_a.union(restrictions_b)

    transport = _preferred_engine_transport(prefs_a.get("travel_mode"), prefs_b.get("travel_mode"))
    openai_key, model_name, openai_base = _load_llm_settings()
    engine = MeetHalfwayRecommender(
        mapbox_token=os.getenv("MAPBOX_ACCESS_TOKEN", "").strip(),
        ors_api_key=os.getenv("OPENROUTESERVICE_API_KEY", "").strip() or None,
        yelp_api_key=os.getenv("YELP_API_KEY", "").strip() or None,
        tavily_key=os.getenv("TAVILY_API_KEY", "").strip(),
        openai_key=openai_key,
        openai_model=model_name,
        openai_base=openai_base,
        transport=transport,
        isochrone_minutes=20,
        use_yelp=True,
        use_llm_extraction=bool(openai_key),
        use_llm_summary=bool(openai_key),
        low_cost_mode=True,
    )

    area_resolution = _resolve_search_area_with_radius_negotiation(engine, loc_a, loc_b, prefs_a, prefs_b)
    search_area = area_resolution["search_area"]
    radius_negotiation = area_resolution["detail"]
    if radius_negotiation.get("too_far"):
        return {
            "status": "too_far_for_recommendation",
            "summary": summary,
            "recommendation_meta": _build_recommendation_meta(
                str(search_area.get("mode") or "unknown"),
                [],
                prefs_a,
                prefs_b,
                radius_negotiation=radius_negotiation,
            ),
        }
    intersection = search_area.get("geometry")
    area_mode = str(search_area.get("mode") or "unknown")

    cuisine_plan = _build_cuisine_keyword_plan(prefs_a, prefs_b)
    venue_types = _combine_venue_preferences(prefs_a, prefs_b)
    search_limit = 8
    dedupe: Dict[Tuple[str, float, float], Any] = {}
    for venue_type in venue_types[:3]:
        found = _search_with_cuisine_fallback(
            engine,
            center,
            venue_type,
            search_limit,
            intersection,
            cuisine_plan,
        )
        for item in found:
            dedupe[(item.name.lower(), round(item.lat, 4), round(item.lon, 4))] = item

    candidates = list(dedupe.values())
    # 饮食禁忌过滤
    if all_restrictions:
        filtered_candidates = [c for c in candidates if not _is_venue_violating_restriction(c, all_restrictions)]
        if filtered_candidates:
            candidates = filtered_candidates
        # else: 保留原始候选但提示
    if not candidates:
        return {"status": "no_candidates", "summary": summary, "dietary_filtered": True}

    engine.tag_with_isochrone(candidates, intersection, area_mode=area_mode)
    slots_a = list(prefs_a.get("availability_slots", []) or [])
    slots_b = list(prefs_b.get("availability_slots", []) or [])
    if not slots_a or not slots_b:
        return {
            "status": "missing_time_selection",
            "summary": summary,
            "recommendation_meta": _build_recommendation_meta(
                area_mode,
                [],
                prefs_a,
                prefs_b,
                radius_negotiation=radius_negotiation,
                meeting_time_alignment=_analyze_meeting_time_alignment(prefs_a, prefs_b, []),
            ),
        }
    time_negotiation = _analyze_time_conflict(slots_a, slots_b)
    overlap_slots = _compute_shared_time_overlap(slots_a, slots_b)
    meeting_time_alignment = _analyze_meeting_time_alignment(prefs_a, prefs_b, overlap_slots)
    time_conflict = not bool(overlap_slots)
    selected_slots = overlap_slots or _build_negotiation_time_slots(slots_a, slots_b)
    time_label = _format_time_slot_label(selected_slots[0]) if overlap_slots else "Flexible"
    recommendation_meta = _build_recommendation_meta(
        area_mode,
        overlap_slots,
        prefs_a,
        prefs_b,
        open_status_stats,
        radius_negotiation=radius_negotiation,
        time_negotiation=time_negotiation,
        meeting_time_alignment=meeting_time_alignment,
    )

    try:
        asyncio.run(engine.enrich_all_async(candidates, city_hint="", year_hint=2026, time_slot=time_label, party_size=2))
    except Exception:
        pass

    candidates, open_status_stats = engine.filter_closed_candidates(candidates)
    recommendation_meta = _build_recommendation_meta(
        area_mode,
        overlap_slots,
        prefs_a,
        prefs_b,
        open_status_stats,
        radius_negotiation=radius_negotiation,
        time_negotiation=time_negotiation,
        meeting_time_alignment=meeting_time_alignment,
    )
    if not candidates:
        return {
            "status": "no_open_candidates",
            "summary": summary,
            "recommendation_meta": recommendation_meta,
        }

    scored = engine.score_candidates(
        loc_a,
        loc_b,
        center,
        candidates,
        w_dist=0.34,
        w_rating=0.25,
        w_pref=0.21,
        time_slots=selected_slots,
        availability={
            "a": slots_a,
            "b": slots_b,
        },
        radius_tolerance={
            "a": _distance_miles_to_minutes(prefs_a.get("distance_miles", 15), prefs_a.get("travel_mode")),
            "b": _distance_miles_to_minutes(prefs_b.get("distance_miles", 15), prefs_b.get("travel_mode")),
        },
        time_conflict=time_conflict,
    )
    scored = _apply_ambiance_preference(scored, prefs_a.get("ambiance_preference", "balanced"), prefs_b.get("ambiance_preference", "balanced"))
    _attach_parking_recommendations(scored[:10] if business_context else scored[:5], engine)
    scored = _apply_business_meeting_fit(scored, business_context)
    budget, cuisine_label, primary_venue_type = _summarize_preference_context(prefs_a, prefs_b)
    if business_context:
        recommendation_text = (
            "Business meeting shortlist: prioritized quiet professional settings, commute fairness, "
            f"arrival reliability, and nearby parking. Shared time: {time_label}. "
            f"Average budget: ${budget:.0f} per person."
        )
    elif shopping_context:
        shopping_goal = cuisine_label if cuisine_label != "flexible" else "shopping"
        recommendation_text = (
            "Shopping shortlist: prioritized malls, clothing stores, and department stores near the fair meeting area. "
            f"Shopping goal: {shopping_goal}. Shared time: {time_label}. Parking options are included when available."
        )
    else:
        recommendation_text = engine.generate_recommendation_text(
            loc_a,
            loc_b,
            center,
            scored[:5],
            budget=budget,
            cuisine=cuisine_label,
            time_slot=time_label,
            party_size=2,
            venue_type=primary_venue_type,
        )
    recommendation_meta["meeting_mode"] = _meeting_mode_label(business_context, shopping_context)

    return {
        "status": "ok",
        "summary": summary,
        "center": center,
        "transport": transport,
        "shared_slots": overlap_slots,
        "recommendation_meta": recommendation_meta,
        "recommendation_text": recommendation_text,
        "recommendations": scored[:5],
    }


# ============================================================================
# Candidate serialisation & UI helpers (used by check_result + vote pages)
# ============================================================================

_AMBIANCE_LABEL = {"quiet": "安静舒缓", "balanced": "氛围均衡", "lively": "热闹活跃"}


def _attach_parking_recommendations(
    candidates: list,
    engine: MeetHalfwayRecommender,
    limit_per_venue: int = 3,
) -> list:
    """Attach nearby destination parking options to each venue candidate."""
    for candidate in candidates:
        parking_options: list[Dict[str, Any]] = []
        try:
            venue_loc = Location(float(candidate.lat), float(candidate.lon))
            parking_lots = engine.search_nearby_venues(
                center=venue_loc,
                venue_type="parking",
                keyword="",
                limit=max(1, int(limit_per_venue)),
                intersection=None,
            )
            for parking in parking_lots[:limit_per_venue]:
                distance_km = engine.haversine_km(venue_loc, Location(float(parking.lat), float(parking.lon)))
                parking_options.append(
                    {
                        "name": parking.name,
                        "lat": float(parking.lat),
                        "lon": float(parking.lon),
                        "distance_m": int(distance_km * 1000),
                        "maps_url": f"https://www.google.com/maps/search/?api=1&query={float(parking.lat)},{float(parking.lon)}",
                    }
                )
        except Exception:
            parking_options = []
        candidate.parking_recommendations = parking_options
    return candidates


def _build_recommendation_reason(c: Any, summary: Dict[str, Any]) -> str:
    """Generate a short human-readable reason string for a CandidateRestaurant."""
    parts: list[str] = []
    bd = getattr(c, "score_breakdown", {}) or {}
    fairness = getattr(c, "fairness_delta_minutes", 0.0)
    best_time = getattr(c, "best_time_slot", "") or ""
    rating = getattr(c, "rating_proxy", 0.5)
    in_iso = getattr(c, "in_isochrone_intersection", False)
    search_area_mode = getattr(c, "search_area_mode", "intersection")
    time_conflict = bool(getattr(c, "time_conflict", False))
    closest_gap = float(getattr(c, "closest_time_gap_minutes", 0.0) or 0.0)
    severe_time_gap = bool(getattr(c, "severe_time_gap", False))

    if fairness < 3:
        parts.append("双方通勤时间几乎一致")
    elif fairness < 8:
        parts.append(f"双方通勤时间差约 {fairness:.0f} 分钟")

    if in_iso:
        parts.append("位于双方都能接受的可达区域内")
    elif search_area_mode == "union_fallback":
        parts.append("由于双方通勤范围没有重叠，因此从合并可达区域中选出")

    if rating >= 0.75:
        parts.append(f"综合口碑较高（{rating:.2f}）")
    elif rating >= 0.55:
        parts.append(f"综合口碑稳定（{rating:.2f}）")

    if time_conflict:
        parts.append("目前还没有共同可用时段")
        if severe_time_gap and closest_gap > 0:
            parts.append(f"最接近的可约时间仍相差约 {closest_gap:.0f} 分钟")
    elif best_time:
        parts.append(f"建议到店时间：{best_time}")

    ambiance_fit = float(bd.get("ambiance_fit", 0))
    pref_a = summary.get("prefs_a", {}).get("ambiance_preference", "balanced")
    pref_b = summary.get("prefs_b", {}).get("ambiance_preference", "balanced")
    if ambiance_fit >= 0.75:
        parts.append(f"氛围贴合双方偏好（{_AMBIANCE_LABEL.get(pref_a, pref_a)} / {_AMBIANCE_LABEL.get(pref_b, pref_b)}）")

    business_fit = float(bd.get("business_fit", 0))
    if business_fit >= 0.65:
        parts.append("适合商务会谈：环境更安静、场所更中立，并考虑到停车/到达便利")

    if getattr(c, "venue_category", "") in {"mall", "clothing", "department_store"}:
        parts.append("适合购物/逛商场场景，可作为非吃饭目的地")

    web_title = (getattr(c, "web_signals", {}) or {}).get("title", "")
    if web_title and len(web_title) < 80:
        parts.append(f'网页线索："{web_title}"')

    venue_status = str((getattr(c, "web_signals", {}) or {}).get("status", "uncertain")).lower()
    if venue_status == "uncertain":
        parts.append("营业状态仍需二次确认")

    return "；".join(parts) if parts else "综合评分表现较强"


def _serialise_candidates_for_vote(
    scored: list,
    summary: Dict[str, Any],
) -> list[Dict[str, Any]]:
    """Convert CandidateRestaurant objects to plain dicts for JSON storage & vote page."""
    result = []
    for c in scored[:5]:
        bd = getattr(c, "score_breakdown", {}) or {}
        ws = getattr(c, "web_signals", {}) or {}
        result.append({
            "name": c.name,
            "lat": float(c.lat),
            "lon": float(c.lon),
            "place_name": c.place_name or "",
            "final_score": round(float(c.final_score), 4),
            "fairness_delta_minutes": round(float(getattr(c, "fairness_delta_minutes", 0.0)), 2),
            "rating_proxy": round(float(c.rating_proxy), 3),
            "crowd_index": round(float(ws.get("crowd_index", 0.5)), 3),
            "best_time_slot": getattr(c, "best_time_slot", "") or "",
            "closest_time_gap_minutes": round(float(getattr(c, "closest_time_gap_minutes", 0.0) or 0.0), 2),
            "severe_time_gap": bool(getattr(c, "severe_time_gap", False)),
            "in_isochrone_intersection": bool(getattr(c, "in_isochrone_intersection", False)),
            "search_area_mode": getattr(c, "search_area_mode", "intersection"),
            "time_conflict": bool(getattr(c, "time_conflict", False)),
            "venue_status": str(ws.get("status", "uncertain")),
            "score_breakdown": {k: round(float(v), 4) for k, v in bd.items()},
            "web_title": ws.get("title", ""),
            "parking_recommendations": list(getattr(c, "parking_recommendations", []) or []),
            "recommendation_reason": _build_recommendation_reason(c, summary),
        })
    return result


def _render_saved_parking_options(candidate: Dict[str, Any]) -> None:
    parking = list(candidate.get("parking_recommendations", []) or [])
    if not parking:
        st.caption("Nearby parking: no parking lots found nearby yet.")
        return
    st.markdown("**Nearby parking:**")
    for option in parking[:3]:
        name = html.escape(str(option.get("name", "Parking")))
        maps_url = html.escape(str(option.get("maps_url", "")))
        distance_m = int(option.get("distance_m", 0) or 0)
        if maps_url:
            st.markdown(f"- <a href='{maps_url}' target='_blank'>{name}</a> ({distance_m} m)", unsafe_allow_html=True)
        else:
            st.caption(f"- {name} ({distance_m} m)")


def _render_candidate_cards(candidates: list[Dict[str, Any]]) -> None:
    """Render venue candidate cards in the UI."""
    medals = ["#1", "#2", "#3"]
    from meethalfway import MeetHalfwayRecommender, Location
    import os
    for idx, c in enumerate(candidates):
        medal = medals[idx] if idx < len(medals) else f"#{idx+1}"
        with st.container():
            st.markdown(
                f"""
                <div style="background:rgba(255,255,255,0.90);border:1px solid rgba(75,115,165,0.18);
                border-radius:18px;padding:15px 20px;margin-bottom:10px;
                box-shadow:0 6px 18px rgba(40,75,125,0.09);">
                <span style="font-size:1.15rem;font-weight:800;color:#1a3a5c;">{medal} &nbsp; {c.get('name','')}</span>
                <br/><span style="color:#4a6a8a;font-size:0.88rem;">{c.get('place_name','')}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            cols = st.columns(4)
            cols[0].metric("AI 评分", f"{float(c.get('final_score', 0)):.2f}")
            cols[1].metric("公平性差值", f"{float(c.get('fairness_delta_minutes', 0)):.1f} 分钟")
            cols[2].metric("口碑代理分", f"{float(c.get('rating_proxy', 0)):.2f}")
            cols[3].metric("拥挤指数", f"{float(c.get('crowd_index', 0.5)):.2f}")
            status_label = str(c.get("venue_status", "uncertain")).lower()
            st.caption(f"营业状态检查：{status_label}")
            reason = c.get("recommendation_reason", "")
            if reason:
                st.caption(f"推荐理由：{reason}")
            if c.get("time_conflict"):
                st.caption("建议到店时间：目前还没有共同可用时段")
                if c.get("severe_time_gap"):
                    st.caption(
                        f"时间提醒：双方最接近的可约时间仍相差约 {float(c.get('closest_time_gap_minutes', 0)):.0f} 分钟，建议手动协商。"
                    )
            elif c.get("best_time_slot"):
                st.caption(f"建议到店时间：{c['best_time_slot']}")
            if c.get("search_area_mode") == "union_fallback":
                st.caption("可达性说明：由于双方通勤范围没有重叠，这个地点来自合并可达区域。")
            bd = c.get("score_breakdown", {})
            if bd:
                st.caption(
                    "评分拆解："
                    f"距离 {bd.get('distance', bd.get('dist', 0)):.2f}  "
                    f"| 口碑 {bd.get('rating', 0):.2f}  "
                    f"| 时间重叠 {bd.get('availability_overlap', 0):.2f}  "
                    f"| 氛围匹配 {bd.get('ambiance_fit', 0):.2f}"
                )

            # --- Nearby Parking Lots ---
            if c.get("parking_recommendations"):
                _render_saved_parking_options(c)
                continue
            try:
                # Only run if lat/lon present
                lat = c.get("lat")
                lon = c.get("lon")
                if lat is not None and lon is not None:
                    st.markdown("<div style='margin-top:8px;'><b>🚗 Nearby Parking:</b></div>", unsafe_allow_html=True)
                    # Use a small radius for parking search
                    mapbox_token = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
                    ors_key = os.getenv("OPENROUTESERVICE_API_KEY", "").strip() or None
                    yelp_key = os.getenv("YELP_API_KEY", "").strip() or None
                    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
                    openai_key = os.getenv("OPENAI_API_KEY", "").strip() or None
                    model_name = os.getenv("MODEL_NAME", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
                    openai_base = os.getenv("OPENAI_API_BASE", "").strip() or None
                    engine = MeetHalfwayRecommender(
                        mapbox_token=mapbox_token,
                        ors_api_key=ors_key,
                        yelp_api_key=yelp_key,
                        tavily_key=tavily_key,
                        openai_key=openai_key,
                        openai_model=model_name,
                        openai_base=openai_base,
                        transport="drive",
                        low_cost_mode=True,
                        use_yelp=False,
                        use_llm_extraction=False,
                        use_llm_summary=False,
                        max_enriched_candidates=0,
                    )
                    venue_loc = Location(float(lat), float(lon))
                    # Search for parking lots within 400m (approx 0.25 miles)
                    parking_lots = engine.search_nearby_venues(
                        center=venue_loc,
                        venue_type="parking",
                        keyword="",
                        limit=4,
                        intersection=None,
                    )
                    if parking_lots:
                        for p in parking_lots:
                            dist_km = engine.haversine_km(venue_loc, Location(p.lat, p.lon))
                            dist_m = int(dist_km * 1000)
                            maps_url = f"https://www.google.com/maps/search/?api=1&query={p.lat},{p.lon}"
                            st.markdown(f"- <a href='{maps_url}' target='_blank'>{p.name}</a> ({dist_m} m)", unsafe_allow_html=True)
                    else:
                        st.caption("No parking lots found nearby.")
            except Exception as e:
                st.caption(f"[Parking search error: {e}]")


def _render_vote_button(room_id: str) -> None:
    """Show 'Go Vote' button and voting status."""
    summary = _preference_summary(room_id)
    keys = summary.get("keys", ["P1", "P2"])
    votes = {key: _load_vote(room_id, key) for key in keys}
    st.divider()
    cols = st.columns(len(keys))
    for col, key in zip(cols, keys):
        col.metric(_role_label(key), "Submitted" if votes[key] else "Not yet")
    if all(votes.values()):
        if st.button("See Final Results", type="primary", use_container_width=True, key="goto_final"):
            st.session_state.current_page = "final_result"
            st.rerun()
    else:
        if st.button("Go Vote for Your Top 3", type="primary", use_container_width=True, key="goto_vote"):
            st.session_state.current_page = "venue_vote"
            st.rerun()

# ============================================================================
# Styling
# ============================================================================
def inject_page_styles():
    st.markdown(
        """
        <style>
        :root {
            --ink: #18344f;
            --muted: #5f7287;
            --line: rgba(77, 109, 145, 0.18);
            --panel: rgba(255, 255, 255, 0.86);
            --panel-strong: rgba(255, 255, 255, 0.96);
            --rose: #ff5d8f;
            --blue: #377dff;
            --green: #2ba970;
            --gold: #ffb13d;
            --shadow: 0 18px 48px rgba(30, 60, 98, 0.12);
            --btn-secondary-bg: rgba(255, 255, 255, 0.92);
            --btn-secondary-bg-hover: rgba(255, 255, 255, 0.98);
            --btn-secondary-text: #18344f;
            --btn-secondary-border: rgba(81, 113, 151, 0.22);
            --btn-primary-bg: #ff4d57;
            --btn-primary-bg-hover: #ef434e;
            --btn-primary-text: #ffffff;
        }

        @media (prefers-color-scheme: dark) {
            :root {
                --btn-secondary-bg: rgba(19, 24, 35, 0.96);
                --btn-secondary-bg-hover: rgba(28, 35, 49, 0.98);
                --btn-secondary-text: #eef4ff;
                --btn-secondary-border: rgba(149, 176, 214, 0.26);
                --btn-primary-bg: #ff5f68;
                --btn-primary-bg-hover: #ff737b;
                --btn-primary-text: #ffffff;
            }
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(255, 120, 170, 0.14), transparent 26%),
                radial-gradient(circle at top right, rgba(60, 130, 255, 0.14), transparent 28%),
                linear-gradient(180deg, #f8fbff 0%, #f7fafc 38%, #eef4fb 100%);
            color: var(--ink);
        }

        .block-container {
            padding-top: 1.8rem;
            padding-bottom: 3rem;
            max-width: 1380px;
        }

        div[data-testid="stButton"] > button,
        div[data-testid="stFormSubmitButton"] > button {
            background: var(--btn-secondary-bg) !important;
            color: var(--btn-secondary-text) !important;
            border: 1px solid var(--btn-secondary-border) !important;
            box-shadow: 0 8px 24px rgba(30, 60, 98, 0.08);
            transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease, transform 0.2s ease;
        }

        div[data-testid="stButton"] > button:hover,
        div[data-testid="stFormSubmitButton"] > button:hover {
            background: var(--btn-secondary-bg-hover) !important;
            color: var(--btn-secondary-text) !important;
            border-color: var(--btn-secondary-border) !important;
            transform: translateY(-1px);
        }

        div[data-testid="stButton"] > button[kind="primary"],
        div[data-testid="stFormSubmitButton"] > button[kind="primary"] {
            background: var(--btn-primary-bg) !important;
            color: var(--btn-primary-text) !important;
            border-color: transparent !important;
        }

        div[data-testid="stButton"] > button[kind="primary"]:hover,
        div[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
            background: var(--btn-primary-bg-hover) !important;
            color: var(--btn-primary-text) !important;
        }

        div[data-testid="stButton"] > button:disabled,
        div[data-testid="stFormSubmitButton"] > button:disabled {
            opacity: 0.7;
            color: var(--btn-secondary-text) !important;
        }

        .poster-hero {
            position: relative;
            overflow: hidden;
            border-radius: 30px;
            border: 1px solid rgba(85, 117, 158, 0.16);
            background:
                radial-gradient(circle at 18% 18%, rgba(255,255,255,0.95), transparent 28%),
                radial-gradient(circle at 82% 24%, rgba(255,255,255,0.7), transparent 22%),
                linear-gradient(135deg, #fef4f8 0%, #f6fbff 36%, #edf9f2 100%);
            box-shadow: 0 24px 56px rgba(32, 64, 102, 0.14);
            padding: 30px 34px 28px;
            margin-bottom: 18px;
        }

        .hero-kicker {
            display: inline-flex;
            align-items: center;
            padding: 7px 14px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid rgba(65, 102, 155, 0.14);
            color: #35506a;
            font-weight: 700;
            font-size: 0.9rem;
            margin-bottom: 14px;
        }
        .hero-title {
            font-size: 3rem;
            line-height: 1.05;
            font-weight: 900;
            max-width: 760px;
            margin: 0 0 10px 0;
        }

        .hero-subtitle {
            max-width: 760px;
            color: #496175;
            font-size: 1.05rem;
            line-height: 1.65;
            margin-bottom: 18px;
        }

        .hero-pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 18px;
        }

        .hero-pill {
            padding: 10px 14px;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid rgba(66, 99, 139, 0.12);
            box-shadow: 0 8px 20px rgba(57, 88, 123, 0.08);
            font-weight: 700;
            color: #24415d;
        }

        .hero-grid {
            display: grid;
            grid-template-columns: 1.4fr 1fr;
            gap: 18px;
        }

        .glass-panel {
            background: var(--panel);
            border: 1px solid rgba(81, 113, 151, 0.14);
            border-radius: 24px;
            box-shadow: var(--shadow);
            backdrop-filter: blur(12px);
            padding: 22px;
        }

        .poster-metrics {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }

        .metric-card {
            background: var(--panel-strong);
            border-radius: 18px;
            padding: 16px;
            border: 1px solid rgba(76, 109, 150, 0.12);
            box-shadow: 0 10px 24px rgba(41, 69, 103, 0.08);
        }

        .metric-label {
            color: var(--muted);
            font-size: 0.86rem;
            margin-bottom: 8px;
            font-weight: 700;
        }

        .metric-value {
            font-size: 1.45rem;
            font-weight: 800;
            color: var(--ink);
        }

        .workflow-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 14px;
            margin-bottom: 18px;
        }

        .workflow-card {
            background: rgba(255,255,255,0.82);
            border: 1px solid rgba(75, 109, 150, 0.14);
            border-radius: 24px;
            padding: 18px;
            box-shadow: 0 16px 30px rgba(36, 67, 104, 0.08);
            min-height: 162px;
        }

        .workflow-head {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
        }

        .workflow-no {
            width: 34px;
            height: 34px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 900;
            font-size: 1rem;
            box-shadow: 0 8px 16px rgba(0,0,0,0.14);
        }

        .workflow-title {
            font-size: 1.05rem;
            font-weight: 800;
            color: var(--ink);
        }

        .workflow-card p {
            margin: 0;
            color: #567085;
            line-height: 1.55;
            font-size: 0.95rem;
        }

        .privacy-board {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin-top: 12px;
        }

        .privacy-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.95), rgba(245,249,255,0.92));
            border: 1px solid rgba(78, 112, 151, 0.12);
            border-radius: 18px;
            padding: 14px 16px;
        }

        .privacy-card strong {
            display: block;
            color: #284563;
            margin-bottom: 6px;
            font-size: 0.95rem;
        }

        .privacy-card span {
            color: #5f7387;
            line-height: 1.5;
            font-size: 0.92rem;
        }

        .location-mode-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 14px;
            margin: 16px 0 10px;
        }

        .location-mode-card {
            background: rgba(255, 255, 255, 0.84);
            border: 1px solid rgba(77, 109, 145, 0.16);
            border-radius: 22px;
            padding: 18px;
            box-shadow: 0 14px 28px rgba(36, 67, 104, 0.08);
            min-height: 148px;
        }

        .location-mode-card.active {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(239,247,255,0.98));
            border-color: rgba(55, 125, 255, 0.34);
            box-shadow: 0 18px 36px rgba(55, 125, 255, 0.14);
        }

        .location-mode-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 34px;
            height: 34px;
            border-radius: 12px;
            font-size: 1rem;
            font-weight: 900;
            margin-bottom: 12px;
            color: white;
            background: linear-gradient(135deg, #ff6b8f, #ff8a5b);
            box-shadow: 0 10px 20px rgba(255, 93, 143, 0.24);
        }

        .location-mode-card:nth-child(2) .location-mode-badge {
            background: linear-gradient(135deg, #377dff, #40b7ff);
            box-shadow: 0 10px 20px rgba(55, 125, 255, 0.22);
        }

        .location-mode-card:nth-child(3) .location-mode-badge {
            background: linear-gradient(135deg, #2ba970, #60c26c);
            box-shadow: 0 10px 20px rgba(43, 169, 112, 0.22);
        }

        .location-mode-card strong {
            display: block;
            color: var(--ink);
            font-size: 1rem;
            margin-bottom: 8px;
        }

        .location-mode-card p {
            margin: 0;
            color: #5b7084;
            line-height: 1.55;
            font-size: 0.94rem;
        }

        div[data-testid="stRadio"] label p {
            font-weight: 700;
            color: #26425e;
        }

        .location-hint {
            background: rgba(245, 250, 255, 0.88);
            border: 1px solid rgba(77, 109, 145, 0.14);
            border-radius: 18px;
            padding: 14px 16px;
            margin-bottom: 14px;
            color: #536c82;
        }

        .preference-intro {
            background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(244,249,255,0.96));
            border: 1px solid rgba(77, 109, 145, 0.14);
            border-radius: 20px;
            padding: 16px 18px;
            margin: 8px 0 16px;
            color: #4f667d;
            line-height: 1.55;
        }

        .preference-intro strong {
            color: #193654;
        }

        @media (max-width: 980px) {
            .location-mode-grid {
                grid-template-columns: 1fr;
            }
        }

        /* Side button panel */
        .side-button-panel {
            position: fixed;
            right: 20px;
            top: 100px;
            width: 200px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            z-index: 100;
        }

        .side-button {
            padding: 14px 16px;
            border-radius: 14px;
            border: none;
            background: rgba(255, 255, 255, 0.95);
            border: 1px solid rgba(81, 113, 151, 0.2);
            color: #18344f;
            font-weight: 700;
            font-size: 0.9rem;
            cursor: pointer;
            box-shadow: 0 8px 24px rgba(30, 60, 98, 0.12);
            transition: all 0.3s ease;
            text-align: center;
        }

        .side-button:hover {
            background: rgba(255, 255, 255, 0.98);
            box-shadow: 0 12px 32px rgba(30, 60, 98, 0.18);
            transform: translateY(-2px);
        }

        .side-button.active {
            background: var(--blue);
            color: white;
            border-color: var(--blue);
        }

        .side-button.active:hover {
            background: #2563d9;
        }

        /* Navigation buttons */
        .nav-buttons {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid rgba(77, 109, 145, 0.18);
        }

        .nav-button {
            padding: 12px 24px;
            border-radius: 12px;
            border: none;
            background: var(--panel);
            border: 1px solid rgba(81, 113, 151, 0.2);
            color: #18344f;
            font-weight: 700;
            font-size: 0.95rem;
            cursor: pointer;
            box-shadow: 0 8px 24px rgba(30, 60, 98, 0.08);
            transition: all 0.3s ease;
        }

        .nav-button:hover {
            background: var(--panel-strong);
            box-shadow: 0 12px 32px rgba(30, 60, 98, 0.12);
        }

        .nav-button.next {
            background: var(--blue);
            color: white;
            border-color: var(--blue);
        }

        .nav-button.next:hover {
            background: #2563d9;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _safe_clipboard_copy(text: str, key: str) -> bool:
    payload = json.dumps(text)
    result = streamlit_js_eval(
        js_expressions=f"""
        (function () {{
            const text = {payload};
            if (navigator.clipboard && window.isSecureContext) {{
                navigator.clipboard.writeText(text);
                return "ok";
            }}
            const textarea = document.createElement("textarea");
            textarea.value = text;
            textarea.style.position = "fixed";
            textarea.style.opacity = "0";
            document.body.appendChild(textarea);
            textarea.focus();
            textarea.select();
            const copied = document.execCommand("copy");
            document.body.removeChild(textarea);
            return copied ? "ok" : "failed";
        }})();
        """,
        key=f"copy_{key}",
    )
    return result == "ok"


def _get_app_base_url() -> str:
    """Best-effort public base URL for invite links."""
    browser_url = streamlit_js_eval(
        js_expressions="""
        (() => {
            try {
                if (window.top && window.top.location && window.top.location.origin) {
                    return window.top.location.origin;
                }
            } catch (e) {}
            if (window.location && window.location.origin) {
                return window.location.origin;
            }
            return window.location.href;
        })()
        """,
        key="detect_app_base_url",
    )
    if isinstance(browser_url, str) and browser_url.strip():
        parsed = urlparse(browser_url)
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            st.session_state["detected_app_base_url"] = base_url
            return base_url

    cached = str(st.session_state.get("detected_app_base_url", "")).strip()
    if cached:
        return cached.rstrip("/")

    configured = str(os.getenv("PUBLIC_APP_URL", "") or os.getenv("APP_BASE_URL", "")).strip()
    if configured:
        return configured.rstrip("/")

    default_port = 8502
    try:
        if STREAMLIT_CONFIG_PATH.exists():
            config_data = tomllib.loads(STREAMLIT_CONFIG_PATH.read_text(encoding="utf-8"))
            default_port = int(config_data.get("server", {}).get("port", default_port) or default_port)
    except Exception:
        pass

    return f"http://localhost:{default_port}"


def _build_invite_link(room_id: str) -> str:
    base_url = _get_app_base_url()
    return f"{base_url}/?room={quote(str(room_id or '').strip())}"


def _build_room_page_link(room_id: str, page: str = "check_result") -> str:
    base_url = _get_app_base_url()
    room_value = quote(str(room_id or "").strip())
    page_value = quote(str(page or "check_result").strip())
    return f"{base_url}/?room={room_value}&page={page_value}"


def _extract_room_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw and "room=" not in raw:
        return raw
    try:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query or "")
        room_values = params.get("room") or []
        if room_values and room_values[0].strip():
            return room_values[0].strip()
    except Exception:
        return ""
    return ""


def _missing_preference_fields(venue_type: Any, availability_slots: Any) -> list[str]:
    missing = []
    if not list(venue_type or []):
        missing.append("at least one place type")
    if not list(availability_slots or []):
        missing.append("at least one available time")
    return missing


def _render_result_ready_notifier(
    room_id: str,
    *,
    current_page: str,
    poll_seconds: int = 20,
) -> None:
    room_key = str(room_id or "").strip()
    if not room_key:
        return

    recommendation = _load_room_recommendation(room_key) or {}
    is_ready = recommendation.get("status") == "ready"
    generated_at = str(recommendation.get("generated_at", "") or recommendation.get("updated_at", "") or "")
    result_url = _build_room_page_link(room_key, page="check_result")
    payload = {
        "room_id": room_key,
        "current_page": current_page,
        "is_ready": bool(is_ready),
        "generated_at": generated_at,
        "result_url": result_url,
        "poll_ms": int(max(poll_seconds, 5) * 1000),
    }
    payload_json = json.dumps(payload)

    components.html(
        f"""
        <script>
        (function() {{
            const cfg = {payload_json};
            const roomKey = `mhai-result-notified:${{cfg.room_id}}:${{cfg.generated_at || 'pending'}}`;
            const fallbackKey = `mhai-result-fallback:${{cfg.room_id}}:${{cfg.generated_at || 'pending'}}`;
            const messageTitle = "MeetHalfway AI";
            const messageBody = "Your shared recommendations are ready. Open the result page to review them.";

            function markSeen() {{
                try {{
                    localStorage.setItem(roomKey, "1");
                    localStorage.setItem(fallbackKey, "1");
                }} catch (e) {{}}
            }}

            function openResults() {{
                try {{
                    window.parent.location.href = cfg.result_url;
                }} catch (e) {{
                    window.location.href = cfg.result_url;
                }}
            }}

            function showFallbackAlertOnce() {{
                try {{
                    if (localStorage.getItem(fallbackKey) === "1") {{
                        return;
                    }}
                }} catch (e) {{}}
                alert(messageBody);
                try {{
                    localStorage.setItem(fallbackKey, "1");
                }} catch (e) {{}}
            }}

            function ensureNotificationPermissionOnce() {{
                const permissionKey = `mhai-result-permission:${{cfg.room_id}}`;
                if (!("Notification" in window)) {{
                    return;
                }}
                try {{
                    if (localStorage.getItem(permissionKey) === "1") {{
                        return;
                    }}
                }} catch (e) {{}}

                try {{
                    localStorage.setItem(permissionKey, "1");
                }} catch (e) {{}}

                if (Notification.permission === "default") {{
                    try {{
                        Notification.requestPermission();
                    }} catch (e) {{}}
                }}
            }}

            if (cfg.is_ready && cfg.generated_at) {{
                let alreadySeen = false;
                try {{
                    alreadySeen = localStorage.getItem(roomKey) === "1";
                }} catch (e) {{}}
                if (!alreadySeen) {{
                    const finish = () => {{
                        markSeen();
                        if (cfg.current_page !== "check_result") {{
                            openResults();
                        }}
                    }};

                    if ("Notification" in window) {{
                        const notify = () => {{
                            try {{
                                const notification = new Notification(messageTitle, {{
                                    body: messageBody,
                                    tag: roomKey,
                                }});
                                notification.onclick = function() {{
                                    window.focus();
                                    openResults();
                                    notification.close();
                                }};
                            }} catch (e) {{
                                showFallbackAlertOnce();
                            }}
                            finish();
                        }};

                        if (Notification.permission === "granted") {{
                            notify();
                        }} else if (Notification.permission === "default") {{
                            Notification.requestPermission().then((permission) => {{
                                if (permission === "granted") {{
                                    notify();
                                }} else {{
                                    showFallbackAlertOnce();
                                    finish();
                                }}
                            }}).catch(() => {{
                                showFallbackAlertOnce();
                                finish();
                            }});
                        }} else {{
                            showFallbackAlertOnce();
                            finish();
                        }}
                    }} else {{
                        showFallbackAlertOnce();
                        finish();
                    }}
                }}
            }} else {{
                ensureNotificationPermissionOnce();
                if (cfg.current_page === "check_result") {{
                    window.setTimeout(() => {{
                        try {{
                            window.parent.location.href = cfg.result_url;
                        }} catch (e) {{
                            window.location.href = cfg.result_url;
                        }}
                    }}, cfg.poll_ms);
                }}
            }}
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


def _render_location_mode_cards(selected_mode: str) -> None:
    cards = [
        (
            "1) Auto location permission",
            "GPS",
            "Auto detect with GPS",
            "Let the browser request permission and try to read your live device location.",
        ),
        (
            "2) Map picker",
            "MAP",
            "Pick a point on the map",
            "Best when GPS is blocked or you want to choose a nearby starting point manually.",
        ),
        (
            "3) Enter address",
            "ADDR",
            "Type an address",
            "Search by street, city, or landmark and confirm the matched address.",
        ),
    ]
    markup = []
    for value, icon, title, body in cards:
        active_class = " active" if value == selected_mode else ""
        markup.append(
            f'<div class="location-mode-card{active_class}">'
            f'<div class="location-mode-badge">{icon}</div>'
            f"<strong>{title}</strong>"
            f"<p>{body}</p>"
            f"</div>"
        )
    st.markdown(f'<div class="location-mode-grid">{"".join(markup)}</div>', unsafe_allow_html=True)


def _get_browser_geolocation_diagnostics(component_key: str) -> Dict[str, Any]:
    raw = streamlit_js_eval(
        js_expressions="""
        new Promise((resolve) => {
            const hostname = window.location.hostname || "";
            const secureOk = window.isSecureContext || ["localhost", "127.0.0.1"].includes(hostname);
            const base = {
                hostname,
                href: window.location.href,
                isSecureContext: !!window.isSecureContext,
                secureOk,
                hasGeolocation: !!navigator.geolocation,
                permission: "unknown",
            };
            if (navigator.permissions && navigator.permissions.query) {
                navigator.permissions.query({ name: "geolocation" })
                    .then((result) => resolve({ ...base, permission: result.state || "unknown" }))
                    .catch(() => resolve(base));
            } else {
                resolve(base);
            }
        })
        """,
        key=component_key,
    )
    if isinstance(raw, dict):
        return raw
    return {}


def _request_browser_geolocation(component_key: str):
    return get_geolocation(component_key=component_key)


@st.cache_data(show_spinner=False, ttl=300)
def _fallback_ip_geolocation() -> Optional[Dict[str, float]]:
    try:
        import requests

        resp = requests.get("https://ipwho.is/", timeout=6)
        resp.raise_for_status()
        data = resp.json() if isinstance(resp.json(), dict) else {}
        if not data.get("success"):
            return None
        lat = data.get("latitude")
        lon = data.get("longitude")
        if lat is None or lon is None:
            return None
        return {"lat": float(lat), "lon": float(lon)}
    except Exception:
        return None


@st.cache_data(show_spinner=False, ttl=120)
def _address_suggestions(query: str) -> list[Dict[str, Any]]:
    q = (query or "").strip()
    if len(q) < 2:
        return []

    q_norm = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", q)
    q_norm = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", q_norm)
    q_norm = " ".join(q_norm.split())

    queries = [q]
    if q_norm and q_norm != q:
        queries.append(q_norm)
    if "kansas city" not in q_norm.lower():
        queries.append(f"{q_norm}, Kansas City, MO")

    results: list[Dict[str, Any]] = []
    seen: set[str] = set()

    def add_result(label: str, lat: Any, lon: Any, source: str) -> None:
        clean = (label or "").strip()
        if not clean:
            return
        key = clean.lower()
        if key in seen:
            return
        try:
            results.append({"label": clean, "lat": float(lat), "lon": float(lon), "source": source})
            seen.add(key)
        except Exception:
            return

    try:
        import requests

        mapbox_token = (os.getenv("MAPBOX_ACCESS_TOKEN") or "").strip()
        if mapbox_token:
            for qq in queries:
                resp = requests.get(
                    f"https://api.mapbox.com/geocoding/v5/mapbox.places/{qq}.json",
                    params={
                        "access_token": mapbox_token,
                        "autocomplete": "true",
                        "limit": 8,
                        "types": "address,place,poi",
                        "country": "US",
                        "proximity": "-94.5786,39.0997",
                    },
                    timeout=8,
                )
                if resp.status_code == 200:
                    data = resp.json() if isinstance(resp.json(), dict) else {}
                    for item in data.get("features", []):
                        center = item.get("center") or []
                        if isinstance(center, list) and len(center) >= 2:
                            add_result(str(item.get("place_name", "")), center[1], center[0], "mapbox")
                if len(results) >= 8:
                    break

        for qq in queries:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": qq,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "limit": 8,
                    "countrycodes": "us",
                },
                headers={"User-Agent": "MeetHalfwayAI/1.0"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json() if isinstance(resp.json(), list) else []
                for item in data:
                    add_result(str(item.get("display_name", "")), item.get("lat"), item.get("lon"), "osm")
            if len(results) >= 8:
                break
    except Exception:
        pass

    return results[:8]


def _geocode_address(query: str) -> Optional[Location]:
    q = (query or "").strip()
    if not q:
        return None

    q_norm = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", q)
    q_norm = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", q_norm)
    q_norm = " ".join(q_norm.split())

    try:
        import requests

        mapbox_token = (os.getenv("MAPBOX_ACCESS_TOKEN") or "").strip()
        if mapbox_token:
            resp = requests.get(
                f"https://api.mapbox.com/geocoding/v5/mapbox.places/{q_norm}.json",
                params={
                    "access_token": mapbox_token,
                    "limit": 1,
                    "autocomplete": "true",
                    "types": "address,place,poi",
                    "country": "US",
                    "proximity": "-94.5786,39.0997",
                },
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json() if isinstance(resp.json(), dict) else {}
                features = data.get("features") or []
                if features:
                    center = features[0].get("center") or []
                    if isinstance(center, list) and len(center) >= 2:
                        return Location(float(center[1]), float(center[0]))

        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q_norm, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "MeetHalfwayAI/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json() if isinstance(resp.json(), list) else []
        if data:
            return Location(float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception:
        return None
    return None


@st.cache_data(show_spinner=False, ttl=120)
def _reverse_geocode(lat: float, lon: float) -> str:
    try:
        import requests

        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "zoom": 18,
                "addressdetails": 1,
            },
            headers={"User-Agent": "MeetHalfwayAI/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json() if isinstance(resp.json(), dict) else {}
        return str(data.get("display_name", "")).strip()
    except Exception:
        return ""


# ============================================================================
# Page: Home (Introduction)
# ============================================================================
def render_home_page():
    """Render the home introduction page."""
    st.markdown(
        """
        <div class="poster-hero">
            <div class="hero-grid">
                <div>
                    <div class="hero-kicker">Privacy-first dating and dining planner</div>
                    <div class="hero-title">MeetHalfway AI</div>
                    <div class="hero-subtitle">
                        No more recommendations centered around just one person. We combine commute fairness,
                        mutual preferences, shared availability, venue popularity, and privacy safeguards
                        to find truly balanced places for both people to meet.
                    </div>
                    <div class="hero-pill-row">
                        <div class="hero-pill">Midpoint + overlap-based recommendations</div>
                        <div class="hero-pill">Private location handling in session memory</div>
                        <div class="hero-pill">Place voting + time negotiation</div>
                    </div>
                </div>
                <div class="glass-panel">
                    <div class="poster-metrics">
                        <div class="metric-card">
                            <div class="metric-label">Core Goal</div>
                            <div class="metric-value">Fairness</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">Privacy Policy</div>
                            <div class="metric-value">No location sharing between users</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">Recommendation Target</div>
                            <div class="metric-value">Restaurants / Date spots</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-label">Interaction Model</div>
                            <div class="metric-value">Voting + negotiation</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Workflow steps
    colors = ["#3578ff", "#28b36e", "#ff9d2f", "#8c63ff", "#ff5d8f", "#20a3a8"]
    steps = [
        ("Private location check-in", "Each user shares location with the system only, not with each other."),
        ("Set commute radius", "Choose acceptable travel radius for both sides to define a safe overlap area."),
        ("Search overlap area", "Find public places only inside the mutually acceptable overlap zone."),
        ("Vote on places", "Both users vote independently on the same candidates, with mutual preference prioritized."),
        ("Align available time", "Combine shared availability with time-slot preferences."),
        ("Generate final suggestion", "Balance fairness, preference alignment, and privacy in the final output."),
    ]

    cards = []
    for idx, (title, note) in enumerate(steps, start=1):
        cards.append(
            (
                f'<div class="workflow-card">'
                f'<div class="workflow-head">'
                f'<div class="workflow-no" style="background:{colors[idx - 1]};">{idx}</div>'
                f'<div class="workflow-title">{title}</div>'
                f'</div>'
                f'<p>{note}</p>'
                f'</div>'
            )
        )
    st.markdown(f'<div class="workflow-grid">{"".join(cards)}</div>', unsafe_allow_html=True)

    # Privacy cards
    st.markdown(
        """
        <div class="privacy-board">
            <div class="privacy-card">
                <strong>No direct location exchange</strong>
                <span>Users never share coordinates or addresses directly with each other.</span>
            </div>
            <div class="privacy-card">
                <strong>Radius and outcome first</strong>
                <span>The UI emphasizes travel radius, overlap area, and recommendations instead of raw locations.</span>
            </div>
            <div class="privacy-card">
                <strong>Vote-based coordination</strong>
                <span>Both users evaluate the same candidate list and converge via preference voting.</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ============================================================================
# Page: Select Action
# ============================================================================
def render_action_select_page():
    """Render the action selection page."""
    st.markdown(
        """
        <style>
        .action-hero {
            background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(243,248,255,0.96));
            border: 1px solid rgba(82, 113, 153, 0.16);
            border-radius: 28px;
            padding: 24px 28px 18px 28px;
            box-shadow: 0 18px 44px rgba(46, 78, 118, 0.10);
            margin-bottom: 22px;
        }
        .action-kicker {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 999px;
            background: rgba(255, 93, 143, 0.12);
            color: #c84b76;
            font-size: 0.82rem;
            font-weight: 700;
            letter-spacing: 0.02em;
            margin-bottom: 8px;
        }
        .action-title {
            font-size: 2.35rem;
            font-weight: 900;
            color: #18344f;
            margin: 0 0 8px 0;
        }
        .action-subtitle {
            color: #5b7088;
            font-size: 1.02rem;
            margin: 0;
        }
        .action-card {
            background: rgba(255,255,255,0.90);
            border: 1px solid rgba(86, 119, 160, 0.18);
            border-radius: 24px;
            padding: 20px 20px 14px 20px;
            box-shadow: 0 12px 28px rgba(42, 74, 112, 0.08);
            min-height: 150px;
            margin-bottom: 10px;
        }
        .action-card-icon {
            width: 52px;
            height: 52px;
            border-radius: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.4rem;
            margin-bottom: 12px;
            background: linear-gradient(135deg, rgba(255, 96, 128, 0.18), rgba(255, 176, 94, 0.16));
        }
        .action-card-title {
            color: #1b3a59;
            font-size: 1.35rem;
            font-weight: 800;
            margin-bottom: 6px;
        }
        .action-card-copy {
            color: #607489;
            font-size: 0.96rem;
            line-height: 1.55;
            margin-bottom: 0;
        }
        </style>
        <div class="action-hero">
            <div class="action-kicker">Start Here</div>
            <div class="action-title">What would you like to do?</div>
            <p class="action-subtitle">Pick the flow that fits your situation best. You can create a new invite, join one you received, or fill both sides directly.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2, gap="large")
    with col1:
        st.markdown(
            """
            <div class="action-card">
                <div class="action-card-icon">🔗</div>
                <div class="action-card-title">Generate Link</div>
                <p class="action-card-copy">Create a new meeting room, get a shareable invite link, and send it to your partner.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(
            "Create a New Invite",
            use_container_width=True,
            key="action_generate_link",
            type="primary",
        ):
            st.session_state.selected_action = "generate_link"
            st.session_state.current_page = "generate_link"
            st.rerun()
    with col2:
        st.markdown(
            """
            <div class="action-card">
                <div class="action-card-icon">📩</div>
                <div class="action-card-title">Join Link</div>
                <p class="action-card-copy">Open a room someone shared with you and continue with your own location and preferences.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(
            "Join an Existing Invite",
            use_container_width=True,
            key="action_join_link",
        ):
            st.session_state.selected_action = "join_link"
            st.session_state.current_page = "join_link"
            st.rerun()

    st.markdown(
        """
        <div class="action-card" style="margin-top: 12px;">
            <div class="action-card-icon">🗺️</div>
            <div class="action-card-title">Direct Two-Person Flow</div>
            <p class="action-card-copy">Best when you already know both starting points and want to fill both sides in one session.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button(
        "Enter Both People Manually",
        use_container_width=True,
        key="action_know_position",
    ):
        st.session_state.selected_action = "know_position"
        st.session_state.current_page = "know_position"
        st.rerun()

def render_generate_link_page():
    """Render the Generate Link page."""
    st.header("🔗 Generate Link")
    st.write("Create a unique link to share with your meeting partner.")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Step 1: Enter Your Information")
        st.caption("Create your own Room ID or leave blank and we will generate automatically")
        room_id = st.text_input("Room ID", value=st.session_state.room_id, placeholder="Enter custom ID or leave empty")
        participant_count = st.number_input(
            "How many people are joining?",
            min_value=2,
            max_value=MAX_PARTICIPANTS,
            value=int(st.session_state.get("participant_count", 2) or 2),
            step=1,
        )
        st.session_state.participant_count = int(participant_count)
        your_name = st.text_input("Your Name")

        st.markdown('<label>Your Email <span style="color: #999; font-size: 0.85em;">(optional)</span></label>', unsafe_allow_html=True)
        _ = st.text_input("Your Email", label_visibility="collapsed", placeholder="you@example.com")
        st.caption("💡 You can provide your email to receive the latest notifications.")

        st.session_state.room_id = room_id

    with col2:
        st.subheader("Generated Link")

        if st.button("🔗 Generate Link", type="primary", use_container_width=True, key="gen_link_btn"):
            import uuid

            final_room_id = room_id.strip() if room_id and room_id.strip() else str(uuid.uuid4())[:8]
            final_link = _build_invite_link(final_room_id)

            st.session_state.link_generated = True
            st.session_state.generated_room_id = final_room_id
            st.session_state.generated_link = final_link
            st.session_state.room_id = final_room_id
            st.session_state.user_role = "Person 1"
            st.session_state.creator_name = your_name
            st.session_state.user_name = your_name
            _set_room_participant_count(final_room_id, int(participant_count))

        if st.session_state.link_generated:
            st.success("✅ Link Generated!")
            st.markdown("**Your Invitation Link:**")
            st.code(st.session_state.generated_link, language="text")
            st.markdown("**Room ID:**")
            st.code(st.session_state.generated_room_id, language="text")


            st.info(f"Share this link or Room ID **{st.session_state.generated_room_id}** with the other {int(st.session_state.participant_count) - 1} participant(s) to start!")
            if "localhost" in st.session_state.generated_link or "127.0.0.1" in st.session_state.generated_link:
                st.warning("This invite currently points to a local-only address. For another device to open it, run the app on a LAN/public URL first or set PUBLIC_APP_URL in your environment.")

# ============================================================================
# Page: Join Link
# ============================================================================
def render_join_link_page():
    """Render the Join Link page."""
    st.header("✅ Confirm Your Details")

    # Check if user created a room in previous step
    is_creator = st.session_state.link_generated and st.session_state.generated_room_id

    # Check URL parameters for automatic room joining
    query_params = st.query_params
    room_from_url = query_params.get("room", "") if query_params else ""
    active_room_id = ""
    if is_creator and st.session_state.generated_room_id:
        active_room_id = st.session_state.generated_room_id
    elif room_from_url:
        active_room_id = room_from_url
    else:
        active_room_id = st.session_state.get("room_id", "")
    if active_room_id:
        _render_result_ready_notifier(active_room_id, current_page="join_link")
        st.caption("This page checks periodically for finished recommendations and will notify you when they are ready.")

    if is_creator:
        # Creator flow - auto-fill data
        st.write("As the meeting creator, confirm your details:")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Your Meeting Info")
            st.info(f"**Room ID:** {st.session_state.generated_room_id}")
            st.info(f"**Link:** {st.session_state.generated_link}")

            # Get name from session if available
            if "creator_name" not in st.session_state:
                st.session_state.creator_name = ""

            your_name = st.text_input("Your Name", value=st.session_state.creator_name, key="creator_name_input")
            st.session_state.creator_name = your_name

        with col2:
            st.subheader("Your Role")
            st.success("**Your Role: Person 1** (as the creator)")

            st.markdown("---")
            st.caption("You are the meeting creator. Other participants will join as Person 2 through Person 5 as needed.")

        # Save to session state for next steps
        st.session_state.room_id = st.session_state.generated_room_id
        st.session_state.user_role = "Person 1"
        st.session_state.user_name = your_name

    elif room_from_url:
        # Room link flow - allow either participant to resume from the same shared room.
        st.write("Welcome! Please confirm who you are before continuing in this room:")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Meeting Info")
            st.info(f"**Room ID:** {room_from_url}")
            st.caption("You're joining via a room invitation link")

            room_summary = _preference_summary(room_from_url)
            participant_count = int(room_summary.get("participant_count", 2) or 2)
            options = _role_options(participant_count)
            default_role = st.session_state.get("room_link_role", "Person 2")
            if default_role not in options:
                default_role = options[min(1, len(options) - 1)]
            selected_role = st.selectbox(
                "I am joining as",
                options,
                index=options.index(default_role),
                key="room_link_role",
            )
            role_key = _normalize_user_role(selected_role)

            saved_name = _load_saved_profile_name(room_from_url, role_key)
            previous_role_key = st.session_state.get("room_link_name_role")
            if previous_role_key != role_key:
                st.session_state.joiner_name_from_url = saved_name
                st.session_state.room_link_name_role = role_key
            elif "joiner_name_from_url" not in st.session_state:
                st.session_state.joiner_name_from_url = saved_name

            st.subheader("Your Details")
            your_name = st.text_input(
                "Your Name",
                value=st.session_state.joiner_name_from_url,
                key="joiner_name_from_url_input",
                placeholder="Enter your name",
            )
            st.session_state.joiner_name_from_url = your_name

        with col2:
            saved_location = _load_saved_location(room_from_url, role_key)
            saved_preferences = _load_saved_preferences(room_from_url, role_key)
            resume_page = _resume_page_for_participant(room_from_url, role_key)

            st.subheader("Your Role")
            st.success(f"**Your Role: {selected_role}**")
            if role_key == "P1":
                st.caption("Use this if you're reopening the room as the creator or continuing Person 1's side.")
            else:
                st.caption(f"Use this if you're joining or continuing {selected_role}'s side.")

            if saved_name or saved_location or saved_preferences:
                status_bits = []
                if saved_name:
                    status_bits.append("name saved")
                if saved_location:
                    status_bits.append("location saved")
                if saved_preferences:
                    status_bits.append("preferences saved")
                st.caption("We found your previous progress: " + ", ".join(status_bits) + ".")

            if resume_page == "check_result":
                st.caption("Continuing will reopen the shared results for this role.")
            elif resume_page == "user_info_step2":
                st.caption("Continuing will reopen your saved details so you can keep editing or resubmit.")
            else:
                st.caption("Continuing will reopen location entry for this role.")

        # Save to session state for next steps
        st.session_state.room_id = room_from_url
        st.session_state.user_role = selected_role
        st.session_state.user_name = your_name
        st.session_state.selected_action = "join_link"

        if not your_name:
            st.warning("Please enter your name to continue")
        elif st.button("Continue With This Invitation", type="primary", use_container_width=True, key=f"continue_room_{room_from_url}_{role_key}"):
            _persist_user_profile(room_from_url, role_key, your_name)
            st.session_state.current_page = resume_page
            st.rerun()

    else:
        # Joiner flow - manual input
        st.write("Enter the room ID or link provided by your meeting partner. You can also use this later to come back and continue where you left off.")
        st.caption("If you accidentally close the page, rejoin with the same Room ID and the same role. Your latest saved location/preferences will continue syncing inside that same room.")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Join Existing Meeting")

            input_method = st.radio("How to join?", ["Room ID", "Full Link"], horizontal=True)

            if input_method == "Room ID":
                room_id = st.text_input("Enter Room ID", placeholder="e.g., abc123 or john-sarah-2024")
            else:
                link = st.text_input("Enter Full Link", placeholder="https://your-app-url/?room=...")
                room_id = _extract_room_id(link)

            existing_summary = _preference_summary(room_id) if room_id else {"participant_count": st.session_state.get("participant_count", 2)}
            participant_count = int(existing_summary.get("participant_count", 2) or 2)
            your_role = st.selectbox("Your Role", _role_options(participant_count))
            role_key = _normalize_user_role(your_role)
            saved_name = _load_saved_profile_name(room_id, role_key) if room_id else ""
            saved_location = _load_saved_location(room_id, role_key) if room_id else None
            saved_preferences = _load_saved_preferences(room_id, role_key) if room_id else {}
            resume_page = _resume_page_for_participant(room_id, role_key) if room_id else "user_info_step1"

            if "joiner_name" not in st.session_state:
                st.session_state.joiner_name = ""
            if saved_name and st.session_state.joiner_name != saved_name:
                st.session_state.joiner_name = saved_name

            your_name = st.text_input("Your Name", value=st.session_state.joiner_name, key="joiner_name_input")
            st.session_state.joiner_name = your_name

        with col2:
            st.subheader("Summary")
            if room_id:
                st.success(f"✅ Room ID: **{room_id}**")
                st.info(f"👤 **Role:** {your_role}\n\n**Name:** {your_name if your_name else '(Not entered)'}")
                if saved_name or saved_location or saved_preferences:
                    status_bits = []
                    if saved_name:
                        status_bits.append("name saved")
                    if saved_location:
                        status_bits.append("location saved")
                    if saved_preferences:
                        status_bits.append("preferences saved")
                    st.caption("Found previous progress for this room and role: " + ", ".join(status_bits) + ".")
                    if resume_page == "user_info_step2":
                        st.caption("Continuing will reopen your saved details so you can keep editing or resubmit.")
                    elif resume_page == "check_result":
                        st.caption("Continuing will reopen your saved progress/results.")
                    else:
                        st.caption("Continuing will reopen location entry.")
            else:
                st.warning("Please enter a valid Room ID or Link")

        # Save to session state for next steps
        st.session_state.room_id = room_id
        st.session_state.user_role = your_role
        st.session_state.user_name = your_name
        st.session_state.selected_action = "join_link"

        if room_id and your_name:
            if st.button("Continue", type="primary", use_container_width=True, key="continue_manual_join"):
                _persist_user_profile(room_id, your_role, your_name)
                st.session_state.current_page = resume_page
                st.rerun()


# ============================================================================
# Page: Check Result
# ============================================================================
def render_check_result_page():
    """Render the Check Result page."""
    st.header("📊 Check Result")
    st.write("View the recommendation results for your meeting.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Your Room")
        room_id = st.text_input("Room ID to check", value=st.session_state.room_id)
    if room_id:
        _render_result_ready_notifier(room_id, current_page="check_result")
        st.caption("If the other person is still finishing their form, this page will keep checking and notify you when results are ready.")
    
    with col2:
        st.subheader("Results")
        if room_id:
            summary = _preference_summary(room_id)
            rec_cached = _load_room_recommendation(room_id)
            st.info(f"Results for room: **{room_id}**")

            total_people = int(summary.get("participant_count", 2) or 2)
            st.metric("Preferences Submitted", f"{summary.get('preferences_ready_count', 0)}/{total_people}")
            st.metric("Locations Confirmed", f"{summary.get('locations_ready_count', 0)}/{total_people}")
            st.metric("Weighted Center Ready", "Yes" if summary["weighted_center"] else "Not yet")
            st.metric("Recommendation", "Ready" if rec_cached and rec_cached.get("status") == "ready" else "Pending")
        else:
            st.warning("Enter a room ID to see results")

    if room_id:
        summary = _preference_summary(room_id)
        st.caption("Privacy mode: personal details are hidden on this page. Only room-level readiness is shown.")

        if summary["weighted_center"]:
            st.success("Everyone is ready. The shared center has been computed and recommendation generation can proceed.")
        else:
            st.warning("Waiting for all participants to submit location and preferences before recommendations can be generated.")

        rec_cached = _load_room_recommendation(room_id)
        recommendation_state = _compute_room_recommendations(room_id)
        if rec_cached and rec_cached.get("status") == "ready" and rec_cached.get("candidates") and rec_cached.get("recommendation_text"):
            # Already have a vote-ready candidate list saved
            st.markdown("### Recommended Venues")
            _render_recommendation_warnings(rec_cached.get("recommendation_meta", {}))
            _render_recommendation_summary(rec_cached.get("recommendation_text", ""))
            _render_candidate_cards(rec_cached["candidates"])
            _render_vote_button(room_id)
        elif recommendation_state["status"] == "ok":
            st.markdown("### Recommended Venues")
            scored = recommendation_state["recommendations"]
            # Serialise to dicts and generate reasons, save for vote page
            candidate_dicts = _serialise_candidates_for_vote(scored, recommendation_state.get("summary", {}))
            _render_recommendation_warnings(recommendation_state.get("recommendation_meta", {}))
            _render_recommendation_summary(recommendation_state.get("recommendation_text", ""))
            _save_room_recommendation(room_id, {
                "status": "ready",
                "generated_at": _utc_timestamp(),
                "room_id": room_id,
                "candidates": candidate_dicts,
                "recommendation_meta": recommendation_state.get("recommendation_meta", {}),
                "recommendation_text": recommendation_state.get("recommendation_text", ""),
            })
            _render_candidate_cards(candidate_dicts)
            _render_vote_button(room_id)
        elif recommendation_state["status"] == "no_candidates":
            st.error("Everyone is ready, but no venue candidates were found in the shared area yet.")
        elif recommendation_state["status"] == "no_open_candidates":
            _render_recommendation_warnings(recommendation_state.get("recommendation_meta", {}))
            st.error("We found places nearby, but none could be safely kept as open for the selected time. Please adjust the time and try again.")
        elif recommendation_state["status"] == "too_far_for_recommendation":
            _render_recommendation_warnings(recommendation_state.get("recommendation_meta", {}))
            st.error("These two locations are too far apart for a fair shared recommendation under the current commute settings.")
        elif rec_cached and rec_cached.get("status") == "failed":
            st.warning(rec_cached.get("message", "Recommendation could not be generated yet."))
    
    if st.button("🔄 Refresh Results", use_container_width=True):
        st.rerun()


def render_dual_preferences_page():
    """Render a single-page form for both people when locations are already known."""
    st.header("Dual Preferences")
    st.write("Fill in both people's commute limits and preferences. We will then generate the shared shortlist directly.")

    loc_a = st.session_state.get("location_A")
    loc_b = st.session_state.get("location_B")
    if not (isinstance(loc_a, Location) and isinstance(loc_b, Location)):
        st.error("Both locations are required before filling preferences.")
        return

    saved = st.session_state.get("direct_preferences", {}) or {"A": {}, "B": {}}

    def _render_pref_block(role_key: str) -> Dict[str, Any]:
        defaults = saved.get(role_key, {}) or {}
        loc = loc_a if role_key == "A" else loc_b
        st.subheader(f"Person {role_key}")
        default_meeting_type = str(defaults.get("meeting_type", "Dinner Date") or "Dinner Date")
        if default_meeting_type not in DIRECT_MEETING_TYPE_OPTIONS:
            default_meeting_type = "Dinner Date"
        meeting_type = st.selectbox(
            f"Meeting type - Person {role_key}",
            options=DIRECT_MEETING_TYPE_OPTIONS,
            index=DIRECT_MEETING_TYPE_OPTIONS.index(default_meeting_type),
            key=f"direct_meeting_type_{role_key}",
        )
        if _is_business_meeting_type(meeting_type):
            st.caption("Business mode will favor quiet professional venues, easy arrival, and nearby parking.")
        elif _is_shopping_meeting_type(meeting_type):
            st.caption("Shopping mode can find malls, clothing stores, or department stores near the fair meeting area.")
        cuisine = st.text_input(
            f"Food preference or shopping goal - Person {role_key}",
            value=defaults.get("cuisine", ""),
            key=f"direct_cuisine_{role_key}",
            placeholder="sushi, brunch, clothes, Nike shoes, suit, Plaza mall...",
        )

        # 新增：饮食禁忌/宗教饮食选项
        DIETARY_RESTRICTIONS_OPTIONS = [
            "不吃猪肉 (No Pork)",
            "不吃牛肉 (No Beef)",
            "清真 (Halal)",
            "犹太洁食 (Kosher)",
            "素食 (Vegetarian)",
            "纯素 (Vegan)",
            "无海鲜 (No Seafood)",
            "无坚果 (No Nuts)",
        ]
        dietary_restrictions = st.multiselect(
            f"饮食禁忌/宗教饮食 - Person {role_key}",
            options=DIETARY_RESTRICTIONS_OPTIONS,
            default=defaults.get("dietary_restrictions", []),
            key=f"direct_dietary_restrictions_{role_key}",
            help="如有宗教或健康相关饮食禁忌，请选择。"
        )
        dietary_restrictions_custom = st.text_input(
            f"补充其他饮食禁忌（可选） - Person {role_key}",
            value=defaults.get("dietary_restrictions_custom", ""),
            key=f"direct_dietary_restrictions_custom_{role_key}",
            placeholder="如：不吃羊肉、无麸质、忌辣等"
        )

        budget = st.slider(
            f"Budget per person ($) - Person {role_key}",
            min_value=10,
            max_value=200,
            value=int(defaults.get("budget", 50) or 50),
            step=5,
            key=f"direct_budget_{role_key}",
        )
        distance = _render_radius_selector_block(
            loc,
            int(defaults.get("distance_miles", 15) or 15),
            slider_key=f"direct_distance_{role_key}",
            map_key=f"direct_distance_preview_{role_key}",
            label=f"Max commute distance (miles) - Person {role_key}",
            min_value=1,
            max_value=40,
            help_text=f"Adjust Person {role_key}'s radius with the slider, then review the circle map below.",
        )
        travel_mode = st.selectbox(
            f"Travel mode - Person {role_key}",
            options=["transit", "walk", "drive"],
            index=["transit", "walk", "drive"].index(normalize_transport_mode(defaults.get("travel_mode", "transit"))),
            key=f"direct_travel_mode_{role_key}",
        )
        venue_type = st.multiselect(
            f"Preferred venue type - Person {role_key}",
            options=list(UI_TO_ENGINE_VENUE.keys()),
            default=defaults.get("venue_type", ["Restaurant"]) or ["Restaurant"],
            key=f"direct_venue_type_{role_key}",
        )
        availability_slots = st.multiselect(
            f"Available times - Person {role_key}",
            options=TIME_SLOT_OPTIONS,
            default=defaults.get("availability_slots", []),
            format_func=_format_time_slot_label,
            key=f"direct_availability_{role_key}",
        )
        ambiance_preference = st.select_slider(
            f"Preferred ambiance - Person {role_key}",
            options=["quiet", "balanced", "lively"],
            value=defaults.get("ambiance_preference", "balanced"),
            key=f"direct_ambiance_{role_key}",
        )
        return {
            "meeting_type": meeting_type,
            "cuisine": cuisine.strip(),
            "budget": budget,
            "distance_miles": distance,
            "venue_type": venue_type or ["Restaurant"],
            "surprise": bool(defaults.get("surprise", True)),
            "travel_mode": normalize_transport_mode(travel_mode),
            "availability_slots": availability_slots,
            "ambiance_preference": ambiance_preference,
            "dietary_restrictions": dietary_restrictions,
            "dietary_restrictions_custom": dietary_restrictions_custom.strip(),
        }

    col_a, col_b = st.columns(2)
    with col_a:
        payload_a = _render_pref_block("A")
    with col_b:
        payload_b = _render_pref_block("B")

    if st.button("Generate Shared Recommendations", type="primary", use_container_width=True, key="direct_generate_recs"):
        missing_a = _missing_preference_fields(payload_a.get("venue_type"), payload_a.get("availability_slots"))
        missing_b = _missing_preference_fields(payload_b.get("venue_type"), payload_b.get("availability_slots"))
        if missing_a or missing_b:
            parts = []
            if missing_a:
                parts.append("Person A must choose " + " and ".join(missing_a))
            if missing_b:
                parts.append("Person B must choose " + " and ".join(missing_b))
            st.warning(". ".join(parts) + " before generating recommendations.")
            return
        st.session_state.direct_preferences = {"A": payload_a, "B": payload_b}
        _reset_direct_flow_results()
        st.session_state.direct_recommendation_meta = {}
        with st.spinner("Generating the shared shortlist..."):
            rec_state = _compute_direct_recommendations()
        if rec_state.get("status") == "ok":
            st.session_state.direct_candidates = _serialise_candidates_for_vote(
                rec_state["recommendations"],
                rec_state.get("summary", {}),
            )
            st.session_state.direct_recommendation_meta = rec_state.get("recommendation_meta", {})
            st.session_state.direct_recommendation_text = rec_state.get("recommendation_text", "")
            st.session_state.current_page = "venue_vote"
            st.rerun()
        if rec_state.get("status") == "no_open_candidates":
            _render_recommendation_warnings(rec_state.get("recommendation_meta", {}))
            st.warning("No venues could be confidently kept open for the selected time. Try changing the time or venue type.")
        elif rec_state.get("status") == "no_candidates":
            st.warning("No matching venues were found in the shared area. Try widening the commute distance or venue types.")
        elif rec_state.get("status") == "missing_time_selection":
            st.warning("Each person must choose at least one available time before recommendations can be generated.")
        elif rec_state.get("status") == "too_far_for_recommendation":
            _render_recommendation_warnings(rec_state.get("recommendation_meta", {}))
            st.warning("These two locations are too far apart for a fair shared recommendation under the current commute settings.")
        else:
            st.warning("Both people's preferences and locations are required before recommendations can be generated.")


# ============================================================================
# Page: Vote - each person ranks their Top 3 from the 5 candidates
# ============================================================================
def render_vote_page():
    """Each person picks and ranks their favourite 3 out of 5 candidate venues."""
    st.header("Vote for Your Favourites")

    room_id = st.session_state.get("room_id", "")
    user_role = st.session_state.get("user_role", "")
    role_key = _normalize_user_role(user_role)

    if st.session_state.get("direct_flow_active") and not room_id:
        candidates = st.session_state.get("direct_candidates", []) or []
        _render_recommendation_warnings(st.session_state.get("direct_recommendation_meta", {}) or {})
        if not candidates:
            st.info("Recommendations are not ready yet. Please complete the dual preference form first.")
            return

        _render_recommendation_summary(st.session_state.get("direct_recommendation_text", ""))
        st.subheader("Candidate Venues")
        _render_candidate_cards(candidates)

        name_options = [c.get("name", f"#{i+1}") for i, c in enumerate(candidates)]
        vote_a = _load_direct_vote("A")
        vote_b = _load_direct_vote("B")

        def _vote_defaults(vote_list: list[str]) -> list[str]:
            base = name_options[:3] if len(name_options) >= 3 else (name_options + name_options)[:3]
            merged = (vote_list + base)[:3]
            return merged

        defaults_a = _vote_defaults(vote_a)
        defaults_b = _vote_defaults(vote_b)

        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("Person A Ranking")
            a1 = st.selectbox("Person A - 1st", name_options, index=name_options.index(defaults_a[0]), key="direct_vote_a1")
            a2 = st.selectbox("Person A - 2nd", name_options, index=name_options.index(defaults_a[1]), key="direct_vote_a2")
            a3 = st.selectbox("Person A - 3rd", name_options, index=name_options.index(defaults_a[2]), key="direct_vote_a3")
        with col_b:
            st.subheader("Person B Ranking")
            b1 = st.selectbox("Person B - 1st", name_options, index=name_options.index(defaults_b[0]), key="direct_vote_b1")
            b2 = st.selectbox("Person B - 2nd", name_options, index=name_options.index(defaults_b[1]), key="direct_vote_b2")
            b3 = st.selectbox("Person B - 3rd", name_options, index=name_options.index(defaults_b[2]), key="direct_vote_b3")

        picks_a = [a1, a2, a3]
        picks_b = [b1, b2, b3]
        if st.button("Submit Both Rankings", type="primary", use_container_width=True, key="submit_direct_votes"):
            if len(set(picks_a)) < 3 or len(set(picks_b)) < 3:
                st.error("Each person must rank three different venues.")
            else:
                _save_direct_vote("A", picks_a)
                _save_direct_vote("B", picks_b)
                st.session_state.current_page = "final_result"
                st.rerun()
        return

    if not room_id:
        st.error("Room ID is missing. Please go back to Confirm Details first.")
        if st.button("Back"):
            st.session_state.current_page = "check_result"
            st.rerun()
        return
    if role_key not in ("A", "B"):
        st.error("Role is missing. Please go back to Confirm Details and set your role.")
        return

    rec = _load_room_recommendation(room_id)
    if not rec or rec.get("status") != "ready":
        st.info("Recommendations are not ready yet. Please wait until both people have submitted their details.")
        if st.button("Check again"):
            st.rerun()
        return

    candidates = rec.get("candidates") or []

    st.subheader("Candidate Venues")
    st.caption("Review the 5 candidates below, then rank your Top 3 at the bottom of the page.")
    _render_recommendation_warnings(rec.get("recommendation_meta", {}))
    _render_recommendation_summary(rec.get("recommendation_text", ""))

    for idx, c in enumerate(candidates, start=1):
        with st.container():
            st.markdown(
                f"""
                <div style="background:rgba(255,255,255,0.88);border:1px solid rgba(80,120,160,0.18);
                border-radius:18px;padding:16px 20px;margin-bottom:12px;
                box-shadow:0 6px 20px rgba(40,80,130,0.08);">
                <span style="font-size:1.15rem;font-weight:800;color:#1a3a5c;">#{idx} &nbsp; {c.get('name','')}</span>
                <br/><span style="color:#4a6a8a;font-size:0.9rem;">{c.get('place_name') or c.get('address','')}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            cols = st.columns(4)
            cols[0].metric("评分", f"{float(c.get('final_score', 0)):.2f}")
            cols[1].metric("公平性差值", f"{float(c.get('fairness_delta_minutes', 0)):.1f} 分钟")
            cols[2].metric("口碑代理分", f"{float(c.get('rating_proxy', 0)):.2f}")
            cols[3].metric("拥挤指数", f"{float(c.get('crowd_index', 0.5)):.2f}")
            st.caption(f"营业状态检查：{str(c.get('venue_status', 'uncertain')).lower()}")
            reason = c.get("recommendation_reason", "")
            if reason:
                st.caption(f"推荐理由：{reason}")
            if c.get("time_conflict"):
                st.caption("建议到店时间：目前还没有共同可用时段")
                if c.get("severe_time_gap"):
                    st.caption(
                        f"时间提醒：双方最接近的可约时间仍相差约 {float(c.get('closest_time_gap_minutes', 0)):.0f} 分钟，建议手动协商。"
                    )
            elif c.get("best_time_slot"):
                st.caption(f"建议到店时间：{c['best_time_slot']}")
            if c.get("search_area_mode") == "union_fallback":
                st.caption("可达性说明：由于双方通勤范围没有重叠，这个地点来自合并可达区域。")
            bd = c.get("score_breakdown", {})
            if bd:
                st.caption(
                    "评分拆解："
                    f"距离 {bd.get('distance', bd.get('dist', 0)):.2f}  "
                    f"| 口碑 {bd.get('rating', 0):.2f}  "
                    f"| 时间重叠 {bd.get('availability_overlap', 0):.2f}  "
                    f"| 氛围匹配 {bd.get('ambiance_fit', 0):.2f}"
                )

    st.divider()
    st.subheader(f"Your Ranking - Person {role_key}")
    st.caption("Pick your #1, #2, and #3 choices. You must choose three different venues.")

    name_options = [c.get("name", f"#{i+1}") for i, c in enumerate(candidates)]
    saved_vote = _load_vote(room_id, role_key)

    prev_1 = saved_vote[0] if len(saved_vote) > 0 else name_options[0]
    prev_2 = saved_vote[1] if len(saved_vote) > 1 else name_options[1] if len(name_options) > 1 else name_options[0]
    prev_3 = saved_vote[2] if len(saved_vote) > 2 else name_options[2] if len(name_options) > 2 else name_options[0]

    col1, col2, col3 = st.columns(3)
    with col1:
        choice_1 = st.selectbox("1st Choice", name_options,
                                index=name_options.index(prev_1) if prev_1 in name_options else 0,
                                key=f"vote_1_{role_key}")
    with col2:
        choice_2 = st.selectbox("2nd Choice", name_options,
                                index=name_options.index(prev_2) if prev_2 in name_options else min(1, len(name_options)-1),
                                key=f"vote_2_{role_key}")
    with col3:
        choice_3 = st.selectbox("3rd Choice", name_options,
                                index=name_options.index(prev_3) if prev_3 in name_options else min(2, len(name_options)-1),
                                key=f"vote_3_{role_key}")

    picks = [choice_1, choice_2, choice_3]
    if len(set(picks)) < 3:
        st.warning("Please pick three different venues for your ranking.")

    if st.button("Submit My Ranking", type="primary", use_container_width=True):
        if len(set(picks)) < 3:
            st.error("Please choose three different venues before submitting.")
        else:
            _save_vote(room_id, role_key, picks)
            st.success("Your ranking has been saved!")
            summary = _preference_summary(room_id)
            room_votes = [_load_vote(room_id, key) for key in summary.get("keys", ["P1", "P2"])]
            if all(room_votes):
                st.session_state.current_page = "final_result"
                st.rerun()

    summary = _preference_summary(room_id)
    keys = summary.get("keys", ["P1", "P2"])
    votes = {key: _load_vote(room_id, key) for key in keys}
    st.divider()
    cols = st.columns(len(keys))
    for col, key in zip(cols, keys):
        col.metric(f"{_role_label(key)} ranking", "Submitted" if votes[key] else "Waiting")

    if all(votes.values()):
        if st.button("See Final Results", type="primary", use_container_width=True):
            st.session_state.current_page = "final_result"
            st.rerun()


# ============================================================================
# Page: Final Result - AI-combined top 3 for both people to choose from
# ============================================================================
def render_final_result_page():
    """Show the AI-merged top-3 shortlist that both people can discuss and book from."""
    st.header("最终推荐 shortlist")

    room_id = st.session_state.get("room_id", "")
    if st.session_state.get("direct_flow_active") and not room_id:
        vote_a = _load_direct_vote("A")
        vote_b = _load_direct_vote("B")
        candidates = st.session_state.get("direct_candidates", []) or []
        direct_meta = st.session_state.get("direct_recommendation_meta", {}) or {}
        medals = ["🥇", "🥈", "🥉"]
        if not (vote_a and vote_b and candidates):
            st.info("Please complete voting for both users first.")
            return

        final_3 = _compute_combined_ranking(vote_a, vote_b)[:3]
        candidates_by_name = {c.get("name", ""): c for c in candidates}

        st.info("These 3 places are the final shortlist, merged from both users' votes. Discuss together and decide!")
        _render_recommendation_warnings(direct_meta)
        _render_recommendation_summary(st.session_state.get("direct_recommendation_text", ""))
        for i, name in enumerate(final_3, start=1):
            c = candidates_by_name.get(name, {})
            score_a = (3 - vote_a.index(name)) if name in vote_a else 0
            score_b = (3 - vote_b.index(name)) if name in vote_b else 0
            total_pts = score_a + score_b
            st.markdown(
                f"""
                <div style="background:rgba(255,255,255,0.92);border:1.5px solid rgba(70,110,170,0.2);
                border-radius:20px;padding:20px 24px;margin-bottom:14px;
                box-shadow:0 8px 28px rgba(40,80,140,0.10);">
                <span style="font-size:1.6rem;">{medals[i-1]}</span>
                &nbsp;
                <span style="font-size:1.25rem;font-weight:800;color:#1a3558;">{name}</span>
                &nbsp;
                <span style="float:right;background:#f0f6ff;border-radius:999px;padding:4px 14px;
                font-size:0.9rem;font-weight:700;color:#2a66cc;">{total_pts} pts</span>
                <br/>
                <span style="color:#546a7e;font-size:0.88rem;">{c.get('place_name') or c.get('address','')}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            reason = c.get("recommendation_reason", "")
            if reason:
                st.caption(f"Reason: {reason}")
            _render_saved_parking_options(c)
            sc1, sc2, sc3 = st.columns(3)
            sc1.metric("A's Vote", f"#{vote_a.index(name)+1}" if name in vote_a else "-")
            sc2.metric("B's Vote", f"#{vote_b.index(name)+1}" if name in vote_b else "-")
            sc3.metric("Combined Score", f"{total_pts} pts")

        if st.button("Vote Again", use_container_width=True, key="direct_vote_again"):
            st.session_state.current_page = "venue_vote"
            st.rerun()
        return

    if not room_id:
        st.error("Room ID is missing.")
        return

    summary = _preference_summary(room_id)
    keys = summary.get("keys", ["P1", "P2"])
    votes = {key: _load_vote(room_id, key) for key in keys}

    if not all(votes.values()):
        missing = [_role_label(key) for key in keys if not votes[key]]
        st.info(f"Still waiting for {', '.join(missing)} to submit their ranking. You can share this page with the group to continue voting.")
        if st.button("🔄 Refresh"):
            st.rerun()
        return

    final_3 = _compute_combined_ranking(*votes.values())[:3]

    # Pull full candidate details
    rec = _load_room_recommendation(room_id)
    candidates_by_name: Dict[str, Any] = {}
    if rec and rec.get("status") == "ready":
        for c in rec.get("candidates", []):
            candidates_by_name[c.get("name", "")] = c
        _render_recommendation_warnings(rec.get("recommendation_meta", {}))
        _render_recommendation_summary(rec.get("recommendation_text", ""))

    st.markdown(
        """
        <div style="background:linear-gradient(135deg,#fff7f0,#f0f7ff);border:1px solid rgba(80,130,200,0.2);
        border-radius:22px;padding:18px 22px;margin-bottom:18px;">
        <p style="margin:0;color:#3a5570;font-size:1rem;">
        <strong>How we ranked:</strong> We used Borda count to merge both users' rankings:
        Each person's 1st place gets 3 points, 2nd place gets 2 points, 3rd place gets 1 point.
        The top 3 places with the highest combined scores are your final shortlist.
        <strong>Now discuss and choose your favorite, then book on your preferred platform.</strong>
        </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    medals = ["🥇", "🥈", "🥉"]
    for i, name in enumerate(final_3):
        c = candidates_by_name.get(name, {})
        with st.container():
            total_pts = sum((len(ranking) - ranking.index(name)) if name in ranking else 0 for ranking in votes.values())
            st.markdown(
                f"""
                <div style="background:rgba(255,255,255,0.92);border:1.5px solid rgba(70,110,170,0.2);
                border-radius:20px;padding:20px 24px;margin-bottom:14px;
                box-shadow:0 8px 28px rgba(40,80,140,0.10);">
                <span style="font-size:1.6rem;">{medals[i]}</span>
                &nbsp;
                <span style="font-size:1.25rem;font-weight:800;color:#1a3558;">{name}</span>
                &nbsp;
                <span style="float:right;background:#f0f6ff;border-radius:999px;padding:4px 14px;
                font-size:0.9rem;font-weight:700;color:#2a66cc;">{total_pts} pts</span>
                <br/>
                <span style="color:#546a7e;font-size:0.88rem;">{c.get('place_name') or c.get('address','')}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            reason = c.get("recommendation_reason", "")
            if reason:
                st.caption(f"Reason: {reason}")
            _render_saved_parking_options(c)
            vote_cols = st.columns(len(keys) + 1)
            for col, key in zip(vote_cols, keys):
                ranking = votes[key]
                col.metric(_role_label(key), f"#{ranking.index(name)+1}" if name in ranking else "Not ranked")
            vote_cols[-1].metric("Combined Score", f"{total_pts} pts")

    st.divider()
    st.success(
        "These are your **final 3 recommended meeting places**."
        "Discuss together, then use Google Maps, Yelp, or any other platform to check reviews and make a reservation."
    )

    col_a, col_b, col_c = st.columns(3)
    for i, name in enumerate(final_3):
        c = candidates_by_name.get(name, {})
        lat = c.get("lat")
        lon = c.get("lon")
        if lat and lon:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={float(lat)},{float(lon)}"
            yelp_url = f"https://www.yelp.com/search?find_desc={name.replace(' ', '+')}"
            [col_a, col_b, col_c][i].markdown(
                f"[{medals[i]} Google Maps]({maps_url})  \n[Yelp search]({yelp_url})"
            )

    if st.button("🔄 Vote Again", use_container_width=True):
        st.session_state.current_page = "venue_vote"
        st.rerun()


# ============================================================================
# Page: User Information - Step 1 (Private Location Input)
# ============================================================================
def render_user_info_step1_page():
    """Render the user information step 1 - Private location input."""
    st.header("Your Location")
    st.write(f"Person {st.session_state.user_role} - Please share your location")

    user_role = st.session_state.user_role.replace("Person ", "").upper()
    room_id = st.session_state.room_id
    if room_id:
        _render_result_ready_notifier(room_id, current_page="user_info_step1")
        st.caption("If your partner finishes the room before you do, this page can notify you when shared results become available.")

    if f"location_{user_role}" not in st.session_state:
        st.session_state[f"location_{user_role}"] = _load_saved_location(room_id, user_role)
    if f"gps_request_{user_role}" not in st.session_state:
        st.session_state[f"gps_request_{user_role}"] = False
    if f"gps_request_nonce_{user_role}" not in st.session_state:
        st.session_state[f"gps_request_nonce_{user_role}"] = 0
    if f"gps_result_nonce_{user_role}" not in st.session_state:
        st.session_state[f"gps_result_nonce_{user_role}"] = -1
    if f"location_candidate_{user_role}" not in st.session_state:
        st.session_state[f"location_candidate_{user_role}"] = None
    if f"gps_error_{user_role}" not in st.session_state:
        st.session_state[f"gps_error_{user_role}"] = None
    if f"ip_location_candidate_{user_role}" not in st.session_state:
        st.session_state[f"ip_location_candidate_{user_role}"] = None
    if f"map_pick_{user_role}" not in st.session_state:
        st.session_state[f"map_pick_{user_role}"] = None
    if f"map_center_{user_role}" not in st.session_state:
        st.session_state[f"map_center_{user_role}"] = {"lat": 39.0997, "lon": -94.5786}

    selected_mode = st.session_state.get(f"location_mode_{user_role}", "1) Auto location permission")
    _render_location_mode_cards(selected_mode)

    mode = st.radio(
        "Location mode",
        [
            "1) Auto location permission",
            "2) Map picker",
            "3) Enter address",
        ],
        horizontal=True,
        key=f"location_mode_{user_role}",
        label_visibility="collapsed",
    )

    if mode.startswith("1)"):
        st.subheader("Option 1: Auto location permission")
        st.caption("Allow browser location permission and use your current location.")
        st.markdown(
            """
            <div class="location-hint">
                Fastest option when browser location is enabled. Keep this tab active, accept the browser prompt, then wait a few seconds for the coordinates to return.
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_request, col_refresh = st.columns(2)
        with col_request:
            if st.button("Request GPS & Detect Location", type="primary", use_container_width=True, key=f"gps_request_btn_{user_role}"):
                st.session_state[f"gps_request_{user_role}"] = True
                st.session_state[f"gps_request_nonce_{user_role}"] += 1
                st.session_state[f"gps_result_nonce_{user_role}"] = -1
                st.session_state[f"location_candidate_{user_role}"] = None
                st.session_state[f"ip_location_candidate_{user_role}"] = None
                st.session_state[f"gps_error_{user_role}"] = None
        with col_refresh:
            if st.button("Refresh Location", use_container_width=True, key=f"gps_refresh_{user_role}"):
                st.session_state[f"gps_request_{user_role}"] = True
                st.session_state[f"gps_request_nonce_{user_role}"] += 1
                st.session_state[f"gps_result_nonce_{user_role}"] = -1
                st.session_state[f"location_candidate_{user_role}"] = None
                st.session_state[f"ip_location_candidate_{user_role}"] = None
                st.session_state[f"gps_error_{user_role}"] = None

        diagnostics = {}
        if st.session_state[f"gps_request_{user_role}"]:
            request_nonce = st.session_state[f"gps_request_nonce_{user_role}"]
            diagnostics = _get_browser_geolocation_diagnostics(
                component_key=f"geo_diag_{room_id}_{user_role}_{request_nonce}"
            )
            if diagnostics:
                if not diagnostics.get("hasGeolocation", True):
                    st.session_state[f"gps_error_{user_role}"] = {
                        "code": 0,
                        "message": "This browser does not support geolocation.",
                    }
                elif not diagnostics.get("secureOk", False):
                    st.session_state[f"gps_error_{user_role}"] = {
                        "code": -1,
                        "message": (
                            "Geolocation requires HTTPS or localhost. "
                            f"Current host: {diagnostics.get('hostname', 'unknown')}"
                        ),
                    }
                elif diagnostics.get("permission") == "denied":
                    st.session_state[f"gps_error_{user_role}"] = {
                        "code": 1,
                        "message": "Location permission is denied in the browser settings.",
                    }

            geo_raw = _request_browser_geolocation(component_key=f"geo_{room_id}_{user_role}_{request_nonce}")
            if isinstance(geo_raw, dict) and st.session_state[f"gps_result_nonce_{user_role}"] != request_nonce:
                st.session_state[f"gps_result_nonce_{user_role}"] = request_nonce
                if geo_raw.get("error"):
                    st.session_state[f"gps_error_{user_role}"] = geo_raw["error"]
                else:
                    try:
                        lat = float(geo_raw.get("coords", {}).get("latitude", 0))
                        lon = float(geo_raw.get("coords", {}).get("longitude", 0))
                        accuracy = float(geo_raw.get("coords", {}).get("accuracy", 0) or 0)
                        if lat != 0 and lon != 0:
                            st.session_state[f"location_candidate_{user_role}"] = {
                                "lat": lat,
                                "lon": lon,
                                "accuracy": accuracy,
                            }
                            st.session_state[f"gps_error_{user_role}"] = None
                    except Exception:
                        st.session_state[f"gps_error_{user_role}"] = {
                            "code": -2,
                            "message": "We received a browser response but could not parse the coordinates.",
                        }

        candidate = st.session_state.get(f"location_candidate_{user_role}")
        gps_error = st.session_state.get(f"gps_error_{user_role}")
        ip_candidate = st.session_state.get(f"ip_location_candidate_{user_role}")

        if not candidate:
            if gps_error:
                if not ip_candidate:
                    fallback = _fallback_ip_geolocation()
                    if fallback:
                        st.session_state[f"ip_location_candidate_{user_role}"] = fallback
                        ip_candidate = fallback
                error_code = gps_error.get("code")
                error_message = gps_error.get("message", "Unable to access your location.")
                if error_code == 1:
                    st.error("Location permission was denied. Please allow location access in the browser and click Refresh Location.")
                elif error_code == 2:
                    st.error("Your device or this browser container could not determine an exact GPS position. Please try Refresh Location or use Map picker.")
                elif error_code == 3:
                    st.error("Location lookup timed out. Please click Refresh Location and keep the tab active.")
                elif error_code == -1:
                    st.error("Browser geolocation works only on HTTPS or localhost pages. If you opened this app from another non-secure address, switch to localhost.")
                else:
                    st.error(f"Could not access your location: {error_message}")
            elif st.session_state[f"gps_request_{user_role}"]:
                st.info("Waiting for browser geolocation. Keep this tab active and click Allow if your browser shows a location prompt.")
            else:
                st.info("Click the button above. Your browser will request location permission.")

            if diagnostics:
                permission = diagnostics.get("permission", "unknown")
                secure_label = "yes" if diagnostics.get("secureOk") else "no"
                st.caption(
                    f"Browser GPS diagnostics: permission={permission}, secure-context={secure_label}, host={diagnostics.get('hostname', 'unknown')}"
                )

            if ip_candidate:
                approx_address = _reverse_geocode(ip_candidate["lat"], ip_candidate["lon"])
                st.warning("Exact GPS is unavailable in this browser right now. We found an approximate location from your network instead.")
                if approx_address:
                    st.caption(f"Approximate area: {approx_address}")
                st.caption(f"Approximate coordinates: {ip_candidate['lat']:.5f}, {ip_candidate['lon']:.5f}")
                if st.button("Use Approximate Network Location", use_container_width=True, key=f"use_ip_location_{user_role}"):
                    st.session_state[f"location_{user_role}"] = Location(ip_candidate["lat"], ip_candidate["lon"])
                    _persist_user_location(room_id, user_role, st.session_state[f"location_{user_role}"], "ip-approx")
                    st.success("Approximate location confirmed!")
        else:
            accuracy = float(candidate.get("accuracy", 0) or 0)
            accuracy_note = f" (accuracy about {accuracy:.0f} m)" if accuracy > 0 else ""
            st.success(f"Location detected: {candidate['lat']:.5f}, {candidate['lon']:.5f}{accuracy_note}")
            resolved_address = _reverse_geocode(candidate["lat"], candidate["lon"])
            if resolved_address:
                st.caption(f"Detected area: {resolved_address}")
            if st.button("Confirm This Location", type="primary", use_container_width=True, key=f"confirm_gps_{user_role}"):
                st.session_state[f"location_{user_role}"] = Location(candidate["lat"], candidate["lon"])
                _persist_user_location(room_id, user_role, st.session_state[f"location_{user_role}"], "gps")
                st.success("Location confirmed!")

    elif mode.startswith("2)"):
        st.subheader("Option 2: Map picker")
        st.caption("Map defaults to Kansas City. Hover to see crosshair, click to choose a point, then confirm it.")

        st.markdown(
            """
            <style>
            .leaflet-container {
                cursor: crosshair !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        center = st.session_state[f"map_center_{user_role}"]
        picked = st.session_state[f"map_pick_{user_role}"]
        map_data = folium.Map(location=[center["lat"], center["lon"]], zoom_start=12)
        if picked:
            folium.CircleMarker(
                location=[picked["lat"], picked["lon"]],
                radius=8,
                color="#ff2b2b",
                fill=True,
                fill_color="#ff2b2b",
                fill_opacity=0.9,
                tooltip="Pending confirmation",
            ).add_to(map_data)

        map_result = st_folium(
            map_data,
            width=None,
            height=460,
            key=f"picker_map_{user_role}",
            use_container_width=True,
        )
        clicked = map_result.get("last_clicked") if isinstance(map_result, dict) else None
        if clicked:
            picked = {"lat": float(clicked["lat"]), "lon": float(clicked["lng"])}
            st.session_state[f"map_pick_{user_role}"] = picked
            st.session_state[f"map_center_{user_role}"] = picked

        latest = st.session_state.get(f"map_pick_{user_role}")
        if latest:
            st.info(f"Selected coordinates: {latest['lat']:.6f}, {latest['lon']:.6f}")
            address_name = _reverse_geocode(latest["lat"], latest["lon"])
            if address_name:
                st.caption(f"Approx street/place: {address_name}")
            else:
                st.caption("Approx street/place: unavailable for this point")
        else:
            st.info("No map point selected yet. Click once on the map to create a red pending point.")

        if st.button(
            "Confirm this clicked location",
            type="primary",
            use_container_width=True,
            key=f"confirm_map_{user_role}",
            disabled=(latest is None),
        ):
            st.session_state[f"location_{user_role}"] = Location(latest["lat"], latest["lon"])
            _persist_user_location(room_id, user_role, st.session_state[f"location_{user_role}"], "map-picker")
            st.success("Location confirmed from map picker!")

    else:
        st.subheader("Option 3: Enter address")
        st.caption("Type a few numbers/letters and we will auto-match address candidates like delivery apps.")

        query = st.text_input("Your Address", placeholder="e.g., 111 Main St, Kansas City, MO", key=f"address_query_{user_role}")
        suggestions = _address_suggestions(query)
        suggestion_labels = [item["label"] for item in suggestions]
        selected = st.selectbox(
            "Suggestions",
            options=[""] + suggestion_labels,
            format_func=lambda x: x if x else "Select a matched address",
            key=f"address_suggestions_{user_role}",
        )
        selected_item = next((item for item in suggestions if item["label"] == selected), None)
        if query.strip() and not suggestions:
            st.caption("No instant matches yet. Keep typing with spaces (example: 5310 Rockhill).")
        elif selected_item:
            st.caption(f"Matched coordinates: {selected_item['lat']:.6f}, {selected_item['lon']:.6f}")
        final_address = selected or query

        if st.button("Confirm Address", type="primary", use_container_width=True, key=f"confirm_address_{user_role}"):
            location = None
            if selected_item:
                location = Location(float(selected_item["lat"]), float(selected_item["lon"]))
            else:
                location = _geocode_address(final_address)
            if location is not None:
                st.session_state[f"location_{user_role}"] = location
                _persist_user_location(room_id, user_role, location, "address")
                st.success("Location confirmed from address!")
            else:
                st.error("Could not locate this address. Please refine your input.")

    st.markdown("---")
    location = st.session_state.get(f"location_{user_role}")
    if location:
        st.success("Your location is confirmed.")
    else:
        st.warning("Please confirm your location to continue.")


# ============================================================================
# Page: User Information - Step 2 (Preferences)
# ============================================================================
def render_user_info_step2_page():
    """Render the user information step 2 - Preferences & search strategy."""
    st.header("✍️ Your Preferences")
    st.write(f"Person {st.session_state.user_role} - a few quick choices so we can narrow things down.")
    room_id = st.session_state.room_id
    role_label = st.session_state.user_role
    role_key = _normalize_user_role(role_label)
    if room_id:
        _render_result_ready_notifier(room_id, current_page="user_info_step2")
        st.caption("You can leave this tab open. We will keep checking the room and notify you when the shared recommendations are ready.")

    if not str(room_id or "").strip():
        st.error("Room ID is missing. Please go back to Confirm Details first.")
        return
    if _role_index(role_key) is None:
        st.error("Role is missing. Please go back to Confirm Details and set your role.")
        return

    saved_preferences = _load_saved_preferences(room_id, role_key)
    summary = _preference_summary(room_id)
    participant_count = int(summary.get("participant_count", 2) or 2)
    partner_keys = _partner_roles(role_key, total=participant_count)
    partner_ready = [
        key for key in partner_keys
        if (summary.get("participants", {}).get(key, {}) or {}).get("preferences")
    ]
    current_location = st.session_state.get(f"location_{role_key}") or _load_saved_location(room_id, role_key)

    defaults = {
        "meeting_type": saved_preferences.get("meeting_type", "Dinner Date"),
        "cuisine": saved_preferences.get("cuisine", ""),
        "budget": int(saved_preferences.get("budget", 50) or 50),
        "distance_miles": int(saved_preferences.get("distance_miles", 15) or 15),
        "venue_type": saved_preferences.get("venue_type", ["Restaurant"]) or ["Restaurant"],
        "surprise": bool(saved_preferences.get("surprise", True)),
        "travel_mode": normalize_transport_mode(saved_preferences.get("travel_mode", "transit")),
        "availability_slots": saved_preferences.get("availability_slots", []),
        "ambiance_preference": saved_preferences.get("ambiance_preference", "balanced"),
    }

    widget_defaults = {
        f"meeting_type_{role_label}": defaults["meeting_type"],
        f"cuisine_{role_label}": defaults["cuisine"],
        f"budget_{role_label}": defaults["budget"],
        f"distance_{role_label}": defaults["distance_miles"],
        f"venue_type_{role_label}": defaults["venue_type"],
        f"surprise_{role_label}": defaults["surprise"],
        f"travel_mode_{role_label}": defaults["travel_mode"],
        f"availability_slots_{role_label}": defaults["availability_slots"],
        f"ambiance_preference_{role_label}": defaults["ambiance_preference"],
    }
    for key, value in widget_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    st.markdown(
        """
        <div class="preference-intro">
            <strong>Keep this simple:</strong> answer the easy questions first, skip anything optional, and we'll use the overlap between both people to find a fair meeting spot.
        </div>
        """,
        unsafe_allow_html=True,
    )

    status_cols = st.columns(2)
    with status_cols[0]:
        if saved_preferences:
            submitted_at = saved_preferences.get("submitted_at", "")
            st.success(f"✅ Your preferences are saved{f' ({submitted_at})' if submitted_at else ''}")
        else:
            st.info("Fill out this form and click Submit Preferences to save your choices for this room.")
    with status_cols[1]:
        if len(partner_ready) == len(partner_keys):
            st.success("All other participants have already submitted preferences")
        else:
            st.info(f"Other preferences submitted: {len(partner_ready)}/{len(partner_keys)}")

    with st.container(border=True):
        st.subheader("Quick Basics")
        basics_left, basics_right = st.columns(2)

        with basics_left:
            meeting_type = st.selectbox(
                "What are you meeting for?",
                ROOM_MEETING_TYPE_OPTIONS,
                key=f"meeting_type_{role_label}",
            )
            if _is_business_meeting_type(meeting_type):
                st.caption("Business mode prioritizes quiet professional venues, neutral atmosphere, arrival reliability, and parking.")
            elif _is_shopping_meeting_type(meeting_type):
                st.caption("Shopping mode can recommend malls, clothing stores, or department stores near the fair meeting area.")
            budget = st.slider(
                "About how much per person?",
                min_value=0,
                max_value=200,
                step=10,
                key=f"budget_{role_label}",
            )

        with basics_right:
            cuisine = st.text_input(
                "Food preference or shopping goal? (optional)",
                placeholder="e.g., Italian, Japanese, clothes, Nike shoes, suit, Plaza mall",
                key=f"cuisine_{role_label}",
            )
            ambiance_preference = st.select_slider(
                "What kind of vibe do you want?",
                options=["quiet", "balanced", "lively"],
                value=st.session_state[f"ambiance_preference_{role_label}"],
                format_func=lambda x: {
                    "quiet": "Quiet / calm",
                    "balanced": "Balanced",
                    "lively": "Lively / busy",
                }[x],
                key=f"ambiance_preference_{role_label}",
            )

    with st.container(border=True):
        st.subheader("Getting There")
        travel_mode = st.radio(
            "How will you get there?",
            options=["walk", "transit", "drive"],
            format_func=lambda x: {
                "walk": "Walk",
                "transit": "Transit / Bus / Train",
                "drive": "Drive",
            }[x],
            horizontal=True,
            key=f"travel_mode_{role_label}",
        )
        distance = _render_radius_selector_block(
            current_location,
            int(st.session_state[f"distance_{role_label}"]),
            slider_key=f"distance_{role_label}",
            map_key=f"distance_preview_{role_key}",
            label="Max travel distance (miles)",
            min_value=1,
            max_value=50,
            help_text="Use the slider above to set your commute radius. The map below stays centered on your confirmed location and draws your current travel circle.",
        )

    with st.container(border=True):
        st.subheader("Food, Place, and Time")
        preferences_left, preferences_right = st.columns(2)

        with preferences_left:
            venue_type = st.multiselect(
                "What kinds of places sound good?",
                ["Restaurant", "Cafe", "Bar", "Park", "Mall", "Shopping", "Clothing Store", "Department Store", "Museum", "Theater", "Parking"],
                key=f"venue_type_{role_label}",
            )
            surprise = st.checkbox(
                "I'm open to a surprise suggestion",
                key=f"surprise_{role_label}",
            )

        with preferences_right:
            availability_slots = st.multiselect(
                "When are you free?",
                options=TIME_SLOT_OPTIONS,
                format_func=_format_time_slot_label,
                key=f"availability_slots_{role_label}",
                help="Pick every 30-minute time block that works for you.",
            )

    st.caption("We keep only the shared time overlap across everyone, and travel mode helps protect people with tighter commute limits.")
    submitted = st.button("Submit Preferences", type="primary", use_container_width=True, key=f"submit_preferences_{role_key}")

    if saved_preferences:
        st.caption("Saved data is shared by room ID, so the other person can submit from their own session and we can combine both sides later.")

    updated_summary = _preference_summary(room_id)
    if submitted:
        missing_fields = _missing_preference_fields(venue_type, availability_slots)
        if missing_fields:
            st.session_state["last_preferences_submit_message"] = (
                "Please choose " + " and ".join(missing_fields) + " before continuing."
            )
            st.session_state["last_preferences_submit_level"] = "warning"
            st.rerun()

        payload = {
            "meeting_type": meeting_type,
            "cuisine": cuisine.strip(),
            "budget": budget,
            "distance_miles": distance,
            "venue_type": venue_type,
            "surprise": surprise,
            "travel_mode": normalize_transport_mode(travel_mode),
            "availability_slots": availability_slots,
            "ambiance_preference": ambiance_preference,
        }
        _persist_user_preferences(room_id, role_key, payload)
        updated_summary = _preference_summary(room_id)
        st.session_state["last_preferences_submit_message"] = f"Preferences saved to room {room_id} for {_role_label(role_key)}."
        st.session_state["last_preferences_submit_level"] = "success"
        if updated_summary["both_preferences_ready"] and updated_summary["both_locations_ready"]:
            with st.spinner("Everyone is ready - searching for venues and generating recommendations..."):
                rec_state = _compute_room_recommendations(room_id)
            if rec_state.get("status") == "ok":
                candidate_dicts = _serialise_candidates_for_vote(
                    rec_state["recommendations"], rec_state.get("summary", {})
                )
                _save_room_recommendation(room_id, {
                    "status": "ready",
                    "generated_at": _utc_timestamp(),
                    "room_id": room_id,
                    "candidates": candidate_dicts,
                    "recommendation_meta": rec_state.get("recommendation_meta", {}),
                    "recommendation_text": rec_state.get("recommendation_text", ""),
                })
                st.session_state["last_preferences_submit_message"] = "Recommendations generated! Time to vote for your favourites."
                st.session_state["last_preferences_submit_level"] = "success"
                st.session_state.selected_action = st.session_state.selected_action or "check_result"
                st.session_state.current_page = "check_result"
                st.rerun()
            elif rec_state.get("status") == "no_candidates":
                st.session_state["last_preferences_submit_message"] = "Everyone is ready but no venues were found in the area - try widening distance preferences."
                st.session_state["last_preferences_submit_level"] = "warning"
            else:
                st.session_state["last_preferences_submit_message"] = "Recommendation could not be generated yet. Everyone may not be ready."
                st.session_state["last_preferences_submit_level"] = "warning"
        st.session_state.selected_action = st.session_state.selected_action or "check_result"
        st.session_state.current_page = "check_result"
        st.rerun()

    if st.session_state.get("last_preferences_submit_message"):
        if st.session_state.get("last_preferences_submit_level") == "warning":
            st.warning(st.session_state["last_preferences_submit_message"])
        else:
            st.success(st.session_state["last_preferences_submit_message"])
        st.session_state["last_preferences_submit_message"] = ""
        st.session_state["last_preferences_submit_level"] = ""
    if partner_ready:
        slot_sets = [set(p.get("availability_slots", []) or []) for p in updated_summary.get("all_prefs", []) if p]
        overlap_slots = sorted(set.intersection(*slot_sets)) if slot_sets and all(slot_sets) else []
        if overlap_slots:
            st.info(
                "Shared time overlap: " +
                ", ".join(_format_time_slot_label(slot) for slot in overlap_slots[:8]) +
                (" ..." if len(overlap_slots) > 8 else "")
            )
        else:
            st.warning("No shared time overlap yet. Once everyone selects matching 30-minute blocks, we'll use only the intersection.")

    if updated_summary["both_preferences_ready"]:
        if updated_summary["both_locations_ready"] and updated_summary["weighted_center"]:
            center = updated_summary["weighted_center"]
            st.success("All participants have submitted preferences. The shared weighted search center is ready.")
            st.info(
                f"Weighted center preview: {center['lat']:.5f}, {center['lon']:.5f} | "
                + "weights: " + ", ".join(f"P{i+1} {w:.2f}" for i, w in enumerate(updated_summary.get("weights") or []))
            )
        else:
            st.success("All participants have submitted preferences. Once all locations are confirmed, we can compute the weighted center and candidate venues.")


# ============================================================================
# Page: Already Know Others Position
# ============================================================================
def render_know_position_page():
    """Render the Already Know Others Position page."""
    st.header("👥 Already Know Others Position")
    st.write("If you already know both starting points, choose a rough map point for each person or type both addresses directly.")

    room_id = st.session_state.room_id
    if "know_position_mode" not in st.session_state:
        st.session_state.know_position_mode = "Map picker"
    default_known_state = {
        "known_map_center_a": {"lat": 39.0997, "lon": -94.5786},
        "known_map_center_b": {"lat": 39.0950, "lon": -94.5750},
        "known_map_pick_a": None,
        "known_map_pick_b": None,
    }
    for key, default in default_known_state.items():
        if key not in st.session_state:
            st.session_state[key] = default

    mode = st.radio(
        "How do you want to provide both locations?",
        ["Map picker", "Enter addresses"],
        horizontal=True,
        key="know_position_mode",
    )

    def _address_entry(prefix: str, label: str) -> Optional[Location]:
        st.subheader(f"{label} Address")
        query = st.text_input(
            f"{label} address",
            key=f"known_addr_query_{prefix}",
            placeholder="e.g., 111 Main St, Kansas City, MO",
        )
        suggestions = _address_suggestions(query)
        suggestion_labels = [item["label"] for item in suggestions]
        selected = st.selectbox(
            f"{label} suggestions",
            options=[""] + suggestion_labels,
            format_func=lambda x: x if x else "Select a matched address",
            key=f"known_addr_select_{prefix}",
        )
        selected_item = next((item for item in suggestions if item["label"] == selected), None)
        if selected_item:
            st.caption(f"Matched coordinates: {selected_item['lat']:.6f}, {selected_item['lon']:.6f}")
            return Location(float(selected_item["lat"]), float(selected_item["lon"]))
        if query.strip() and not suggestions:
            st.caption("No instant matches yet. Keep typing a more specific street or city.")
        return _geocode_address(query) if query.strip() else None

    if mode == "Map picker":
        st.caption("Recommended when you only know rough starting areas. Click once on each map to drop a point for Person A and Person B.")
        st.markdown(
            """
            <style>
            .leaflet-container {
                cursor: crosshair !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        st.subheader("Person A Map")
        center_a = st.session_state["known_map_center_a"]
        picked_a = st.session_state["known_map_pick_a"]
        map_a = folium.Map(location=[center_a["lat"], center_a["lon"]], zoom_start=12)
        if picked_a:
            folium.CircleMarker(location=[picked_a["lat"], picked_a["lon"]], radius=8, color="#ff5d8f", fill=True, fill_color="#ff5d8f", fill_opacity=0.9, tooltip="Person A pending point").add_to(map_a)
        result_a = st_folium(map_a, width=None, height=360, key="known_map_a", use_container_width=True)
        click_a = result_a.get("last_clicked") if isinstance(result_a, dict) else None
        if click_a:
            st.session_state["known_map_pick_a"] = {"lat": float(click_a["lat"]), "lon": float(click_a["lng"])}
            st.session_state["known_map_center_a"] = st.session_state["known_map_pick_a"]
        latest_a = st.session_state["known_map_pick_a"]
        if latest_a:
            st.info(f"Person A: {latest_a['lat']:.6f}, {latest_a['lon']:.6f}")
            label_a = _reverse_geocode(latest_a["lat"], latest_a["lon"])
            if label_a:
                st.caption(label_a)

        st.subheader("Person B Map")
        center_b = st.session_state["known_map_center_b"]
        picked_b = st.session_state["known_map_pick_b"]
        map_b = folium.Map(location=[center_b["lat"], center_b["lon"]], zoom_start=12)
        if picked_b:
            folium.CircleMarker(location=[picked_b["lat"], picked_b["lon"]], radius=8, color="#377dff", fill=True, fill_color="#377dff", fill_opacity=0.9, tooltip="Person B pending point").add_to(map_b)
        result_b = st_folium(map_b, width=None, height=360, key="known_map_b", use_container_width=True)
        click_b = result_b.get("last_clicked") if isinstance(result_b, dict) else None
        if click_b:
            st.session_state["known_map_pick_b"] = {"lat": float(click_b["lat"]), "lon": float(click_b["lng"])}
            st.session_state["known_map_center_b"] = st.session_state["known_map_pick_b"]
        latest_b = st.session_state["known_map_pick_b"]
        if latest_b:
            st.info(f"Person B: {latest_b['lat']:.6f}, {latest_b['lon']:.6f}")
            label_b = _reverse_geocode(latest_b["lat"], latest_b["lon"])
            if label_b:
                st.caption(label_b)

        a_location = Location(**st.session_state["known_map_pick_a"]) if st.session_state["known_map_pick_a"] else None
        b_location = Location(**st.session_state["known_map_pick_b"]) if st.session_state["known_map_pick_b"] else None
    else:
        st.caption("Recommended when you know both exact addresses. We will auto-match likely locations for each person.")
        col1, col2 = st.columns(2)
        with col1:
            a_location = _address_entry("a", "Person A")
        with col2:
            b_location = _address_entry("b", "Person B")

    st.subheader("Map Preview")
    preview_map = folium.Map(location=[39.0997, -94.5786], zoom_start=12)
    if a_location:
        folium.Marker([a_location.lat, a_location.lon], popup="Person A", icon=folium.Icon(color="pink", icon="user")).add_to(preview_map)
    if b_location:
        folium.Marker([b_location.lat, b_location.lon], popup="Person B", icon=folium.Icon(color="blue", icon="user")).add_to(preview_map)
    st_folium(preview_map, width=None, height=420, key="known_position_preview", use_container_width=True)

    if not a_location or not b_location:
        st.warning("Please provide both Person A and Person B locations before continuing.")
    elif st.button("Confirm Both Locations", type="primary", use_container_width=True, key="confirm_known_locations"):
        st.session_state["location_A"] = a_location
        st.session_state["location_B"] = b_location
        st.session_state.direct_flow_active = True
        st.session_state.room_id = ""
        st.session_state.user_role = ""
        _reset_direct_flow_results()
        if room_id:
            mode_key = mode.lower().replace(" ", "-")
            _persist_user_location(room_id, "A", a_location, f"known-{mode_key}")
            _persist_user_location(room_id, "B", b_location, f"known-{mode_key}")
        st.success("Both locations confirmed and saved.")
        st.session_state.current_page = "dual_preferences"
        st.rerun()

# ============================================================================
# Side Button Panel
# ============================================================================
def render_side_buttons():
    """Render the side button panel."""
    pages = {
        "home": ("🏠 Home", render_home_page),
        "action_select": ("🧭 Select Action", render_action_select_page),
        "generate_link": ("🔗 Generate Link", render_generate_link_page),
        "join_link": ("✅ Confirm Details", render_join_link_page),
        "user_info_step1": ("Your Location", render_user_info_step1_page),
        "user_info_step2": ("✍️ Your Preferences", render_user_info_step2_page),
        "dual_preferences": ("Dual Preferences", render_dual_preferences_page),
        "check_result": ("📊 Check Result", render_check_result_page),
        "venue_vote": ("🗳️ Vote", render_vote_page),
        "final_result": ("🏆 Final Result", render_final_result_page),
        "know_position": ("🗺️ Know Position", render_know_position_page),
    }
    
    buttons_html = '<div class="side-button-panel">'
    # Only show main pages in side buttons, not sub-pages
    main_pages = ["home", "action_select", "generate_link", "join_link", "venue_vote", "final_result", "know_position"]
    for page_key, (label, _) in pages.items():
        if page_key not in main_pages:
            continue
        active_class = "active" if st.session_state.current_page == page_key else ""
        buttons_html += f'''
        <button class="side-button {active_class}" onclick="window.location.hash='{page_key}' || location.reload()">
            {label}
        </button>
        '''
    buttons_html += '</div>'
    st.markdown(buttons_html, unsafe_allow_html=True)


# ============================================================================
# Navigation
# ============================================================================
def render_navigation():
    """Render next/previous navigation buttons."""
    # Define navigation flow
    primary_flow = ["home", "action_select"]
    
    # Action-specific flows
    action_flows = {
        "generate_link": ["generate_link", "user_info_step1", "user_info_step2", "check_result", "venue_vote", "final_result"],
        "join_link": ["join_link", "user_info_step1", "user_info_step2", "check_result", "venue_vote", "final_result"],
        "check_result": ["check_result", "venue_vote", "final_result"],
        "know_position": ["know_position", "dual_preferences", "venue_vote", "final_result"]
    }
    
    current_page = st.session_state.current_page
    current_action = st.session_state.selected_action

    # Infer action when session lost it, so Previous works on every step.
    if current_action not in action_flows:
        if current_page in action_flows:
            current_action = current_page
            st.session_state.selected_action = current_action
        elif current_page in ("dual_preferences", "venue_vote", "final_result") and st.session_state.get("direct_flow_active"):
            inferred = "know_position"
            current_action = inferred
            st.session_state.selected_action = inferred
        elif current_page in ("user_info_step1", "user_info_step2", "check_result", "dual_preferences", "venue_vote", "final_result"):
            inferred = "generate_link" if st.session_state.get("link_generated") else "join_link"
            current_action = inferred
            st.session_state.selected_action = inferred
    
    # Determine which flow we're in
    flow = None
    page_index = None
    
    if current_page in primary_flow:
        flow = primary_flow
        page_index = primary_flow.index(current_page)
    elif current_action and current_action in action_flows:
        flow = action_flows[current_action]
        if current_page in flow:
            page_index = flow.index(current_page)
    
    # Check if user came from URL (room parameter)
    query_params = st.query_params
    came_from_url = "room" in query_params and query_params["room"]
    
    col_prev, col_spacer, col_next = st.columns([1, 2, 1])
    
    with col_prev:
        # Previous button logic
        if flow and page_index is not None and page_index > 0:
            if st.button("Previous", use_container_width=True):
                st.session_state.current_page = flow[page_index - 1]
                st.rerun()
        elif flow and page_index == 0 and current_page in action_flows:
            # On action entry pages (e.g., Generate Link), allow going back to the initial home page.
            if st.button("Previous", use_container_width=True):
                st.session_state.selected_action = None
                st.session_state.current_page = "home"
                st.rerun()
        elif came_from_url and current_page == "join_link":
            # Users who came from URL can go back to home
            if st.button("Previous", use_container_width=True):
                # Clear the selected action when going back
                st.session_state.selected_action = None
                st.session_state.current_page = "home"
                st.rerun()
    
    with col_next:
        # Next button logic
        if flow and page_index is not None and page_index < len(flow) - 1:
            if current_page in ("join_link", "user_info_step2", "dual_preferences", "venue_vote"):
                pass
            else:
                if st.button("Next", use_container_width=True, type="primary"):
                    if current_page == "user_info_step2":
                        role_label = st.session_state.get("user_role", "")
                        missing_fields = _missing_preference_fields(
                            st.session_state.get(f"venue_type_{role_label}", []),
                            st.session_state.get(f"availability_slots_{role_label}", []),
                        )
                        if missing_fields:
                            st.session_state["last_preferences_submit_message"] = (
                                "Please choose " + " and ".join(missing_fields) + " before continuing."
                            )
                            st.session_state["last_preferences_submit_level"] = "warning"
                            st.rerun()

                    st.session_state.current_page = flow[page_index + 1]
                    st.rerun()
        elif current_page == "home":
            if st.button("Next", use_container_width=True, type="primary"):
                st.session_state.current_page = "action_select"
                st.rerun()
        elif current_page == "action_select":
            # Don't show next button on action_select - let users choose via buttons
            pass


# ============================================================================
# Main App
# ============================================================================
def main():
    inject_page_styles()
    
    # Get page from URL or session state
    from urllib.parse import urlparse, parse_qs
    query_params = st.query_params
    
    # Check if user is joining via invitation link
    if "room" in query_params and query_params["room"]:
        # Only auto-route on entry pages. Otherwise it would override in-flow navigation.
        if st.session_state.current_page in ("home", "action_select", "join_link"):
            st.session_state.selected_action = "join_link"
            st.session_state.current_page = "join_link"
    elif "page" in query_params:
        st.session_state.current_page = query_params["page"]
    
    # Pages mapping
    pages = {
        "home": render_home_page,
        "action_select": render_action_select_page,
        "generate_link": render_generate_link_page,
        "join_link": render_join_link_page,
        "user_info_step1": render_user_info_step1_page,
        "user_info_step2": render_user_info_step2_page,
        "dual_preferences": render_dual_preferences_page,
        "check_result": render_check_result_page,
        "venue_vote": render_vote_page,
        "final_result": render_final_result_page,
        "know_position": render_know_position_page,
    }
    
    # Render current page
    current_page = st.session_state.current_page
    if current_page in pages:
        pages[current_page]()
    else:
        st.session_state.current_page = "home"
        st.rerun()
    
    # Render navigation
    render_navigation()
    
    # Note: Side buttons are a bit tricky in Streamlit; this is a basic approach
    # For full functionality, consider using custom HTML/JS or iframe


if __name__ == "__main__":
    main()
