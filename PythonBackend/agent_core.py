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


class AgentResult:
    def __init__(self, content: str, messages: list, steps=None,
                 exhausted: bool = False, last_failed: bool = False):
        self.content = content
        self.messages = messages
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


MAX_TOOL_RESULT_CHARS = 6000
# Generous enough for a real result (a file preview, a directory listing, a
# full JSON error) while stopping a single verbose tool call -- a brew
# install's build log, a large file read, a long shell capture -- from
# dominating the context. Nothing upstream of this caps tool output size at
# all, and it matters more here than it looks: this doesn't just bloat one
# message, it re-transmits itself on *every subsequent iteration* of this
# task's loop, since the whole `messages` list is resent each time. That
# compounding, not any one huge result by itself, is what actually runs a
# long tool-calling task's context up over time.


def _truncate_tool_result(result: str) -> str:
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    omitted = len(result) - MAX_TOOL_RESULT_CHARS
    return (
        result[:MAX_TOOL_RESULT_CHARS]
        + f"\n\n... [truncated -- {omitted} more characters omitted. If you "
        "need more of this, use a narrower/paginated tool instead of "
        "assuming the rest matters -- file_tail/file_head/file_read_range "
        "for a file, a more specific shell command, etc.]"
    )


def _looks_like_failure(result: str) -> bool:
    """Best-effort read on whether a tool's result represents a failure --
    used only to widen loop detection past exact-repeat matching (see
    run_tool_loop's consecutive_failures), never to change what the model
    actually sees. A false positive/negative here just means the "change
    strategy" nudge fires a call earlier/later than ideal, not a wrong
    answer, so this doesn't need to be exact.

    Covers the two shapes results actually take: _run_one_tool's own
    "Tool error: ..." string for a raised exception, and the {"ok": bool,
    ...} convention nearly every MacBridge-backed tool follows for a
    call that ran but didn't succeed (permission error, timeout, bad
    args, etc.) -- as opposed to a legitimate negative answer to a
    question (e.g. file_exists returning {"ok": true, "exists": false}
    correctly stays "not a failure" here, since only the outer "ok"
    is checked).
    """
    if result.startswith("Tool error:"):
        return True
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("ok") is False


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
                               last_failed=last_result_failed)

        if on_status:
            on_status("thinking")

        response = llm_client.chat(messages=messages, tools=schemas, think=False, role=role)
        message = response["message"]
        messages.append(message.model_dump())

        if not message.tool_calls:
            return AgentResult(content=message.content or "", messages=messages,
                               steps=steps, exhausted=False,
                               last_failed=last_result_failed)

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments or {}

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

                result = _run_one_tool(functions, name, args)

                if on_tool_result:
                    on_tool_result(name, args, result)

            last_call_sig = call_sig
            messages.append({"role": "tool", "name": name, "content": _truncate_tool_result(result)})

            # Record what was tried, for skills.py. Skipped repeats aren't
            # part of a route worth remembering -- they're the loop noticing
            # it was stuck.
            if not is_exact_repeat:
                steps.append({"tool": name, "args": args})
            last_result_failed = _looks_like_failure(result)

            # An exact repeat counts toward the streak too, not just a
            # detected failure -- calling the same thing twice in a row is
            # itself a sign of being stuck, arguably the clearest one, so
            # letting it reset the counter to 0 would hide the pattern
            # right when it's most obvious.
            if is_exact_repeat or _looks_like_failure(result):
                consecutive_failures += 1
            else:
                consecutive_failures = 0

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


def _run_one_tool(functions, name, args) -> str:
    if name not in functions:
        return f"Unknown tool: {name}"
    try:
        result = functions[name](**args)
    except Exception as e:
        return f"Tool error: {e}"
    return result if isinstance(result, str) else json.dumps(result, indent=2)
