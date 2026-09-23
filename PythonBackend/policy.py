"""
policy.py -- the risk model every tool call passes through.

Each call is assessed into:
    risk        READ < LOW < MEDIUM < HIGH
    capability  which permission switch governs it (permissions.CAPABILITIES)
    reversible  whether a mistake could be undone (trash vs rm, draft vs send)
    reasons     human-readable why, shown on the approval card

and then decide() turns that assessment plus the autonomy mode into one of
    "allow"    run it
    "confirm"  ask the user first (the approval card)
    "deny"     refuse outright (capability switched off)

The rules, in words:
  * READ never asks.
  * HIGH always asks, in every mode: sending, publishing, permanent
    deletion, installing software, purchases, security settings, pushing
    code, anything irreversible or external.
  * Touching a protected path (~/.ssh, keychains, system folders) or
    writing outside the allowed folders always asks.
  * manual      asks for everything above READ.
  * balanced    runs LOW, and MEDIUM when it's reversible and in scope.
  * autonomous  runs LOW and MEDIUM within granted permissions.
  * Work nobody asked for (proactive, not user-initiated) asks for
    anything above READ, in every mode.
  * Once a task has read content that looks like a prompt-injection
    attempt, every MEDIUM action in that task asks.

Shell commands and AppleScript are classified by content, since the same
tool ranges from `ls` to `rm -rf`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import IntEnum

import permissions


class Risk(IntEnum):
    READ = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass
class ToolPolicy:
    risk: Risk
    capability: str
    reversible: bool = True
    note: str = ""


@dataclass
class Assessment:
    tool: str
    risk: Risk
    capability: str
    reversible: bool = True
    reasons: list = field(default_factory=list)
    out_of_scope: bool = False
    protected: bool = False
    summary: str = ""

    def escalate(self, risk: Risk, reason: str, reversible: bool | None = None) -> None:
        if risk > self.risk:
            self.risk = risk
        if reversible is not None:
            self.reversible = self.reversible and reversible
        if reason and reason not in self.reasons:
            self.reasons.append(reason)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool, "risk": self.risk.label, "capability": self.capability,
            "reversible": self.reversible, "reasons": list(self.reasons),
            "out_of_scope": self.out_of_scope, "protected": self.protected,
            "summary": self.summary,
        }


R, L, M, H = Risk.READ, Risk.LOW, Risk.MEDIUM, Risk.HIGH

# Static baseline per tool. Dynamic rules below can only ever raise these.
TOOL_POLICIES: dict[str, ToolPolicy] = {
    # apps & media
    "open_app": ToolPolicy(L, "apps"), "close_app": ToolPolicy(M, "apps", note="unsaved work may be lost"),
    "play_song": ToolPolicy(L, "media"), "spotify_search": ToolPolicy(R, "media"),
    "play_artist": ToolPolicy(L, "media"), "play_album": ToolPolicy(L, "media"),
    "play_playlist": ToolPolicy(L, "media"),
    "spotify_play_uri": ToolPolicy(L, "media"), "spotify_now_playing": ToolPolicy(R, "media"),
    "spotify_control": ToolPolicy(L, "media"),
    # sessions & memory & meta
    "current_session": ToolPolicy(R, "memory"), "set_target_session": ToolPolicy(R, "memory"),
    "save_fact": ToolPolicy(L, "memory"), "list_learned_skills": ToolPolicy(R, "memory"),
    "remember": ToolPolicy(L, "memory"), "recall_memory": ToolPolicy(R, "memory"),
    "forget_memory": ToolPolicy(M, "memory", reversible=False, note="deletes a memory"),
    "search_web": ToolPolicy(R, "web"), "ask_user": ToolPolicy(R, "memory"),
    # Market simulator: public prices in, arithmetic out. paper_trade writes
    # a local simulated balance, which is why it's LOW rather than READ --
    # nothing here can reach a broker or move money.
    "list_strategies": ToolPolicy(R, "trading"), "backtest_strategy": ToolPolicy(R, "trading"),
    "sweep_strategy": ToolPolicy(R, "trading"), "walk_forward_test": ToolPolicy(R, "trading"),
    "past_backtests": ToolPolicy(R, "trading"),
    "paper_trade": ToolPolicy(L, "trading", note="simulated account only; no broker, no money"),
    "open_trading_terminal": ToolPolicy(L, "trading", note="opens Terminal windows running the simulator"),
    "run_in_trading_terminal": ToolPolicy(L, "trading", note="types a command into an open simulator window"),
    "schedule_task": ToolPolicy(L, "scheduling", note="runs later under the same approval rules"),
    "set_timer": ToolPolicy(L, "notifications"), "list_scheduled_tasks": ToolPolicy(R, "scheduling"),
    "cancel_scheduled_task": ToolPolicy(L, "scheduling"),
    # calendar / reminders / contacts
    "calendar_list": ToolPolicy(R, "calendar"), "calendar_create": ToolPolicy(M, "calendar"),
    "calendar_delete": ToolPolicy(H, "calendar", reversible=False, note="deletes an event permanently"),
    "reminders_list": ToolPolicy(R, "reminders"), "reminders_create": ToolPolicy(L, "reminders"),
    "reminders_complete": ToolPolicy(L, "reminders"),
    "contacts_search": ToolPolicy(R, "contacts"), "contacts_list": ToolPolicy(R, "contacts"),
    # notifications, clipboard, finder
    "notifications_send": ToolPolicy(L, "notifications"),
    "clipboard_get": ToolPolicy(R, "clipboard"), "clipboard_set": ToolPolicy(L, "clipboard"),
    "finder_open": ToolPolicy(L, "files_read"), "finder_reveal": ToolPolicy(L, "files_read"),
    # mail & browser
    "mail_list": ToolPolicy(R, "mail_read"), "mail_search": ToolPolicy(R, "mail_read"),
    "mail_send": ToolPolicy(H, "mail_send", reversible=False, note="sends an email"),
    "safari_open": ToolPolicy(L, "browser"), "safari_tabs": ToolPolicy(R, "browser"),
    "safari_current_tab": ToolPolicy(R, "browser"),
    # scripting & system
    "shortcuts_run": ToolPolicy(H, "scripting", reversible=False, note="a Shortcut can do anything"),
    "applescript_run": ToolPolicy(M, "scripting", reversible=False),
    "system_get_volume": ToolPolicy(R, "system"), "system_set_volume": ToolPolicy(L, "system"),
    "system_battery": ToolPolicy(R, "system"), "system_sleep": ToolPolicy(M, "system"),
    "system_lock": ToolPolicy(M, "system"),
    # shell (content-classified below)
    "shell_stage": ToolPolicy(M, "shell", reversible=False),
    "shell_stage_script": ToolPolicy(M, "shell", reversible=False),
    "shell_read_front_window": ToolPolicy(R, "shell"),
    "shell_run": ToolPolicy(M, "shell", reversible=False),
    "shell_run_script": ToolPolicy(M, "shell", reversible=False),
    "shell_spawn": ToolPolicy(M, "shell", reversible=False),
    "shell_job_status": ToolPolicy(R, "shell"), "shell_jobs": ToolPolicy(R, "shell"),
    "shell_job_kill": ToolPolicy(L, "shell"),
    # files: reads
    **{name: ToolPolicy(R, "files_read") for name in (
        "file_read", "file_exists", "file_info", "file_list", "file_group", "file_search",
        "file_preview", "file_head", "file_tail", "file_count_lines", "file_read_range",
        "file_hash", "file_verify_checksum", "file_diff", "file_tree", "file_summarize_folder",
        "file_recent_files", "file_largest_files", "file_empty_files", "file_empty_directories",
        "file_broken_symlinks", "file_fuzzy_search", "file_list_trash", "file_permissions",
    )},
    # files: writes
    "file_write": ToolPolicy(L, "files_write"), "file_append": ToolPolicy(L, "files_write"),
    "file_touch": ToolPolicy(L, "files_write"), "file_mkdir": ToolPolicy(L, "files_write"),
    "file_copy": ToolPolicy(L, "files_write"), "file_duplicate": ToolPolicy(L, "files_write"),
    "file_move": ToolPolicy(M, "files_write"), "file_batch_move": ToolPolicy(M, "files_write"),
    "file_batch_copy": ToolPolicy(L, "files_write"), "file_batch_rename": ToolPolicy(M, "files_write"),
    "file_find_replace": ToolPolicy(M, "files_write"),
    "file_trash": ToolPolicy(M, "files_write", note="moves to Trash (recoverable)"),
    "file_restore_from_trash": ToolPolicy(L, "files_write"),
    "file_chmod": ToolPolicy(M, "files_write"), "file_lock": ToolPolicy(L, "files_write"),
    "file_unlock": ToolPolicy(L, "files_write"),
    "file_zip": ToolPolicy(L, "files_write"), "file_unzip": ToolPolicy(L, "files_write"),
    "file_delete": ToolPolicy(H, "files_write", reversible=False, note="permanent delete -- prefer file_trash"),
    "file_batch_delete": ToolPolicy(H, "files_write", reversible=False, note="permanent delete -- prefer file_trash"),
    # keyboard / mouse / screen / accessibility
    **{name: ToolPolicy(M, "desktop", reversible=False) for name in (
        "keyboard_type", "keyboard_press", "keyboard_sequence", "mouse_click", "mouse_drag",
        "ui_action", "automation_sequence",
    )},
    "mouse_move": ToolPolicy(L, "desktop"), "mouse_scroll": ToolPolicy(L, "desktop"),
    "keyboard_activate_app": ToolPolicy(L, "apps"), "ui_activate": ToolPolicy(L, "apps"),
    **{name: ToolPolicy(R, "screen") for name in (
        "keyboard_frontmost_app", "keyboard_check_accessibility", "automation_permissions",
        "mouse_position", "screen_displays", "screen_capture", "ui_apps", "ui_snapshot", "ui_find",
    )},
    # whatsapp
    "whatsapp_status": ToolPolicy(R, "messaging"), "whatsapp_read": ToolPolicy(R, "messaging"),
    "whatsapp_search": ToolPolicy(L, "messaging"), "whatsapp_open": ToolPolicy(L, "messaging"),
    "whatsapp_open_chat": ToolPolicy(L, "messaging"), "whatsapp_open_chat_by_name": ToolPolicy(L, "messaging"),
    "whatsapp_draft": ToolPolicy(L, "messaging", note="draft only -- not sent"),
    "whatsapp_send": ToolPolicy(H, "messaging", reversible=False, note="sends a message"),
    "whatsapp_message": ToolPolicy(H, "messaging", reversible=False, note="sends a message"),
    # lookups
    **{name: ToolPolicy(R, "location") for name in (
        "geocode_location", "get_weather", "wikipedia_search", "wikipedia_summary",
        "nominatim_geocode", "nominatim_reverse_geocode",
    )},
    "create_temp_email": ToolPolicy(L, "temp_email"), "check_temp_email": ToolPolicy(R, "temp_email"),
    "read_temp_email_message": ToolPolicy(R, "temp_email"),
    # notion
    **{name: ToolPolicy(R, "notion") for name in (
        "notion_search", "notion_sync_workspace", "notion_read_note", "notion_get_database_schema",
        "notion_query_database", "notion_retrieve",
    )},
    **{name: ToolPolicy(L, "notion") for name in (
        "notion_create_note", "notion_append_to_note", "notion_create_task", "notion_complete_task",
        "notion_get_today_journal", "notion_add_meeting_notes", "notion_remember",
        "notion_duplicate_page", "notion_create_database",
    )},
    **{name: ToolPolicy(M, "notion") for name in (
        "notion_update_note", "notion_archive_page", "notion_move_page",
    )},
    "notion_add_comment": ToolPolicy(H, "notion", reversible=False, note="posts a comment others can see"),
    "notion_upload_file": ToolPolicy(H, "notion", reversible=False, note="uploads a file"),
}

# extended_tools.py
TOOL_POLICIES.update({
    "window_list": ToolPolicy(R, "screen"), "window_focus": ToolPolicy(L, "desktop"),
    "window_arrange": ToolPolicy(L, "desktop"), "window_set_frame": ToolPolicy(L, "desktop"),
    "window_minimize": ToolPolicy(L, "desktop"),
    "window_close": ToolPolicy(M, "desktop", reversible=False, note="unsaved work in that window may be lost"),
    "ocr_image": ToolPolicy(R, "screen"), "ocr_screen": ToolPolicy(R, "screen"),
    "document_read": ToolPolicy(R, "files_read"), "search_documents": ToolPolicy(R, "files_read"),
    "document_create": ToolPolicy(L, "files_write"),
    "notes_search": ToolPolicy(R, "notes"), "notes_read": ToolPolicy(R, "notes"),
    "notes_create": ToolPolicy(L, "notes"), "notes_append": ToolPolicy(M, "notes", note="edits an existing note"),
    "music_control": ToolPolicy(L, "media"), "speak": ToolPolicy(L, "notifications"),
    "system_info": ToolPolicy(R, "system"), "permissions_check": ToolPolicy(R, "system"),
    "calendar_calendars": ToolPolicy(R, "calendar"), "safari_read_page": ToolPolicy(R, "browser"),
    "git_status": ToolPolicy(R, "dev"), "git_diff": ToolPolicy(R, "dev"), "git_log": ToolPolicy(R, "dev"),
    "git_commit": ToolPolicy(L, "dev", note="local commit only -- never pushes"),
    "code_search": ToolPolicy(R, "dev"),
    "run_tests": ToolPolicy(L, "dev", reversible=False, note="runs the project's own test code"),
    "daily_briefing": ToolPolicy(R, "calendar"),
})

# Path arguments of non-file_* tools, checked against the allowed folders:
# (argument, is_write). Dev tools additionally expect a project folder.
_EXTRA_PATH_ARGS = {
    "document_read": [("path", False)], "document_create": [("path", True)],
    "search_documents": [("folder", False)], "ocr_image": [("path", False)],
    "git_status": [("repo", False)], "git_diff": [("repo", False)], "git_log": [("repo", False)],
    "git_commit": [("repo", True)], "code_search": [("path", False)], "run_tests": [("path", True)],
}
_PROJECT_TOOLS = {"git_commit", "run_tests"}

# Tools registered later (extended_tools, scheduler, ...) declare their own
# policy via register(); anything still unknown is treated as MEDIUM.
DEFAULT_POLICY = ToolPolicy(M, "system", reversible=False, note="unclassified tool")


def register(name: str, risk: Risk, capability: str, reversible: bool = True, note: str = "") -> None:
    TOOL_POLICIES[name] = ToolPolicy(risk, capability, reversible, note)


# ---------------------------------------------------------------------------
# Shell command classification
# ---------------------------------------------------------------------------

_READ_CMDS = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "fd", "wc",
    "sort", "uniq", "cut", "tr", "awk", "column", "nl", "echo", "printf", "pwd", "whoami", "id",
    "date", "cal", "uname", "sw_vers", "system_profiler", "df", "du", "ps", "pgrep", "lsof",
    "netstat", "ifconfig", "ipconfig", "scutil", "pmset", "ioreg", "vm_stat", "sysctl", "uptime",
    "which", "whereis", "type", "file", "stat", "mdls", "mdfind", "md5", "shasum", "sha256sum",
    "diff", "cmp", "tree", "env", "printenv", "hostname", "ping", "dig", "nslookup", "host",
    "traceroute", "top", "jq", "xmllint", "plutil", "basename", "dirname", "realpath", "readlink",
    "true", "false", "test", "[", "sleep", "seq", "yes", "tput", "locale", "arch", "sw_vers",
    "pbpaste", "textutil", "pdftotext", "strings", "hexdump", "xxd", "od", "man", "tldr",
    "networkQuality", "airport", "security_find_readonly", "log", "last", "w", "who", "groups",
    "launchctl_list", "mdutil", "tmutil_status", "bc", "expr", "cal", "ncal", "zcat", "gzcat",
    "unzip_list", "defaults_read", "osascript_read", "xcrun_readonly", "sips_info", "afinfo",
}
_LOW_CMDS = {
    "mkdir", "touch", "open", "say", "afplay", "pbcopy", "tar", "zip", "gzip", "gunzip", "unzip",
    "ditto", "tee_append", "wget", "screencapture", "sips", "qlmanage", "caffeinate",
}
_BUILD_TEST = re.compile(
    r"^(?:python3?\s+-m\s+(?:pytest|unittest|py_compile|compileall|mypy|pyflakes|ruff|black\s+--check)|"
    r"pytest|mypy|ruff\s+check|flake8|pyflakes|swift\s+(?:build|test)|swiftc\s+-typecheck|"
    r"xcodebuild\b(?!.*\b(?:archive|-exportArchive|install)\b)|make(?:\s+(?:test|check|build|all))?$|"
    r"npm\s+(?:test|run\s+(?:test|build|lint|typecheck))|yarn\s+(?:test|build|lint)|pnpm\s+(?:test|build|lint)|"
    r"cargo\s+(?:build|test|check|clippy)|go\s+(?:build|test|vet)|tsc\b|eslint\b|gradle\s+(?:build|test)|"
    r"mvn\s+(?:test|compile|package)|javac\b|java\b|node\s+--check)"
)
_GIT_READ = {"status", "diff", "log", "show", "branch", "remote", "rev-parse", "ls-files", "blame",
             "grep", "describe", "shortlog", "reflog", "tag", "config", "stash_list", "fetch", "whatchanged"}
_GIT_LOW = {"add", "commit", "stash", "switch", "checkout_b", "init", "clone", "worktree", "notes"}
_GIT_MEDIUM = {"pull", "merge", "rebase", "cherry-pick", "revert", "checkout", "restore", "mv", "rm", "am",
               "apply", "reset", "tag_create"}
_HIGH_CMDS = {
    "rm": "permanently deletes files", "srm": "permanently deletes files", "shred": "destroys files",
    "sudo": "runs with administrator rights", "su": "switches user", "doas": "runs as another user",
    "mkfs": "formats a disk", "newfs": "formats a disk", "dd": "writes raw disk data",
    "diskutil": "changes disks/partitions", "fdisk": "changes partitions", "gpt": "changes partitions",
    "shutdown": "shuts the Mac down", "reboot": "restarts the Mac", "halt": "halts the Mac",
    "launchctl": "changes background services", "csrutil": "changes System Integrity Protection",
    "spctl": "changes Gatekeeper security", "tccutil": "changes privacy permissions",
    "security": "touches the keychain", "networksetup": "changes network settings",
    "systemsetup": "changes system settings", "pfctl": "changes the firewall",
    "chown": "changes file ownership", "installer": "installs software",
    "softwareupdate": "installs system updates", "mas": "installs App Store apps",
    "brew": "installs or removes software", "port": "installs software",
    "pip": "installs Python packages", "pip3": "installs Python packages", "pipx": "installs software",
    "gem": "installs Ruby packages", "npx": "downloads and runs packages",
    "scp": "copies files to another machine", "sftp": "transfers files to another machine",
    "ssh": "runs commands on another machine", "rsync_remote": "copies files to another machine",
    "mail": "sends email", "sendmail": "sends email", "crontab": "changes scheduled jobs",
    "kextload": "loads kernel extensions", "nvram": "changes firmware variables",
    "pmset_set": "changes power settings", "dscl": "changes user accounts", "sysadminctl": "changes user accounts",
    "passwd": "changes a password", "chflags": "changes protected file flags", "killall_system": "kills system processes",
    "gh": "acts on GitHub", "twine": "publishes packages", "defaults_security": "changes security settings",
    "fdesetup": "changes disk encryption", "profiles": "changes configuration profiles",
    "xattr_quarantine": "removes download quarantine protection",
}
_SPLIT_RE = re.compile(r"\|\||&&|;|\||\n|\$\(|`")
_PIPE_TO_SHELL = re.compile(r"(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b")
_REDIRECT_OVERWRITE = re.compile(r"(?<![0-9&>])>(?!>)\s*(?!&|/dev/null)(\S+)")
_REDIRECT_APPEND = re.compile(r">>\s*(\S+)")


def _segments(command: str) -> list[str]:
    return [seg.strip().rstrip(")").strip() for seg in _SPLIT_RE.split(command) if seg.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        tokens = segment.split()
    # Drop leading VAR=value assignments and `env`/`command`/`time`/`nice` wrappers.
    while tokens and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
                      or tokens[0] in ("command", "time", "nice", "nohup", "exec", "builtin")):
        tokens = tokens[1:]
    return tokens


def _classify_git(tokens: list[str], a: Assessment) -> None:
    args = [t for t in tokens[1:] if not t.startswith("-C")]
    sub = next((t for t in args if not t.startswith("-")), "")
    flags = set(args)
    if sub == "push":
        a.escalate(H, "git push publishes commits", reversible=False)
    elif sub == "reset" and "--hard" in flags:
        a.escalate(H, "git reset --hard discards uncommitted work", reversible=False)
    elif sub == "clean" and any(f.startswith("-") and "f" in f for f in flags):
        a.escalate(H, "git clean deletes untracked files", reversible=False)
    elif sub == "branch" and ({"-D", "--delete", "-d"} & flags):
        a.escalate(H, "deletes a git branch", reversible=False)
    elif sub == "checkout" and ("--" in args or "." in args):
        a.escalate(H, "git checkout -- discards local changes", reversible=False)
    elif sub == "stash" and ({"drop", "clear"} & flags):
        a.escalate(H, "drops stashed work", reversible=False)
    elif sub in ("status", "diff", "log", "show", "blame", "grep", "describe", "shortlog",
                 "reflog", "ls-files", "rev-parse", "fetch", "remote", "config", "tag", "branch"):
        a.escalate(R, "")
        if sub in ("config",) and len([t for t in args if not t.startswith("-")]) > 2:
            a.escalate(M, "changes git configuration")
    elif sub in ("add", "commit", "stash", "switch", "init", "clone", "worktree", "notes"):
        a.escalate(L, f"git {sub} (local, reversible)")
    elif sub in _GIT_MEDIUM:
        a.escalate(M, f"git {sub} changes the working tree")
    else:
        a.escalate(M, f"git {sub or '(unknown)'}")


def classify_command(command: str, cwd: str | None = None) -> Assessment:
    """Classifies a shell command line. Every segment of a pipeline/chain
    is assessed and the worst one wins."""
    a = Assessment(tool="shell", risk=R, capability="shell", reversible=True)
    text = (command or "").strip()
    if not text:
        return a
    if _PIPE_TO_SHELL.search(text):
        a.escalate(H, "downloads a script and runs it", reversible=False)
    for target in _REDIRECT_OVERWRITE.findall(text):
        a.escalate(M, f"overwrites {target}", reversible=False)
        _check_path_scope(a, target, write=True)
    for target in _REDIRECT_APPEND.findall(text):
        a.escalate(L, f"appends to {target}")
        _check_path_scope(a, target, write=True)
    any_dev = False
    for segment in _segments(text):
        tokens = _tokens(segment)
        if not tokens:
            continue
        cmd = tokens[0].rsplit("/", 1)[-1]
        rest = tokens[1:]
        if _BUILD_TEST.match(" ".join([cmd] + rest)):
            any_dev = True
            a.escalate(L, f"build/test command: {cmd}")
            continue
        if cmd == "git":
            any_dev = True
            _classify_git(tokens, a)
            continue
        if cmd in ("find", "fd"):
            if {"-delete", "-exec", "-execdir", "-ok"} & set(rest):
                if "-delete" in rest or any(t in ("rm", "/bin/rm") for t in rest):
                    a.escalate(H, "find deletes matching files", reversible=False)
                else:
                    a.escalate(M, "find runs a command on each match", reversible=False)
            continue
        if cmd == "sed" and any(t.startswith("-i") for t in rest):
            a.escalate(M, "edits files in place", reversible=False)
            continue
        if cmd in ("mv",):
            a.escalate(M, "moves/renames files")
            for p in [t for t in rest if not t.startswith("-")]:
                _check_path_scope(a, p, write=True)
            continue
        if cmd in ("cp", "ditto", "ln"):
            a.escalate(L if cmd != "ln" else M, f"{cmd} creates files")
            dest = [t for t in rest if not t.startswith("-")]
            if dest:
                _check_path_scope(a, dest[-1], write=True)
            continue
        if cmd == "rmdir":
            a.escalate(M, "removes empty folders")
            continue
        if cmd == "chmod":
            if any(t in ("-R", "777", "a+rwx", "o+w") for t in rest):
                a.escalate(H, "broad permission change", reversible=False)
            else:
                a.escalate(M, "changes file permissions")
            continue
        if cmd in ("kill", "pkill", "killall"):
            targets = " ".join(rest).lower()
            if any(s in targets for s in ("loginwindow", "windowserver", "launchd", "kernel_task", " -1", "systemuiserver")):
                a.escalate(H, "kills a system process", reversible=False)
            else:
                a.escalate(M, "stops a process", reversible=False)
            continue
        if cmd == "curl":
            joined = " ".join(rest)
            if re.search(r"(?:^|\s)(?:-d|--data\S*|-F|--form|-T|--upload-file|-X\s*(?:POST|PUT|PATCH|DELETE))\b", joined):
                a.escalate(H, "sends data to a remote server", reversible=False)
            elif re.search(r"(?:^|\s)(?:-o|-O|--output)\b", joined):
                a.escalate(L, "downloads a file")
            else:
                a.escalate(R, "")
            continue
        if cmd == "rsync":
            if any(":" in t and not t.startswith("/") for t in rest if not t.startswith("-")):
                a.escalate(H, "copies files to another machine", reversible=False)
            elif "--delete" in rest:
                a.escalate(H, "rsync --delete removes files", reversible=False)
            else:
                a.escalate(M, "syncs files")
            continue
        if cmd == "defaults":
            if rest and rest[0] in ("read", "domains", "find", "export"):
                a.escalate(R, "")
            elif any(k in " ".join(rest).lower() for k in ("security", "firewall", "loginwindow", "gatekeeper", "alf", "screensaver", "lsquarantine")):
                a.escalate(H, "changes security settings", reversible=False)
            else:
                a.escalate(M, "changes app preferences")
            continue
        if cmd == "npm" or cmd in ("yarn", "pnpm", "bun"):
            if rest and rest[0] in ("install", "i", "add", "ci", "update", "upgrade", "uninstall", "remove", "publish", "link"):
                a.escalate(H, "installs, removes or publishes packages", reversible=False)
            else:
                a.escalate(M, f"{cmd} {rest[0] if rest else ''}".strip())
            continue
        if cmd == "cargo" and rest and rest[0] in ("install", "publish"):
            a.escalate(H, f"cargo {rest[0]}", reversible=False)
            continue
        if cmd in ("python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh", "swift", "osascript", "deno"):
            if rest and rest[0] in ("--version", "-V", "--help", "-h"):
                a.escalate(R, "")
            elif cmd.startswith("python") and rest[:2] == ["-m", "pip"]:
                a.escalate(H, "installs Python packages", reversible=False)
            elif cmd.startswith("python") and rest[:2] == ["-m", "venv"]:
                a.escalate(L, "creates a virtual environment")
            else:
                a.escalate(M, f"runs a {cmd} program (arbitrary code)", reversible=False)
            continue
        if cmd == "xattr" and "com.apple.quarantine" in " ".join(rest) and "-d" in " ".join(rest):
            a.escalate(H, "removes download quarantine protection", reversible=False)
            continue
        if cmd == "pmset" and rest and rest[0] not in ("-g",):
            a.escalate(H, "changes power settings", reversible=False)
            continue
        if cmd in _HIGH_CMDS:
            a.escalate(H, _HIGH_CMDS[cmd], reversible=False)
            continue
        if cmd in _READ_CMDS:
            if cmd in ("sysctl",) and "-w" in rest:
                a.escalate(H, "changes kernel settings", reversible=False)
            if cmd == "tee":
                a.escalate(M, "writes files")
            continue
        if cmd == "tee":
            a.escalate(L if "-a" in rest else M, "writes files")
            for p in [t for t in rest if not t.startswith("-")]:
                _check_path_scope(a, p, write=True)
            continue
        if cmd in _LOW_CMDS:
            a.escalate(L, f"{cmd}")
            continue
        if cmd in ("cd", "export", "set", "unset", "alias", "source", ".", "history", "clear"):
            continue
        a.escalate(M, f"unrecognized command '{cmd}'", reversible=False)
    if any_dev:
        a.capability = "dev"
        if cwd and not permissions.in_project(cwd):
            a.escalate(M, f"dev command outside project folders ({cwd})")
    if re.search(r"(~|/Users/[^/\s]+)/\.(ssh|gnupg|aws)\b|Library/Keychains", text):
        a.escalate(H, "touches credentials", reversible=False)
        a.protected = True
    return a


# ---------------------------------------------------------------------------
# AppleScript classification
# ---------------------------------------------------------------------------

_AS_HIGH = [
    (r"\bdo shell script\b", None),  # classified by its own command below
    (r"tell application \"(?:Messages|Mail)\".*\bsend\b", "sends a message"),
    (r"\bempty (?:the )?trash\b", "empties the Trash"),
    (r"\bdelete\b", "deletes something"),
    (r"\b(?:System Settings|System Preferences|Keychain|security)\b", "touches system/security settings"),
    (r"\badministrator privileges\b", "asks for administrator rights"),
    (r"\b(?:shut down|restart|log out)\b", "shuts down or logs out"),
    (r"\bpassword\b", "handles a password"),
]
_AS_MUTATING = re.compile(
    r"\b(?:set|make|move|duplicate|click|keystroke|key code|open|quit|activate|save|close|"
    r"launch|reopen|select|perform|write|create|import|export|print|play|pause)\b", re.I)


def classify_applescript(script: str) -> Assessment:
    a = Assessment(tool="applescript_run", risk=R, capability="scripting", reversible=True)
    text = script or ""
    for pattern, reason in _AS_HIGH:
        match = re.search(pattern, text, re.I | re.S)
        if not match:
            continue
        if reason is None:
            embedded = re.findall(r'do shell script\s+"((?:[^"\\]|\\.)*)"', text, re.I)
            sub = classify_command(" ; ".join(e.replace('\\"', '"') for e in embedded)) if embedded else None
            if sub is None:
                a.escalate(H, "runs a shell command built at runtime", reversible=False)
            else:
                for r in sub.reasons:
                    a.escalate(sub.risk, f"shell: {r}", reversible=sub.reversible)
                a.escalate(max(sub.risk, M), "runs a shell command", reversible=sub.reversible)
            continue
        a.escalate(H, reason, reversible=False)
    if a.risk < M and _AS_MUTATING.search(text):
        a.escalate(M, "controls an application", reversible=False)
    return a


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PATH_READ_ARGS = ("path", "pathA", "pathB", "source")
_PATH_WRITE_ARGS = ("path", "to_path", "destination", "restoreTo", "from_path")


def _check_path_scope(a: Assessment, path: str, write: bool) -> None:
    if not isinstance(path, str) or not path or path.startswith("-") or path in ("/dev/null",):
        return
    if permissions.is_protected(path):
        a.protected = True
        a.escalate(H, f"protected location: {path}", reversible=False)
        return
    ok = permissions.can_write(path) if write else permissions.can_read(path)
    if not ok:
        a.out_of_scope = True
        a.escalate(max(a.risk, M), f"{'writes' if write else 'reads'} outside allowed folders: {path}")


# Tools where every path is only read, vs tools that change something.
_WRITE_TOOLS = {
    "file_write", "file_append", "file_touch", "file_mkdir", "file_move", "file_copy",
    "file_duplicate", "file_delete", "file_find_replace", "file_trash", "file_chmod", "file_lock",
    "file_unlock", "file_zip", "file_unzip", "file_restore_from_trash", "file_batch_move",
    "file_batch_copy", "file_batch_rename", "file_batch_delete",
}


def _file_paths(tool: str, args: dict) -> list[tuple[str, bool]]:
    """(path, is_write) pairs for a file tool's arguments."""
    out: list[tuple[str, bool]] = []
    write_tool = tool in _WRITE_TOOLS
    if tool in ("file_batch_move", "file_batch_copy", "file_batch_rename"):
        for op in args.get("operations") or []:
            if isinstance(op, dict):
                if op.get("from"):
                    out.append((op["from"], tool != "file_batch_copy"))
                if op.get("to"):
                    out.append((op["to"], True))
        return out
    if tool == "file_batch_delete":
        return [(p, True) for p in args.get("paths") or [] if isinstance(p, str)]
    if tool in ("file_copy", "file_zip", "file_unzip"):
        src = args.get("from_path") or args.get("source")
        dst = args.get("to_path") or args.get("destination")
        if src:
            out.append((src, False))
        if dst:
            out.append((dst, True))
        return out
    for key in ("path", "pathA", "pathB", "from_path", "to_path", "source", "destination", "restoreTo", "file_path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            out.append((value, write_tool))
    return out


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------

def assess(tool: str, args: dict | None) -> Assessment:
    """Risk assessment for one concrete tool call."""
    args = args or {}
    base = TOOL_POLICIES.get(tool, DEFAULT_POLICY)
    a = Assessment(tool=tool, risk=base.risk, capability=base.capability, reversible=base.reversible)
    if base.note:
        a.reasons.append(base.note)
    if tool not in TOOL_POLICIES:
        a.reasons.append("unclassified tool")

    if tool in ("shell_run", "shell_stage", "shell_spawn"):
        c = classify_command(str(args.get("command", "")), args.get("cwd"))
        a.risk, a.reversible, a.capability = c.risk, c.reversible, c.capability
        a.reasons = c.reasons or ["read-only command"]
        a.out_of_scope, a.protected = c.out_of_scope, c.protected
        if tool == "shell_spawn":
            a.escalate(max(a.risk, L), "runs in the background")
        if tool == "shell_stage":
            a.escalate(max(a.risk, L), "opens a Terminal window")
    elif tool in ("shell_run_script", "shell_stage_script"):
        path = str(args.get("path", ""))
        a.escalate(M, f"runs script {path}", reversible=False)
        _check_path_scope(a, path, write=False)
    elif tool == "applescript_run":
        c = classify_applescript(str(args.get("script", "")))
        a.risk, a.reversible, a.reasons = c.risk, c.reversible, c.reasons or ["read-only script"]
    elif tool == "file_write":
        if args.get("overwrite"):
            a.escalate(M, "overwrites an existing file", reversible=False)
    elif tool in ("file_move", "file_copy") and args.get("overwrite"):
        a.escalate(M, "may overwrite the destination", reversible=False)
    elif tool == "automation_sequence":
        for step in args.get("steps") or []:
            path = step.get("path", "") if isinstance(step, dict) else ""
            if path in ("/whatsapp/send", "/mail/send"):
                a.escalate(H, "sequence sends a message", reversible=False)
            elif path.startswith("/applescript/") or path.startswith("/shell/"):
                a.escalate(H, "sequence runs scripts", reversible=False)
    elif tool in ("keyboard_activate_app", "ui_activate", "open_app", "keyboard_type",
                  "keyboard_press", "keyboard_sequence"):
        app = str(args.get("app") or args.get("app_name") or args.get("bundle_id") or "")
        if app and (permissions.app_blocked(app) or "systempreferences" in app.lower() or "keychain" in app.lower()):
            a.escalate(H, f"controls a protected app: {app}", reversible=False)
    elif tool == "notion_upload_file" and args.get("file_path"):
        _check_path_scope(a, str(args["file_path"]), write=False)
    elif tool == "file_chmod":
        mode = str(args.get("mode", ""))
        if mode.endswith("777") or "o+w" in mode:
            a.escalate(H, "makes a file world-writable", reversible=False)

    if tool.startswith("file_") or tool in ("finder_open", "finder_reveal"):
        for path, write in _file_paths(tool, args):
            _check_path_scope(a, path, write=write)
    for key, write in _EXTRA_PATH_ARGS.get(tool, []):
        value = args.get(key)
        if isinstance(value, str) and value:
            _check_path_scope(a, value, write=write)
            if tool in _PROJECT_TOOLS and not permissions.in_project(value):
                a.out_of_scope = True
                a.escalate(M, f"outside your project folders: {value}")
    if tool == "document_create" and args.get("overwrite"):
        a.escalate(M, "replaces an existing file", reversible=False)
    if tool == "run_tests" and args.get("command"):
        c = classify_command(str(args["command"]), args.get("path"))
        for reason in c.reasons:
            a.escalate(c.risk, f"test command: {reason}", reversible=c.reversible)

    a.summary = summarize_call(tool, args)
    return a


def summarize_call(tool: str, args: dict) -> str:
    """One readable line describing a call, for approval cards and logs."""
    from redact import redact
    args = args or {}
    if tool in ("shell_run", "shell_stage", "shell_spawn"):
        s = f"Run: {args.get('command', '')}"
        if args.get("cwd"):
            s += f"   (in {args['cwd']})"
    elif tool == "mail_send":
        s = f"Email {args.get('to', '?')}: \"{args.get('subject', '')}\""
    elif tool == "whatsapp_send":
        s = f"WhatsApp {args.get('expected_recipient', '?')}: \"{str(args.get('expected_text', ''))[:120]}\""
    elif tool == "whatsapp_message":
        s = f"WhatsApp {args.get('to', '?')}: \"{str(args.get('text', ''))[:160]}\""
    elif tool == "applescript_run":
        s = "AppleScript: " + " ".join(str(args.get("script", "")).split())[:200]
    elif tool in ("file_delete", "file_trash", "file_write", "file_append", "file_read"):
        s = f"{tool.replace('file_', '').title()}: {args.get('path', '')}"
    elif tool in ("file_move", "file_copy"):
        s = f"{tool.replace('file_', '').title()}: {args.get('from_path')} → {args.get('to_path')}"
    elif tool == "file_batch_delete":
        paths = args.get("paths") or []
        s = f"Delete {len(paths)} item(s): " + ", ".join(map(str, paths[:5])) + ("…" if len(paths) > 5 else "")
    elif tool == "calendar_create":
        s = f"Create event \"{args.get('title', '')}\" at {args.get('start', '')}"
    elif tool == "calendar_delete":
        s = f"Delete calendar event {args.get('id', '')}"
    else:
        shown = {k: (v if not isinstance(v, str) or len(v) < 120 else v[:117] + "…") for k, v in args.items()}
        s = f"{tool}({', '.join(f'{k}={v!r}' for k, v in shown.items())})"
    return redact(s)[:400]


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    action: str            # allow | confirm | deny
    assessment: Assessment
    why: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "why": self.why, **self.assessment.to_dict()}


# The Jugnu account is a separate macOS user that exists to be Jugnu's own
# workspace: what happens inside it doesn't touch the user's account. So work
# that stays inside it runs without asking -- including deleting and
# installing there. What still asks, even there: anything that reaches the
# outside world (sending, publishing, purchases, pushing code) or the whole
# Mac (power, admin rights, system security), and paths outside that
# account's own folders.
_SANDBOX_CAPABILITIES = {"files_read", "files_write", "shell", "dev", "apps", "desktop", "screen", "clipboard",
                         "notifications", "browser", "scripting", "notes", "reminders", "calendar", "contacts"}
_REACHES_OUTSIDE = re.compile(
    r"git push|publish|sends?\b|email|message|purchase|\bbuy|payment|shuts the Mac|restarts the Mac|halts the Mac|"
    r"administrator|switches user|another user|system update|security|keychain|firewall|network|power|"
    r"login item|launchctl|world-writable|protected app|ownership|"
    r"/Users/Shared|/Volumes/", re.I)   # shared with the user's account, or external drives


def _stays_in_sandbox(a: Assessment) -> bool:
    return (a.capability in _SANDBOX_CAPABILITIES and not a.protected and not a.out_of_scope
            and not any(_REACHES_OUTSIDE.search(r or "") for r in a.reasons + [a.summary]))


def decide(a: Assessment, *, mode: str | None = None, user_initiated: bool = True,
           tainted: bool = False, pre_approved: bool = False, sandboxed: bool = False) -> Decision:
    mode = mode or permissions.autonomy_mode()
    if not permissions.capability_enabled(a.capability):
        return Decision("deny", a, f"The '{a.capability}' permission is switched off in Settings → Permissions.")
    if a.risk == Risk.READ and not a.protected and not a.out_of_scope:
        return Decision("allow", a, "read-only")
    if sandboxed and user_initiated and _stays_in_sandbox(a):
        return Decision("allow", a, "inside the Jugnu account, Jugnu's own workspace")
    if a.risk >= Risk.HIGH:
        return Decision("confirm", a, "consequential or irreversible -- always confirmed")
    if a.protected:
        return Decision("confirm", a, "protected location")
    if a.out_of_scope:
        return Decision("confirm", a, "outside the folders Jugnu may use freely")
    if pre_approved:
        return Decision("allow", a, "approved earlier in this task")
    if not user_initiated:
        return Decision("confirm", a, "not part of a task you started")
    if tainted and a.risk >= Risk.MEDIUM:
        return Decision("confirm", a, "this task read content that tried to give instructions")
    if mode == "manual":
        return Decision("confirm", a, "manual mode previews every action")
    if mode == "balanced":
        if a.risk <= Risk.LOW:
            return Decision("allow", a, "low risk")
        if a.reversible:
            return Decision("allow", a, "reversible and in scope")
        return Decision("confirm", a, "balanced mode confirms actions that can't be undone")
    return Decision("allow", a, "autonomous mode, within granted permissions")
