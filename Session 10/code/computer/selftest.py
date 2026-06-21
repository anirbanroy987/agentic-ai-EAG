"""Offline logic tests — the binary-independent parts of the computer skill.

These run with NO cua-driver daemon and NO gateway: they exercise the pure
logic (perception regex + first-occurrence dedup, post-condition checks,
arg validation, stale-index detection, the platform shim). The live,
binary-dependent paths are covered by the staged per-app runs in run.py.

    uv run python -m computer.selftest
"""
from __future__ import annotations

from . import layers, perception, recovery, sequencing, driver
from . import platform as plat
from .driver import WindowState
from .electron import is_electron


# The REAL Windows cua-driver markdown (`[N] Role "Label" [id=… actions=[…]]`),
# a DUPLICATED window subtree (guide §6.3), and one macOS-doc fallback line
# (`… [element_index N]`) — the parser must handle both formats.
SAMPLE_TREE = """
- Window "Calculator"
  - [0] Window "Calculator" [value="Calculator" id=TitleBar actions=[set_value]]
  - [7] Text "Display is 0" [id=CalculatorResults actions=[invoke]]
  - [19] Button "Multiply by" [id=multiplyButton actions=[invoke]]
  - [30] Button "Seven" [id=num7Button actions=[invoke]]
  - [22] Button "Equals" [id=equalButton actions=[invoke]]
  - Group "Memory controls"
    - Button "Clear all memory"
  - Window "Calculator"
    - [219] Button "Multiply by" [id=multiplyButton actions=[invoke]]
- AXStaticText "56" [element_index 99]
"""


def test_parse_and_dedup() -> None:
    rows = perception.parse_rows(SAMPLE_TREE)
    by_label = {r.label: r.index for r in rows}
    assert by_label["Multiply by"] == 19, "first 'Multiply by' should be index 19"
    assert all(r.index != 219 for r in rows), "duplicate subtree index 219 must be deduped"
    assert by_label["Seven"] == 30, "Windows '[N] Role \"Label\"' format must parse"
    assert by_label["56"] == 99, "macOS '[element_index N]' fallback must parse"
    # Non-indexed rows ("Clear all memory" has no [N]) are correctly skipped.
    assert "Clear all memory" not in by_label
    print("[ok] parse Windows + macOS formats, first-occurrence dedup")


def test_view_lookups() -> None:
    view = perception.filter(WindowState(1, 2, 9, SAMPLE_TREE), "press the Seven key")
    assert view.find_label("Seven").index == 30
    assert view.find_label("nope") is None
    assert any("Display is 0" in t for t in view.read_text())
    assert {r.index for r in view.by_role("Button")} >= {19, 30, 22}
    print("[ok] perception view lookups + subgoal prioritise")


def test_read_heuristics() -> None:
    assert layers.looks_like_read("read the calculator display")
    assert layers.looks_like_read("what is the value")
    assert not layers.looks_like_read("draw a circle")
    # Action+read goals must NOT short-circuit to L1 (the bug that read 0 and
    # falsely reported success on "compute 42x18 then read the result").
    assert not layers.looks_like_read("compute 42x18 then read the result")
    assert not layers.looks_like_read("open Calculator and read the answer")
    print("[ok] L1 read-goal heuristic (action goals excluded)")


def test_click_validation() -> None:
    try:
        driver.click(1, 2)  # neither element_index nor (x,y) → raises before any call
    except ValueError:
        print("[ok] click requires element_index XOR (x,y)")
        return
    raise AssertionError("click() should reject a missing address")


def test_post_condition() -> None:
    before = WindowState(1, 2, 3, "display 0")
    after = WindowState(1, 2, 3, "display 56")
    assert sequencing.post_condition_met(before, after, expect="56")
    assert sequencing.post_condition_met(before, after)            # changed
    assert not sequencing.post_condition_met(before, before)       # no change
    print("[ok] post-condition (expect token + changed fallback)")


def test_stale_index() -> None:
    assert recovery.is_stale_index_error("Error: element index 5 not found in cache")
    assert not recovery.is_stale_index_error("network timeout")
    print("[ok] stale-index (Invariant B) detection")


def test_platform_shim() -> None:
    assert plat.OS_NAME in ("windows", "macos", "linux")
    assert plat.MODIFIER in ("ctrl", "cmd")
    assert "z" in plat.hotkey("mod", "z")
    assert plat.MODIFIER in plat.hotkey("mod", "z")
    assert plat.LAUNCH_KEY in plat.launch_args("Calculator")
    assert is_electron("Visual Studio Code") and not is_electron("Calculator")
    assert len(plat.empty_tree_suspects()) >= 3
    print(f"[ok] platform shim (os={plat.OS_NAME}, mod={plat.MODIFIER}, key={plat.LAUNCH_KEY})")


def main() -> int:
    tests = [
        test_parse_and_dedup, test_view_lookups, test_read_heuristics,
        test_click_validation, test_post_condition, test_stale_index,
        test_platform_shim,
    ]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} OFFLINE LOGIC TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
