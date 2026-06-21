The Computer skill drives real desktop applications through a running
cua-driver daemon. It walks a layered cascade starting from the cheapest
path and escalating only when needed:

  Layer 1   — read a value straight from the accessibility tree (no LLM)
  Layer 2a  — a deterministic key/click sequence you supply (no LLM)
  Layer 2b  — accessibility-tree judge: a cheap text model picks one action
              per turn from a filtered element legend
  Layer 3   — vision fallback: screenshot + set-of-marks + a vision model
              returns pixel coordinates (for canvas/game/opaque apps)
  page      — Electron/Chromium apps (VS Code, Slack, Discord, …) are driven
              through their DOM via Chrome DevTools selectors, not the AX tree

The escalation is internal: you pass the app and goal, the skill chooses the
layer. It enforces the cua-driver invariants (scan before any indexed action;
re-scan after every state-changing action; verify every action by re-scan)
and the six-trap empty-tree recovery (permissions, background window,
Electron, canvas, Qt-env, stale index), tuned per OS (Windows / macOS / Linux).

Inputs in `metadata`:
  app             (required) app name (Windows/Linux) or bundle_id (macOS)
  goal            (required) free-text description of what to do or read
  subgoals        (optional) ordered list to decompose the goal; for the
                  Electron `page` path, a list of {action, selector, value?}
  sequence        (optional) deterministic L2a steps:
                  [{action:"click", label:"7"} | {action:"type", value:"hi"}
                   | {action:"key", value:"Return"} | {action:"hotkey", keys:["mod","a"]}]
  post_condition  (optional) text expected after success — used to verify
  electron        (optional) force the Electron page path
  debugging_port  (optional) CDP port for the Electron path (default 9222)
  force_path      (optional) pin a layer: "a11y" | "vision" | "page"

Output: a ComputerOutput with `path` (the layer that ran), `content` (for
read goals), `actions` (the turn-by-turn trace), and `trajectory_dir` (the
recorded run, replayable via cua-driver replay_trajectory). On an
unrecoverable empty tree the skill fails with an actionable suspect list;
the Planner should route around (different app, grant permissions, or hand
back to the user). Use when a task lives in a native or Electron desktop app
rather than a web page (which the Browser skill handles).
