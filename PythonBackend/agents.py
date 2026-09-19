"""
agents.py -- the specialist agents the planner can hand tasks to.

Each Agent is a *capability domain*, not a separate model: it's a named
subset of tools plus a short slice of Jugnu's rules (see memory.py's
TOOL_RULES) so a task stays on-topic instead of wandering into other
domains. Every agent's tool-selection loop runs on the same PLANNER-role
model (see executor.py / llm_client.py) -- the domain split here is about
which tools a task is allowed to reach for, not about which model reasons
about them.

To add a capability later (browser, coding, Notion, whatever comes next):
  1. Add the tool function(s) + docstring to tools.py as usual.
  2. Add a TOOL_RULES entry for it in memory.py (optional but recommended).
  3. Register a new Agent below. Leave autonomous_safe at its default
     (False) unless every single tool in tool_names is read-only or
     already a "just do it" action -- see the Agent class docstring below
     and proactive.py for what that flag actually gates.
That's it -- the planner picks it up automatically via agent_capabilities_block(),
with zero changes to planner.py or executor.py.

Every entry below has previously drifted out of sync with tools.py by hand
-- a renamed tool, a whole sub-domain (files, shell jobs) that grew in
tools.py without anyone coming back here to wire it in, a new tool nobody
assigned to any agent at all. Those were all found by manually diffing this
file against tools.py. _validate_registry() at the bottom now does that
diff on every import instead: it fails loudly (ImportError) if a tool_name
here doesn't exist, if a real tool in tools.py isn't claimed by any agent
and isn't explicitly excused in _INTENTIONALLY_UNASSIGNED, or if an
autonomous_safe=True agent holds a tool outside _AUTONOMOUS_SAFE_TOOLS.
Keep both of those sets honest when you add tools -- that's the whole
point of making this a check instead of a comment.
"""

from tools.tools import FUNCTIONS, SCHEMAS

_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in SCHEMAS}

# Shared desktop controls from the updated tools.py. These can mutate the UI;
# any agent receiving them must keep autonomous_safe=False.
_DESKTOP_TOOL_NAMES = [
    "automation_permissions",
    "mouse_position", "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "screen_displays", "screen_capture",
    "ui_apps", "ui_activate", "ui_snapshot", "ui_find", "ui_action",
    "automation_sequence",
]

_KEYBOARD_TOOL_NAMES = [
    "keyboard_type", "keyboard_press", "keyboard_sequence",
    "keyboard_activate_app", "keyboard_frontmost_app",
    "keyboard_check_accessibility",
]


class Agent:
    def __init__(self, name, role, tool_names, autonomous_safe=False):
        self.name = name
        self.role = role
        self.tool_names = list(tool_names)
        # Whether Jugnu may run this agent's tasks entirely on its own
        # initiative (see proactive.py) with no confirmation, versus
        # proposing the action and waiting for the user to say go ahead.
        #
        # True ONLY for agents whose entire tool surface is read-only, or
        # already the established "just do it, don't ask" pattern
        # save_fact gets everywhere (see TOOL_RULES in memory.py). This is
        # a whitelist, not a blacklist, deliberately: an agent with even
        # one destructive or external-facing tool (send, delete, create,
        # run) has to stay False, because the model driving that agent's
        # tool loop could reach for ANY of its tools while pursuing an
        # autonomously-chosen task, not just the safe-sounding one that
        # motivated picking this agent in the first place.
        self.autonomous_safe = autonomous_safe

    @property
    def schemas(self):
        return [_SCHEMA_BY_NAME[n] for n in self.tool_names if n in _SCHEMA_BY_NAME]

    @property
    def functions(self):
        return {n: FUNCTIONS[n] for n in self.tool_names if n in FUNCTIONS}

    @property
    def tool_count(self):
        return len(self.tool_names)

    def __repr__(self):
        flag = "autonomous_safe" if self.autonomous_safe else "confirm-first"
        return f"<Agent {self.name} ({self.tool_count} tools, {flag})>"


AGENTS = {
    "research": Agent(
        name="research",
        role="Searches the web and reasons over sources. Time-sensitive facts, "
             "news, prices, schedules.",
        tool_names=["search_web"],
        autonomous_safe=True,  # search_web is read-only -- nothing to undo
    ),
    "calendar": Agent(
        name="calendar",
        role="Manages calendar events and reminders.",
        # Fixed: was "reminder_create" (no such tool -- tools.py defines
        # reminders_create). Also added calendar_delete so the agent can
        # actually cancel/undo what it creates, and reminders_complete so
        # it can check things off instead of only ever creating them.
        tool_names=[
            "calendar_list", "calendar_create", "calendar_delete",
            "reminders_list", "reminders_create", "reminders_complete",
        ],
    ),
    "contacts": Agent(
        name="contacts",
        role="Looks people up in Contacts.",
        tool_names=["contacts_search", "contacts_list"],
        autonomous_safe=True,  # both tools are read-only lookups
    ),
    "files": Agent(
        name="files",
        role="Reads, writes, moves, and organizes files and folders on the Mac.",
        # Fixed: tools.py defines ~30 file_* tools; this agent previously
        # only saw 11 of them, so the planner could route a "files" task
        # here and the agent would genuinely lack the tool for it (e.g.
        # file_tree, file_hash, file_trash, file_batch_move) with no way
        # to tell the planner "wrong agent" -- it would just fail or
        # hallucinate a workaround. Now it gets the full domain.
        #
        # Fixed again: "full domain" above still left out file_append,
        # file_touch, file_duplicate, file_count_lines, file_find_replace,
        # file_zip, and file_unzip -- all real tools in tools.py that this
        # agent had no way to reach. _validate_registry() below is what
        # caught it this time instead of another manual diff.
        tool_names=[
            "file_read", "file_write", "file_append", "file_touch",
            "file_mkdir", "file_move", "file_copy", "file_duplicate",
            "file_delete", "file_exists", "file_info", "file_list",
            "file_group", "file_search", "file_preview", "file_head",
            "file_tail", "file_count_lines", "file_read_range", "file_hash",
            "file_verify_checksum", "file_diff", "file_find_replace",
            "file_tree", "file_summarize_folder", "file_recent_files",
            "file_largest_files", "file_empty_files", "file_empty_directories",
            "file_broken_symlinks", "file_fuzzy_search", "file_trash",
            "file_list_trash", "file_restore_from_trash", "file_chmod",
            "file_permissions", "file_lock", "file_unlock", "file_zip",
            "file_unzip", "file_batch_move", "file_batch_copy",
            "file_batch_rename", "file_batch_delete",
        ],
    ),
    "system": Agent(
        name="system",
        role="Opens/closes apps, plays music, sends notifications, manages the "
             "clipboard, and reveals files in Finder.",
        # Fixed: was "notification" (no such tool -- tools.py defines
        # notifications_send). Also added clipboard_get/set and
        # finder_open/reveal, which existed in tools.py but weren't
        # reachable by any agent.
        #
        # Deliberately NOT included: applescript_run. Every other tool
        # here does one specific, bounded thing; applescript_run lets the
        # model execute arbitrary AppleScript autonomously, which is a
        # meaningfully bigger blast radius (it can do anything any of the
        # other tools do, and things none of them do, with no per-action
        # review). Wire it in yourself if you want that tradeoff, but it
        # shouldn't be a silent side effect of a tool-routing cleanup.
        tool_names=[
            "open_app", "close_app", "play_song", "notifications_send",
            "clipboard_get", "clipboard_set", "finder_open", "finder_reveal",
            "shortcuts_run",
            "system_get_volume", "system_set_volume", "system_battery",
            "system_sleep", "system_lock",
        ],
    ),
    "mail": Agent(
        name="mail",
        role="Reads, searches, and sends email.",
        # New -- mail_list/search/send existed in tools.py but no agent
        # could reach them.
        tool_names=["mail_list", "mail_search", "mail_send"],
    ),
    "browser": Agent(
        name="browser",
        role="Opens URLs and reads open tabs in Safari.",
        # New -- same gap as mail, for safari_*.
        # Not autonomous-safe: safari_open pops a new tab into whatever
        # the user's already looking at, unprompted -- not destructive,
        # but surprising enough to check first.
        tool_names=["safari_open", "safari_tabs", "safari_current_tab"],
    ),
    "memory": Agent(
        name="memory",
        role="Records durable facts about the user for future recall.",
        tool_names=["save_fact"],
        # Already the established "just do it, don't ask" tool everywhere
        # else (see TOOL_RULES in memory.py) -- this just makes that
        # explicit for the autonomous-action path too.
        autonomous_safe=True,
    ),
    "notion": Agent(
        name="notion",
        role="Reads, writes, and organizes notes, tasks, journal entries, and "
             "databases in Notion -- searching, creating, updating, moving, "
             "duplicating, archiving, commenting on, and attaching files to pages.",
        # New -- notion_service.py's NotionService existed as a standalone
        # class with no agent (or tool wrappers) reaching it at all.
        tool_names=[
            "notion_search", "notion_sync_workspace", "notion_retrieve",
            "notion_create_note", "notion_read_note", "notion_update_note",
            "notion_append_to_note", "notion_archive_page", "notion_move_page",
            "notion_duplicate_page", "notion_add_comment", "notion_upload_file",
            "notion_create_database", "notion_get_database_schema",
            "notion_query_database", "notion_create_task", "notion_complete_task",
            "notion_get_today_journal", "notion_add_meeting_notes", "notion_remember",
        ],
    ),
    "lookup": Agent(
        name="lookup",
        role="Looks up weather forecasts, place coordinates/addresses, and "
             "Wikipedia facts -- for direct data-API lookups, as opposed to "
             "'research' which searches the open web for anything else.",
        # New -- extra_tools.py's geocoding/weather/wikipedia functions
        # existed with proper docstrings but no agent referenced them.
        autonomous_safe=True,  # every tool here is a read-only data lookup
        tool_names=[
            "geocode_location", "get_weather",
            "wikipedia_search", "wikipedia_summary",
            "nominatim_geocode", "nominatim_reverse_geocode",
        ],
    ),
    "temp_email": Agent(
        name="temp_email",
        role="Creates and checks disposable email inboxes for site signups "
             "and verification codes -- never touches the user's real email "
             "(that's the 'mail' agent).",
        # Not autonomous-safe: create_temp_email is low-stakes but still
        # creates something real (a live inbox somewhere) the user didn't
        # ask for -- worth a check first, not a silent side effect.
        tool_names=["create_temp_email", "check_temp_email", "read_temp_email_message"],
    ),
    "automation": Agent(
        name="automation",
        role="Runs terminal commands or script files, reads back Terminal output, "
             "and controls the Mac's mouse, keyboard and on-screen app/browser "
             "interfaces. Inspects accessibility controls, captures screenshots, "
             "clicks buttons, drags, scrolls, and fills fields. Use this for direct "
             "desktop interactions beyond a specific agent's tools. Route WhatsApp "
             "chat tasks to the whatsapp agent.",
        # Shell staging follows tools.py: auto_run=True executes visibly by
        # default; auto_run=False leaves the command for review. Desktop input
        # can cause external side effects, so this domain is not proactive-safe.
        #
        # Fixed: shell_spawn/shell_job_status/shell_job_kill/shell_jobs existed
        # in tools.py for exactly this agent's use case (fire off a long-running
        # command, then check on or kill it later) but weren't reachable -- an
        # agent that can start a background job had no way to ever check on it.
        autonomous_safe=False,
        tool_names=[
            "shell_run", "shell_run_script",
            "shell_stage", "shell_stage_script", "shell_read_front_window",
            "shell_spawn", "shell_job_status", "shell_job_kill", "shell_jobs",
        ] + _KEYBOARD_TOOL_NAMES + _DESKTOP_TOOL_NAMES,
    ),
    "whatsapp": Agent(
        name="whatsapp",
        role="Opens WhatsApp chats, resolves contacts, reads the exposed chat "
             "interface, searches chats, and drafts or sends messages requested "
             "by the user. Prefers the native Mac app; can interact with WhatsApp "
             "Web using browser/desktop controls when explicitly needed. Reading "
             "is limited to exposed UI, not a complete message-history database.",
        autonomous_safe=False,
        tool_names=[
            "whatsapp_status", "whatsapp_open", "whatsapp_open_chat",
            "whatsapp_open_chat_by_name",
            "whatsapp_read", "whatsapp_search", "whatsapp_draft", "whatsapp_send",
            "contacts_search", "contacts_list",
            "safari_open", "safari_tabs", "safari_current_tab",
        ] + _KEYBOARD_TOOL_NAMES + _DESKTOP_TOOL_NAMES,
    ),
}

# Every task-executing agent can also quietly bank a fact it stumbles across
# mid-task (e.g. the research agent notices the user's favorite team) --
# that's cheap and side-effect-free, unlike letting every agent touch the
# calendar or the file system.
for _agent in AGENTS.values():
    if "save_fact" not in _agent.tool_names:
        _agent.tool_names.append("save_fact")


# ---------------------------------------------------------------------------
# Registry self-check
# ---------------------------------------------------------------------------
# Every bug fixed in the comments above ("was 'reminder_create'", "only saw
# 11 of them", "existed... but no agent could reach them") was found by a
# human eyeballing tools.py against this file. That doesn't scale and it
# already missed things twice (files, then automation). _validate_registry()
# below is that same diff, done automatically on every import, so a future
# typo or a tool added to tools.py and never wired in fails fast with a
# specific message instead of silently shipping a half-working agent.

# Tools that exist in tools.py's FUNCTIONS but are deliberately unreachable
# by any agent's tool loop. Anything landing here needs a *reason*, spelled
# out, the same way the comments above explain every fix -- this dict is the
# only thing that stops the check below from failing on them.
_INTENTIONALLY_UNASSIGNED = {
    "applescript_run": (
        "Runs arbitrary AppleScript with no per-action review -- a strictly "
        "bigger blast radius than every other tool combined (see the "
        "'system' agent's comment above). Wire it into an agent on purpose; "
        "it should never be a silent side effect of a routing change."
    ),
    "ask_user": (
        "Blocks the process on a synchronous input() call. The intended "
        "escalation path is an agent reporting what's missing (see "
        "AGENT_DISCIPLINE in memory.py) and the planner/persona layer "
        "deciding whether to ask -- not a sub-agent blocking mid-task on "
        "stdin. Give it to an agent deliberately if that design changes."
    ),
}

# The ONLY tools an autonomous_safe=True agent may hold. This is the Agent
# class docstring's "whitelist, not a blacklist" promise turned into a check:
# flipping autonomous_safe=True on an agent that also carries a destructive
# tool is exactly the mistake that flag exists to prevent, so it's enforced
# here rather than trusted to code review.
_AUTONOMOUS_SAFE_TOOLS = frozenset({
    "search_web",
    "contacts_search", "contacts_list",
    "save_fact",
    "geocode_location", "get_weather",
    "wikipedia_search", "wikipedia_summary",
    "nominatim_geocode", "nominatim_reverse_geocode",
})


def _validate_registry() -> None:
    problems = []

    all_claimed = set()
    for agent in AGENTS.values():
        for tool_name in agent.tool_names:
            if tool_name not in FUNCTIONS:
                problems.append(
                    f"agent '{agent.name}' lists tool_name '{tool_name}', "
                    f"which does not exist in tools.py's FUNCTIONS. Typo, or "
                    f"a tool that got renamed/removed?"
                )
            all_claimed.add(tool_name)
        if agent.autonomous_safe:
            unsafe = [t for t in agent.tool_names if t not in _AUTONOMOUS_SAFE_TOOLS]
            if unsafe:
                problems.append(
                    f"agent '{agent.name}' has autonomous_safe=True but "
                    f"carries {unsafe}, which {'is' if len(unsafe) == 1 else 'are'} "
                    f"not in _AUTONOMOUS_SAFE_TOOLS. Either the flag is wrong "
                    f"for this agent, or that tool needs to be added to the "
                    f"whitelist (only if it's genuinely read-only/no-undo)."
                )

    orphaned = set(FUNCTIONS) - all_claimed - set(_INTENTIONALLY_UNASSIGNED)
    if orphaned:
        problems.append(
            f"tools.py defines {sorted(orphaned)}, but no agent's tool_names "
            f"includes them and they're not in _INTENTIONALLY_UNASSIGNED. "
            f"Add them to the right agent, or excuse them with a reason."
        )

    if problems:
        raise ImportError(
            "agents.py registry is out of sync with tools.py:\n- "
            + "\n- ".join(problems)
        )


_validate_registry()


def agent_capabilities_block() -> str:
    """Human-readable capability list injected into the planner's prompt.

    Deliberately does NOT list tool names or schemas -- the planner decides
    WHO should do the work, not HOW. It doesn't need to know the tools exist,
    only what each agent is for.
    """
    return "\n".join(f"- {agent.name}: {agent.role}" for agent in AGENTS.values())


def get_agent(name: str) -> Agent:
    """AGENTS[name], but with a useful error if the planner names an agent
    that doesn't exist instead of a bare KeyError."""
    try:
        return AGENTS[name]
    except KeyError:
        raise KeyError(
            f"No agent named '{name}'. Available: {sorted(AGENTS)}"
        ) from None
