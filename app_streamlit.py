import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import folium
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium
from streamlit_js_eval import get_geolocation
from streamlit_js_eval import eval_js
from streamlit_js_eval import eval_js

from meethalfway import (
    Location,
    MEET_SCENARIOS,
    VENUE_TYPES,
    MeetHalfwayRecommender,
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


init_session_state()


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
                # Display room ID
                st.markdown(f"**Room ID:** `{st.session_state.generated_room_id}`")
            
                # Copy buttons with true clipboard functionality
                col_copy_1, col_copy_2 = st.columns(2)
                with col_copy_1:
                    if st.button("📋 Copy Link", use_container_width=True, key="copy_link_btn"):
                        try:
                            eval_js(f"navigator.clipboard.writeText('{st.session_state.generated_link}')")
                            st.success("✅ Link copied to clipboard!")
                        except Exception as e:
                            st.error(f"Failed to copy: {str(e)}")
            
                with col_copy_2:
                    if st.button("📋 Copy Room ID", use_container_width=True, key="copy_room_btn"):
                        try:
                            eval_js(f"navigator.clipboard.writeText('{st.session_state.generated_room_id}')")
                            st.success("✅ Room ID copied to clipboard!")
                        except Exception as e:
                            st.error(f"Failed to copy: {str(e)}")
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
    st.markdown("""
        <style>
        .action-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 20px;
            margin: 30px 0;
        }
        
        .action-card {
            background: rgba(255, 255, 255, 0.9);
            border: 2px solid rgba(81, 113, 151, 0.2);
            border-radius: 20px;
            padding: 30px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 8px 24px rgba(30, 60, 98, 0.08);
            min-height: 200px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
        }
        
        .action-card:hover {
            border-color: #377dff;
            box-shadow: 0 12px 32px rgba(55, 125, 255, 0.15);
            transform: translateY(-4px);
        }
        
        .action-icon {
            font-size: 3rem;
            margin-bottom: 15px;
        }
        
        .action-title {
            font-size: 1.3rem;
            font-weight: 800;
            color: #18344f;
            margin-bottom: 10px;
        }
        
        .action-description {
            font-size: 0.95rem;
            color: #5f7287;
            line-height: 1.5;
        }
        </style>
    """, unsafe_allow_html=True)
    
    st.header("What would you like to do?")
    st.write("Choose an option to get started:")
    
    # Create 2x2 grid using columns
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button(
            "🔗 Generate Link\n\nCreate a new meeting and\nshare the link with someone",
            use_container_width=True,
            key="action_generate_link"
        ):
            st.session_state.selected_action = "generate_link"
            st.session_state.current_page = "generate_link"
            st.rerun()
    
    with col2:
        if st.button(
            "📨 Join Link\n\nJoin an existing meeting\nusing a link or room ID",
            use_container_width=True,
            key="action_join_link"
        ):
            st.session_state.selected_action = "join_link"
            st.session_state.current_page = "join_link"
            st.rerun()
    
    col3, col4 = st.columns(2)
    
    with col3:
        if st.button(
            "📊 Check Result\n\nView the recommendation\nresults for your meeting",
            use_container_width=True,
            key="action_check_result"
        ):
            st.session_state.selected_action = "check_result"
            st.session_state.current_page = "check_result"
            st.rerun()
    
    with col4:
        if st.button(
            "🗺️ I Already Know\nOthers Position\n\nInput locations directly",
            use_container_width=True,
            key="action_know_position"
        ):
            st.session_state.selected_action = "know_position"
            st.session_state.current_page = "know_position"
            st.rerun()


# ============================================================================
# Page: Generate Link
# ============================================================================
def render_generate_link_page():
    """Render the Generate Link page."""
    st.header("🔗 Generate Link")
    st.write("Create a unique link to share with your meeting partner.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Step 1: Enter Your Information")
        
        st.caption("Create your own Room ID or leave blank and we will generate automatically")
        room_id = st.text_input("Room ID", value=st.session_state.room_id, placeholder="Enter custom ID or leave empty")
        
        your_name = st.text_input("Your Name")
        
        # Email input with optional label
        st.markdown('<label>Your Email <span style="color: #999; font-size: 0.85em;">(optional)</span></label>', unsafe_allow_html=True)
        your_email = st.text_input("Your Email", label_visibility="collapsed", placeholder="you@example.com")
        st.caption("💡 You can provide your email to receive the latest notifications.")
        
        st.session_state.room_id = room_id
    
    with col2:
        st.subheader("Generated Link")
        
        # Generate Link button
        if st.button("🔗 Generate Link", type="primary", use_container_width=True, key="gen_link_btn"):
            import uuid
            if not room_id or room_id == st.session_state.room_id:
                final_room_id = str(uuid.uuid4())[:8]
            else:
                final_room_id = room_id
            
            final_link = f"http://localhost:8501/?room={final_room_id}"
            st.session_state.link_generated = True
            st.session_state.generated_room_id = final_room_id
            st.session_state.generated_link = final_link
            st.session_state.creator_name = your_name
            st.session_state.user_name = your_name
            st.session_state.user_role = "Person A"
        
        # Display generated link if available
        if st.session_state.link_generated:
            st.success("✅ Link Generated!")
            
            # Display link
            st.markdown("**Your Invitation Link:**")
            st.code(st.session_state.generated_link, language="text")
            
            # Display room ID
            st.markdown("**Room ID:**")
            st.code(st.session_state.generated_room_id, language="text")
            
            # Copy button
            col_copy_1, col_copy_2 = st.columns(2)
            with col_copy_1:
                if st.button("📋 Copy Link", use_container_width=True):
                    st.write(st.session_state.generated_link)
                    st.success("Link copied to clipboard!")
            
            with col_copy_2:
                if st.button("📋 Copy Room ID", use_container_width=True):
                    st.write(st.session_state.generated_room_id)
                    st.success("Room ID copied to clipboard!")
            
                # Copy button
                col_copy_1, col_copy_2 = st.columns(2)
                with col_copy_1:
                    if st.button("📋 Copy Link", use_container_width=True, key="copy_link_btn"):
                        try:
                            eval_js(f"navigator.clipboard.writeText('{st.session_state.generated_link}')")
                            st.success("✅ Link copied to clipboard!")
                        except Exception as e:
                            st.error(f"Failed to copy: {str(e)}")
            
                with col_copy_2:
                    if st.button("📋 Copy Room ID", use_container_width=True, key="copy_room_btn"):
                        try:
                            eval_js(f"navigator.clipboard.writeText('{st.session_state.generated_room_id}')")
                            st.success("✅ Room ID copied to clipboard!")
                        except Exception as e:
                            st.error(f"Failed to copy: {str(e)}")
            st.info(f"Share this link or Room ID **{st.session_state.generated_room_id}** with your partner to start!")

def render_generate_link_page():
    """Render the Generate Link page."""
    st.header("🔗 Generate Link")
    st.write("Create a unique link to share with your meeting partner.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Step 1: Enter Your Information")
        
        st.caption("Create your own Room ID or leave blank and we will generate automatically")
        room_id = st.text_input("Room ID", value=st.session_state.room_id, placeholder="Enter custom ID or leave empty")
        
        your_name = st.text_input("Your Name")
        
        # Email input with optional label
        st.markdown('<label>Your Email <span style="color: #999; font-size: 0.85em;">(optional)</span></label>', unsafe_allow_html=True)
        your_email = st.text_input("Your Email", label_visibility="collapsed", placeholder="you@example.com")
        st.caption("💡 You can provide your email to receive the latest notifications.")
        
        st.session_state.room_id = room_id
    
    with col2:
        st.subheader("Generated Link")
        
        # Generate Link button
        if st.button("🔗 Generate Link", type="primary", use_container_width=True, key="gen_link_btn"):
            import uuid
            if not room_id or room_id == st.session_state.room_id:
                final_room_id = str(uuid.uuid4())[:8]
            else:
                final_room_id = room_id
            
            final_link = f"http://localhost:8501/?room={final_room_id}"
            st.session_state.link_generated = True
            st.session_state.generated_room_id = final_room_id
            st.session_state.generated_link = final_link
            st.session_state.creator_name = your_name
            st.session_state.user_name = your_name
            st.session_state.user_role = "Person A"
        
        # Display generated link if available
        if st.session_state.link_generated:
            st.success("✅ Link Generated!")
            
            # Display link
            st.markdown("**Your Invitation Link:**")
            st.code(st.session_state.generated_link, language="text")
            
            # Display room ID
            st.markdown("**Room ID:**")
            st.code(st.session_state.generated_room_id, language="text")
            
            
            st.info(f"Share this link or Room ID **{st.session_state.generated_room_id}** with your partner to start!")

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
            st.success("**Your Role: Person A** (as the creator)")
            
            st.markdown("---")
            st.caption("You are the meeting creator. Your partner will join as Person B and fill in their own details.")
        
        # Save to session state for next steps
        st.session_state.room_id = st.session_state.generated_room_id
        st.session_state.user_role = "Person A"
        st.session_state.user_name = your_name
    
    elif room_from_url:
        # Joiner from URL link - auto-fill room_id
        st.write("Welcome! You've been invited to a meeting. Please confirm your details:")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Meeting Info")
            st.info(f"**Room ID:** {room_from_url}")
            st.caption("You're joining via an invitation link")
            
            # Default to Person B for joiners
            default_role = "Person B"
            st.subheader("Your Details")
            
            if "joiner_name_from_url" not in st.session_state:
                st.session_state.joiner_name_from_url = ""
            
            your_name = st.text_input("Your Name", value=st.session_state.joiner_name_from_url, key="joiner_name_from_url_input", placeholder="Enter your name")
            st.session_state.joiner_name_from_url = your_name
        
        with col2:
            st.subheader("Your Role")
            st.success(f"**Your Role: {default_role}**")
            st.caption("As the invited guest, you are joining as Person B")
        
        # Save to session state for next steps
        st.session_state.room_id = room_from_url
        st.session_state.user_role = default_role
        st.session_state.user_name = your_name
        
        if not your_name:
            st.warning("Please enter your name to continue")
    
    else:
        # Joiner flow - manual input
        st.write("Enter the room ID or link provided by your meeting partner:")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Join Existing Meeting")
            
            input_method = st.radio("How to join?", ["Room ID", "Full Link"], horizontal=True)
            
            if input_method == "Room ID":
                room_id = st.text_input("Enter Room ID", placeholder="e.g., abc123 or john-sarah-2024")
            else:
                link = st.text_input("Enter Full Link", placeholder="http://localhost:8501/?room=...")
                # Extract room ID from link
                if "room=" in link:
                    room_id = link.split("room=")[-1]
                else:
                    room_id = ""
            
            your_role = st.selectbox("Your Role", ["Person A", "Person B"])
            
            if "joiner_name" not in st.session_state:
                st.session_state.joiner_name = ""
            
            your_name = st.text_input("Your Name", value=st.session_state.joiner_name, key="joiner_name_input")
            st.session_state.joiner_name = your_name
        
        with col2:
            st.subheader("Summary")
            if room_id:
                st.success(f"✅ Room ID: **{room_id}**")
                st.info(f"👤 **Role:** {your_role}\n\n**Name:** {your_name if your_name else '(Not entered)'}")
            else:
                st.warning("Please enter a valid Room ID or Link")
        
        # Save to session state for next steps
        st.session_state.room_id = room_id
        st.session_state.user_role = your_role
        st.session_state.user_name = your_name


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
    
    with col2:
        st.subheader("Results")
        if room_id:
            st.info(f"Results for room: **{room_id}**")
            
            # Placeholder for results
            st.metric("Recommended Venues", 3)
            st.metric("Voting Complete", "Yes")
            st.metric("Available Times Found", 2)
        else:
            st.warning("Enter a room ID to see results")
    
    if st.button("🔄 Refresh Results", use_container_width=True):
        st.rerun()


# ============================================================================
# Page: User Information - Step 1 (Private Location Input)
# ============================================================================
def render_user_info_step1_page():
    """Render the user information step 1 - Private location input."""
    st.header("📍 Your Location")
    st.write(f"Person {st.session_state.user_role} - Please share your location")
    
    user_role = st.session_state.user_role.replace("Person ", "").upper()
    room_id = st.session_state.room_id
    
    # Initialize location state
    if f"location_{user_role}" not in st.session_state:
        st.session_state[f"location_{user_role}"] = None
    if f"gps_request_{user_role}" not in st.session_state:
        st.session_state[f"gps_request_{user_role}"] = False
    if f"location_candidate_{user_role}" not in st.session_state:
        st.session_state[f"location_candidate_{user_role}"] = None
    
    tab1, tab2 = st.tabs(["📡 GPS", "🗺️ Address"])
    
    with tab1:
        st.subheader("Option 1: GPS Location")
        st.caption("Use your device's GPS to automatically detect your location")
        
        col_request, col_refresh = st.columns(2)
        with col_request:
            if st.button(
                "📍 Request GPS & Detect Location",
                type="primary",
                use_container_width=True,
                key=f"gps_request_{user_role}"
            ):
                st.session_state[f"gps_request_{user_role}"] = True
        
        with col_refresh:
            if st.button(
                "🔄 Refresh Location",
                use_container_width=True,
                key=f"gps_refresh_{user_role}"
            ):
                st.session_state[f"gps_request_{user_role}"] = True
                st.session_state[f"location_candidate_{user_role}"] = None
        
        # Try to get GPS location
        if st.session_state[f"gps_request_{user_role}"]:
            geo_raw = get_geolocation(component_key=f"geo_{room_id}_{user_role}")
            if geo_raw:
                try:
                    lat = float(geo_raw.get("coords", {}).get("latitude", 0))
                    lon = float(geo_raw.get("coords", {}).get("longitude", 0))
                    if lat != 0 and lon != 0:
                        st.session_state[f"location_candidate_{user_role}"] = {"lat": lat, "lon": lon}
                except:
                    pass
        
        candidate = st.session_state.get(f"location_candidate_{user_role}")
        
        if not candidate:
            st.info("Click the button above. Your browser will request permission to access your location.")
        else:
            st.success(f"✅ Location detected: {candidate['lat']:.5f}, {candidate['lon']:.5f}")
            
            if st.button(
                "✅ Confirm This Location",
                type="primary",
                use_container_width=True,
                key=f"confirm_gps_{user_role}"
            ):
                st.session_state[f"location_{user_role}"] = Location(candidate["lat"], candidate["lon"])
                st.success("Location confirmed!")
    
    with tab2:
        st.subheader("Option 2: Address")
        st.caption("Enter your address to find your location")
        
        address = st.text_input(
            "Your Address",
            placeholder="e.g., 111 Main St, Kansas City, MO",
            key=f"address_{user_role}"
        )
        
        if st.button(
            "📍 Confirm Address",
            type="primary",
            use_container_width=True,
            key=f"confirm_address_{user_role}"
        ):
            if address.strip():
                st.info(f"Address: {address}")
                st.session_state[f"location_{user_role}"] = Location(39.0997, -94.5786)  # Placeholder
                st.success("Location confirmed!")
            else:
                st.error("Please enter an address")
    
    # Summary
    st.markdown("---")
    location = st.session_state.get(f"location_{user_role}")
    if location:
        st.success("✅ **Your location is confirmed**")
    else:
        st.warning("⚠️ Please confirm your location to continue")


# ============================================================================
# Page: User Information - Step 2 (Preferences)
# ============================================================================
def render_user_info_step2_page():
    """Render the user information step 2 - Preferences & search strategy."""
    st.header("✍️ Your Preferences")
    st.write(f"Person {st.session_state.user_role} - Tell us your preferences")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Meeting Type")
        meeting_type = st.selectbox(
            "What's the occasion?",
            ["Dinner Date", "Lunch Date", "Coffee", "Drinks", "Activity", "Other"],
            key=f"meeting_type_{st.session_state.user_role}"
        )
        
        st.subheader("Cuisine Preference")
        cuisine = st.text_input(
            "Preferred cuisine (optional)",
            placeholder="e.g., Italian, Japanese, Mexican",
            key=f"cuisine_{st.session_state.user_role}"
        )
        
        st.subheader("Budget Range")
        budget = st.slider(
            "Budget per person ($)",
            min_value=0,
            max_value=200,
            value=50,
            step=10,
            key=f"budget_{st.session_state.user_role}"
        )
    
    with col2:
        st.subheader("Distance Tolerance")
        distance = st.slider(
            "Max travel distance (miles)",
            min_value=1,
            max_value=50,
            value=15,
            key=f"distance_{st.session_state.user_role}"
        )
        
        st.subheader("Venue Type")
        venue_type = st.multiselect(
            "Types of places you prefer",
            ["Restaurant", "Cafe", "Bar", "Park", "Museum", "Theater"],
            default=["Restaurant"],
            key=f"venue_type_{st.session_state.user_role}"
        )
        
        st.subheader("Openness to Surprises")
        surprise = st.checkbox(
            "Include surprise recommendations",
            value=True,
            key=f"surprise_{st.session_state.user_role}"
        )
    
    st.success("✅ Preferences saved")


# ============================================================================
# Page: Already Know Others Position
# ============================================================================
def render_know_position_page():
    """Render the Already Know Others Position page."""
    st.header("🗺️ Already Know Others Position")
    st.write("Input locations directly if you already know your partner's location.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Person A Location")
        a_address = st.text_input("Address", key="a_addr", placeholder="e.g., 111 Main St, Kansas City")
        a_lat = st.number_input("Latitude", key="a_lat", value=39.0997, format="%.5f")
        a_lon = st.number_input("Longitude", key="a_lon", value=-94.5786, format="%.5f")
    
    with col2:
        st.subheader("Person B Location")
        b_address = st.text_input("Address", key="b_addr", placeholder="e.g., 450 Grand Blvd, Kansas City")
        b_lat = st.number_input("Latitude", key="b_lat", value=39.0950, format="%.5f")
        b_lon = st.number_input("Longitude", key="b_lon", value=-94.5750, format="%.5f")
    
    st.subheader("Map Preview")
    
    # Create a simple map
    map_data = folium.Map(location=[39.0997, -94.5786], zoom_start=13)
    folium.Marker([a_lat, a_lon], popup="Person A", icon=folium.Icon(color="pink", icon="user")).add_to(map_data)
    folium.Marker([b_lat, b_lon], popup="Person B", icon=folium.Icon(color="blue", icon="user")).add_to(map_data)
    
    st_folium(map_data, width=700, height=400)
    
    if st.button("✅ Confirm Locations", type="primary", use_container_width=True):
        st.success("Locations confirmed! Ready for recommendation.")


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
        "user_info_step1": ("📍 Your Location", render_user_info_step1_page),
        "user_info_step2": ("✍️ Your Preferences", render_user_info_step2_page),
        "check_result": ("📊 Check Result", render_check_result_page),
        "know_position": ("🗺️ Know Position", render_know_position_page),
    }
    
    buttons_html = '<div class="side-button-panel">'
    # Only show main pages in side buttons, not sub-pages
    main_pages = ["home", "action_select", "generate_link", "join_link", "check_result", "know_position"]
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
        "generate_link": ["generate_link", "user_info_step1", "user_info_step2"],
        "join_link": ["join_link", "user_info_step1", "user_info_step2"],
        "check_result": ["check_result"],
        "know_position": ["know_position"]
    }
    
    current_page = st.session_state.current_page
    current_action = st.session_state.selected_action
    
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
            if st.button("⬅️ Previous", use_container_width=True):
                st.session_state.current_page = flow[page_index - 1]
                st.rerun()
        elif came_from_url and current_page == "join_link":
            # Users who came from URL can go back to home
            if st.button("⬅️ Previous", use_container_width=True):
                # Clear the selected action when going back
                st.session_state.selected_action = None
                st.session_state.current_page = "home"
                st.rerun()
    
    with col_next:
        # Next button logic
        if flow and page_index is not None and page_index < len(flow) - 1:
            if st.button("Next", use_container_width=True, type="primary"):
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
        # Automatically go to join_link page when room parameter is present
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
        "check_result": render_check_result_page,
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
