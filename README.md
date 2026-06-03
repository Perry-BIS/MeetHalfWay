# MeetHalfway 

A fair, real-time, and explainable meeting-place recommendation engine for **2–5 people**.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-App-FF4B4B?logo=streamlit&logoColor=white)
![Status](https://img.shields.io/badge/Status-Competition%20Ready-0A7B83)

<img width="1199" height="1199" alt="image" src="https://github.com/user-attachments/assets/727e6737-c900-47cb-9d3d-e92fe9bb6d35" />


## Why This Project Stands Out

MeetHalfway does more than compute a geometric midpoint. For a group of **two to five** participants, it enforces fairness through **travel-time isochrone intersection** — the shared region everyone can reasonably reach — then combines live status signals, crowding/queue risk, and reputation factors into a practical, explainable recommendation.

Beyond the usual "where to meet" and "when to meet," it also weighs:
- Travel radius tolerance,
- Availability overlap,
- Venue popularity and density,
- Mutual voting preferences.

## Key Highlights

- **Fairness by travel time:** optimize the balance for everyone, not just map distance.
- **2–5 participants:** a room-based flow computes the shared reachable area across the whole group, not just a single pair.
- **Real-world decision quality:** detect closures, high queue risk, and busy periods from live web signals.
- **Always-on demo:** interactive map, ranked recommendations, surprise mode, and a clearly-labeled offline fallback when live services are unavailable.
- **Privacy-first workflow:** locations are processed in memory only and can be submitted separately by each participant.

## Core Capabilities

| Module | Design | Value |
|---|---|---|
| Geo fairness constraint | Per-user travel-time isochrone intersection (radius-tolerance circles as fallback) | Keeps candidates reachable for everyone (2–5 people) |
| Intelligent scoring | Fairness + rating + preference + risk/crowd penalties | Robust multi-criteria ranking |
| Live signals | Web retrieval + LLM/keyword semantic extraction | Captures current open / crowd / queue conditions |
| Engineering resilience | Async concurrency + retries + multi-source fallbacks + labeled offline sample | Stays usable under API/network instability |
| Explainable visualization | Streamlit map showing each reachable region, the shared overlap, and ranked venues | Easy for judges to grasp at a glance |

## Product Flow

1. Create a room for **2–5 people**; each participant submits a location (address / map click / privacy-separated upload) and preferences.
2. Build each person's travel-time isochrone and compute the shared reachable area (the intersection everyone can reach).
3. Retrieve venue candidates inside that area and enrich them with live signals.
4. Output explainable scores, ranked picks, natural-language reasons, and a map showing every reachable region, the shared overlap, and the ranked venues.

## Tech Stack

- Frontend: Streamlit
- Geo layer: OpenRouteService / Mapbox + Shapely
- Place retrieval: Mapbox / OSM Overpass (fallback chain)
- Live signal retrieval: Tavily / DuckDuckGo (fallback chain)
- Semantic reasoning: OpenAI-compatible model (keyword fallback available)
- Concurrency: asyncio + httpx

## Quick Start

### 1) Install dependencies

```powershell
pip install -r requirements.txt
```

### 2) Configure environment variables

Copy `.env.example` to `.env`, then fill your own keys.

Recommended:
- `OPENROUTESERVICE_API_KEY`

Optional enhancements:
- `MAPBOX_ACCESS_TOKEN`
- `TAVILY_API_KEY`
- `YELP_API_KEY`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE`
- `MODEL_NAME`

### 3) Run the app

```powershell
streamlit run app_streamlit_new.py
```

## Repository Structure (Competition Core)

- `app_streamlit_new.py`: main visual app entry
- `app_streamlit.py`: compatibility wrapper that forwards to the new app
- `meethalfway.py`: core algorithm and scoring
- `requirements.txt`: dependency list
- `.env.example`: environment template

## Privacy & Security Notes

- No real API keys are committed (`.env` / secrets files are ignored).
- Local runtime traces and personal artifacts are excluded from upload.
- User coordinates are used for in-session computation only and are not persisted.

## Deployment

Recommended target: Streamlit Community Cloud

1. Connect this repository.
2. Set entry file to `app_streamlit_new.py`.
3. Configure secrets in the platform settings.
