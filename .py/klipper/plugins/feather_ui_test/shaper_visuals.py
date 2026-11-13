# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

"""Synthetic shaper screens for framebuffer capture; no measurement or config writes."""

CASES = (
    "start", "start-standard", "history", "progress", "processing-y", "cancelling",
    "graph", "graph-log-reference", "graph-single-axis", "table", "reference",
    "fullscreen", "fullscreen-log", "save", "leave",
)


def show(catalog, kind):
    from copy import deepcopy
    from ff5m_ui.shaper import history, progress, start
    from ff5m_ui.shaper.actions import ShaperAction
    from ff5m_ui.shaper.results import ShaperResults
    from ff5m_ui.shaper.state import ShaperState
    from ff5m_ui.screen import ScreenPage
    from ui import StateStore

    entries = [dict(id="visual-%d" % i, saved_at=1793613222. - i * 86400,
                    axes=["x", "y"] if i % 2 == 0 else ["y"], latest=i == 0) for i in range(5)]
    # (latest saved entry, sweeping): together the two cases show each start input both ways.
    start_inputs = {"start": (entries[0], True), "start-standard": (None, False)}
    if kind in start_inputs:
        catalog._show_component(deepcopy(start.PAGE), start.values(*start_inputs[kind]))
        return
    if kind == "history":
        catalog._show_component(deepcopy(history.PAGE), {history.HistoryState.ENTRIES: entries})
        return
    if kind in ("progress", "processing-y", "cancelling"):
        status = dict(state="processing" if kind == "processing-y" else "measuring",
                      axis="y" if kind == "processing-y" else "x", frequency=43.,
                      requested_axes=("x", "y"), completed_axes=("x",) if kind == "processing-y" else (),
                      freq_start=5., freq_end=100., samples=32000, elapsed=80, cancel_requested=kind == "cancelling")
        catalog._show_component(deepcopy(progress.PAGE), progress.values(status))
        return
    if kind not in CASES:
        raise ValueError("Unknown shaper visual case: %s" % kind)

    result = StateStore(tuple(ShaperState))[ShaperState.RESULT]
    results = {axis: result for axis in (("x",) if kind == "graph-single-axis" else ("x", "y"))}
    selections = {axis: {"name": "mzv", "freq": 63.4} for axis in results}
    view = ShaperResults()
    view.write(results, selections)
    if kind in ("graph-log-reference", "fullscreen-log"):
        view.select(ShaperAction("scale"))
    if kind == "fullscreen-log":
        view.select(ShaperAction("axis", "y"))
    if kind == "reference":
        view.select(ShaperAction("method", "zvd"))
    if kind == "table":
        view.select(ShaperAction("view", "table"))
    elif kind.startswith("fullscreen"):
        view.select(ShaperAction("view", "fullscreen"))
    previous = None
    if kind == "graph-log-reference":
        previous = {"x": dict(result, psd_sum=[value * .6 for value in result["psd_sum"]])}
    tree = view.write(results, selections, saved_at=1793613222., reference=previous,
                      reference_at=1793526822. if previous else None)
    catalog._show_component(tree, tree.state.as_dict())
    if kind in ("save", "leave"):
        from feather.calibration.shaper import confirmation_dialog
        title, message, actions = confirmation_dialog(kind, selections)
        catalog.host._show_message(message, ScreenPage.UI_TEST_PREVIEW, title=title, actions=actions)
