"""Top-level shell for OneChain."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dash import Input, Output, State, callback_context, dcc, html, no_update
import dash_bootstrap_components as dbc
import dash_mantine_components as dmc

from analytics.aggregate import is_aggregate_eligible
from analytics.alerts import ALERT_CATEGORY_COLORS, build_details_feed, group_swap_rows
from analytics.rate_alerts import compute_rate_crossings, marker_state
from config import AppConfig
from features.advanced import build_advanced_controls, build_advanced_placeholder
from features.bottom_panels import build_bottom_panels
from features.bottom_panels.rendering import build_iv_rv_figure, build_price_figure
from features.chain.heatmap import build_rate_heatmap_table
from features.chain.rendering import build_chain_tab, resolve_chain_report
from features.chain.table_model import build_chain_table_data, build_chain_table_style
from features.charts.rendering import build_charts_tab
from features.controls import build_controls_layout
from features.options_analysis import build_gex_rr_charts_row, build_metric_charts, build_metric_drawer_shell
from features.playback import build_playback_drawer_body
from features.required_move_drawer import build_drawer_shell
from services.live_collector import (
    get_or_create_hover_state,
    get_or_create_interpretor_state,
    get_or_create_rate_ring_state,
)
from services.runtime import RuntimeServices
from ui.formatters import _interp_streak_text, _interp_strike_text, format_mark_time_label
from state.builders import build_active_view_context_from_aggregate_state, build_active_view_context_from_pair_state
from state.models import (
    ActiveViewContext,
    AutomationState,
    ControlDraftState,
    LoadedReportState,
    PlaybackState,
    RequestPlanState,
    RefreshState,
)
from state.selectors import get_active_loaded_pair
from ui.pair_navigation import build_pair_navigation


# Tab-bar indicator colors, keyed to the design tokens (success / warning / muted).
_INDICATOR_COLORS = {"live": "#2dd4a0", "default": "#f5b544", "unavailable": "#8a97b5"}

_IST = ZoneInfo("Asia/Kolkata")


def _rate_crossings_for(services: RuntimeServices, view_key: str | None) -> dict:
    """Read the server-side rate ring for one pair (snapshot under lock) and compute its crossings —
    the shared source for the Layer-1 markers and the Layer-3 heatmap. ``{}`` when empty."""
    if not view_key:
        return {}
    ring_state = get_or_create_rate_ring_state(services)
    with ring_state.lock:
        pair_ring = ring_state.rings.get(view_key)
        snapshot = {cid: list(dq) for cid, dq in pair_ring.items()} if pair_ring else {}
    if not snapshot:
        return {}
    return compute_rate_crossings(snapshot, now=datetime.now(_IST).replace(tzinfo=None))


def _compute_rate_markers(services: RuntimeServices, view_key: str | None) -> dict | None:
    """Tier-2 Layer-1 marker states for one pair: the crossings reduced to one state per crossed
    contract+rule (shortest crossed window) → ``{(strike, side, rule): "up"|"down"}`` (or ``None`` when
    empty / no crossings). The colour is applied natively in the chain style (`build_conditional_styles`)."""
    crossings = _rate_crossings_for(services, view_key)
    if not crossings:
        return None
    markers: dict = {}
    for cid, by_rule in crossings.items():
        strike_str, _, side = cid.partition("|")
        try:
            skey = round(float(strike_str), 4)
        except ValueError:
            continue
        for rule, windows in by_rule.items():
            state = marker_state(windows)
            if state is not None:
                markers[(skey, side, rule)] = state
    return markers or None


def _interpretor_read_for(services: RuntimeServices, view_key: str | None) -> dict | None:
    """The active pair's interpretor display read (snapshot under the lock), or ``None``. Live +
    per-pair only — the caller gates on ``is_live_pair``, exactly like the rate markers."""
    if not view_key:
        return None
    state = get_or_create_interpretor_state(services)
    with state.lock:
        return state.reads.get(view_key)


def _hover_snapshot_for(services: RuntimeServices, view_key: str | None) -> dict | None:
    """The active pair's A2 09:20 per-strike hover snapshot (under the lock), or ``None``. Live +
    per-pair only — the caller gates on ``is_live_pair``, exactly like ``_interpretor_read_for``."""
    if not view_key:
        return None
    state = get_or_create_hover_state(services)
    with state.lock:
        return state.pairs.get(view_key)


def _format_clock(value: object, fmt: str) -> str | None:
    """Parse a stored naive-IST ISO timestamp and format it; None on failure."""
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).strftime(fmt)
    except ValueError:
        return None


def _build_change_row(item: dict) -> html.Div:
    """One structured Changes-feed row: a category-coloured dot · muted mono time · body.

    ``item`` is a ``{time, category, body}`` dict from ``analytics.alerts.build_details_feed``. The
    dot colour is the shared per-category accent (teal=rr / coral=gex / amber=level), so type is
    legible at a glance without a text tag; the ⚠ glyph and the redundant instrument name are
    already stripped by the feed builder."""
    color = ALERT_CATEGORY_COLORS[item["category"]]
    return html.Div(
        [
            html.Span(className="oc-change-dot", style={"backgroundColor": color}),
            html.Span(item.get("time", ""), className="oc-change-time"),
            html.Span(item.get("body", ""), className="oc-change-body"),
        ],
        className="oc-change-item",
    )


def _build_change_group(rows: list[dict]) -> html.Div:
    """A highest ⇄ 2nd-highest positional swap, tied into one cluster: the inner row divider is
    dropped so the two rows read as one unit. Single changes are separated by the thicker
    inter-change lines, so the group — having no internal divider — reads as one distinct block (no
    accent bar; the per-row dot colour still marks OI vs Volume). See
    ``analytics.alerts.group_swap_rows``."""
    return html.Div(
        [_build_change_row(item) for item in rows],
        className="oc-change-group",
    )


def _interp_line(label: str, side: dict, stamp_side: dict | None) -> html.Div:
    stamp_side = stamp_side or {}
    primary = _interp_strike_text(stamp_side.get("primary"))
    second = _interp_strike_text(stamp_side.get("second"))
    return html.Div(
        [
            html.Span(label, className="oc-interp-side"),
            html.Span(f"streak {_interp_streak_text(side)}"),
            html.Span("|", className="oc-interp-sep"),
            html.Span(f"09:20 {primary}"),
            html.Span(f"(2nd {second})", className="oc-interp-second"),
        ],
        className="oc-interp-line",
    )


def _build_interp_pinned(read: dict | None) -> html.Div | None:
    """The interpretor pinned block — CE / PE signed shift-streak + frozen 09:20 walls — for the
    Analytics panel's Changes column. ``None`` when there is no read (server not warmed yet)."""
    if not read:
        return None
    stamp = read.get("stamp_0920") or {}
    return html.Div(
        [
            html.Div("Interpretor", className="oc-panel-section-title"),
            _interp_line("CE", read.get("call") or {}, stamp.get("call")),
            _interp_line("PE", read.get("put") or {}, stamp.get("put")),
        ],
        className="oc-interp-pinned",
    )


def build_shell_layout(config: AppConfig) -> html.Div:
    """Build the shell with explicit state stores."""
    return dbc.Container(
        [
            dcc.Store(id="control-draft-store", storage_type="memory", data=ControlDraftState().to_dict()),
            dcc.Store(id="request-plan-store", storage_type="memory", data=None),
            # Each loaded/live state is split into a small meta store (everything except
            # report["rows"]) + a rows store ({view_key: rows}). Scalar-only callbacks read meta;
            # rows-consumers read meta + rows and merge via LoadedReportState.from_meta_and_rows.
            # Every writer MUST emit both halves of a store together.
            dcc.Store(id="live-loaded-report-meta-store", storage_type="memory", data=None),
            dcc.Store(id="live-loaded-report-rows-store", storage_type="memory", data=None),
            dcc.Store(id="loaded-report-meta-store", storage_type="memory", data=None),
            dcc.Store(id="loaded-report-rows-store", storage_type="memory", data=None),
            dcc.Store(id="active-view-context-store", storage_type="memory", data=ActiveViewContext(kind="pair", source="live").to_dict()),
            dcc.Store(id="playback-state-store", storage_type="memory", data=PlaybackState().to_dict()),
            dcc.Store(id="refresh-state-store", storage_type="memory", data=RefreshState().to_dict()),
            dcc.Store(id="automation-state-store", storage_type="local", data=AutomationState().to_dict()),
            dcc.Store(id="advanced-report-store", storage_type="memory", data=None),
            dcc.Store(id="alerts-state-store", storage_type="memory", data=None),
            # Bumped (monotonic int) by the alerts callback only on a NEW critical alert for the active
            # pair; a clientside callback plays a short beep when it changes.
            dcc.Store(id="critical-alert-signal-store", storage_type="memory", data=None),
            dcc.Store(id="alert-sound-sink", storage_type="memory", data=None),
            # Tier-2 rate-cluster sound: bumped by the cluster detector, played by a clientside beep.
            dcc.Store(id="rate-sound-signal-store", storage_type="memory", data=None),
            dcc.Store(id="rate-sound-sink", storage_type="memory", data=None),
            dcc.Store(id="rmd-selected-store", storage_type="memory", data={}),
            # Resolved pair context for the left-drawer metric charts (asset/segment/expiry +
            # underlying). Lives here (not in a tab) so the relocated metric callbacks always find it.
            # Pair-change writes also carry a one-shot `restore` stash ({"1": [...], "2": [...]}) that
            # restore_oa_selections applies to the contract pickers in a follow-up response.
            dcc.Store(id="oa-data-store", storage_type="memory", data={}),
            # Per-pair metric-chart contract selections `{pair_key: {"1": [...], "2": [...]}}` — so each
            # sub-tab (pair) keeps its own picks across switches (mirrors rmd-selected-store's buckets).
            dcc.Store(id="oa-selections-store", storage_type="memory", data={}),
            # Drives the Charts tab's tab-entry-only refresh: a token that changes on
            # tab-entry / active-pair switch / strike-range change, but NOT on auto-refresh ticks.
            dcc.Store(id="charts-refresh-token", storage_type="memory", data=None),
            # Secondary pair-row signature — changes ONLY when the pair set / active pair / kind
            # changes, never on a same-pairs auto-refresh tick. Gates render_secondary_pair_row so
            # the sub-tabs aren't re-created under the user's click every tick (the sub-tab switch
            # race). See compute_pair_row_signature / render_secondary_pair_row.
            dcc.Store(id="pair-row-signature-store", storage_type="memory", data=None),
            dcc.Store(id="vix-heartbeat-store", storage_type="memory", data=None),
            # One-shot lazy-mount flags: flip False→True the first time their surface
            # opens, which both stops the mount callback re-injecting and re-fires the figure callbacks
            # so the freshly-mounted charts paint data on that first open.
            dcc.Store(id="bottom-charts-mounted", storage_type="memory", data=False),
            dcc.Store(id="metric-charts-mounted", storage_type="memory", data=False),
            dcc.Store(id="scroll-memory-sink", storage_type="memory", data=None),
            dcc.Store(id="tab-href-sink", storage_type="memory", data=None),
            # Tracks the newest fetch group this tab has already repainted, so the render poll can
            # O(1)-skip when the server collector hasn't produced anything newer.
            dcc.Store(id="last-rendered-group-store", storage_type="memory", data=None),
            # The browser render poll: always on. Each tick, render_live_data checks the server
            # collector for a newer fetch group and repaints if so (it does NOT fetch — the
            # server-side collector thread does). Replaces the old browser-driven auto-refresh + VIX
            # intervals; fetching + VIX now live in services/live_collector.py.
            dcc.Interval(id="live-render-interval", interval=config.live_render_interval_ms, disabled=False),
            dcc.Interval(id="snapshot-automation-interval", interval=10000, disabled=False),
            dmc.Group(
                [
                    dmc.Group(
                        [
                            html.Div(className="onechain-brand-mark"),
                            html.Div(
                                [
                                    html.Div("OneChain", className="onechain-app-title"),
                                    html.Div("Options Analytics Terminal", className="onechain-app-subtitle"),
                                ]
                            ),
                        ],
                        gap="sm",
                        align="center",
                        wrap="nowrap",
                    ),
                    html.A(
                        ["ChorBazaar", html.Span("↗", className="onechain-chorbazaar-arrow")],
                        href="https://chorbazaar.co.in",
                        target="_blank",
                        className="onechain-chorbazaar-link",
                    ),
                ],
                justify="space-between",
                align="center",
                wrap="nowrap",
                className="onechain-app-header",
            ),
            dbc.Card(
                [
                    dbc.CardHeader(
                        dbc.Button(
                            [
                                html.Span("Controls"),
                                html.Span("▾", id="controls-collapse-chevron", className="onechain-controls-chevron"),
                            ],
                            id="controls-collapse-button",
                            color="primary",
                            className="w-100",
                        )
                    ),
                    dbc.Collapse(
                        dbc.CardBody(build_controls_layout(config)),
                        id="controls-collapse",
                        is_open=True,
                    ),
                ],
                className="onechain-controls-card mb-3",
            ),
            html.Div(
                html.Div(
                    [
                        dbc.Tabs(
                            [
                                dbc.Tab(label="Chain", tab_id="chain-tab", id="chain-tab-nav"),
                                dbc.Tab(label="Charts", tab_id="charts-tab", id="charts-tab-nav"),
                                dbc.Tab(label="Advanced", tab_id="advanced-tab", id="advanced-tab-nav"),
                            ],
                            id="main-tabs",
                            active_tab="chain-tab",
                        ),
                        html.Div(
                            [
                                html.Span(id="lot-size-indicator", className="onechain-tabbar-indicator"),
                                html.Span(id="vix-status-indicator", className="onechain-tabbar-indicator"),
                                html.Span(id="chain-fetch-indicator", className="onechain-tabbar-indicator"),
                                # View toggles — persistent here (the secondary pair row, which would
                                # otherwise host Aggregate, disappears in aggregate mode). Aggregate is
                                # a boolean Switch read by reduce_control_draft; Greeks keeps its
                                # session persistence and is read by render_tab_content.
                                dbc.Switch(id="aggregate-mode-selector", label="Agg", value=False, className="mb-0"),
                                dbc.Switch(
                                    id="greeks-toggle",
                                    label="Greeks",
                                    value=False,
                                    persistence=True,
                                    persistence_type="session",
                                    className="mb-0",
                                ),
                                # Tier-2 Layer 3 — swaps the Chain tab to the full rate surface view.
                                dbc.Switch(
                                    id="rate-heatmap-toggle",
                                    label="Surface",
                                    value=False,
                                    persistence=True,
                                    persistence_type="session",
                                    className="mb-0",
                                ),
                                # Mark — zero-friction save: a small button popping a name field
                                # (pre-filled with the current IST time) + Save, no need to reopen the
                                # builder.
                                dbc.Button("★ Mark", id="mark-open-button", color="warning", outline=True, size="sm", n_clicks=0),
                                dbc.Popover(
                                    dbc.PopoverBody(
                                        [
                                            dcc.Input(
                                                id="mark-name-input",
                                                type="text",
                                                placeholder="Bookmark name",
                                                className="form-control form-control-sm mb-2",
                                                style={"minWidth": "200px"},
                                            ),
                                            dbc.Button("Save", id="mark-save-button", color="warning", size="sm", n_clicks=0),
                                            html.Div("", id="mark-save-status", className="onechain-controls-status small mt-2"),
                                        ]
                                    ),
                                    id="mark-popover",
                                    target="mark-open-button",
                                    placement="bottom",
                                    is_open=False,
                                    # Above the sticky tab bars (primary 1200 / secondary 1190) so the
                                    # loaded-instruments row never overlaps it — the dropdown overlay tier.
                                    style={"zIndex": 3000},
                                ),
                            ],
                            id="tabbar-status-indicators",
                            className="d-flex align-items-center gap-3 ms-auto",
                        ),
                    ],
                    className="d-flex align-items-center justify-content-between flex-wrap",
                ),
                id="primary-tabs-shell",
                className="onechain-primary-tabs-shell",
            ),
            # Seed with the hidden empty pair tabs so `loaded-pair-tabs` exists at page load —
            # otherwise `sync_active_view_context`'s initial fire references a missing component
            # and the browser throws a "nonexistent object in Input" error that jams dispatch of
            # dynamically-rendered events (e.g. the chain table's active_cell). render_secondary_pair_row
            # overwrites these children as usual.
            html.Div(
                build_pair_navigation(request_plan_state=None, loaded_report_state=None, active_pair_key=None),
                id="loaded-pair-tabs-row",
                className="onechain-secondary-tabs-shell mt-2",
            ),
            html.Div(id="tab-content", className="mt-3"),
            # The Charts tab is a PERSISTENT container (mounted once, not rebuilt by
            # render_tab_content) holding only the strike-distribution grid — rendered into
            # `charts-distribution-content` by `render_charts_distribution` on tab-entry /
            # pair-switch / strike-change, never on ticks. The former Time-Series section moved to
            # always-on surfaces: the two metric charts → the left drawer, GEX + RR → the bottom
            # analytics panel. Shown/hidden by `toggle_charts_container`.
            html.Div(
                [html.Div(id="charts-distribution-content", className="mt-3")],
                id="charts-container",
                className="mt-3",
                style={"display": "none"},
            ),
            # Advanced is ALSO a persistent container, populated only by the Run callback —
            # so the computed result survives tab switches and is never touched by
            # auto-refresh. Its controls strip (count + Run) is STATIC at the top so it shows even
            # before the first Run; the Run callback fills `advanced-dashboard-content` below it.
            # Shown/hidden by the Advanced tab toggle.
            html.Div(
                [
                    build_advanced_controls(),
                    html.Div(
                        build_advanced_placeholder("Run advanced analytics to view the dashboard."),
                        id="advanced-dashboard-content",
                    ),
                ],
                id="advanced-container",
                className="mt-3",
                style={"display": "none"},
            ),
            _build_bottom_panel_shell(),
            # Bottom slide-in Playback drawer (overlay, mutually exclusive with the analytics panel).
            _build_playback_drawer_shell(),
            # Right-side sliding drawer of per-option required-move sparklines (static shell;
            # its body is filled by the required_move_drawer callbacks).
            build_drawer_shell(),
            # Left-side sliding drawer of the two per-option metric charts (static shell; opened by
            # clicking the chain's underlying-price cell — see options_analysis.manage_metric_drawer).
            build_metric_drawer_shell(),
        ],
        fluid=True,
        className="onechain-shell",
        style={"paddingBottom": "56px"},
    )


def _build_bottom_charts() -> list:
    """The 4 bottom-panel charts (Price | IV-RV row + Aggregate-GEX | RR row). Injected into
    `bottom-charts-mount` on the panel's first open (lazy mount) rather than mounted
    at cold load. Seeded with dark+pan placeholder figures so the first paint is on-theme; the feature
    callbacks then swap real figures live (and re-fire once `bottom-charts-mounted` flips, so the
    first open paints data, not the placeholder)."""
    return [
        dbc.Row(
            [
                dbc.Col(dcc.Graph(id="bottom-price-chart-graph", figure=build_price_figure([], ""), config={"displayModeBar": True}), md=6),
                dbc.Col(dcc.Graph(id="bottom-iv-rv-chart-graph", figure=build_iv_rv_figure([], 22, "", aggregate=False), config={"displayModeBar": True}), md=6),
            ],
            className="g-2 mt-1",
        ),
        build_gex_rr_charts_row(),
    ]


def _build_bottom_panel_shell() -> html.Div:
    """The fixed-position bottom analytics panel.

    The whole full-width header strip is the expand/collapse toggle. Inside the
    collapsible body, the 5-button window toggle and the dynamic
    ``bottom-panels-container`` are siblings — the window toggle is **static** (lives
    here, not inside the dynamic container) so re-rendering the content never
    destroys it. Wrapper z-index sits above the sticky tab bars (1200 / 1190).
    """
    return html.Div(
        [
            # Single full-width header strip = the Analytics expand/collapse toggle: the ▲/▼
            # label + the live alert marquee (owned solely by the alerts callback, so the toggle —
            # which rewrites only the label span — never clobbers it).
            dbc.Button(
                html.Div(
                    [
                        html.Span("▲ Analytics Panel", id="bottom-panel-toggle-label", className="text-nowrap flex-shrink-0", style={"fontSize": "0.9rem", "fontWeight": 600}),
                        html.Div(id="bottom-panel-ticker", className="oc-ticker ms-3 flex-grow-1"),
                    ],
                    className="d-flex align-items-center w-100",
                ),
                id="bottom-panel-toggle-button",
                color="dark",
                className="w-100 text-start",
                style={"borderRadius": "0"},
            ),
            # Panel body splits horizontally: left 75% = the analytics rows (window toggle +
            # bottom-panels-container, own scroll); right 25% = the per-pair change feed
            # (`bottom-panel-details-content`, independent scroll).
            dbc.Collapse(
                html.Div(
                    [
                        html.Div(
                            [
                                # The window selector governs ONLY the IV/RV surfaces — the Vol
                                # Analytics metric row + the IV-vs-RV chart. Labeled as such so it
                                # never reads as a global control (it isn't one).
                                html.Div(
                                    [
                                        html.Span("IV/RV Window", className="oc-panel-section-title mb-0 me-2"),
                                        dbc.RadioItems(
                                            id="bottom-panel-window-selector",
                                            options=[
                                                {"label": "22D", "value": "22"},
                                                {"label": "90D", "value": "90"},
                                                {"label": "180D", "value": "180"},
                                                {"label": "252D", "value": "252"},
                                            ],
                                            value="22",
                                            class_name="btn-group btn-group-sm",
                                            input_class_name="btn-check",
                                            label_class_name="btn btn-sm",
                                            label_checked_class_name="active",
                                        ),
                                    ],
                                    className="d-flex align-items-center mb-2",
                                ),
                                html.Div(id="bottom-panels-container"),
                                # Lazy-mount point for the 4 charts (Price | IV-RV, Aggregate-GEX | RR).
                                # Empty at cold load; `mount_bottom_charts` injects `_build_bottom_charts()`
                                # on the panel's first open so Plotly isn't initialized until first use
                                # (lazy mount). Figures are then swapped live by the feature callbacks.
                                html.Div(id="bottom-charts-mount"),
                            ],
                            style={"width": "75%", "overflowY": "auto", "maxHeight": "80vh"},
                            className="p-2",
                        ),
                        html.Div(
                            [
                                html.Div("Changes", className="fw-bold small text-light mb-1"),
                                html.Div(id="bottom-panel-details-content"),
                            ],
                            style={"width": "25%", "overflowY": "auto", "maxHeight": "80vh", "borderLeft": "1px solid rgba(255,255,255,0.15)"},
                            className="p-2",
                        ),
                    ],
                    className="d-flex",
                ),
                id="bottom-panel-collapse",
                is_open=False,
            ),
        ],
        style={
            "position": "fixed",
            "bottom": "0",
            "left": "0",
            "width": "100%",
            "zIndex": "2000",
            "backgroundColor": "rgba(10,15,30,0.97)",
            "backdropFilter": "blur(10px)",
            "boxShadow": "0 -8px 28px rgba(0,0,0,0.5)",
            "borderTop": "1px solid #1e2a45",
        },
    )


_PB_DRAWER_HEIGHT = "190px"


def _playback_arrow_style(is_open: bool) -> dict:
    """Arrow rides the drawer's top edge when open; when closed it sits flush on top of the
    analytics-panel header (no gap), horizontally centered. ``is_open`` is kept in sync by
    ``sync_playback_arrow``."""
    return {
        "position": "fixed",
        # Closed: 34px — ~1px above the ~33px analytics-header top, so the arrow's bottom edge clears
        # the header (not tucked under) while still reading as attached. Open: rides the drawer's top edge.
        "bottom": _PB_DRAWER_HEIGHT if is_open else "34px",
        # Centered on the viewport — with the label gone the bare arrowhead itself centers (it no
        # longer sits left of a "Playback" text block), landing under the chain's central strike column.
        "left": "50%",
        "transform": "translateX(-50%)",
        # Below the bottom analytics panel (2000) so an open panel covers the arrow instead of the
        # arrow obscuring the panel header (matches the side-drawer arrows at 1900). The playback
        # drawer and the panel are mutually exclusive, so hiding the arrow under the open panel costs
        # nothing — closing the panel brings it back.
        "zIndex": 1900,
        "borderRadius": "6px 6px 0 0",
        # Wide-and-short shape so this bottom tab matches the sleek side-drawer tabs rotated 90°:
        # small vertical padding + lineHeight 1 keep the *protrusion* (upward) thin, while the larger
        # horizontal padding runs it wide along the bottom edge. Shape only — the button grows upward
        # from its fixed `bottom` anchor, so it stays flush with the panel header (no gap), and zIndex
        # is untouched (still hides behind the expanded panel).
        "padding": "3px 16px",
        "lineHeight": "1",
        # Animate the bottom shift so the arrow tracks the Offcanvas slide (mirrors the
        # required-move drawer's `transition: right 0.3s ease`) instead of snapping.
        "transition": "bottom 0.3s ease",
    }


def _build_playback_drawer_shell() -> html.Div:
    """Bottom slide-in Playback drawer + its always-visible arrow toggle.

    The toggle pokes out just above the analytics-panel header; clicking it slides the Offcanvas up
    as an overlay (``backdrop=False`` so live shows behind, no layout push). Open/close is owned by
    ``toggle_bottom_overlays`` (mutually exclusive with the analytics panel). Fixed compact height
    (no 30vh void) so the arrow can attach to the drawer's top edge when open. Closing the drawer
    does NOT return to live — only the in-drawer Back to Live button does.
    """
    toggle = dbc.Button(
        "▴",
        id="playback-drawer-toggle",
        n_clicks=0,
        color="dark",
        size="sm",
        style=_playback_arrow_style(False),
    )
    drawer = dbc.Offcanvas(
        build_playback_drawer_body(),
        id="playback-drawer",
        title=html.Span("Playback", style={"fontSize": "0.9rem", "fontWeight": 600, "whiteSpace": "nowrap"}),
        placement="bottom",
        is_open=False,
        backdrop=False,
        scrollable=False,
        # No header X — the arrow toggle is the single open/close control (the X was redundant).
        close_button=False,
        style={"height": _PB_DRAWER_HEIGHT, "zIndex": 2200},
    )
    return html.Div([toggle, drawer])


def register_shell_callbacks(app, services: RuntimeServices) -> None:
    """Register shell-level rendering and workflow callbacks."""

    @app.callback(
        Output("controls-collapse", "is_open"),
        Output("controls-collapse-chevron", "children"),
        Input("controls-collapse-button", "n_clicks"),
        State("controls-collapse", "is_open"),
    )
    def toggle_controls(n_clicks: int, is_open: bool):
        new_open = (not is_open) if n_clicks else is_open
        return new_open, ("▾" if new_open else "▸")

    @app.callback(
        Output("tab-content", "children"),
        Input("main-tabs", "active_tab"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("refresh-state-store", "data"),
        # Heatmap toggle is an Input (unlike Greeks) — it swaps the whole Chain view, so it *should*
        # rebuild on flip (Tier-2 Layer 3).
        Input("rate-heatmap-toggle", "value"),
        # Meta is the trigger; rows (the bulk) are read as State and merged back. They are
        # written together by every writer, so the State read is never stale.
        State("loaded-report-rows-store", "data"),
        # Greeks is a State, not an Input: toggling it must NOT rebuild the whole chain. A separate
        # lighter callback (`swap_greeks_columns`) patches just the table's columns + styles. On a
        # data tick this still rebuilds with the current greeks state.
        State("greeks-toggle", "value"),
    )
    def render_tab_content(
        active_tab: str,
        loaded_report_meta: dict | None,
        active_view_context: dict | None,
        refresh_state_data: dict | None,
        show_heatmap: bool | None,
        loaded_report_rows: dict | None,
        show_greeks: bool | None,
    ):
        loaded_state = LoadedReportState.from_meta_and_rows(loaded_report_meta, loaded_report_rows)
        active_context = ActiveViewContext.from_dict(active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict())
        refresh_state = RefreshState.from_dict(refresh_state_data)
        active_pair_key = active_context.pair_key

        if active_tab == "chain-tab":
            aggregate_badge = (
                html.Span("Aggregate Mode", className="oc-agg-badge d-inline-block mb-2")
                if loaded_state is not None
                and is_aggregate_eligible(loaded_state)
                and active_context.kind == "aggregate"
                else None
            )
            is_live_pair = active_context.source == "live" and active_context.kind == "pair"
            # Tier-2 Layer 3 — the full rate heatmap view (toggle beside Greeks); live + per-pair only.
            if bool(show_heatmap) and is_live_pair:
                resolved = resolve_chain_report(
                    loaded_state, active_pair_key, refresh_state.last_strike_range, active_context
                )
                if resolved is not None:
                    report, display_rows = resolved
                    crossings = _rate_crossings_for(services, active_context.pair_key)
                    return html.Div([aggregate_badge, build_rate_heatmap_table(report, crossings, display_rows)])
            # Tier-2 Layer-1 rate markers — live + per-pair only (blank in aggregate/playback, like
            # the rest); computed from the server-side ring for the active pair.
            rate_markers = _compute_rate_markers(services, active_context.pair_key) if is_live_pair else None
            # Interpretor read — live + per-pair only (blank in aggregate/playback); absent in the
            # Greeks / Surface views by construction (their LTP/OI/Straddle columns don't exist).
            interpretor = _interpretor_read_for(services, active_context.pair_key) if is_live_pair else None
            # A2 09:20 per-strike hover snapshot — same live + per-pair gating as the interpretor read.
            hover = _hover_snapshot_for(services, active_context.pair_key) if is_live_pair else None
            chain_tab = build_chain_tab(
                loaded_state,
                active_pair_key,
                refresh_state.last_strike_range,
                services.view_data_service,
                active_view_context=active_context,
                show_greeks=bool(show_greeks),
                rate_markers=rate_markers,
                interpretor=interpretor,
                hover=hover,
            )
            return html.Div([aggregate_badge, chain_tab])
        # Charts and Advanced each live in their own persistent container. Charts holds the
        # distribution grid (rendered by render_charts_distribution on tab-entry/pair/strike,
        # never on ticks) + the merged Time-Series section; Advanced is manual-Run-only.
        # tab-content stays empty for both.
        return html.Div()

    @app.callback(
        Output("chain-option-table", "columns"),
        Output("chain-option-table", "style_data_conditional"),
        Input("greeks-toggle", "value"),
        State("loaded-report-meta-store", "data"),
        State("loaded-report-rows-store", "data"),
        State("active-view-context-store", "data"),
        State("refresh-state-store", "data"),
        State("rate-heatmap-toggle", "value"),
        prevent_initial_call=True,
    )
    def swap_greeks_columns(show_greeks, loaded_report_meta, loaded_report_rows, active_view_context, refresh_state_data, show_heatmap):
        # In the Layer-3 heatmap view the chain-option-table isn't mounted — nothing to patch.
        if show_heatmap:
            return no_update, no_update
        # Greeks-without-rebuild: flipping the toggle patches only the chain table's columns +
        # conditional styles (the data — identical between views — stays put), instead of rebuilding
        # the whole tab. Re-resolves the same report + windowed rows the chain was built from, then
        # recomputes the two greeks-dependent props. If no table/data, no-op (and if the table isn't
        # mounted — e.g. on another tab — Dash drops the patch; returning to Chain rebuilds via
        # render_tab_content with the current greeks State).
        loaded_state = LoadedReportState.from_meta_and_rows(loaded_report_meta, loaded_report_rows)
        active_context = ActiveViewContext.from_dict(active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict())
        refresh_state = RefreshState.from_dict(refresh_state_data)
        resolved = resolve_chain_report(loaded_state, active_context.pair_key, refresh_state.last_strike_range, active_context)
        if resolved is None:
            return no_update, no_update
        report, display_rows = resolved
        final_table_data = build_chain_table_data(report, view_data_service=services.view_data_service, display_rows=display_rows)
        columns, style_data_conditional = build_chain_table_style(final_table_data, report, bool(show_greeks), display_rows=display_rows)
        return columns, style_data_conditional

    @app.callback(
        Output("charts-container", "style"),
        Input("main-tabs", "active_tab"),
    )
    def toggle_charts_container(active_tab: str):
        return {"display": "block"} if active_tab == "charts-tab" else {"display": "none"}

    @app.callback(
        Output("charts-refresh-token", "data"),
        Input("main-tabs", "active_tab"),
        Input("active-view-context-store", "data"),
        Input("strike-range-selector", "value"),
        State("charts-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def compute_charts_refresh_token(active_tab, active_view_context, strike_range, prev):
        # The single "should the Charts tab refresh?" decision. Emits a new token (with a bumped
        # counter) on tab-entry / active-pair switch / strike-range change, and **ignores
        # auto-refresh ticks** — an active-view-context change that only bumped fetch_group_id
        # (same pair_key + strike_range) is not a refresh event. All Charts-tab content callbacks
        # key off this token, so none of them re-render on a tick.
        if active_tab != "charts-tab":
            return no_update
        context = ActiveViewContext.from_dict(
            active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict()
        )
        prev = prev or {}
        trigger_ids = {t["prop_id"].split(".")[0] for t in (callback_context.triggered or [])}
        if "main-tabs" not in trigger_ids and (
            prev.get("pair_key") == context.pair_key and prev.get("strike_range") == strike_range
        ):
            # Not a tab-entry, and neither pair nor strike changed → a bare tick. Ignore.
            return no_update
        return {"pair_key": context.pair_key, "strike_range": strike_range, "n": int(prev.get("n", 0)) + 1}

    @app.callback(
        Output("charts-distribution-content", "children"),
        Input("charts-refresh-token", "data"),
        State("loaded-report-meta-store", "data"),
        State("loaded-report-rows-store", "data"),
        State("refresh-state-store", "data"),
        State("active-view-context-store", "data"),
        prevent_initial_call=True,
    )
    def render_charts_distribution(_token, loaded_report_meta, loaded_report_rows, refresh_state_data, active_view_context):
        # Fires only when the refresh token changes (tab-entry / pair-switch / strike-change),
        # never on ticks. Reads the latest data as State.
        loaded_state = LoadedReportState.from_meta_and_rows(loaded_report_meta, loaded_report_rows)
        active_context = ActiveViewContext.from_dict(
            active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict()
        )
        refresh_state = RefreshState.from_dict(refresh_state_data)
        return build_charts_tab(
            loaded_state,
            active_context.pair_key,
            refresh_state.last_strike_range,
            active_view_context=active_context,
        )

    # VIX indicator — heartbeat-driven (NOT the 10s tick / the 330 KB loaded store). vix-heartbeat-store
    # is bumped by the render poll (live_core.render_live_data) when the server collector fetches a
    # newer VIX, so the readout updates within a render tick of a fetch. BS pricing reads vix_status()
    # fresh per wave regardless of this display callback. Trade-off (accepted): with nothing being
    # collected, a failed VIX leaves the readout on "No VIX available yet" until the next successful fetch.
    @app.callback(
        Output("vix-status-indicator", "children"),
        Output("vix-status-indicator", "style"),
        Input("vix-heartbeat-store", "data"),
    )
    def render_vix_indicator(_vix_heartbeat: dict | None):
        status = services.vix_service.vix_status()
        if status.state == "live":
            clock = _format_clock(status.timestamp, "%H:%M")
            vix_text = f"VIX {status.value:.1f}%" + (f" @ {clock}" if clock else "")
        elif status.state == "default":
            vix_text = f"VIX {status.value:.1f}% (default)"
        else:
            vix_text = "No VIX available yet"
        vix_style = {"color": _INDICATOR_COLORS.get(status.state, "#8a97b5"), "fontWeight": 600}
        return vix_text, vix_style

    # Lot size + chain timestamp — data-driven (new fetch / sub-tab switch only, never on a timer).
    @app.callback(
        Output("lot-size-indicator", "children"),
        Output("chain-fetch-indicator", "children"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        State("refresh-state-store", "data"),
    )
    def render_pair_indicators(
        loaded_report_state: dict | None,
        active_view_context: dict | None,
        refresh_state_data: dict | None,
    ):
        loaded_state = LoadedReportState.from_dict(loaded_report_state) if loaded_report_state else None
        lot_text = "Lot Size: —"
        chain_text = "Chain — no data"
        if loaded_state is not None:
            active_context = ActiveViewContext.from_dict(active_view_context) if active_view_context else None
            active_pair_key = active_context.pair_key if active_context is not None else None
            active_pair = get_active_loaded_pair(loaded_state, active_pair_key)
            if active_pair is not None:
                lot_size = active_pair.report.get("lot_size")
                if lot_size:
                    lot_text = f"Lot Size: {int(lot_size)}"
            stamp = active_pair.report.get("capture_timestamp") if active_pair is not None else None
            if not stamp:
                stamp = RefreshState.from_dict(refresh_state_data).last_fetch_timestamp
            clock = _format_clock(stamp, "%H:%M:%S")
            if clock:
                chain_text = f"Chain @ {clock}"
        return lot_text, chain_text

    # Per-tab scroll memory (restore side). Capture + the actual scrolling live in
    # assets/scroll_memory.js; this hands the JS the active (tab|pair) key after each
    # main-tab / sub-tab switch and content re-render. Keyed off active-view-context-store
    # (always present, unlike loaded-pair-tabs which is absent in aggregate mode).
    app.clientside_callback(
        """
        function(activeTab, viewContext, _children) {
            if (window.ocScrollRestore && activeTab) {
                var pairKey = (viewContext && viewContext.pair_key) ? viewContext.pair_key : "";
                window.ocScrollRestore(activeTab + "|" + pairKey);
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("scroll-memory-sink", "data"),
        Input("main-tabs", "active_tab"),
        Input("active-view-context-store", "data"),
        Input("tab-content", "children"),
        prevent_initial_call=False,
    )

    # Strip the `href="#"` off the dbc.Tabs nav-links (primary + secondary pair tabs) so
    # the browser stops showing "localhost:8053/#" on hover and stops appending `#` on
    # click. dbc handles tab switching via its own click listener, not the anchor href,
    # so removing href doesn't affect tab switching. Re-runs whenever the tab bars
    # re-render (the pair row is rebuilt on loaded-state / view-context changes).
    app.clientside_callback(
        """
        function(_activeTab, _pairRow) {
            setTimeout(function() {
                document.querySelectorAll(
                    '#main-tabs a.nav-link, #loaded-pair-tabs a.nav-link'
                ).forEach(function(a) {
                    a.removeAttribute('href');
                    a.style.cursor = 'pointer';
                });
            }, 0);
            return window.dash_clientside.no_update;
        }
        """,
        Output("tab-href-sink", "data"),
        Input("main-tabs", "active_tab"),
        Input("loaded-pair-tabs-row", "children"),
        prevent_initial_call=False,
    )

    # Critical-alert sound. The alerts callback bumps `critical-alert-signal-store` (monotonic int)
    # only on a NEW critical alert for the active pair; this plays a short two-tone beep via WebAudio
    # (a module-scoped AudioContext, resumed on the signal). No binary asset — self-contained, no CDN.
    # Browsers suspend audio until the first user gesture, so the very first beep after a cold load may
    # be swallowed (the try/catch and resume().catch() absorb it) — it works for the session after one
    # click. Nothing plays on page load (prevent_initial_call) or when the signal is falsy/unchanged.
    app.clientside_callback(
        """
        function(signal) {
            if (!signal) { return window.dash_clientside.no_update; }
            try {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (!AC) { return window.dash_clientside.no_update; }
                if (!window.ocAlertAudioCtx) { window.ocAlertAudioCtx = new AC(); }
                var ctx = window.ocAlertAudioCtx;
                if (ctx.state === 'suspended') { ctx.resume().catch(function(){}); }
                var t0 = ctx.currentTime;
                [[880, 0.0], [1320, 0.14]].forEach(function(step) {
                    var osc = ctx.createOscillator();
                    var gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = step[0];
                    var start = t0 + step[1];
                    gain.gain.setValueAtTime(0.0001, start);
                    gain.gain.exponentialRampToValueAtTime(0.2, start + 0.01);
                    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.12);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(start);
                    osc.stop(start + 0.13);
                });
            } catch (e) { /* autoplay blocked until first gesture, or no WebAudio */ }
            return window.dash_clientside.no_update;
        }
        """,
        Output("alert-sound-sink", "data"),
        Input("critical-alert-signal-store", "data"),
        prevent_initial_call=True,
    )

    # Tier-2 rate-cluster sound — the cluster detector bumps `rate-sound-signal-store` (already
    # cooldown + throttle gated server-side); this reuses the same WebAudio two-tone beep.
    app.clientside_callback(
        """
        function(signal) {
            if (!signal) { return window.dash_clientside.no_update; }
            try {
                var AC = window.AudioContext || window.webkitAudioContext;
                if (!AC) { return window.dash_clientside.no_update; }
                if (!window.ocAlertAudioCtx) { window.ocAlertAudioCtx = new AC(); }
                var ctx = window.ocAlertAudioCtx;
                if (ctx.state === 'suspended') { ctx.resume().catch(function(){}); }
                var t0 = ctx.currentTime;
                [[880, 0.0], [1320, 0.14]].forEach(function(step) {
                    var osc = ctx.createOscillator();
                    var gain = ctx.createGain();
                    osc.type = 'sine';
                    osc.frequency.value = step[0];
                    var start = t0 + step[1];
                    gain.gain.setValueAtTime(0.0001, start);
                    gain.gain.exponentialRampToValueAtTime(0.2, start + 0.01);
                    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.12);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(start);
                    osc.stop(start + 0.13);
                });
            } catch (e) { /* autoplay blocked until first gesture, or no WebAudio */ }
            return window.dash_clientside.no_update;
        }
        """,
        Output("rate-sound-sink", "data"),
        Input("rate-sound-signal-store", "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("pair-row-signature-store", "data"),
        Input("request-plan-store", "data"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        State("pair-row-signature-store", "data"),
        prevent_initial_call=False,
    )
    def compute_pair_row_signature(request_plan_state, loaded_report_state, active_view_context, prev_sig):
        # The secondary pair-row only changes when the *structure* changes — the pair set, the
        # active pair (highlight), or aggregate-vs-pair kind. A same-pairs auto-refresh tick bumps
        # `loaded-report-meta-store`'s version but leaves all of these identical. So derive a cheap
        # signature here and only let it change when one of those does; `render_secondary_pair_row`
        # keys off this, so the sub-tabs are NOT re-created under the user's click every tick.
        request_plan = RequestPlanState.from_dict(request_plan_state)
        loaded_state = LoadedReportState.from_dict(loaded_report_state) if loaded_report_state else None
        active_context = ActiveViewContext.from_dict(active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict())
        pair_order = list(loaded_state.pair_order) if loaded_state else list(request_plan.pair_order)
        signature = {
            "kind": active_context.kind,
            "pair_order": pair_order,
            "active_pair_key": active_context.pair_key,
        }
        if signature == prev_sig:
            return no_update
        return signature

    @app.callback(
        Output("loaded-pair-tabs-row", "children"),
        Input("pair-row-signature-store", "data"),
        State("request-plan-store", "data"),
        State("loaded-report-meta-store", "data"),
        State("active-view-context-store", "data"),
    )
    def render_secondary_pair_row(
        _signature: dict | None,
        request_plan_state: dict | None,
        loaded_report_state: dict | None,
        active_view_context: dict | None,
    ):
        # Fires only when `pair-row-signature-store` changes (structure change), never on a
        # same-pairs tick — so the sub-tabs component stays put and clicks aren't clobbered.
        request_plan = RequestPlanState.from_dict(request_plan_state)
        loaded_state = LoadedReportState.from_dict(loaded_report_state) if loaded_report_state else None
        active_context = ActiveViewContext.from_dict(active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict())
        if active_context.kind == "aggregate":
            # Hidden shell, NOT a bare html.Div(): keep `loaded-pair-tabs` MOUNTED — it's a `sync_active_view_context` Input, and unmounting it (under suppress_callback_exceptions) jams the off-toggle.
            return build_pair_navigation(
                request_plan_state=None,
                loaded_report_state=None,
                active_pair_key=active_context.pair_key,
            )
        return build_pair_navigation(
            request_plan_state=request_plan,
            loaded_report_state=loaded_state,
            active_pair_key=active_context.pair_key,
        )

    @app.callback(
        Output("active-view-context-store", "data"),
        Input("loaded-pair-tabs", "active_tab"),
        Input("request-plan-store", "data"),
        Input("loaded-report-meta-store", "data"),
        Input("control-draft-store", "data"),
        State("active-view-context-store", "data"),
        prevent_initial_call=False,
    )
    def sync_active_view_context(
        selected_pair_key: str | None,
        request_plan_state: dict | None,
        loaded_report_state: dict | None,
        control_draft_state: dict | None,
        existing_context: dict | None,
    ):
        _ = RequestPlanState.from_dict(request_plan_state)
        loaded_state = LoadedReportState.from_dict(loaded_report_state) if loaded_report_state else None
        draft_state = ControlDraftState.from_dict(control_draft_state)

        if loaded_state is None:
            return build_active_view_context_from_pair_state(
                source="idle",
                pair_key=None,
                metadata={"reason": "no_loaded_state"},
            ).to_dict()

        source = loaded_state.source
        fetch_group_id = loaded_state.fetch_group_id
        available_pair_keys = list(loaded_state.pair_order)

        aggregate_enabled = (
            draft_state.request_mode == "single_instrument"
            and bool(draft_state.toggles.get("aggregate_mode", False))
            and loaded_state is not None
            and is_aggregate_eligible(loaded_state)
        )
        if aggregate_enabled and loaded_state is not None:
            return build_active_view_context_from_aggregate_state(
                source=source,
                instrument_key=loaded_state.loaded_pairs[0].request.instrument_key.value,
                aggregate_view_key=f"aggregate|{loaded_state.loaded_pairs[0].request.instrument_key.value}",
                fetch_group_id=fetch_group_id,
                metadata={"pair_count": len(loaded_state.loaded_pairs)},
            ).to_dict()

        current_pair_key = selected_pair_key or (existing_context or {}).get("pair_key")
        resolved_pair_key = current_pair_key if current_pair_key in available_pair_keys else (available_pair_keys[0] if available_pair_keys else None)
        return build_active_view_context_from_pair_state(
            source=source,
            pair_key=resolved_pair_key,
            fetch_group_id=fetch_group_id,
            metadata={"pair_count": len(available_pair_keys)},
        ).to_dict()

    @app.callback(
        Output("bottom-panel-collapse", "is_open"),
        Output("playback-drawer", "is_open"),
        Output("bottom-panel-toggle-label", "children"),
        Input("bottom-panel-toggle-button", "n_clicks"),
        Input("playback-drawer-toggle", "n_clicks"),
        State("bottom-panel-collapse", "is_open"),
        State("playback-drawer", "is_open"),
        prevent_initial_call=True,
    )
    def toggle_bottom_overlays(_panel_clicks, _drawer_clicks, panel_open, drawer_open):
        # The Analytics panel (full-width) and the Playback drawer both overlay the bottom, so they
        # are mutually exclusive: opening one closes the other. (Closing the drawer does NOT return
        # to live — only the in-drawer Back to Live button does.)
        trigger = callback_context.triggered_id
        if trigger == "playback-drawer-toggle":
            new_drawer = not drawer_open
            new_panel = False if new_drawer else panel_open
        else:
            new_panel = not panel_open
            new_drawer = False if new_panel else drawer_open
        panel_label = "▼ Analytics Panel" if new_panel else "▲ Analytics Panel"
        return new_panel, new_drawer, panel_label

    # Lazy-mount the bottom-panel charts on the panel's first open. Until then
    # `bottom-charts-mount` is empty, so Plotly isn't initialized at cold load. Setting the flag also
    # re-fires the chart figure callbacks (they take `bottom-charts-mounted` as an Input), so the
    # freshly-mounted graphs paint live data — not their placeholder — on that first open.
    @app.callback(
        Output("bottom-charts-mount", "children"),
        Output("bottom-charts-mounted", "data"),
        Input("bottom-panel-collapse", "is_open"),
        State("bottom-charts-mounted", "data"),
        prevent_initial_call=True,
    )
    def mount_bottom_charts(panel_open, already_mounted):
        if not panel_open or already_mounted:
            return no_update, no_update
        return _build_bottom_charts(), True

    # Same lazy mount for the two left-drawer metric charts, gated on the drawer's first open.
    @app.callback(
        Output("metric-charts-mount", "children"),
        Output("metric-charts-mounted", "data"),
        Input("metric-drawer", "is_open"),
        State("metric-charts-mounted", "data"),
        prevent_initial_call=True,
    )
    def mount_metric_charts(drawer_open, already_mounted):
        if not drawer_open or already_mounted:
            return no_update, no_update
        return build_metric_charts(), True

    @app.callback(
        Output("playback-drawer-toggle", "children"),
        Output("playback-drawer-toggle", "style"),
        Input("playback-drawer", "is_open"),
    )
    def sync_playback_arrow(is_open):
        # Keep the arrow's glyph + position synced to the drawer however it opens/closes (arrow or
        # the Offcanvas X), so it rides the drawer's top edge when open and never goes stale.
        return ("▾" if is_open else "▴"), _playback_arrow_style(bool(is_open))

    @app.callback(
        Output("mark-popover", "is_open"),
        Output("mark-name-input", "value", allow_duplicate=True),
        Input("mark-open-button", "n_clicks"),
        State("mark-popover", "is_open"),
        prevent_initial_call=True,
    )
    def toggle_mark_popover(_clicks, is_open):
        # Toggle the Mark popover; pre-fill the name with the current IST wall-clock time on open
        # (editable). `save_current_view` clears the field on a successful save.
        new_open = not is_open
        return new_open, (format_mark_time_label() if new_open else no_update)

    @app.callback(
        Output("bottom-panel-details-content", "children"),
        Input("alerts-state-store", "data"),
        Input("active-view-context-store", "data"),
        Input("bottom-panel-collapse", "is_open"),
        prevent_initial_call=True,
    )
    def render_details_feed(alerts_state, active_view_context, is_open):
        # Fills the right 25% of the Analytics panel with the active pair's change feed. Skips work
        # while the panel is closed; a live refresh updates the feed without touching the toggle.
        if not is_open:
            return no_update
        context = ActiveViewContext.from_dict(
            active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict()
        )
        if context.source != "live" or context.kind == "aggregate":
            return html.Div("Change feed is available in live single-pair view.", className="oc-change-empty small p-2")
        # Pinned interpretor block (streak + 09:20 walls) above the feed — shown whenever live+per-pair,
        # regardless of whether any alert changes have been recorded.
        pinned = _build_interp_pinned(_interpretor_read_for(services, context.pair_key))
        full = ((alerts_state or {}).get("alerts", {}) or {}).get(context.pair_key, []) or []
        feed = build_details_feed(full)
        if not feed:
            feed_block = html.Div("No changes recorded for this pair yet today.", className="oc-change-empty small p-2")
        else:
            # Tie any highest ⇄ 2nd-highest positional swap into one visually-bound group (thin left
            # bar); every other change renders as a flat row, in order.
            feed_block = html.Div(
                [
                    _build_change_group(payload) if kind == "group" else _build_change_row(payload)
                    for kind, payload in group_swap_rows(feed)
                ],
                className="oc-change-feed",
            )
        return html.Div([block for block in (pinned, feed_block) if block is not None])

    @app.callback(
        Output("bottom-panels-container", "children"),
        Input("loaded-report-meta-store", "data"),
        Input("active-view-context-store", "data"),
        Input("bottom-panel-window-selector", "value"),
        Input("advanced-report-store", "data"),
        Input("bottom-panel-collapse", "is_open"),
        State("loaded-report-rows-store", "data"),
    )
    def render_bottom_panels(
        loaded_report_meta: dict | None,
        active_view_context: dict | None,
        window_value: str | None,
        advanced_report_data: dict | None,
        panel_open: bool,
        loaded_report_rows: dict | None,
    ):
        # Gate: skip the metric-row build while the analytics panel is collapsed. `is_open` is an
        # Input (not State) so opening the panel re-fires this and renders fresh.
        if not panel_open:
            return no_update
        # rows needed for aggregate mode (resolve_panel_context → build_aggregate_report).
        loaded_state = LoadedReportState.from_meta_and_rows(loaded_report_meta, loaded_report_rows)
        active_context = ActiveViewContext.from_dict(active_view_context or ActiveViewContext(kind="pair", source="idle").to_dict())
        return build_bottom_panels(
            loaded_report_state=loaded_state,
            active_pair_key=active_context.pair_key,
            view_data_service=services.view_data_service,
            active_view_context=active_context,
            vol_window=int(window_value or 22),  # 22 = the shortest real window (22/90/180/252)
            advanced_report_data=advanced_report_data,
        )
