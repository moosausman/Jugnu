"""
main.py -- CLI entry point (REPL) for Jugnu.

This used to duplicate bridge.py's whole context-building/plan/execute/
persist pipeline by hand. Both frontends now share that spine via
session.run_turn() -- this file only owns the terminal presentation
(printing the plan, task headers, tool previews) via callbacks.
"""

from memory import load_conversation, clear_conversation
import approvals
import run_context
import session

# No UI here: consequential actions are confirmed on this terminal.
approvals.use_console(True)
import learning  # noqa: E402
learning.install_hooks()

# ------------------------
# Load conversation
# ------------------------

conversation = load_conversation()

print("Jugnu ready. Type 'quit' to exit or 'clear' to reset memory.\n")

# ------------------------
# Main loop
# ------------------------

while True:
    user_input = input("\nYou: ").strip()

    if not user_input:
        continue

    if user_input.lower() == "quit":
        print("Memory saved. Goodbye.")
        break

    if user_input.lower() == "clear":
        conversation = clear_conversation()
        print("Memory cleared.")
        continue

    # Tracks whether the current turn's plan has more than one task, so
    # single-task turns stay quiet (no task headers for work that's just
    # "the whole answer") the same way the old inline version did.
    turn_state = {"multi_task": False}

    def on_plan(plan):
        turn_state["multi_task"] = len(plan.tasks) > 1
        if turn_state["multi_task"]:
            print(f"\n📋 Plan ({len(plan.tasks)} tasks):")
            for t in plan.tasks:
                deps = f"  (needs {t.depends_on})" if t.depends_on else ""
                print(f"   {t.id}. [{t.agent}] {t.description}{deps}")

    def on_task_start(task):
        if turn_state["multi_task"]:
            print(f"\n→ Task {task.id} [{task.agent}]: {task.description}")

    def on_status(task, label):
        if label != "thinking":
            print(f"   ⚙️  {label}")

    def on_tool_result(task, name, args, result):
        preview = result[:120] + ("..." if len(result) > 120 else "")
        print(f"      ↳ {preview}")

    # bridge.py's handle_send already wraps its equivalent call in a
    # try/except so one bad turn (model unreachable, a tool raising
    # something no retry could fix) emits an error and keeps the process
    # alive for the next request -- this REPL didn't have the same
    # guard, so the same failure here took the whole session down and
    # lost the rest of the conversation with it. Same containment here now.
    ctx = run_context.RunContext(goal=user_input, origin="user",
                                 emit=lambda e: print(f"   … {e['message']}") if e.get("type") == "progress" else None)
    token = run_context.activate(ctx)
    try:
        answer = session.run_turn(
            conversation,
            user_input,
            on_plan=on_plan,
            on_task_start=on_task_start,
            on_status=on_status,
            on_tool_result=on_tool_result,
        )
    except KeyboardInterrupt:
        print("\n⏹  Stopped.")
        continue
    except Exception as e:
        print(f"\n⚠️  That turn hit an error and didn't complete: {e}")
        print("   Nothing from it was saved -- try again, or type 'clear' to reset.")
        continue
    finally:
        run_context.deactivate(token)

    print(f"\nJugnu: {answer}")
