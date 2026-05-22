"""
agent6.py — the orchestrator loop. Wires Memory, Perception, Decision, Action.

Fixed order each iteration:
    memory.read -> perception.observe -> (attach bytes?) -> decision.next_step
    -> action.execute -> memory.record_outcome -> append history -> repeat

The loop is the ONLY component that crosses the artifact wall: when Perception
attaches an artifact to the next goal, the loop materializes the bytes from the
ArtifactStore and hands them to Decision. Decision never requests bytes itself.

Run:
    uv run python agent6.py "your query here"
    uv run python agent6.py            # runs a default Query A
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import action
import decision
import perception
from artifacts import artifacts
from memory import memory
from schemas import Goal, GoalList

MAX_ITERATIONS = 12
_MCP_SCRIPT = Path(__file__).resolve().parent / "mcp_server.py"


@asynccontextmanager
async def mcp_session():
    params = StdioServerParameters(command=sys.executable, args=[str(_MCP_SCRIPT)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def load_tools(session: ClientSession):
    return (await session.list_tools()).tools


def final_answer_from(history: list[dict]) -> str:
    """The final answer is the most recent substantive answer event in history."""
    answers = [ev for ev in history if ev.get("kind") == "answer" and ev.get("text")]
    if answers:
        return answers[-1]["text"]
    return "(no answer was produced)"


def _print_iteration_header(it: int) -> None:
    print(f"\n─── iter {it} " + "─" * 60)


def _print_goals(goal_list) -> None:
    for g in goal_list.goals:
        flag = "done" if g.done else "open"
        attach = (f"  attach={','.join(g.attach_artifact_ids)}"
                  if g.attach_artifact_ids else "")
        print(f"[perception]    [{flag}] {g.text}{attach}")


async def run(query: str) -> str:
    run_id = uuid.uuid4().hex[:8]
    history: list[dict] = []
    prior_goals: list[Goal] = []

    print("═" * 72)
    print(f"agent6  run_id={run_id}")
    print(f"query : {query}")
    print("═" * 72)

    # Lecture's Run 1 trace puts memory.remember(query) at the TOP of run()
    # — classify the user's input BEFORE the loop, so a fact-assertion
    # ("Mom's birthday is 15 May 2026") gets persisted even if the loop
    # itself dies mid-execution. For a question-shaped query, the classifier
    # will emit `scratchpad` and nothing is persisted; for a fact-shaped
    # query, the fact is durably written and a future run's memory.read()
    # will surface it. Wrapped in try/except: persistence is best-effort.
    try:
        remembered = memory.remember(query, source="user_query", run_id=run_id)
        if remembered is not None:
            print(f"[memory.remember] stored {remembered.kind}: "
                  f"{remembered.descriptor[:120]}")
    except Exception as e:
        print(f"[memory.remember] skipped — classifier failed: "
              f"{type(e).__name__}: {str(e)[:160]}")

    # Lecture: goals are immutable in Session 6 once perception sets them on
    # iter 1. We snapshot that first GoalList and reconcile every later
    # perception reply against it — only `done` and `attach_artifact_ids`
    # flips are accepted; new/removed/reordered goals are dropped.
    locked_goals: list[Goal] | None = None

    async with mcp_session() as session:
        mcp_tools = await load_tools(session)
        tools = decision.mcp_tools_for_decision(mcp_tools)
        print(f"[mcp] tools: {[t.name for t in mcp_tools]}")

        for it in range(1, MAX_ITERATIONS + 1):
            _print_iteration_header(it)

            # 1. consult memory (cheap, no LLM)
            hits = memory.read(query, history)
            print(f"[memory.read]   {len(hits)} hits")

            # 2. perception: decompose / verify / attach
            goal_list = perception.observe(query, hits, history, prior_goals, run_id)
            if locked_goals is None:
                # iter 1: this IS the goal list. Freeze it.
                # Also force every goal to open — perception sometimes sees a
                # memory hit and pre-marks the goal `done`, but `done` means
                # "decision answered this in a prior iter" and there are no
                # prior iters yet. If we let the pre-mark stand, the loop
                # short-circuits via all_done before decision ever runs and
                # the final answer is empty.
                for g in goal_list.goals:
                    if g.done:
                        print(f"[orch]          unmarking pre-done goal "
                              f"'{g.text[:60]}' — iter 1 always runs decision")
                        g.done = False
                locked_goals = list(goal_list.goals)
            else:
                # Later iters: reconcile. Take only `done` and `attach` flips
                # for goal IDs we already know; ignore anything else.
                done_by_id = {g.id: g.done for g in goal_list.goals}
                attach_by_id = {g.id: g.attach_artifact_ids for g in goal_list.goals}
                for g in locked_goals:
                    if g.id in done_by_id:
                        g.done = done_by_id[g.id]
                    if g.id in attach_by_id:
                        g.attach_artifact_ids = attach_by_id[g.id]
                goal_list = GoalList(goals=locked_goals)
            prior_goals = goal_list.goals
            _print_goals(goal_list)

            # 3. done?
            if goal_list.all_done:
                print("\n[done] all goals satisfied")
                break

            goal = goal_list.next_unfinished()
            if goal is None:
                print("\n[done] no unfinished goal")
                break

            # 4. the loop crosses the artifact wall (only here)
            attached: list[tuple[str, bytes]] = []
            for art_id in goal.attach_artifact_ids:
                if art_id and artifacts.exists(art_id):
                    blob = artifacts.get_bytes(art_id)
                    attached.append((art_id, blob))
                    print(f"[attach]        {art_id} ({len(blob)} bytes)")

            # 5. decision: one goal -> answer or one tool call
            out = decision.next_step(goal, hits, attached, history, tools)

            if out.is_answer:
                print(f"[decision]      ANSWER: {out.answer[:200]}")
                history.append({
                    "iter": it, "kind": "answer",
                    "goal_id": goal.id, "text": out.answer,
                })
                # Mark the goal done locally so the next perception turn sees
                # it as satisfied — relying on the LLM to flip done from
                # history alone was loop-prone (it also occasionally invented
                # duplicate goals when it saw one stuck "open"). prior_goals
                # is the same list perception just returned, so mutating in
                # place is what feeds the next iter.
                for g in prior_goals:
                    if g.id == goal.id:
                        g.done = True
                        break
                continue

            # 6. action: dispatch the tool
            tc = out.tool_call
            print(f"[decision]      TOOL_CALL: {tc.name}({tc.arguments})")
            result_text, art_id = await action.execute(session, tc)
            print(f"[action]        -> {result_text[:160]}")

            # 7. record the outcome in memory (carries handle + keywords)
            memory.record_outcome(
                tool_call=tc, result_text=result_text,
                artifact_id=art_id, run_id=run_id, goal_id=goal.id,
            )
            history.append({
                "iter": it, "kind": "action",
                "goal_id": goal.id, "tool": tc.name, "arguments": tc.arguments,
                "result_descriptor": result_text[:300], "artifact_id": art_id,
            })
        else:
            print(f"\n[stop] reached MAX_ITERATIONS={MAX_ITERATIONS}")

    answer = final_answer_from(history)

    print("\n" + "═" * 72)
    print("FINAL ANSWER:\n" + answer)
    print("═" * 72)
    return answer


def main() -> None:
    default_q = ("Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me "
                 "his birth date, death date, and three key contributions to "
                 "information theory.")
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else default_q
    asyncio.run(run(query))


if __name__ == "__main__":
    main()