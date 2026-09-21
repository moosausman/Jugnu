"""Run a Plan produced by planner.py.

Tasks are executed in dependency order. Results are kept in shared memory so
later tasks and the final synthesis operate on retrieved values rather than
guesses. Tool-using tasks use the PLANNER model; tool-free answers and final
synthesis use the smaller AGENT model.
"""

from agent_core import run_tool_loop
from agents import AGENTS
from memory import agent_system_prompt, facts_as_string, persona_reply
import skills


MAX_TASK_ITERATIONS = 15


def _topological_order(tasks):
    by_id = {task.id: task for task in tasks}
    done = set()
    ordered = []

    def visit(task, stack):
        if task.id in done:
            return
        if task.id in stack:
            raise ValueError(f"circular dependency involving task {task.id}")
        for dependency in task.depends_on:
            visit(by_id[dependency], stack | {task.id})
        ordered.append(task)
        done.add(task.id)

    for task in tasks:
        visit(task, frozenset())

    return ordered


def _task_context(task, shared_memory, recent_context):
    parts = []
    if recent_context:
        parts.append(f"Context:\n{recent_context}")

    parts.append(f"Task: {task.description}")

    if task.depends_on:
        parts.append("Results from earlier steps in this plan:")
        for dependency in task.depends_on:
            parts.append(
                f"- Task {dependency}: "
                f"{shared_memory.get(dependency, '(no result)')}"
            )

    return "\n\n".join(parts)


def run_task(
    task,
    shared_memory,
    recent_context="",
    on_status=None,
    on_tool_call=None,
    on_tool_result=None,
) -> str:
    user_message = _task_context(task, shared_memory, recent_context)

    if task.agent == "answer":
        if on_status:
            on_status("thinking")
        return persona_reply(user_message)

    if task.agent == "daily_review":
        # This local import prevents the executor <-> daily_review cycle.
        if on_status:
            on_status("thinking")
        import daily_review

        return daily_review.build_report()

    agent = AGENTS[task.agent]

    # Has this been done before? A known-good route is handed to the agent
    # up front so it starts from a solution instead of searching for one.
    # That search is the thing that makes repeat tasks slow and burns the
    # iteration budget -- see skills.py.
    known = skills.recall(task.description, task.agent)
    if known:
        user_message = f"{user_message}\n\n{skills.as_prompt_block(known)}"

    messages = [
        {
            "role": "system",
            "content": agent_system_prompt(task.agent, facts_as_string()),
        },
        {"role": "user", "content": user_message},
    ]
    result = run_tool_loop(
        messages=messages,
        schemas=agent.schemas,
        functions=agent.functions,
        role="PLANNER",
        max_iterations=MAX_TASK_ITERATIONS,
        on_status=on_status,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    # Did this actually work? Deliberately conservative: only a clean finish
    # counts. Learning a route that ran out of budget, or whose last tool
    # call failed, would teach the wrong lesson and then keep recommending
    # it -- exactly the failure mode this is meant to remove.
    succeeded = bool(result.steps) and not result.exhausted and not result.last_failed

    if succeeded:
        # record() already bumps `successes` on the matching stored skill
        # (and refreshes its steps). Calling feedback(worked=True) as well
        # counted every successful reuse twice, inflating success rates so
        # a degrading skill took twice as long to be retired.
        skills.record(task.description, task.agent, result.steps, succeeded=True)
    elif known:
        # The recalled route didn't hold up. A skill that stops working
        # gets retired automatically rather than being suggested forever.
        skills.feedback(known["id"], worked=False)

    return result.content


def execute_plan(
    plan,
    recent_context="",
    on_task_start=None,
    on_status=None,
    on_tool_call=None,
    on_tool_result=None,
) -> str:
    """Execute a plan and return one user-facing answer."""
    if plan.needs_clarification:
        return plan.clarify

    ordered = _topological_order(plan.tasks)
    shared_memory = {}

    for task in ordered:
        if on_task_start:
            on_task_start(task)

        result = run_task(
            task,
            shared_memory,
            recent_context=recent_context,
            on_status=(
                (lambda label, current=task: on_status(current, label))
                if on_status
                else None
            ),
            on_tool_call=(
                (
                    lambda name, args, current=task: on_tool_call(
                        current, name, args
                    )
                )
                if on_tool_call
                else None
            ),
            on_tool_result=(
                (
                    lambda name, args, response, current=task: on_tool_result(
                        current, name, args, response
                    )
                )
                if on_tool_result
                else None
            ),
        )
        shared_memory[task.id] = result

    if len(ordered) == 1:
        return shared_memory[ordered[0].id]

    return _synthesize(plan, ordered, shared_memory)


def _synthesize(plan, ordered, shared_memory) -> str:
    lines = [f"Goal: {plan.goal}", "", "Work completed by the plan:"]
    for task in ordered:
        lines.append(
            f"- [{task.agent}] {task.description}\n"
            f"  Result: {shared_memory[task.id]}"
        )

    lines.append(
        "\nGive the user one direct final answer using the above. "
        "Don't mention tasks, agents, or steps -- just answer."
    )
    return persona_reply("\n".join(lines))
