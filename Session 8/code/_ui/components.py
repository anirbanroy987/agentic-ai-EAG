"""Reusable presentational widgets.

Everything here is pure rendering — no I/O, no state mutation. Pass in
the data, get back HTML (rendered via st.markdown) or a Streamlit
control. Keeps the page modules focused on layout rather than markup.
"""

from __future__ import annotations

import time
from typing import Iterable

import streamlit as st


def badge(status: str) -> str:
    """Return a status pill (HTML string).

    Statuses recognised: running, complete, failed, pending, skipped, idle.
    Unknown → pending.
    """
    status = (status or "idle").lower()
    valid = {"running", "complete", "failed", "pending", "skipped", "idle"}
    if status not in valid:
        status = "pending"
    return (
        f'<span class="badge badge-{status}">'
        f'<span class="badge-dot"></span>{status}'
        f'</span>'
    )


def metric_card(label: str, value: str, *, delta: str | None = None,
                positive: bool = False) -> str:
    """A single metric card. Numbers right-aligned tabular-nums via CSS."""
    delta_html = ""
    if delta:
        cls = "metric-delta positive" if positive else "metric-delta"
        delta_html = f'<div class="{cls}">{delta}</div>'
    return (
        '<div class="metric-card">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div>'
        f'{delta_html}'
        '</div>'
    )


def metric_row(metrics: Iterable[tuple[str, str, str | None, bool]]) -> None:
    """Render a row of metric cards via Streamlit columns.

    Each tuple is (label, value, delta, positive).
    """
    metrics = list(metrics)
    if not metrics:
        return
    cols = st.columns(len(metrics))
    for col, (label, value, delta, positive) in zip(cols, metrics):
        with col:
            st.markdown(
                metric_card(label, value, delta=delta, positive=positive),
                unsafe_allow_html=True,
            )


def section_title(text: str, *, eyebrow: str | None = None) -> None:
    """A section heading with an optional small uppercase label above."""
    if eyebrow:
        st.markdown(
            f'<div class="metric-label" style="margin-bottom:4px">{eyebrow}</div>',
            unsafe_allow_html=True,
        )
    st.markdown(f"### {text}")


def hero_header(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="hero-title">{title}</div>'
        f'<div class="hero-subtitle">{subtitle}</div>',
        unsafe_allow_html=True,
    )


def fmt_time(t: float | None) -> str:
    """Epoch seconds → HH:MM:SS local time, or em-dash."""
    if not t:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(t))


def fmt_dur(s: float | None) -> str:
    """Seconds → e.g. `4.23 s` or `1m 12s`, or em-dash."""
    if s is None or s < 0:
        return "—"
    if s < 60:
        return f"{s:.2f} s"
    m, s2 = divmod(s, 60)
    return f"{int(m)}m {s2:.0f}s"


def toast(message: str, *, icon: str = "✓") -> None:
    """Wrapper around st.toast so callers don't depend on the API directly."""
    try:
        st.toast(f"{icon}  {message}")
    except Exception:
        # st.toast is recent; degrade gracefully on older Streamlit
        st.info(f"{icon}  {message}")
