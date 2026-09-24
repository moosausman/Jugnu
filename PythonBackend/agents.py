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
# Session control rides with the desktop tools: any agent that can drive a
# screen needs to be able to say WHICH screen.
_SESSION_TOOL_NAMES = ["current_session", "set_target_session"]

_DESKTOP_TOOL_NAMES = _SESSION_TOOL_NAMES + [
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


_MAX_TOOLS_PER_AGENT = 128

# The generalist's toolbox: the core of every domain, for the task that
# genuinely spans several. It used to carry every tool (126 schemas, ~17k
# tokens re-sent on every step); every tool it drops is still held by the
# specialist for that domain.
_OPERATOR_TOOLS = [
    "search_web", "safari_open", "safari_read_page", "safari_current_tab", "safari_tabs", "document_read",
    "document_create", "file_read", "file_write", "file_append", "file_list", "file_search",
    "file_fuzzy_search", "file_move", "file_copy", "file_mkdir", "file_trash", "file_exists", "file_info",
    "shell_run", "shell_run_script", "applescript_run", "open_app", "close_app", "clipboard_get",
    "clipboard_set", "notifications_send", "finder_reveal", "play_song", "spotify_control",
    "calendar_list", "calendar_create", "reminders_list", "reminders_create", "set_timer",
    "schedule_task", "mail_list", "mail_search", "mail_send", "notes_search", "notes_create",
    "contacts_search", "whatsapp_message", "get_weather", "remember", "recall_memory",
    "window_list", "window_focus", "ui_find", "ui_action", "keyboard_type", "keyboard_press",
    "mouse_click", "screen_capture", "ocr_screen", "current_session", "set_target_session",
]
_OPERATOR_EXCLUDED = {
    n for n in FUNCTIONS if n.startswith(("notion_", "whatsapp_")) or n in {
        "create_temp_email", "check_temp_email", "read_temp_email_message",
        "file_group", "file_hash", "file_verify_checksum", "file_empty_files", "file_empty_directories",
        "file_broken_symlinks", "file_lock", "file_unlock", "file_chmod", "file_permissions",
        "file_list_trash", "file_restore_from_trash", "file_batch_copy", "file_batch_rename",
        "file_duplicate", "file_count_lines", "file_touch", "spotify_search", "spotify_play_uri",
        "mouse_position", "screen_displays", "keyboard_check_accessibility", "automation_permissions",
        "shell_stage_script", "shell_read_front_window", "nominatim_geocode", "nominatim_reverse_geocode",
        "wikipedia_search", "wikipedia_summary", "ui_find",
    }
}


class Agent:
    def __init__(self, name, role, tool_names, autonomous_safe=False, exclusive=False,
                 default_tier="standard"):
        self.name = name
        self.role = role
        self.tool_names = list(tool_names)
        # Drives the shared screen (keyboard, mouse, frontmost app). Two such
        # tasks must never run at the same time -- they'd fight over focus --
        # so the executor runs exclusive tasks one at a time, while
        # everything else in a plan can run in parallel.
        self.exclusive = exclusive
        # Which model tier this agent's tasks use unless the plan says
        # otherwise: fast | standard | deep.
        self.default_tier = default_tier
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
        role="Searches the web and reasons over sources, and reads documents and open web pages. "
             "Time-sensitive facts, news, prices, schedules; research syntheses with cited sources.",
        tool_names=[
            "document_read", "safari_read_page","search_web"],
        autonomous_safe=True,  # search_web is read-only -- nothing to undo
    ),
    "calendar": Agent(
        name="calendar",
        role="Manages calendar events, reminders, timers, and tasks scheduled for later or on repeat.",
        # Fixed: was "reminder_create" (no such tool -- tools.py defines
        # reminders_create). Also added calendar_delete so the agent can
        # actually cancel/undo what it creates, and reminders_complete so
        # it can check things off instead of only ever creating them.
        tool_names=[
            "calendar_calendars", "daily_briefing",
            "calendar_list", "calendar_create", "calendar_delete",
            "reminders_list", "reminders_create", "reminders_complete",
            "set_timer", "schedule_task", "list_scheduled_tasks", "cancel_scheduled_task",
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
        role="Reads, writes, moves, renames, organizes and searches files and folders on the Mac, "
             "and moves things to the Trash (recoverable). Use for anything about the user's files.",
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
        # Everyday file work only: every schema is re-sent on every step, so
        # rarely used maintenance tools live with file_maintenance instead.
        tool_names=[
            "document_read", "document_create", "search_documents", "ocr_image",
            "file_read", "file_write", "file_append", "file_mkdir", "file_move", "file_copy",
            "file_delete", "file_exists", "file_info", "file_list", "file_search",
            "file_fuzzy_search", "file_preview", "file_read_range", "file_find_replace",
            "file_tree", "file_summarize_folder", "file_recent_files", "file_largest_files",
            "file_trash", "file_list_trash", "file_restore_from_trash", "file_zip", "file_unzip",
            "file_batch_move", "file_batch_rename", "file_batch_delete", "file_diff",
        ],
    ),
    "file_maintenance": Agent(
        name="file_maintenance",
        role="Less common file housekeeping: checksums and hashes, file permissions and locks, "
             "empty files/folders, broken symlinks, grouping by type, duplicating, counting lines.",
        tool_names=[
            "file_list", "file_exists", "file_info", "file_head", "file_tail", "file_count_lines",
            "file_group", "file_hash", "file_verify_checksum", "file_empty_files",
            "file_empty_directories", "file_broken_symlinks", "file_lock", "file_unlock",
            "file_chmod", "file_permissions", "file_duplicate", "file_touch", "file_batch_copy",
            "file_trash",
        ],
    ),
    "system": Agent(
        name="system",
        exclusive=True,
        role="Opens/closes apps (including the Jugnu trading terminal -- one or several windows, "
             "here or on the Jugnu account), plays music on Spotify (songs, artists, albums, "
             "playlists -- in the background), controls volume/sleep/lock/battery, sends "
             "notifications, manages the clipboard, reveals files in Finder, runs AppleScript "
             "and named Shortcuts.",
        # Fixed: was "notification" (no such tool -- tools.py defines
        # notifications_send). Also added clipboard_get/set and
        # finder_open/reveal, which existed in tools.py but weren't
        # reachable by any agent.
        #
        # applescript_run is included now that every call is classified by
        # content (policy.classify_applescript) and gated: read-only scripts
        # run, anything that sends, deletes or touches security settings
        # waits for the user's approval.
        tool_names=[
            "window_list", "window_focus", "speak", "music_control", "system_info", "permissions_check",
            "open_app", "close_app", "open_trading_terminal", "run_in_trading_terminal",
            "notifications_send",
            "current_session", "set_target_session",
            "play_song", "play_artist", "play_album", "play_playlist", "spotify_search", "spotify_play_uri",
            "spotify_now_playing", "spotify_control",
            "clipboard_get", "clipboard_set", "finder_open", "finder_reveal",
            "shortcuts_run",
            "system_get_volume", "system_set_volume", "system_battery",
            "system_sleep", "system_lock",
            "applescript_run",
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
        exclusive=True,
        role="Opens URLs and reads open tabs in Safari.",
        # New -- same gap as mail, for safari_*.
        # Not autonomous-safe: safari_open pops a new tab into whatever
        # the user's already looking at, unprompted -- not destructive,
        # but surprising enough to check first.
        tool_names=[
            "safari_read_page","safari_open", "safari_tabs", "safari_current_tab"],
    ),
    "memory": Agent(
        name="memory",
        role="Jugnu's own long-term memory: saves facts and preferences the user states "
             "(\"remember that...\"), recalls what it knows, forgets things on request, and "
             "reports which procedures it has learned.",
        tool_names=["save_fact", "remember", "recall_memory", "forget_memory", "list_learned_skills"],
        # Already the established "just do it, don't ask" tool everywhere
        # else (see TOOL_RULES in memory.py) -- this just makes that
        # explicit for the autonomous-action path too.
        # Not autonomous-safe any more: it can delete memories
        # (forget_memory). Unprompted memory writes are gated by the tool
        # runtime regardless -- unattended work asks before anything but reads.
        autonomous_safe=False,
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
             "Wikipedia facts, and assembles the daily briefing -- for direct data-API "
             "lookups, as opposed to 'research' which searches the open web.",
        # New -- extra_tools.py's geocoding/weather/wikipedia functions
        # existed with proper docstrings but no agent referenced them.
        autonomous_safe=True,  # every tool here is a read-only data lookup
        tool_names=[
            "daily_briefing",
            "geocode_location", "get_weather",
            "wikipedia_search", "wikipedia_summary",
            "nominatim_geocode", "nominatim_reverse_geocode",
        ],
    ),
    "trading": Agent(
        name="trading",
        role="Backtests trading strategies on historical prices, checks whether an edge survives "
             "costs and out-of-sample testing, runs paper accounts with imaginary money, and "
             "Simulation only: it never buys anything and never advises what to buy.",
        autonomous_safe=True,   # public prices in, arithmetic out
        tool_names=[
            "list_strategies", "backtest_strategy", "sweep_strategy",
            "walk_forward_test", "stress_test_strategy", "compare_allocations", "paper_trade", "past_backtests",
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
        exclusive=True,
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
            "window_list", "window_focus", "window_arrange", "window_set_frame", "window_minimize", "window_close",
            "ocr_screen", "ocr_image",
            "shell_run", "shell_run_script",
            "shell_stage", "shell_stage_script", "shell_read_front_window",
            "shell_spawn", "shell_job_status", "shell_job_kill", "shell_jobs",
            "applescript_run",
        ] + _KEYBOARD_TOOL_NAMES + _DESKTOP_TOOL_NAMES,
    ),
    "whatsapp": Agent(
        name="whatsapp",
        exclusive=True,
        role="Opens WhatsApp chats, resolves contacts, reads the exposed chat "
             "interface, searches chats, and drafts or sends messages requested "
             "by the user. Prefers the native Mac app; can interact with WhatsApp "
             "Web using browser/desktop controls when explicitly needed. Reading "
             "is limited to exposed UI, not a complete message-history database.",
        autonomous_safe=False,
        # whatsapp_message does the whole send in one call, so this agent no
        # longer carries the generic keyboard/mouse/screen tools (and their
        # ~4k tokens of schemas on every step). The automation agent still
        # has them for anything unusual.
        tool_names=[
            "whatsapp_message", "whatsapp_status", "whatsapp_open", "whatsapp_open_chat",
            "whatsapp_open_chat_by_name", "whatsapp_read", "whatsapp_search",
            "whatsapp_draft", "whatsapp_send", "contacts_search",
        ],
    ),
    "notes": Agent(
        name="notes",
        role="Apple Notes: searches, reads, creates and appends to the user's notes.",
        tool_names=["notes_search", "notes_read", "notes_create", "notes_append"],
    ),
    "writer": Agent(
        name="writer",
        role="Produces substantial written work -- notes, summaries, study guides, practice "
             "problems with solutions, plans, reports, drafts of emails/messages -- and saves "
             "it to files. Use for any task whose output is a document or long-form text.",
        tool_names=[
            "document_create", "document_read", "search_documents", "notes_create", "notes_append",
            "file_write", "file_append", "file_read", "file_mkdir", "file_exists", "file_list",
            "file_preview", "remember", "recall_memory",
        ],
        default_tier="deep",
    ),
    "developer": Agent(
        name="developer",
        role="Software engineering on the user's projects: reads and searches code, edits "
             "files, runs builds/tests/linters, uses git (status, diff, commit), diagnoses and "
             "fixes bugs, and verifies fixes by re-running tests.",
        tool_names=[
            "git_status", "git_diff", "git_log", "git_commit", "code_search", "run_tests", "document_read",
            "shell_run", "shell_run_script", "shell_spawn", "shell_job_status", "shell_job_kill",
            "shell_jobs", "file_read", "file_write", "file_append", "file_find_replace",
            "file_search", "file_fuzzy_search", "file_tree", "file_list", "file_exists",
            "file_info", "file_head", "file_tail", "file_read_range", "file_diff", "file_mkdir",
            "file_copy", "file_move", "file_trash", "file_count_lines", "search_web",
        ],
        default_tier="deep",
    ),
    "operator": Agent(
        name="operator",
        role="The generalist, with the core tools of every domain: files, shell, apps, browser, "
             "keyboard/mouse/screen, mail, calendar, reminders, WhatsApp, web research, memory. Use "
             "only for a task that genuinely spans several domains in one step. Prefer a "
             "specialist whenever one fits -- it's cheaper and has the full toolset for its domain.",
        # Broad on purpose, but capped: providers limit how many tool schemas
        # one request may carry (OpenAI: 128), and each schema costs context
        # on every call. Specialist-only tools (Notion, WhatsApp internals,
        # rare file maintenance) stay with their specialists. Holding a tool
        # is never permission to use it unchecked -- tool_runtime's policy
        # gate applies to every call regardless of agent.
        tool_names=[n for n in _OPERATOR_TOOLS if n in FUNCTIONS],
        exclusive=True,
    ),
}

# Every agent whose tools go through MacBridge acts in whichever login is
# active, so each one needs to be able to see and change it -- not just the
# desktop agents. Otherwise a files task meant for the Jugnu account has no
# way to confirm or correct where it's working.
for _name in ("calendar", "files", "mail", "browser"):
    for _tool in _SESSION_TOOL_NAMES:
        if _tool not in AGENTS[_name].tool_names:
            AGENTS[_name].tool_names.append(_tool)

# Every task-executing agent can also quietly bank a fact it stumbles across
# mid-task (e.g. the research agent notices the user's favorite team) --
# that's cheap and side-effect-free, unlike letting every agent touch the
# calendar or the file system.
for _agent in AGENTS.values():
    # ask_user lets any agent stop and put a question to the user when
    # something essential is missing (agent_core pauses the plan on it).
    for _tool in ("save_fact", "recall_memory", "ask_user"):
        if _tool not in _agent.tool_names:
            _agent.tool_names.append(_tool)


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
_INTENTIONALLY_UNASSIGNED: dict = {
    # Empty: every tool is held by at least one specialist, and every call is gated
    # by tool_runtime's policy regardless of which agent makes it. Add an
    # entry (with a reason) only if a tool must be kept from all agents.
}

# The ONLY tools an autonomous_safe=True agent may hold. This is the Agent
# class docstring's "whitelist, not a blacklist" promise turned into a check:
# flipping autonomous_safe=True on an agent that also carries a destructive
# tool is exactly the mistake that flag exists to prevent, so it's enforced
# here rather than trusted to code review.
_AUTONOMOUS_SAFE_TOOLS = frozenset({
    "search_web", "ask_user", "daily_briefing", "document_read", "safari_read_page",
    "contacts_search", "contacts_list",
    "save_fact", "list_learned_skills", "remember", "recall_memory",
    "geocode_location", "get_weather",
    "wikipedia_search", "wikipedia_summary",
    "nominatim_geocode", "nominatim_reverse_geocode",
    "list_strategies", "backtest_strategy", "sweep_strategy", "walk_forward_test", "stress_test_strategy", "compare_allocations",
    "paper_trade", "past_backtests",
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

    for agent in AGENTS.values():
        if len(agent.tool_names) > _MAX_TOOLS_PER_AGENT:
            problems.append(f"agent '{agent.name}' has {len(agent.tool_names)} tools; providers accept at most "
                            f"{_MAX_TOOLS_PER_AGENT} per request")

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
