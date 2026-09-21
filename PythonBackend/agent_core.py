"""
agent_core.py -- the generic think/act loop shared by every agent.

An "agent" is nothing more than: a system prompt + a subset of tool schemas
+ whichever model its role resolves to (see llm_client.py). This module
doesn't know or care which agent is running -- it just drives the
call-model -> run-any-requested-tools -> feed-results-back cycle until the
model stops asking for tools. By default every loop here is driven by the
PLANNER (big) model, because choosing a tool and its arguments is a
decision, and decisions are the planner's job -- see the `role` param below.

Used by executor.py to run each of the planner's tasks (and could just as
well be used to reinstate the old single-agent "everything" loop if you
ever want it back -- just pass it every tool instead of a subset).
"""

import json

import llm_client
import run_context
import tool_runtime


class AgentResult:
    def __init__(self, content: str, messages: list, steps=None,
                 exhausted: bool = False, last_failed: bool = False,
                 outcomes=None, needs_input: str | None = None):
        self.content = content
        self.messages = messages
        # Structured record of every tool call (tool_runtime.ToolOutcome) --
        # what the verifier, the learning loop and the UI reason about.
        self.outcomes = outcomes or []
        # Set when the agent called ask_user: the question to put to the
        # user. The executor stops the plan there instead of guessing.
        self.needs_input = needs_input
        # The tool sequence this run actually used, in order. skills.py
        # stores it so the same job doesn't have to be worked out again --
        # see executor.run_task.
        self.steps = steps or []
        # True when the loop hit max_iterations rather than finishing. A
        # procedure that ran out of budget is not one worth learning.
        self.exhausted = exhausted
        # True when the final tool call failed, which usually means the
        # answer is an explanation of a failure rather than a success.
        self.last_failed = last_failed


# Result truncation, redaction, failure classification and untrusted-
# content wrapping all live in tool_runtime.execute() now -- the one path
# every tool call takes.


CONSECUTIVE_FAILURE_NUDGE_THRESHOLD = 3


def run_tool_loop(
    messages,
    schemas,
    functions,
    role="PLANNER",
    max_iterations=12,
    on_status=None,
    on_tool_call=None,
    on_tool_result=None,
):
    """Runs one agent's loop to completion and returns its final content.

    messages   -- full message list to send (system + task), mutated in
                  place as the loop runs. Pass in a fresh list per task --
                  don't reuse across unrelated agents/tasks.
    schemas    -- tool schemas this agent may see. A subset of the global
                  tool set is what makes it "an agent" rather than "the model".
    functions  -- name -> callable map for executing tool calls.
    role       -- which llm_client role decides each tool call. Defaults to
                  "PLANNER" (the big model): deciding which tool to call and
                  with what arguments is exactly the kind of judgment call
                  planning is, so it gets the smarter model by default.
                  Pass role="AGENT" only for a specific agent you're
                  confident is trivial enough for the small model to drive
                  unsupervised.
    on_status(label)             -- called with "thinking" or a tool name
    on_tool_call(name, args)     -- called right before running a tool
    on_tool_result(name, args, result) -- called right after
    """
    ctx = run_context.current()
    schemas_by_name = {s["function"]["name"]: s for s in schemas or []}
    outcomes: list = []
    iterations = 0
    steps: list = []          # tool sequence, for skills.py
    last_result_failed = False
    last_call_sig = None  # (name, sorted-args-json) of the previous tool call --
    # lets us catch a stuck agent repeating itself exactly and stop before
    # burning the rest of max_iterations on identical, doomed-to-repeat calls.
    consecutive_failures = 0  # broader than last_call_sig: counts failures
    # in a row regardless of whether the arguments matched exactly, so
    # trying three *different* variations of the same broken approach
    # (which last_call_sig alone never catches, since none of them repeat
    # each other byte-for-byte) still gets caught and nudged.

    while True:
        iterations += 1
        if iterations > max_iterations:
            # Out of budget. Used to `raise RuntimeError(...)` here, which
            # propagated all the way out to bridge.py's catch-all and
            # replaced this task's entire result -- everything it already
            # tried -- with a bare crash message. Instead, spend one more
            # call with tools removed (so it physically cannot keep going)
            # asking the model to explain where it got stuck, the same
            # "one more call, no tools, just phrase it" pattern
            # executor.py's _synthesize() already uses for the final
            # multi-task answer.
            messages.append({
                "role": "user",
                "content": (
                    f"You've used all {max_iterations} tool calls for this task "
                    "without finishing. Don't call any more tools. Briefly explain "
                    "what you were trying to do, what you tried, and what's "
                    "blocking it, so the user gets a real answer instead of "
                    "silence or a crash."
                ),
            })
            response = llm_client.chat(messages=messages, think=False, role=role)
            message = response["message"]
            messages.append(message.model_dump())
            return AgentResult(content=message.content or "", messages=messages,
                               steps=steps, exhausted=True,
                               last_failed=last_result_failed, outcomes=outcomes)

        ctx.checkpoint()
        run_context.check_task_stop()
        if on_status:
            on_status("thinking")

        response = llm_client.chat(messages=messages, tools=schemas, think=False, role=role)
        message = response["message"]
        messages.append(message.model_dump())

        if not message.tool_calls:
            return AgentResult(content=message.content or "", messages=messages,
                               steps=steps, exhausted=False,
                               last_failed=last_result_failed, outcomes=outcomes)

        question = None
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments or {}

            if name == "ask_user" and question is None:
                # Not executed: the question goes to the user through the
                # normal reply path, and the plan pauses on it. Every tool
                # call in this message still gets an answer (wire format).
                question = str((args or {}).get("question") or "").strip() or "I need more information to continue."
                messages.append({"role": "tool", "name": name,
                                 "content": "Question passed to the user; waiting for their reply."})
                continue

            call_sig = (name, json.dumps(args, sort_keys=True, default=str))
            is_exact_repeat = call_sig == last_call_sig
            if is_exact_repeat:
                # Same tool, same args, back to back -- running it again won't
                # produce a new result. Skip the (potentially expensive: network,
                # disk, another model call) execution and tell the agent so it
                # can try something else instead of spinning to max_iterations.
                result = (
                    f"(skipped -- you just called {name} with identical arguments "
                    "and got a result already. Use that result, try different "
                    "arguments, or give your final answer.)"
                )
            else:
                if on_status:
                    on_status(name)
                if on_tool_call:
                    on_tool_call(name, args)

                outcome = tool_runtime.execute(name, args, functions, schemas_by_name, ctx=ctx)
                outcomes.append(outcome)
                result = outcome.text

                if on_tool_result:
                    on_tool_result(name, args, result)

            last_call_sig = call_sig
            messages.append({"role": "tool", "name": name, "content": result})

            # Record what was tried, for skills.py. Skipped repeats aren't
            # part of a route worth remembering -- they're the loop noticing
            # it was stuck.
            if not is_exact_repeat:
                steps.append({"tool": name, "args": args})
                last_result_failed = not outcome.ok
            else:
                last_result_failed = True

            # An exact repeat counts toward the streak too, not just a
            # detected failure -- calling the same thing twice in a row is
            # itself a sign of being stuck, arguably the clearest one, so
            # letting it reset the counter to 0 would hide the pattern
            # right when it's most obvious.
            if last_result_failed:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

        if question is not None:
            return AgentResult(content=question, messages=messages, steps=steps,
                               exhausted=False, last_failed=False, outcomes=outcomes,
                               needs_input=question)

        # Nudge AFTER every tool result for this message has been appended,
        # never between them. Two bugs lived in the old in-loop version:
        #
        #  1. Wire-format corruption. When the model requests several tool
        #     calls in one message and the threshold tripped after the
        #     first, a {"role": "user"} message landed *between* two tool
        #     results -- producing assistant(tool_calls=[a,b]) -> tool(a)
        #     -> user(...) -> tool(b). Every OpenAI-compatible endpoint
        #     (which is what JUGNU_PROVIDER=api talks to, including the
        #     Gemini compat layer BackendBridge defaults to) requires each
        #     tool_call_id to be answered before any non-tool message, and
        #     rejects that sequence with a 400. Ollama tolerated it, so it
        #     only ever showed up in API mode.
        #
        #  2. Repeat spam. consecutive_failures was never cleared after
        #     nudging, so once it reached the threshold the same paragraph
        #     was appended again on failure 4, 5, 6... Resetting here means
        #     one nudge per streak, and the next one needs a fresh run of
        #     CONSECUTIVE_FAILURE_NUDGE_THRESHOLD failures to earn it.
        if consecutive_failures >= CONSECUTIVE_FAILURE_NUDGE_THRESHOLD:
            # last_call_sig only catches an *exact* repeat -- three
            # different half-working variations of the same idea (a real
            # pattern this agent has hit: several differently-worded
            # shell_stage attempts, then several differently-shaped
            # keyboard_sequence attempts, none identical to the last, none
            # of them stepping back) sail right past it. This catches that
            # instead, on the failure itself rather than on the arguments
            # matching.
            messages.append({
                "role": "user",
                "content": (
                    f"({consecutive_failures} tool calls in a row have now failed, "
                    "including different variations of the same approach. Don't "
                    "try another small variation of the same idea -- switch to a "
                    "genuinely different tool or strategy, or stop and explain to "
                    "the user what's blocking you instead of continuing to retry.)"
                ),
            })
            consecutive_failures = 0
