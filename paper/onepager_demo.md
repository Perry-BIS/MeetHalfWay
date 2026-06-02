# MeetHalfway AI — ACM SIGSPATIAL 2026 Demonstration Track

## The Project
MeetHalfway AI is an interactive web system that helps two people decide **where to meet**. Instead of returning a naive geometric midpoint, it computes a **shared travel-time region** from each person's isochrone (via OpenRouteService / Mapbox, intersected with Shapely), removes natural barriers such as rivers and highways from the overlap, and then recommends venues that are genuinely reachable and fair for both sides.

Within that shared region, the system retrieves candidate venues (Mapbox / OpenStreetMap), enriches them with live signals — open status, crowding, queue risk, and reputation — and ranks them with an explainable, fairness-aware score that penalizes asymmetric travel time. Each recommendation comes with a map, a per-venue score breakdown, and a plain-language reason. The system is built with Streamlit and Folium, supports privacy-separated check-in (each participant submits their location independently), and degrades gracefully to free fallbacks when paid APIs are unavailable.

## Why It Fits the Demonstration Track
- **It is a running, interactive system.** Attendees can drag two starting points, switch travel modes, and watch the fair region and ranked venues update live — exactly what a demo session is meant to showcase.
- **Spatial computation is the visible core.** Isochrone retrieval, polygon intersection, natural-barrier subtraction, and travel-time fairness are surfaced directly on the map, not hidden behind an API.
- **It demonstrates responsible GeoAI in practice.** Fairness scoring, privacy-separated interaction, and explainable recommendations let attendees inspect *why* a venue was chosen, making the spatial reasoning transparent.
- **It is robust under conference conditions.** A built-in fallback chain (OpenStreetMap, open web snippets, keyword extraction, distance buffers) keeps the demo working even with unstable networks or missing API keys.

## Why Submit
Meeting-place selection is a familiar spatial decision that most tools still solve poorly. MeetHalfway AI shows how isochrone-intersection fairness, context-aware venue retrieval, live signals, and explanation can be combined into a single, hands-on workflow. SIGSPATIAL is the natural home for this work: its audience cares about reachability, fairness, and location-based services, and the demonstration format lets that audience interact with the spatial reasoning directly rather than read about it.
