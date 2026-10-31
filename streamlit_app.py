import hashlib
import html as _html
import importlib.util
import subprocess
import sys

import pandas as pd
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app import auth, autofill, config, crypto, discover, gmail, pipeline, referral, resume, storage, tracker
from app import search as jobsearch
from app import sources as jobsources
from app.db import SessionLocal, init_db
from app.filters import COUNTRIES, JOB_TYPES, REMOTE_PREFS, SECTORS
from app.models import Application, Contact, Job, Match, SourceRun, User
from app.scrapers import load_companies, load_feeds

st.set_page_config(page_title="Job Hunter", page_icon="🎯", layout="wide", initial_sidebar_state="expanded")
init_db()

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Caveat:wght@600&display=swap');
html, body, [class*="css"], .stMarkdown, button, input, textarea { font-family: 'Inter', system-ui, sans-serif !important; }
[data-testid="stAppViewContainer"], .stApp { background: #0A0F1C; }
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.2rem; max-width: 1320px; }
#MainMenu, footer { visibility: hidden; }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { background: #0D1322; border-right: 1px solid rgba(255,255,255,.06); }
[data-testid="stSidebar"][aria-expanded="true"] { min-width: 288px !important; max-width: 288px !important; }
[data-testid="stSidebarHeader"] { height: 0; padding: 0; min-height: 0; }
[data-testid="stSidebarContent"] { padding: 0 18px 18px; }
[data-testid="stSidebarUserContent"] { padding-top: 1.6rem; }
.logo-row { display: flex; align-items: center; gap: 12px; padding: 4px 6px 22px; }
.logo-row svg { width: 46px; height: 46px; flex: 0 0 auto; }
.logo-row .t1 { font-size: 1.6rem; font-weight: 800; letter-spacing: -.5px; line-height: 1.1; color: #F2F5FC; }
.logo-row .t1 em { font-style: normal; color: #7F7BFF; }
.logo-row .t2 { font-size: .8rem; opacity: .75; margin-top: 3px; }
[data-testid="stSidebar"] [data-testid="stButtonGroup"], [data-testid="stSidebar"] [role="radiogroup"] { flex-direction: column !important; align-items: stretch !important; flex-wrap: nowrap !important; gap: 6px !important; }
[data-testid="stSidebar"] [data-testid="stButtonGroup"] > label { display: none; }
[data-testid="stSidebar"] [role="radiogroup"], [data-testid="stSidebar"] [data-testid="stButtonGroup"] > div { max-width: none !important; width: 100% !important; }
[data-testid="stSidebar"] button[data-variant="pills"], [data-testid="stSidebar"] button[data-testid^="stBaseButton-pills"] { width: 100% !important; justify-content: flex-start !important; position: relative;
    min-height: 52px; padding: 0 18px !important; border: 0 !important; border-radius: 14px !important; background: transparent !important; color: #C3CAE0 !important; box-shadow: none !important; }
[data-testid="stSidebar"] button[data-variant="pills"]:hover, [data-testid="stSidebar"] button[data-testid^="stBaseButton-pills"]:hover { background: rgba(255,255,255,.05) !important; color: #fff !important; }
[data-testid="stSidebar"] button[data-variant="pills"][aria-checked="true"], [data-testid="stSidebar"] button[data-testid="stBaseButton-pillsActive"] {
    background: linear-gradient(135deg, #4F54F0, #5B63FF) !important; color: #fff !important; box-shadow: 0 10px 26px rgba(79,84,240,.35) !important; }
[data-testid="stSidebar"] button[data-variant="pills"] > div, [data-testid="stSidebar"] button[data-testid^="stBaseButton-pills"] > div { justify-content: flex-start !important; width: 100%; }
[data-testid="stSidebar"] button[data-variant="pills"] p, [data-testid="stSidebar"] button[data-testid^="stBaseButton-pills"] p { font-size: 1.05rem !important; font-weight: 500; color: inherit !important; }
[data-testid="stSidebar"] button[data-variant="pills"][aria-checked="true"] p { font-weight: 600; }
[data-testid="stSidebar"] button span[data-testid="stIconMaterial"] { font-size: 1.55rem !important; margin-right: 6px; color: inherit !important; }
[data-testid="stSidebar"] button[data-variant="pills"]::after, [data-testid="stSidebar"] button[data-testid^="stBaseButton-pills"]::after { position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
    min-width: 30px; padding: 3px 10px; border-radius: 999px; font-size: .78rem; font-weight: 700; text-align: center; background: #1A2236; color: #E8ECF7; }
[data-testid="stSidebar"] button[data-variant="pills"][aria-checked="true"]::after, [data-testid="stSidebar"] button[data-testid="stBaseButton-pillsActive"]::after { background: rgba(255,255,255,.22); }
.promo { margin-top: 38px; padding: 20px 18px 0; border-radius: 18px; background: linear-gradient(180deg, #151B34, #10162B); border: 1px solid rgba(124,92,255,.28); overflow: hidden; }
.promo .sp { color: #7FA4FF; font-size: 1.3rem; }
.promo b { display: block; font-size: 1.12rem; line-height: 1.3; margin: 8px 0 8px; color: #F2F5FC; }
.promo p { margin: 0; font-size: .82rem; opacity: .7; line-height: 1.4; }
.promo svg { display: block; width: 100%; height: 70px; margin-top: 8px; }

/* ---------- top bar ---------- */
.topbar { display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin: 0 0 14px; }
.topbar .brand { display: none; font-weight: 800; font-size: 1.2rem; margin-right: auto; }
.userchip { display: flex; align-items: center; gap: 12px; padding: 4px 6px; }
.userchip .av { width: 42px; height: 42px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; color: #fff;
    background: linear-gradient(135deg, #7C5CFF, #4F8CFF); }
.userchip .nm { font-weight: 600; max-width: 220px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.userchip svg { width: 18px; height: 18px; opacity: .7; }

/* ---------- hero ---------- */
.hero { position: relative; display: flex; align-items: center; justify-content: space-between; gap: 24px; overflow: hidden;
    border-radius: 26px; padding: 38px 48px; color: #fff; margin-bottom: 22px;
    background: radial-gradient(90% 150% at 52% -35%, #9A6BFF 0%, rgba(154,107,255,0) 60%),
                radial-gradient(70% 120% at 100% 110%, rgba(34,48,190,.9) 0%, rgba(34,48,190,0) 70%),
                linear-gradient(100deg, #3D3BE6 0%, #4B45E8 45%, #3A32CE 100%);
    box-shadow: 0 24px 60px rgba(79,70,229,.25); }
.hero::before { content: ""; position: absolute; width: 70%; height: 220%; right: -18%; top: -60%; border-radius: 50%;
    border: 2px solid rgba(255,255,255,.07); transform: rotate(-24deg); pointer-events: none; }
.hero-l { position: relative; z-index: 1; min-width: 0; }
.eyebrow { display: flex; align-items: center; gap: 10px; font-size: .82rem; font-weight: 700; letter-spacing: .6px; text-transform: uppercase; opacity: .95; }
.eyebrow .ei { font-size: 1.5rem; }
.hero h1 { margin: 6px 0 0; font-size: 3.1rem; font-weight: 800; letter-spacing: -1px; color: #fff; padding: 0; line-height: 1.12; }
.hero p { margin: .5rem 0 0; font-size: 1.3rem; opacity: .9; }
.checks { display: flex; flex-wrap: wrap; gap: 14px 30px; margin-top: 24px; font-size: .95rem; }
.checks span { display: inline-flex; align-items: center; gap: 10px; opacity: .92; }
.checks i { font-style: normal; width: 26px; height: 26px; border-radius: 50%; display: grid; place-items: center; font-size: .8rem;
    background: rgba(255,255,255,.16); border: 1.5px solid rgba(255,255,255,.55); }
.hero-art { position: relative; z-index: 1; flex: 0 0 auto; width: 360px; height: 190px; }
.hero-art svg { position: absolute; right: 0; top: 0; width: 250px; height: 190px; }
.hero-art .note { position: absolute; left: 0; top: 14px; font-family: 'Caveat', cursive !important; font-size: 1.7rem; line-height: 1.05; transform: rotate(-9deg); color: #fff; }
.hero-art .arw { position: absolute; left: 76px; top: 62px; width: 90px; height: 54px; }

/* ---------- stat cards ---------- */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 22px; margin: 4px 0 22px; }
.stat { position: relative; display: flex; align-items: center; gap: 20px; padding: 20px 26px; border-radius: 22px;
    background: #0F1626; border: 1px solid rgba(255,255,255,.11); }
.stat .ic { width: 72px; height: 72px; border-radius: 50%; display: grid; place-items: center; flex: 0 0 auto; }
.stat .ic svg { width: 34px; height: 34px; }
.stat b { display: block; font-size: 2.4rem; font-weight: 800; line-height: 1.05; }
.stat span { font-size: 1rem; opacity: .78; }
.stat .arr { margin-left: auto; width: 26px; height: 26px; flex: 0 0 auto; }
.stat.t1 { border-color: #14B88A; background: linear-gradient(100deg, rgba(20,184,138,.14), #0F1626 60%); }
.stat.t1 .ic { background: #0E5C4C; color: #3DDC97; } .stat.t1 .arr { color: #2DD4BF; }
.stat.t2 { border-color: #4F5DF0; background: linear-gradient(100deg, rgba(79,93,240,.16), #0F1626 60%); }
.stat.t2 .ic { background: #1D2A72; color: #6AB0FF; } .stat.t2 .arr { color: #7FA4FF; }
.stat.t3 { border-color: rgba(255,255,255,.2); }
.stat.t3 .ic { background: #1E2736; color: #9AA6BD; } .stat.t3 .arr { color: #AEB8CC; }
.stats.plain .stat { padding: 16px 22px; gap: 0; border-radius: 18px; }
.stats.plain .stat b { font-size: 1.9rem; }

/* ---------- filters row ---------- */
[data-testid="stSlider"] label p { font-size: 1.05rem; font-weight: 600; }
[data-testid="stSlider"] [data-testid="stSliderThumbValue"] { background: #151C2E; border: 1px solid rgba(255,255,255,.12); border-radius: 10px;
    padding: 5px 13px; color: #fff; font-weight: 600; }
[data-testid="stSlider"] [role="slider"] { background: #fff !important; box-shadow: 0 0 0 5px rgba(91,99,255,.28); }
[data-testid="stSlider"] [data-baseweb="slider"] > div > div { height: 9px; border-radius: 9px; }
div[data-testid="stCheckbox"] { background: #0F1626; border: 1px solid rgba(255,255,255,.12); border-radius: 999px; padding: 14px 24px; margin-top: 18px; }
div[data-testid="stCheckbox"] label p { font-size: .95rem; font-weight: 500; }

/* ---------- chips ---------- */
.chip { display: inline-flex; align-items: center; gap: 7px; padding: 4px 11px; margin: 3px 6px 3px 0; border-radius: 999px; font-size: .76rem; font-weight: 500;
        background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.12); color: #D6DCEB; }
.chip.ok { background: rgba(16,185,129,.14); border-color: rgba(16,185,129,.4); color: #6EE7B7; }
.chip.miss { background: rgba(244,63,94,.12); border-color: rgba(244,63,94,.35); color: #FDA4AF; }
.chip.accent { background: rgba(124,92,255,.18); border-color: rgba(124,92,255,.5); color: #C9BDFF; }
.chip.warn { background: rgba(245,158,11,.13); border-color: rgba(245,158,11,.5); color: #FBBF24; }

/* ---------- job card ---------- */
.job { display: flex; gap: 22px; align-items: flex-start; }
.job .info { min-width: 0; flex: 1 1 auto; }
.logo { position: relative; flex: 0 0 auto; width: 78px; height: 78px; border-radius: 22px; display: grid; place-items: center;
        font-weight: 800; font-size: 2.1rem; color: #fff; box-shadow: 0 10px 26px rgba(0,0,0,.35); }
.logo .sc { position: absolute; right: -9px; bottom: -9px; min-width: 32px; height: 32px; padding: 0 6px; border-radius: 16px; display: grid; place-items: center;
        font-size: .8rem; font-weight: 800; border: 3px solid #0F1626; color: #fff; }
.job-title { font-weight: 700; font-size: 1.5rem; line-height: 1.25; letter-spacing: -.2px; }
.job-co { opacity: .85; font-size: 1.02rem; margin-top: 8px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.job-co .dot { opacity: .5; } .job-co .pin { color: #8A7BFF; }
.job-tags { margin-top: 18px; }
.job-tags .chip { padding: 9px 18px; font-size: .95rem; background: #161E31; border-color: rgba(255,255,255,.08); margin: 4px 10px 4px 0; }
.job-tags .chip.ok { background: rgba(16,185,129,.12); border-color: rgba(16,185,129,.5); color: #4ADE9B; }
.job-tags .chip.warn { background: rgba(245,158,11,.12); border-color: rgba(245,158,11,.5); color: #FBBF24; }
.s-hi { background: linear-gradient(135deg, #10B981, #047857); }
.s-mid { background: linear-gradient(135deg, #F59E0B, #B45309); }
.s-low { background: linear-gradient(135deg, #64748B, #334155); }
.steps { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
.step { flex: 1 1 200px; background: rgba(124,92,255,.10); border: 1px solid rgba(124,92,255,.35); border-radius: 16px; padding: 14px 16px; }
.step b { color: #C9BDFF; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 24px !important; background: #0F1626; border-color: rgba(255,255,255,.09) !important; }
div[data-testid="stExpander"] details { border-radius: 16px; border-color: rgba(255,255,255,.1); background: #0C1220; }
div[data-testid="stExpander"] summary { padding: 14px 20px; font-weight: 500; }
[data-testid="stButton"], [data-testid="stLinkButton"], .stButton, [data-testid="stSidebar"] [data-testid="stButtonGroup"], [data-testid="stSidebar"] [role="radiogroup"] { width: 100% !important; }
[data-testid="stElementContainer"]:has(> [data-testid="stButton"]), [data-testid="stElementContainer"]:has(> [data-testid="stLinkButton"]),
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(> [data-testid="stButtonGroup"]) { width: 100% !important; }
.stButton > button, .stFormSubmitButton > button { width: 100%; border-radius: 14px; font-weight: 600; min-height: 50px; }
a[data-testid^="stBaseLinkButton"] { width: 100%; justify-content: center; border-radius: 14px; font-weight: 600; min-height: 50px; }
.stButton > button[kind="primary"], a[data-testid="stBaseLinkButton-primary"] { background: linear-gradient(135deg, #7A4DFF, #5B46F5); border: 0; color: #fff; box-shadow: 0 10px 26px rgba(106,77,255,.35); }
.stButton > button[kind="secondary"], a[data-testid="stBaseLinkButton-secondary"] { background: transparent; border: 1px solid rgba(255,255,255,.18); }

@media (max-width: 900px) {
  .hero-art { display: none; }
  .topbar .brand { display: block; }
}
@media (max-width: 640px) {
  .block-container { padding: 1rem .8rem 3rem; }
  .hero { padding: 24px 22px; border-radius: 20px; }
  .hero h1 { font-size: 2rem; }
  .hero p { font-size: 1.05rem; }
  .logo { width: 56px; height: 56px; font-size: 1.5rem; border-radius: 16px; }
  .job { gap: 14px; }
  .job-title { font-size: 1.15rem; }
  .stat b { font-size: 1.8rem; }
  .stat .ic { width: 54px; height: 54px; }
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

ICONS = {"Matches": ":material/dashboard:", "Search": ":material/search:", "Tracker": ":material/checklist:", "Profile": ":material/person:",
         "Contacts": ":material/notifications:", "Run now": ":material/bolt:", "Account": ":material/settings:"}

SVG = {
    "users": '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="8" r="3.2"/><circle cx="17" cy="9" r="2.6"/><path d="M2.5 19c0-3.3 2.9-5.5 6.5-5.5s6.5 2.2 6.5 5.5z"/><path d="M16 13.8c2.9.2 5.5 1.9 5.5 5.2h-4.3c0-2-.4-3.6-1.2-5.2z"/></svg>',
    "bolt": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M13.5 2 4.5 13.5h6L9.5 22l9-11.5h-6z"/></svg>',
    "eyeoff": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18"/><path d="M10.6 6.1A10 10 0 0 1 12 6c5 0 8.5 4 9.5 6a14 14 0 0 1-3 3.8M6.5 7.6A14 14 0 0 0 2.5 12c1 2 4.5 6 9.5 6 1.4 0 2.6-.3 3.7-.8"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>',
    "arrow": '<svg class="arr" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    "chev": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
}
LOGO_SVG = ('<svg viewBox="0 0 48 48"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8E7BFF"/>'
            '<stop offset="1" stop-color="#E26BFF"/></linearGradient></defs><g transform="rotate(40 24 24)">'
            '<path d="M24 3c7 6 8.5 18 6.5 29h-13C15.500 21 17 9 24 3z" fill="url(#lg)"/>'
            '<path d="M17.500 25 9.500 36l8-2zM30.500 25l8 11-8-2z" fill="#6D5BFF"/><circle cx="24" cy="16.500" r="3.600" fill="#0D1322"/>'
            '<path d="M20.500 34 24 45l3.500-11z" fill="#FF8A5B"/></g></svg>')
HERO_ART = ('<svg viewBox="0 0 250 190"><defs><linearGradient id="hs" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7C5CFF"/>'
            '<stop offset="1" stop-color="#4B2FD0"/></linearGradient></defs>'
            '<path d="M40 150h170l14 14H26z" fill="#8F7BFF" opacity=".95"/><rect x="48" y="40" width="154" height="110" rx="12" fill="url(#hs)" stroke="#B7A6FF" stroke-width="3"/>'
            '<rect x="58" y="50" width="134" height="90" rx="6" fill="#33258F"/>'
            '<rect x="98" y="72" width="54" height="40" rx="8" fill="#C9B8FF"/><path d="M114 72v-6a5 5 0 0 1 5-5h12a5 5 0 0 1 5 5v6" fill="none" stroke="#C9B8FF" stroke-width="5"/>'
            '<rect x="98" y="88" width="54" height="5" fill="#8F7BFF"/><rect x="120" y="86" width="10" height="9" rx="2" fill="#6D4DF0"/>'
            '<path d="M222 30l4 11 11 4-11 4-4 11-4-11-11-4 11-4z" fill="#fff"/><path d="M205 8l2.500 6.500 6.500 2.500-6.500 2.500-2.500 6.500-2.500-6.500-6.500-2.500 6.500-2.500z" fill="#7FD6FF"/>'
            '<path d="M236 92l2 5 5 2-5 2-2 5-2-5-5-2 5-2z" fill="#7FD6FF"/></svg>')
HERO_ARROW = ('<svg class="arw" viewBox="0 0 90 54" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M4 10c26-6 56 2 72 30"/><path d="M62 38l14 4 2-15"/></svg>')
PROMO_CHART = ('<svg viewBox="0 0 220 70" preserveAspectRatio="none"><defs><linearGradient id="pc" x1="0" y1="0" x2="0" y2="1">'
               '<stop offset="0" stop-color="#7C5CFF" stop-opacity=".35"/><stop offset="1" stop-color="#7C5CFF" stop-opacity="0"/></linearGradient></defs>'
               '<path d="M0 58C28 50 40 40 66 46S104 58 130 46 170 8 220 6V70H0z" fill="url(#pc)"/>'
               '<path d="M0 58C28 50 40 40 66 46S104 58 130 46 170 8 220 6" fill="none" stroke="#8E7BFF" stroke-width="2.600" stroke-linecap="round"/></svg>')


# ---------------- helpers ----------------
def esc(x) -> str:
    return _html.escape("" if x is None else str(x))   # keeps 0 visible


def csv(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def chips(items, cls: str = "") -> str:
    return "".join(f"<span class='chip {cls}'>{esc(i)}</span>" for i in items)


def hero(title: str, sub: str, eyebrow: str = "", checks: tuple = ()) -> None:
    eb = f"<div class='eyebrow'><span class='ei'>🎯</span>{esc(eyebrow)}</div>" if eyebrow else ""
    ck = "<div class='checks'>" + "".join(f"<span><i>✓</i>{esc(c)}</span>" for c in checks) + "</div>" if checks else ""
    art = f"<div class='hero-art'>{HERO_ART}{HERO_ARROW}<div class='note'>Better jobs<br>await!</div></div>"
    st.markdown(f"<div class='hero'><div class='hero-l'>{eb}<h1>{esc(title)}</h1><p>{esc(sub)}</p>{ck}</div>{art}</div>", unsafe_allow_html=True)


def stats(items: list[tuple[str, object]], icons: tuple = ()) -> None:
    if icons:
        body = "".join(f"<div class='stat t{i % 3 + 1}'><div class='ic'>{SVG[icons[i % len(icons)]]}</div>"
                       f"<div><b>{esc(v)}</b><span>{esc(k)}</span></div>{SVG['arrow']}</div>" for i, (k, v) in enumerate(items))
        st.markdown(f"<div class='stats'>{body}</div>", unsafe_allow_html=True)
        return
    body = "".join(f"<div class='stat'><div><b>{esc(v)}</b><span>{esc(k)}</span></div></div>" for k, v in items)
    st.markdown(f"<div class='stats plain'>{body}</div>", unsafe_allow_html=True)


def company_logo(name: str, score_html: str = "") -> str:
    h = int(hashlib.md5((name or "?").lower().encode()).hexdigest()[:6], 16) % 360
    ch = next((c for c in (name or "?") if c.isalnum()), "?").upper()
    return (f"<div class='logo' style='background:linear-gradient(135deg,hsl({h} 62% 46%),hsl({(h + 35) % 360} 66% 34%))'>{esc(ch)}{score_html}</div>")


def profile_done(p) -> bool:
    return bool(p and (p.resume_text or "").strip() and p.grad_year)


def salary(job) -> str:
    if job.salary_min is None:
        return "not listed"
    return f"{job.salary_min:g}" if job.salary_min == job.salary_max else f"{job.salary_min:g}-{job.salary_max:g}"


def go(page: str) -> None:  # used as a button callback
    st.session_state["nav"] = page
    st.session_state.pop("profile_saved", None)


def more() -> None:
    st.session_state["limit"] = st.session_state.get("limit", 12) + 12


# ---------------- login / register ----------------
def login_screen(s):
    hero("🎯 Job Hunter", "Finds jobs that fit you, checks your eligibility, and tracks every application, automatically.")
    st.markdown(chips(["Watches company career pages", "Checks batch, CGPA and salary", "Tracks every application",
                       "Alerts on your phone"], "accent"), unsafe_allow_html=True)
    with st.container(border=True):
        tabs = st.tabs(["Login", "Create account"] if config.ALLOW_SIGNUP else ["Login"])
        t_login, t_reg = tabs[0], (tabs[1] if len(tabs) > 1 else None)
        with t_login:
            email = st.text_input("Email", key="li_email")
            pw = st.text_input("Password", type="password", key="li_pw")
            if st.button("Login", type="primary", key="li_btn"):
                user = auth.login(s, email, pw)
                if user:
                    st.session_state["uid"] = user.id
                    st.rerun()
                st.error("Wrong email or password.")
        if t_reg is not None:
            with t_reg:
                name = st.text_input("Full name", key="rg_name")
                email = st.text_input("Email", key="rg_email")
                pw = st.text_input("Password (min 6 characters)", type="password", key="rg_pw")
                if st.button("Create account", type="primary", key="rg_btn"):
                    try:
                        user = auth.register(s, email, name, pw)
                        st.session_state["uid"] = user.id
                        st.session_state["nav"] = "Profile"   # new accounts start with the profile
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))


# ---------------- pages ----------------
def page_profile(s, user):
    p = user.profile
    hero("Your profile", "Tell us about you once. Matching, alerts and autofill all use this.")
    if not profile_done(p):
        st.markdown(
            "<div class='steps'>"
            "<div class='step'><b>Step 1</b><br>Fill in your details and add your resume below.</div>"
            "<div class='step'><b>Step 2</b><br>Open <b>Run now</b> to find jobs.</div>"
            "<div class='step'><b>Step 3</b><br>See your best matches in <b>Matches</b>.</div></div>",
            unsafe_allow_html=True)
    with st.container(border=True):
        st.subheader("About you")
        c1, c2 = st.columns(2)
        name = c1.text_input("Full name", user.name)
        phone = c1.text_input("Phone", p.phone)
        college = c1.text_input("College", p.college)
        branch = c1.text_input("Branch", p.branch)
        grad_year = c2.number_input("Graduation year", 2020, 2035, p.grad_year or 2027)
        exp_years = c2.number_input("Years of work experience (0 = fresher)", 0.0, 40.0, float(p.experience_years or 0.0), 0.5,
                                    help="Jobs that clearly need more experience than this are skipped.")
        cgpa = c2.number_input("CGPA (0 = don't filter)", 0.0, 10.0, float(p.cgpa or 0.0), 0.1)
        skills = c2.text_input("Skills (comma separated)", ", ".join(p.skills or []))
        chat = c2.text_input("Telegram chat id (optional)", user.telegram_chat_id or "")
    with st.container(border=True):
        st.subheader("What you want")
        c1, c2 = st.columns(2)
        roles = c1.text_input("Target role keywords", ", ".join(p.target_roles or []), help="e.g. intern, software, data")
        watch = c1.text_input("Only these companies (blank = all)", ", ".join(p.watch_companies or []))
        sectors = c1.multiselect("Sectors (blank = all)", SECTORS, [x for x in (p.sectors or []) if x in SECTORS])
        min_lpa = c2.number_input("Minimum salary (LPA)", 0.0, 500.0, float(p.min_lpa if p.min_lpa is not None else 7.0), 1.0,
                                  help="No maximum. Jobs that don't list a salary are kept.")
        need_sal = c2.checkbox("Hide jobs that don't list a salary", bool(p.require_salary))
        countries = c2.multiselect("Countries (blank = worldwide, pick India to see only India jobs)", COUNTRIES, [x for x in (p.countries or []) if x in COUNTRIES],
                                   help="Remote jobs are always kept.")
        remote_pref = c2.selectbox("Remote preference", REMOTE_PREFS, REMOTE_PREFS.index(p.remote_pref) if p.remote_pref in REMOTE_PREFS else 0)
        job_types = c2.multiselect("Job types (blank = all)", JOB_TYPES, [x for x in (p.job_types or []) if x in JOB_TYPES])
        min_score = c2.slider("Alert me when the match is at least", 0, 100, p.min_score or 60)
    with st.container(border=True):
        st.subheader("Resume")
        pdf = st.file_uploader("Upload resume (PDF)", type=["pdf"])
        text = st.text_area("Resume text (filled from the PDF, or paste it)", p.resume_text, height=200)
    with st.expander("Education and links (used by autofill)"):
        e1, e2 = st.columns(2)
        degree = e1.text_input("Degree", p.degree or "B.Tech")
        city = e1.text_input("Current city", p.city or "")
        tenth = e1.number_input("10th %", 0.0, 100.0, float(p.tenth_pct or 0.0), 0.1)
        twelfth = e1.number_input("12th %", 0.0, 100.0, float(p.twelfth_pct or 0.0), 0.1)
        linkedin = e2.text_input("LinkedIn URL", p.linkedin_url or "")
        github = e2.text_input("GitHub URL", p.github_url or "")
        portfolio = e2.text_input("Portfolio / website", p.portfolio_url or "")
        work_auth = e2.text_input("Work authorization answer", p.work_auth or "", help="e.g. Authorized to work in India. Left blank if empty.")
    with st.expander("Gmail tracking (read-only)"):
        st.caption("Turn on 2-step verification in your Google account, create an App Password and paste it here. "
                   "It is stored encrypted and only used to read emails that mention companies you track.")
        gmail_addr = st.text_input("Gmail address", p.gmail_address or "")
        gmail_pw = st.text_input("App password (leave blank to keep the saved one)", type="password")
    if st.button("Save profile", type="primary"):
        if pdf is not None:
            data = pdf.getvalue()
            try:
                text = resume.extract_text(data) or text
            except Exception:
                st.warning("Couldn't read that PDF. Paste the text instead.")
            p.resume_path = storage.save_resume(user.id, data)
        user.name, user.telegram_chat_id = name, chat or None
        p.phone, p.college, p.branch, p.grad_year = phone, college, branch, int(grad_year)
        p.cgpa = cgpa or None
        p.skills, p.target_roles, p.watch_companies = csv(skills), csv(roles), csv(watch)
        p.min_score, p.resume_text, p.experience_years = min_score, text, exp_years
        p.min_lpa, p.require_salary, p.sectors = min_lpa, need_sal, sectors
        p.countries, p.remote_pref, p.job_types = countries, remote_pref, job_types
        p.degree, p.city, p.tenth_pct, p.twelfth_pct = degree, city, tenth or None, twelfth or None
        p.linkedin_url, p.github_url, p.portfolio_url, p.work_auth = linkedin, github, portfolio, work_auth
        p.gmail_address = gmail_addr.strip()
        if gmail_pw:
            p.gmail_app_password_enc = crypto.encrypt(gmail_pw.replace(" ", ""))
        s.commit()
        st.session_state["rechecked"] = pipeline.reevaluate_matches(s, user)
        st.session_state["profile_saved"] = True
    if st.session_state.get("profile_saved"):
        if profile_done(p):
            st.success("Profile saved. Next, let's find jobs for you.")
            if st.session_state.get("rechecked"):
                st.caption(f"{st.session_state['rechecked']} existing matches were re-checked against your new settings.")
            st.button("Find jobs now →", type="primary", on_click=go, args=("Run now",))
        else:
            st.warning("Saved, but add your resume text (or upload a PDF) so matching can work.")


def _start_autofill(user, m):
    if importlib.util.find_spec("playwright") is None:
        st.error("One-time setup: run `pip install playwright` and then `playwright install chromium`, then try again.")
        return
    subprocess.Popen([sys.executable, "-m", "app.apply", "--user", str(user.id), "--job", str(m.job_id), "--ui"], cwd=str(config.ROOT))
    st.toast("Opening the application page. Check it and click Submit yourself.")


def _draft_block(user, m):
    st.markdown("**Draft answers to common questions (AI)**")
    if st.button("Draft answers", key=f"draft{m.id}"):
        answers = autofill.draft_common_answers(user.profile, m.job)
        if not any(answers.values()):
            st.info("Needs an OpenAI key in .env and a saved resume.")
        for i, (q, a) in enumerate(answers.items()):
            st.text_area(q, a, height=120, key=f"da{m.id}_{i}")
        st.caption("Drafts use only facts from your resume. Read and edit before using.")


def _save_job(s, user, m) -> None:
    tracker.add_application(s, user.id, m.job_id)
    st.toast("Saved to your tracker")


def page_matches(s, user):
    p = user.profile
    first = (user.name or user.email or "there").split()[0]
    hero("Your matches", "Jobs that fit your profile, best first.", eyebrow=f"Welcome back, {first}",
         checks=("Personalized matching", "Fresh opportunities", "Track your progress"))
    if not profile_done(p):
        st.info("Complete your profile first, so we know what to look for.")
        st.button("Go to Profile →", type="primary", on_click=go, args=("Profile",))
        return
    if not p.countries:
        with st.container(border=True):
            st.warning("**Countries is empty in your profile, so you are seeing jobs from every country.** "
                       "Choose your country to keep only jobs you can actually take.")
            c1, c2 = st.columns([1, 2])
            if c1.button("Show only India jobs", type="primary", key="only_india"):
                p.countries = ["India"]
                s.commit()
                st.session_state["rechecked"] = pipeline.reevaluate_matches(s, user)
                st.toast("Done. Jobs outside India were removed from your matches.")
                st.rerun()
            c2.caption("Remote jobs stay only if they have no country, include India, or say anywhere / worldwide / APAC. "
                       "Other countries: Profile > Countries.")
    mine = Match.user_id == user.id
    n_eligible = s.scalar(select(func.count()).select_from(Match).where(mine, Match.eligible.is_(True)))
    n_strong = s.scalar(select(func.count()).select_from(Match).where(mine, Match.eligible.is_(True), Match.score >= 75))
    tracked = s.scalar(select(func.count()).select_from(Application).where(Application.user_id == user.id))
    stats([("Eligible matches", n_eligible), ("Strong (75%+)", n_strong), ("In your tracker", tracked)],
          icons=("users", "bolt", "eyeoff"))
    f1, f2 = st.columns([5, 2], vertical_alignment="center")
    min_score = f1.slider("🎯 Minimum score", 0, 100, p.min_score or 60)
    show_all = f2.checkbox("Also show jobs I'm not eligible for")
    cond = [mine, Match.score >= (0 if show_all else min_score)]
    if not show_all:
        cond.append(Match.eligible.is_(True))
    total = s.scalar(select(func.count()).select_from(Match).where(*cond))
    limit = st.session_state.setdefault("limit", 12)
    if not total:
        scored = s.scalar(select(func.count()).select_from(Match).where(mine))
        if scored:
            st.info(f"{scored} jobs were checked, but none fit your settings. Lower the minimum score, tick the box above to see "
                    "skipped jobs, or relax filters in Profile (countries, job types, role keywords).")
            reason_summary(s, user)
        else:
            st.info("No jobs have been checked for you yet. Open Run now to find jobs.")
            st.button("Run now →", type="primary", on_click=go, args=("Run now",))
        return
    matches = list(s.scalars(select(Match).where(*cond).options(joinedload(Match.job))
                             .order_by(Match.eligible.desc(), Match.score.desc()).limit(limit)))
    for m in matches[:limit]:
        j = m.job
        cls = "s-hi" if m.score >= 75 else "s-mid" if m.score >= 50 else "s-low"
        sal = f"💰 {salary(j)} LPA" if j.salary_min is not None else "💰 salary not listed"
        tags = (f"<span class='chip{'' if j.salary_min is not None else ' warn'}'>{esc(sal)}</span>"
                f"<span class='chip'>📅 {esc(j.job_type or 'Not stated')}</span>"
                + ("<span class='chip'>🌐 Remote</span>" if j.remote else "")
                + chips(closed_badge(j)) + chips(m.matched_skills[:6], "ok"))
        badge = f"<span class='sc {cls}'>{m.score if m.eligible else '–'}</span>"
        with st.container(border=True):
            c_info, c_act = st.columns([7, 2.6])
            c_info.markdown(
                f"<div class='job'>{company_logo(j.company, badge)}<div class='info'>"
                f"<div class='job-title'>{esc(j.title)}</div>"
                f"<div class='job-co'>{esc(j.company)}<span class='dot'>·</span><span class='pin'>📍</span>{esc(j.location or 'Location not listed')}</div>"
                f"<div class='job-tags'>{tags}</div></div></div>", unsafe_allow_html=True)
            with c_act:
                a1, a2 = st.columns([1, 3])
                if a1.button("", icon=":material/bookmark:", key=f"bm{m.id}", help="Save to tracker"):
                    _save_job(s, user, m)
                a2.link_button("Open job ↗", j.url or "https://example.com", type="primary")
                if st.button("Save to tracker", icon=":material/bookmark:", key=f"save{m.id}"):
                    _save_job(s, user, m)
            if not m.eligible:
                st.caption(f"Not eligible: {m.reason}")
            with st.expander("Details, referral and answers"):
                st.write(m.reason)
                if m.missing_skills:
                    st.markdown("Missing skills: " + chips(m.missing_skills, "miss"), unsafe_allow_html=True)
                if st.button("Autofill application (opens a browser on this PC)", key=f"fill{m.id}"):
                    _start_autofill(user, m)
                st.caption("Fills your details and resume, drafts open answers, and never submits. Works when the app runs on your own PC.")
                st.link_button("Find alumni on LinkedIn ↗", referral.linkedin_search_url(j.company, p.college))
                for c in s.scalars(select(Contact).where(Contact.user_id == user.id, Contact.company == j.company)):
                    st.write(f"🤝 You know **{c.name}** at this company. {c.link}")
                st.text_area("Referral message draft (you send it yourself)", referral.draft_message(
                    user.name or "a student", j.company, j.title, j.url, m.matched_skills, p.college), height=150, key=f"msg{m.id}")
                _draft_block(user, m)
    if total > limit:
        st.button(f"Show more ({total - limit} left)", on_click=more)


def page_tracker(s, user):
    hero("Application tracker", "Every job you saved or applied to, in one place.")
    counts = tracker.stats(s, user.id)
    stats([(k, v) for k, v in counts.items() if k in ("Saved", "Applied", "Interview", "Offer")])
    if st.button("Sync Gmail now"):
        if not user.profile.gmail_address:
            st.info("Add your Gmail address and app password in Profile first.")
        else:
            try:
                st.success(f"{gmail.sync_user(s, user)} application(s) updated from email.")
            except Exception as e:
                st.error(f"Gmail sync failed: {e}")
    apps = list(s.scalars(select(Application).where(Application.user_id == user.id).order_by(Application.updated_at.desc())))
    if not apps:
        st.info("Nothing tracked yet. Save a job from Matches.")
    for a in apps:
        with st.container(border=True):
            st.markdown(f"<div class='job-title'>{esc(a.job.title)}</div><div class='job-co'>{esc(a.job.company)}</div>"
                        f"<div style='margin-top:6px'>{chips([a.status], 'accent')}{chips(closed_badge(a.job))}</div>", unsafe_allow_html=True)
            with st.expander("Update"):
                status = st.selectbox("Status", tracker.STATUSES, tracker.STATUSES.index(a.status), key=f"st{a.id}")
                notes = st.text_area("Notes", a.notes, key=f"nt{a.id}")
                if a.follow_up_at:
                    st.caption(f"Follow-up reminder: {a.follow_up_at:%d %b %Y}")
                if st.button("Update", key=f"up{a.id}"):
                    tracker.set_status(s, a, status, notes)
                    st.rerun()


def page_contacts(s, user):
    hero("Referral contacts", "People you know at companies. We flag them when a matching job appears.")
    with st.container(border=True):
        with st.form("add_contact", clear_on_submit=True):
            name, company, link = st.text_input("Name"), st.text_input("Company"), st.text_input("LinkedIn / email")
            if st.form_submit_button("Add contact") and name and company:
                s.add(Contact(user_id=user.id, name=name, company=company, link=link))
                s.commit()
                st.rerun()
    rows = s.scalars(select(Contact).where(Contact.user_id == user.id))
    st.dataframe(pd.DataFrame([{"Name": c.name, "Company": c.company, "Link": c.link} for c in rows]), hide_index=True)


def reason_summary(s, user) -> None:
    rows = s.execute(select(Match.reason, func.count()).where(Match.user_id == user.id, Match.eligible.is_(False))
                     .group_by(Match.reason).order_by(func.count().desc()).limit(6)).all()
    if rows:
        st.markdown("**Why jobs were skipped**")
        for reason, n in rows:
            st.write(f"- {n} jobs: {reason}")


DISCOVER_LOG = config.ROOT / "discover.log"


def _tail(path, n: int = 2) -> str:
    try:
        return " ".join(path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-n:])
    except OSError:
        return ""


@st.fragment(run_every=4)
def _discover_status():
    proc = st.session_state.get("disc_proc")
    if proc is not None and proc.poll() is None:
        st.info("Finding companies. This takes 5 to 15 minutes and you can use other pages meanwhile. " + _tail(DISCOVER_LOG))
    elif proc is not None:
        st.session_state.pop("disc_proc")
        st.session_state["disc_done"] = _tail(DISCOVER_LOG, 3)
        st.rerun()


def discover_card():
    with st.container(border=True):
        st.subheader("Add more companies")
        st.write(f"Monitoring **{len(load_companies())}** companies. Discovery checks about 840 well-known companies "
                 "across all sectors and adds every one we can read.")
        if load_feeds():
            st.caption(f"Job-search feeds are ON ({len(load_feeds())} searches, including TCS, Infosys, HCL, Accenture). "
                       "They run every few hours to save API calls.")
        else:
            st.caption("TCS, Infosys, HCL, Accenture and similar companies use their own career sites, which we cannot read directly. "
                       "Add a free Jooble key in .env (see README) to search for their jobs through a job-search feed.")
        with st.expander("Add career page links"):
            st.caption("One per line: Name | link (or just the link). Use the page that LISTS the jobs, ideally filtered to software jobs "
                       "in your country. JavaScript sites need Playwright (see README).")
            links = st.text_area("Career links", height=120, key="links_box", placeholder="TCS | https://...\nInfosys | https://...")
            if st.button("Add these links", key="links_btn") and links.strip():
                with st.spinner("Checking links..."):
                    added = discover.add_links(links)
                for c in added:
                    st.write(f"✅ {c['name']} added ({c['source']})")
                if not added:
                    st.warning("Nothing added. Each line needs a link starting with http.")
        running = st.session_state.get("disc_proc") is not None
        if running:
            _discover_status()
        else:
            if st.session_state.get("disc_done"):
                st.success("Discovery finished. " + st.session_state["disc_done"])
            if st.button("Find and add companies", type="primary", key="disc_btn"):
                st.session_state.pop("disc_done", None)
                st.session_state["disc_proc"] = subprocess.Popen(
                    [sys.executable, "-u", "-m", "app.discover"], cwd=str(config.ROOT),
                    stdout=open(DISCOVER_LOG, "w"), stderr=subprocess.STDOUT)
                st.rerun()


def source_health_panel(s) -> None:
    """Latest collection run per company/source, plus how many canonical jobs come from direct versus feed sources."""
    latest = s.execute(select(func.max(SourceRun.id)).group_by(SourceRun.source, SourceRun.company_key)).scalars().all()
    if not latest:
        return
    runs = s.scalars(select(SourceRun).where(SourceRun.id.in_(latest)).order_by(SourceRun.outcome.desc(), SourceRun.company_key)).all()
    with st.expander(f"Source health ({len(runs)} sources)"):
        st.dataframe(pd.DataFrame([{"Company / feed": r.company_key, "Source": r.source, "Last outcome": r.outcome,
                                    "Complete listing": "yes" if r.complete else "no", "Listed": r.listed_count, "New": r.new_count,
                                    "When": f"{r.finished_at:%d %b %H:%M}" if r.finished_at else ""} for r in runs]), hide_index=True)
        st.caption("Only a complete listing from a verified source can mark a job as possibly closed. Failed or blocked runs never do.")
        per = s.execute(select(Job.source, func.count()).where(Job.canonical_job_id.is_(None)).group_by(Job.source)).all()
        kinds: dict[str, int] = {}
        for src, n in per:
            kinds[jobsources.kind_of(src)] = kinds.get(jobsources.kind_of(src), 0) + n
        total = sum(kinds.values()) or 1
        st.caption("Coverage of unique jobs by source type: " + ", ".join(f"{k} {n} ({n * 100 // total}%)" for k, n in sorted(kinds.items())))


PIPE_LOG = config.ROOT / "pipeline_run.log"


@st.cache_data(ttl=60, show_spinner=False)
def _run_tables():
    """The two big tables on this page change only when a run finishes, so they are cached for a minute (a click no longer re-queries them)."""
    with SessionLocal() as s2:
        newest = [{"Company": j.company, "Title": j.title, "Location": j.location, "Remote": "Yes" if j.remote else "",
                   "Type": j.job_type or "Not stated", "Salary (LPA)": salary(j), "Link": j.url}
                  for j in s2.scalars(select(Job).order_by(Job.id.desc()).limit(25))]
        per = [{"Company": c, "Source": src, "Jobs": n} for c, src, n in
               s2.execute(select(Job.company, Job.source, func.count()).group_by(Job.company, Job.source).order_by(func.count().desc()))]
    return newest, per


@st.fragment(run_every=4)
def _run_status():
    proc = st.session_state.get("run_proc")
    if proc is not None and proc.poll() is None:
        st.info("Finding and scoring jobs in the background. You can open other pages meanwhile. " + _tail(PIPE_LOG))
    elif proc is not None:
        st.session_state.pop("run_proc")
        st.session_state["run_done"] = _tail(PIPE_LOG, 2)
        _run_tables.clear()
        st.rerun()


def run_card():
    """'Find new jobs now' runs as a separate process, so the website stays fast instead of freezing for minutes."""
    if st.session_state.get("run_proc") is not None:
        _run_status()
        return
    if st.button("Find new jobs now", type="primary", key="run_btn"):
        st.session_state.pop("run_done", None)
        st.session_state["run_proc"] = subprocess.Popen(
            [sys.executable, "-u", "run_pipeline.py"], cwd=str(config.ROOT),
            stdout=open(PIPE_LOG, "w"), stderr=subprocess.STDOUT)
        st.rerun()


def page_run(s, user):
    hero("Find jobs", "Scrapes your company list, then scores every new job for you.")
    p = user.profile
    if not profile_done(p):
        st.warning("Your profile has no resume yet, so jobs can't be matched. Add it in Profile first.")
        st.button("Go to Profile →", on_click=go, args=("Profile",))
    n_jobs = s.scalar(select(func.count()).select_from(Job))
    n_scored = s.scalar(select(func.count()).select_from(Match).where(Match.user_id == user.id))
    n_ok = s.scalar(select(func.count()).select_from(Match).where(Match.user_id == user.id, Match.eligible.is_(True)))
    stats([("Companies monitored", len(load_companies())), ("Jobs found", n_jobs), ("Checked for you", n_scored), ("Eligible for you", n_ok)])
    discover_card()
    run_card()
    last = st.session_state.get("run_done")
    if last:
        st.success("Last run finished. " + last)
        st.button("See my matches →", on_click=go, args=("Matches",))
    reason_summary(s, user)
    st.markdown("#### Newest jobs found")
    newest, per = _run_tables()
    if newest:
        st.dataframe(pd.DataFrame(newest), hide_index=True,
                     column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open")})
    else:
        st.caption("No jobs yet. Click the button above.")
    source_health_panel(s)
    st.markdown("#### Jobs per company")
    st.dataframe(pd.DataFrame(per), hide_index=True)
    st.caption("Tip: run `python run_pipeline.py --loop --every 30` and this happens automatically.")


def page_account(s, user):
    hero("Account", user.email)
    with st.container(border=True):
        if st.button("Log out"):
            st.session_state.clear()
            st.rerun()
        st.divider()
        st.warning("Deleting your account removes your profile, resume, matches, applications and contacts.")
        if st.checkbox("I understand") and st.button("Delete my data"):
            auth.delete_user(s, user.id)
            st.session_state.clear()
            st.rerun()


def closed_badge(job) -> list[str]:
    """Lifecycle badge: shown on matches and tracked jobs when the source stopped listing them."""
    return ["⚠️ possibly closed"] if getattr(job, "status", None) in ("stale", "expired") else []


_POSTED = {"Any time": None, "Last 24 hours": 1, "Last 3 days": 3, "Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30}
_SORTS = {"Best match": "relevance", "Newest": "newest", "Highest salary": "salary"}


def _search_nav(delta: int) -> None:
    st.session_state["se_idx"] = max(0, st.session_state.get("se_idx", 0) + delta)


def page_search(s, user):
    hero("Search all jobs", "Every collected job, de-duplicated across sources. This is separate from your personal matches.")
    missing = jobsearch.unindexed_count(s)
    if missing:
        st.info(f"{missing} job(s) are not searchable yet. Ask whoever runs the app to execute "
                "`python -m app.migrations backfill` (a dry-run report comes first).")
    opts = jobsearch.filter_options(s)
    with st.container(border=True):
        kw = st.text_input("Keywords", key="se_q", placeholder='e.g. python developer, "machine learning", c++')
        c1, c2, c3 = st.columns(3)
        countries = c1.multiselect("Country", opts["country"], key="se_country")
        city = c2.text_input("City", key="se_city")
        remote_choice = c3.selectbox("Work style", ["Any", "Remote only", "Not remote"], key="se_remote")
        c4, c5, c6 = st.columns(3)
        job_types = c4.multiselect("Job type", opts["job_type"], key="se_jt")
        categories = c5.multiselect("Category", opts["category"], key="se_cat")
        company = c6.text_input("Company", key="se_co")
        c7, c8, c9 = st.columns(3)
        min_sal = c7.number_input("Minimum salary (LPA)", 0.0, 500.0, 0.0, step=1.0, key="se_sal")
        unknown = c8.radio("Jobs with no salary listed", ["Include", "Exclude"], horizontal=True, key="se_unk")
        posted = c9.selectbox("Posted", list(_POSTED), key="se_posted")
        c10, c11 = st.columns(2)
        sort_label = c10.selectbox("Sort by", list(_SORTS), key="se_sort")
        stale = c11.checkbox("Also show possibly closed jobs", key="se_stale")
    base = jobsearch.SearchQuery(
        q=kw, country=countries, city=city, remote={"Any": None, "Remote only": True, "Not remote": False}[remote_choice],
        job_type=job_types, category=categories, company=company, min_salary_lpa=min_sal or None,
        unknown_salary=unknown.lower(), posted_within_days=_POSTED[posted], include_stale=stale, sort=_SORTS[sort_label], page_size=10)
    sig = jobsearch._query_hash(base)
    if st.session_state.get("se_sig") != sig:                      # a different query starts again at page 1
        st.session_state.update(se_sig=sig, se_stack=[None], se_idx=0)
    stack, idx = st.session_state["se_stack"], min(st.session_state.get("se_idx", 0), len(st.session_state["se_stack"]) - 1)
    base.cursor = stack[idx]
    try:
        page = jobsearch.search(s, base)
    except jobsearch.BadCursor:
        st.session_state.update(se_stack=[None], se_idx=0)
        st.rerun()
    if page.next_cursor and len(stack) == idx + 1:
        stack.append(page.next_cursor)
    if page.total is not None:
        st.caption(f"{page.total} job(s) found.")
    else:
        more_txt = " (showing the best 1,000 matches)" if page.candidates_capped else ""
        st.caption(f"{page.total_estimate} job(s) found{more_txt}.")
    if base.min_salary_lpa and page.unknown_salary_count:
        verb = "hidden" if base.unknown_salary == "exclude" else "included, marked as salary not listed"
        st.caption(f"{page.unknown_salary_count} job(s) without a listed salary {verb}.")
    if not page.items:
        st.info("No jobs match. Try fewer filters or different keywords.")
    for it in page.items:
        tags = [f"💰 {it['salary_min']:g}{'-' + format(it['salary_max'], 'g') if it['salary_max'] not in (None, it['salary_min']) else ''} LPA"
                if it["salary_known"] else "💰 salary not listed", it["job_type"] or "Not stated", f"via {it['source']}"]
        if it["remote"]:
            tags.append("🌍 Remote")
        if it["status"] == "stale":
            tags.append("⚠️ possibly closed")
        tags += [f"also on {a['source']}" for a in it["also_on"]]
        with st.container(border=True):
            st.markdown(f"<div class='job'><div class='info'><div class='job-title'>{esc(it['title'])}</div>"
                        f"<div class='job-co'>{esc(it['company'])} · {esc(it['location'] or 'Location not listed')}</div></div></div>"
                        f"<div style='margin-top:8px'>{chips(tags)}</div>", unsafe_allow_html=True)
            b1, b2 = st.columns(2)
            b1.link_button("Open job ↗", it["apply_url"] or "https://example.com", key=f"se_open{it['id']}")
            if b2.button("Save to tracker", key=f"se_save{it['id']}"):
                _, created = jobsearch.save_to_tracker(s, user.id, it["id"])
                st.toast("Saved to your tracker" if created else "You already track this job")
            with st.expander("Description"):
                st.write((it["description"] or "")[:1500])
    n1, n2, n3 = st.columns([1, 1, 4])
    n1.button("← Previous", disabled=idx == 0, on_click=_search_nav, args=(-1,), key="se_prev")
    n2.button("Next →", disabled=not page.next_cursor, on_click=_search_nav, args=(1,), key="se_next")
    n3.caption(f"Page {idx + 1}")


PAGES = {"Matches": page_matches, "Search": page_search, "Tracker": page_tracker, "Profile": page_profile,
         "Contacts": page_contacts, "Run now": page_run, "Account": page_account}
if not config.FEATURE_V2_SEARCH:
    PAGES.pop("Search")


def _sidebar(user, eligible: int, tracked: int) -> str | None:
    badge_css = ""
    for i, name in enumerate(PAGES, 1):
        n = {"Matches": eligible, "Tracker": tracked}.get(name)
        if n is not None:
            badge_css += f'[data-testid="stSidebar"] [role="radiogroup"] > button:nth-of-type({i})::after, [data-testid="stSidebar"] [data-testid="stButtonGroup"] button:nth-of-type({i})::after {{ content: "{int(n)}"; }}\n'
    st.markdown(f"<style>{badge_css}</style>", unsafe_allow_html=True)
    with st.sidebar:
        st.markdown(f"<div class='logo-row'>{LOGO_SVG}<div><div class='t1'>Job <em>Hunter</em></div>"
                    "<div class='t2'>Find. Match. Grow.</div></div></div>", unsafe_allow_html=True)
        chosen = st.pills("Go to", list(PAGES), selection_mode="single", key="nav",
                          format_func=lambda k: f"{ICONS[k]} {k}", label_visibility="collapsed")
        st.markdown(f"<div class='promo'><div class='sp'>✦</div><b>Better matches,<br>bigger opportunities.</b>"
                    f"<p>Let AI find the jobs that fit your skills and goals.</p>{PROMO_CHART}</div>", unsafe_allow_html=True)
    return chosen


def main():
    with SessionLocal() as s:
        user = s.get(User, st.session_state["uid"]) if "uid" in st.session_state else None
        if not user:
            st.markdown("<style>[data-testid='stSidebar'], [data-testid='stSidebarCollapsedControl'], [data-testid='collapsedControl'] "
                        "{ display: none !important; }</style>", unsafe_allow_html=True)
            return login_screen(s)
        if "nav" not in st.session_state:   # first screen: profile until it is filled in
            st.session_state["nav"] = "Matches" if profile_done(user.profile) else "Profile"
        eligible = s.scalar(select(func.count()).select_from(Match).where(Match.user_id == user.id, Match.eligible.is_(True))) or 0
        tracked = s.scalar(select(func.count()).select_from(Application).where(Application.user_id == user.id)) or 0
        chosen = _sidebar(user, eligible, tracked)
        who = esc(user.name or user.email)
        st.markdown(f"<div class='topbar'><div class='brand'>🎯 Job Hunter</div><div class='userchip'><span class='av'>{esc((who[:1] or '?').upper())}</span>"
                    f"<span class='nm'>{who}</span>{SVG['chev']}</div></div>", unsafe_allow_html=True)
        page = chosen or st.session_state.get("last_page", "Matches")   # tapping the active pill again keeps the page
        st.session_state["last_page"] = page
        PAGES[page](s, user)


main()
