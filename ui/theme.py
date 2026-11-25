"""Single source of truth for the app's Python-side colours (Plotly + the chain DataTable).

The "Midnight Quant" dark palette. CSS mirrors these as ``--oc-*`` tokens in
``assets/onechain.css``; this module exists because Plotly figures and the ``dash_table.DataTable``
cannot read CSS variables. Applied region-by-region during the redesign — keep the two in sync.
"""

from __future__ import annotations

# --- Surfaces ---
BG = "#0a0f1e"            # deepest navy (page / underlying row)
SURFACE = "#121a2e"       # panels, headers
SURFACE_SOFT = "#18233c"  # raised surface / ITM tint
INSET = "#101725"         # recessed surface / OTM tint
BORDER = "#1e2a45"
BORDER_STRONG = "#2a3a5c"
GRID = "#1e2a45"          # plotly gridlines

# --- Text ---
INK = "#e8edf7"
MUTED = "#8a97b5"

# --- Accents / semantics ---
TEAL = "#22d3a8"          # primary
UP = "#2dd4a0"            # positive / undervalued / supportive
DOWN = "#ff6b6b"          # negative / overvalued / resistive
AMBER = "#f5b544"         # warning / strike
VIOLET = "#a78bfa"        # max-pain / straddle accent
INFO = "#7c9cff"

# --- Option-side convention (call = warm, put = cool) ---
CALL = "#ff6b6b"
PUT = "#2dd4a0"

# --- Low-alpha tints for restrained table fills ---
TEAL_TINT = "rgba(34,211,168,0.10)"
AMBER_TINT = "rgba(245,181,68,0.12)"
VIOLET_TINT = "rgba(167,139,250,0.12)"
