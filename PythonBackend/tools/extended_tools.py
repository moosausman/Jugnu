"""
extended_tools.py -- capabilities added on top of the original tool set:
windows, OCR, documents, Notes, Apple Music, git and code, system status,
speech, the daily briefing and permission checks.

Everything that touches the Mac goes through MacBridge (tools.macbridge.call)
so it runs in whichever macOS session the task targets -- the user's own or
the Jugnu account -- and inherits MacBridge's permissions, timeouts and UI
serialisation. Each tool's risk level is declared in policy.py.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import shlex
import uuid
from typing import Optional

from .macbridge import call


def _shell(command: str, cwd: Optional[str] = None, timeout: float = 60) -> dict:
    args = {"command": command, "timeout": timeout}
    if cwd:
        args["cwd"] = cwd
    return call("/shell/run", args, timeout=timeout + 20)


def _script(source: str, timeout: float = 60) -> dict:
    return call("/applescript/run", {"script": source}, timeout=timeout)


def _as_string(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _resolve_pid(app: str) -> tuple[int | None, str | None]:
    r = call("/windows/list", {"app": app}, timeout=20)
    if not r.get("ok"):
        return None, r.get("error")
    wins = r.get("windows") or []
    if not wins:
        return None, f"{app} has no open windows (is it running?)"
    return wins[0]["pid"], None


def window_list(app: str = "") -> dict:
    """Lists open windows -- all apps, or one app -- with titles, positions
    and sizes (global screen points, origin top-left). Read-only.

    Args:
        app: Optional app name or bundle id, e.g. "Safari". Empty lists every app's windows.
    """
    return call("/windows/list", {"app": app} if app else {}, timeout=20)


def window_focus(app: str, title: str = "", index: int = 0) -> dict:
    """Brings a specific window to the front (un-minimizing it if needed).

    Args:
        app: The app's name, e.g. "Safari".
        title: Part of the window's title, to pick one window among several.
        index: Which window (0 = frontmost) when no title is given.
    """
    pid, err = _resolve_pid(app)
    if err:
        return {"ok": False, "error": err}
    args = {"pid": pid, "index": index}
    if title:
        args["title"] = title
    return call("/windows/focus", args, timeout=20)


def window_arrange(app: str, layout: str, title: str = "", index: int = 0) -> dict:
    """Snaps a window into a standard layout on its screen and reports the
    frame it actually got (apps can refuse or clamp sizes).

    Args:
        app: The app's name.
        layout: "left-half", "right-half", "top-half", "bottom-half", "maximize" or "center".
        title: Optional part of the window title.
        index: Which window when no title is given.
    """
    pid, err = _resolve_pid(app)
    if err:
        return {"ok": False, "error": err}
    args = {"pid": pid, "layout": layout, "index": index}
    if title:
        args["title"] = title
    return call("/windows/arrange", args, timeout=20)


def window_set_frame(app: str, x: float = -1, y: float = -1, width: float = -1, height: float = -1,
                     title: str = "", index: int = 0) -> dict:
    """Moves and/or resizes a window to exact coordinates (global points,
    top-left origin). Omitted values (-1) keep the current ones. Reports the
    frame actually applied.

    Args:
        app: The app's name.
        x: Left edge. -1 keeps it.
        y: Top edge. -1 keeps it.
        width: Width. -1 keeps it.
        height: Height. -1 keeps it.
        title: Optional part of the window title.
        index: Which window when no title is given.
    """
    pid, err = _resolve_pid(app)
    if err:
        return {"ok": False, "error": err}
    args: dict = {"pid": pid, "index": index}
    for key, value in (("x", x), ("y", y), ("width", width), ("height", height)):
        if value is not None and value >= 0:
            args[key] = value
    if title:
        args["title"] = title
    return call("/windows/set-frame", args, timeout=20)


def window_minimize(app: str, minimized: bool = True, title: str = "", index: int = 0) -> dict:
    """Minimizes (or restores) a window.

    Args:
        app: The app's name.
        minimized: True to minimize, False to restore.
        title: Optional part of the window title.
        index: Which window when no title is given.
    """
    pid, err = _resolve_pid(app)
    if err:
        return {"ok": False, "error": err}
    args = {"pid": pid, "index": index, "minimized": minimized}
    if title:
        args["title"] = title
    return call("/windows/minimize", args, timeout=20)


def window_close(app: str, title: str = "", index: int = 0) -> dict:
    """Closes one window via its close button. If the app asks to save
    changes, the window stays open and the result says so.

    Args:
        app: The app's name.
        title: Optional part of the window title.
        index: Which window when no title is given.
    """
    pid, err = _resolve_pid(app)
    if err:
        return {"ok": False, "error": err}
    args = {"pid": pid, "index": index}
    if title:
        args["title"] = title
    return call("/windows/close", args, timeout=20)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def ocr_image(path: str, fast: bool = False) -> dict:
    """Reads the text in an image or screenshot file with on-device OCR
    (nothing leaves the Mac). Read-only.

    Args:
        path: Image path (PNG, JPEG, HEIC, TIFF, or a one-page PDF), e.g. "~/Desktop/Screenshot.png".
        fast: True for quicker, less accurate recognition.
    """
    r = call("/ocr/image", {"path": path, "fast": fast}, timeout=60)
    if r.get("ok") and len(r.get("text", "")) > 4000:
        r.pop("lines", None)  # the joined text is enough; per-line boxes would crowd the context
    return r


def ocr_screen(display_id: int = 0, fast: bool = False) -> dict:
    """Reads all text currently visible on a screen, with each line's
    position in global points (usable with mouse_click). Needs Screen
    Recording permission. Read-only.

    Args:
        display_id: Which display (0 = main).
        fast: True for quicker, less accurate recognition.
    """
    args: dict = {"fast": fast}
    if display_id:
        args["displayId"] = display_id
    return call("/ocr/screen", args, timeout=60)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

_TEXTUTIL_EXTS = {".doc", ".docx", ".rtf", ".rtfd", ".html", ".htm", ".odt", ".wordml", ".webarchive"}
_PLAIN_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".py", ".swift", ".js", ".ts", ".tex",
               ".yaml", ".yml", ".xml", ".log", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".sh"}


def _ext(path: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]+)$", path.strip())
    return m.group(1).lower() if m else ""


def _xml_text(xml: str, tag: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml)]


def document_read(path: str, start_page: int = 1, end_page: int = 0, max_chars: int = 60000) -> dict:
    """Extracts the text of a document for reading or analysis: PDF, Word
    (.docx/.doc), RTF, HTML, ODT, PowerPoint (.pptx), Excel (.xlsx), and
    plain-text/code/markdown/CSV files. Read-only.

    Args:
        path: The document's path, e.g. "~/Documents/CSC236/Syllabus.pdf".
        start_page: PDF only -- first page to read (1-based).
        end_page: PDF only -- last page to read; 0 reads to the end.
        max_chars: Upper bound on returned characters (long documents are truncated).
    """
    ext = _ext(path)
    max_chars = max(1000, min(int(max_chars), 400000))
    if ext == ".pdf":
        args: dict = {"path": path, "startPage": max(1, start_page), "maxChars": max_chars}
        if end_page and end_page >= start_page:
            args["endPage"] = end_page
        return call("/documents/pdf-text", args, timeout=90)
    q = shlex.quote(path if not path.startswith("~") else path)
    qpath = q if not path.startswith("~/") else '"$HOME/' + path[2:].replace('"', '\\"') + '"'
    if ext in _TEXTUTIL_EXTS:
        r = _shell(f"textutil -convert txt -stdout {qpath}", timeout=60)
        if r.get("exitCode") != 0:
            return {"ok": False, "error": (r.get("stderr") or r.get("error") or "textutil failed").strip()}
        text = r.get("stdout", "")
        return {"ok": True, "path": path, "format": ext[1:], "text": text[:max_chars], "truncated": len(text) > max_chars}
    if ext == ".pptx":
        r = _shell(f"unzip -l {qpath} | grep -o 'ppt/slides/slide[0-9]*.xml' | sort -V | "
                   f"while read s; do echo \"@@SLIDE $s\"; unzip -p {qpath} \"$s\"; echo; done", timeout=60)
        if r.get("exitCode") not in (0, None) and not r.get("stdout"):
            return {"ok": False, "error": (r.get("stderr") or "couldn't open the presentation").strip()}
        slides = []
        for chunk in r.get("stdout", "").split("@@SLIDE ")[1:]:
            name, _, xml = chunk.partition("\n")
            slides.append({"slide": len(slides) + 1, "text": " ".join(_xml_text(xml, "a:t"))})
        text = "\n".join(f"--- slide {s['slide']} ---\n{s['text']}" for s in slides)
        return {"ok": True, "path": path, "format": "pptx", "slideCount": len(slides), "text": text[:max_chars],
                "truncated": len(text) > max_chars}
    if ext == ".xlsx":
        r = _shell(f"unzip -p {qpath} xl/sharedStrings.xml; echo '@@SHEETS@@'; "
                   f"for s in $(unzip -l {qpath} | grep -o 'xl/worksheets/sheet[0-9]*.xml' | sort -V); "
                   f"do echo \"@@SHEET $s\"; unzip -p {qpath} \"$s\"; echo; done", timeout=60)
        out = r.get("stdout", "")
        if "@@SHEETS@@" not in out:
            return {"ok": False, "error": (r.get("stderr") or "couldn't open the spreadsheet").strip()}
        shared_xml, _, sheets_xml = out.partition("@@SHEETS@@")
        shared = [html.unescape("".join(re.findall(r"<t[^>]*>([^<]*)</t>", si)))
                  for si in re.findall(r"<si>(.*?)</si>", shared_xml, re.S)]
        sheets = []
        for chunk in sheets_xml.split("@@SHEET ")[1:]:
            name, _, xml = chunk.partition("\n")
            rows = []
            for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S)[:500]:
                cells = []
                for attrs, body in re.findall(r"<c([^>]*)>(.*?)</c>", row, re.S):
                    v = re.search(r"<v>([^<]*)</v>", body)
                    inline = re.search(r"<t[^>]*>([^<]*)</t>", body)
                    if 't="s"' in attrs and v:
                        idx = int(v.group(1))
                        cells.append(shared[idx] if idx < len(shared) else "")
                    elif inline:
                        cells.append(html.unescape(inline.group(1)))
                    elif v:
                        cells.append(v.group(1))
                rows.append(cells)
            sheets.append({"sheet": name.strip().rsplit("/", 1)[-1], "rows": rows})
        text = "\n".join(f"--- {s['sheet']} ---\n" + "\n".join("\t".join(r) for r in s["rows"]) for s in sheets)
        return {"ok": True, "path": path, "format": "xlsx", "sheets": len(sheets), "text": text[:max_chars],
                "truncated": len(text) > max_chars}
    if ext in _PLAIN_EXTS or not ext:
        r = call("/files/read", {"path": path}, timeout=30)
        if not r.get("ok"):
            return r
        text = r.get("content") or r.get("text") or ""
        return {"ok": True, "path": path, "format": ext[1:] or "text", "text": text[:max_chars],
                "truncated": len(text) > max_chars}
    return {"ok": False, "error": f"Unsupported document type '{ext}'. Supported: pdf, docx, doc, rtf, html, odt, "
                                  "pptx, xlsx and plain-text formats. For images use ocr_image."}


def _markdown_to_html(md: str, title: str) -> str:
    """Small, dependency-free Markdown subset -> HTML (headings, lists,
    bold/italic/code, links, paragraphs, horizontal rules, code blocks)."""
    out, in_list, in_code, para = [], None, False, []

    def inline(text: str) -> str:
        t = html.escape(text)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
        return t

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    for line in md.splitlines():
        if line.strip().startswith("```"):
            flush_para()
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        bullet = re.match(r"^\s*[-*+]\s+(.*)", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
        elif bullet or number:
            flush_para()
            kind = "ul" if bullet else "ol"
            if in_list != kind:
                if in_list:
                    out.append(f"</{in_list}>")
                out.append(f"<{kind}>")
                in_list = kind
            out.append(f"<li>{inline((bullet or number).group(1))}</li>")
        elif re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            flush_para()
            out.append("<hr>")
        elif not line.strip():
            flush_para()
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
        else:
            para.append(line.strip())
    flush_para()
    if in_list:
        out.append(f"</{in_list}>")
    if in_code:
        out.append("</pre>")
    style = ("body{font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:12pt;line-height:1.45;"
             "margin:48px;color:#111}h1,h2,h3{line-height:1.2}pre,code{font-family:Menlo,monospace;font-size:10pt}"
             "pre{background:#f4f4f4;padding:10px}")
    return (f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title>"
            f"<style>{style}</style></head><body>{''.join(out)}</body></html>")


def document_create(path: str, content: str, title: str = "", overwrite: bool = False) -> dict:
    """Creates a document from Markdown-style content. The format comes
    from the file extension: .md/.txt (as written), .html, .docx (Word),
    .rtf, .pdf, or .csv (content should be comma-separated rows). Verifies
    the file exists afterwards.

    Args:
        path: Where to save it, e.g. "~/Documents/CSC236/Study Plan.docx".
        content: The document body in Markdown (headings with #, lists with -, **bold**).
        title: Document title (defaults to the file name).
        overwrite: Replace an existing file at that path.
    """
    ext = _ext(path)
    title = title or re.sub(r"\.[^.]+$", "", path.rsplit("/", 1)[-1])
    if ext in (".md", ".markdown", ".txt", ".csv", ".tsv", ""):
        r = call("/files/write", {"path": path, "content": content, "overwrite": overwrite,
                                  "createIntermediateDirectories": True}, timeout=30)
    elif ext in (".html", ".htm"):
        r = call("/files/write", {"path": path, "content": _markdown_to_html(content, title), "overwrite": overwrite,
                                  "createIntermediateDirectories": True}, timeout=30)
    elif ext in (".docx", ".rtf", ".pdf", ".odt"):
        exists = call("/files/exists", {"path": path}, timeout=15)
        if exists.get("exists") and not overwrite:
            return {"ok": False, "error": f"{path} already exists (pass overwrite=true to replace it)"}
        document_html = _markdown_to_html(content, title)
        if ext == ".pdf":
            r = call("/documents/create-pdf", {"path": path, "html": document_html, "overwrite": overwrite},
                     timeout=120)
            if not r.get("ok"):
                return r
        else:
            tmp = f"/tmp/jugnu-doc-{uuid.uuid4().hex[:8]}.html"
            w = call("/files/write", {"path": tmp, "content": document_html, "overwrite": True}, timeout=30)
            if not w.get("ok"):
                return w
            target = path if not path.startswith("~/") else '"$HOME/' + path[2:].replace('"', '\\"') + '"'
            target = target if target.startswith('"') else shlex.quote(target)
            cmd = (f"mkdir -p \"$(dirname {target})\" && textutil -convert {ext[1:]} -output {target} {tmp}; "
                   f"rc=$?; rm -f {tmp}; exit $rc")
            r = _shell(cmd, timeout=90)
            if r.get("exitCode") != 0:
                return {"ok": False, "error": (r.get("stderr") or "conversion failed").strip()}
            r = {"ok": True, "path": path}
    else:
        return {"ok": False, "error": f"Can't create '{ext}' files. Use .md, .txt, .html, .docx, .rtf, .pdf, .odt or .csv."}
    if not r.get("ok"):
        return r
    check = call("/files/info", {"path": path}, timeout=15)
    size = (check.get("info") or {}).get("size") if isinstance(check, dict) else None
    return {"ok": bool(check.get("ok")) and (size or 0) > 0, "path": path, "format": ext[1:] or "text",
            "size": size, "verified": bool(check.get("ok"))}


def search_documents(query: str, folder: str = "~", limit: int = 20, content: bool = True) -> dict:
    """Finds documents by what's IN them (and their names) using Spotlight:
    notes, PDFs, Word files, slides, emails saved to disk. Much faster and
    broader than walking folders. Read-only.

    Args:
        query: Words to look for, e.g. "induction recurrence midterm".
        folder: Folder to search within, e.g. "~/Documents". "~" searches the whole home folder.
        limit: Maximum results.
        content: True searches file contents and names; False searches names only.
    """
    folder_q = '"$HOME' + folder[1:].replace('"', '\\"') + '"' if folder.startswith("~") else shlex.quote(folder)
    terms = [t for t in re.findall(r"[\w'-]+", query) if len(t) > 1][:8]
    if not terms:
        return {"ok": False, "error": "query needs at least one word"}
    if content:
        expr = " && ".join(f'(kMDItemTextContent == "*{t}*"cdw || kMDItemDisplayName == "*{t}*"cdw)' for t in terms)
    else:
        expr = " && ".join(f'kMDItemDisplayName == "*{t}*"cdw' for t in terms)
    cmd = (f"mdfind -onlyin {folder_q} {shlex.quote(expr)} | grep -v '/Library/' | grep -v '/\\.' | head -n {int(limit) * 3}")
    r = _shell(cmd, timeout=45)
    if r.get("exitCode") not in (0, 1, None):
        return {"ok": False, "error": (r.get("stderr") or "Spotlight search failed").strip()}
    paths = [p for p in (r.get("stdout") or "").splitlines() if p.strip()]
    if not paths:
        return {"ok": True, "count": 0, "results": [], "note": "No matches. Spotlight may not index that folder, "
                                                             "or try fewer/different words."}
    q_terms = [t.lower() for t in terms]

    def score(p: str) -> float:
        name = p.rsplit("/", 1)[-1].lower()
        return sum(2 for t in q_terms if t in name) - p.count("/") * 0.05

    paths = sorted(paths, key=score, reverse=True)[:int(limit)]
    info = _shell("for f in " + " ".join(shlex.quote(p) for p in paths) +
                  "; do stat -f '%m\t%z\t%N' \"$f\"; done", timeout=30)
    rows = []
    for line in (info.get("stdout") or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append({"path": parts[2], "modified": dt.datetime.fromtimestamp(int(parts[0])).isoformat(timespec="minutes"),
                         "size": int(parts[1])})
    return {"ok": True, "count": len(rows), "results": rows or [{"path": p} for p in paths]}


# ---------------------------------------------------------------------------
# Notes (Apple Notes, via AppleScript)
# ---------------------------------------------------------------------------

def notes_search(query: str = "", folder: str = "", limit: int = 20) -> dict:
    """Lists or searches Apple Notes by title and body. Read-only.

    Args:
        query: Text to look for; empty lists the most recent notes.
        folder: Optional folder name to restrict to.
        limit: Maximum notes to return.
    """
    scope = f"notes of folder {_as_string(folder)}" if folder else "notes"
    cond = f" whose (name contains {_as_string(query)} or plaintext contains {_as_string(query)})" if query else ""
    script = f"""
    tell application "Notes"
        set out to ""
        set found to ({scope}{cond})
        set n to 0
        repeat with x in found
            set n to n + 1
            if n > {int(limit)} then exit repeat
            set body to plaintext of x
            if (length of body) > 160 then set body to text 1 thru 160 of body
            set out to out & (id of x) & "@@" & (name of x) & "@@" & ((modification date of x) as string) & "@@" & body & "\\n@@END@@\\n"
        end repeat
        return out
    end tell"""
    r = _script(script, timeout=60)
    if not r.get("ok"):
        return r
    notes = []
    for chunk in str(r.get("result") or "").split("\n@@END@@\n"):
        parts = chunk.strip().split("@@", 3)
        if len(parts) == 4:
            notes.append({"id": parts[0], "title": parts[1], "modified": parts[2], "preview": parts[3]})
    return {"ok": True, "count": len(notes), "notes": notes}


def notes_read(title: str = "", note_id: str = "") -> dict:
    """Reads the full text of one Apple Note, by id (preferred) or exact title. Read-only.

    Args:
        title: The note's title.
        note_id: The note's id from notes_search.
    """
    ref = f"note id {_as_string(note_id)}" if note_id else f"first note whose name is {_as_string(title)}"
    r = _script(f'tell application "Notes" to return (name of {ref}) & "@@" & (plaintext of {ref})', timeout=45)
    if not r.get("ok"):
        return r
    name, _, body = str(r.get("result") or "").partition("@@")
    return {"ok": True, "title": name, "text": body}


def notes_create(title: str, body: str, folder: str = "") -> dict:
    """Creates a new Apple Note. The body can use simple Markdown (headings,
    lists, bold), which is rendered.

    Args:
        title: The note's title.
        body: The note's content.
        folder: Optional folder name (created if missing); default is the Notes default folder.
    """
    html_body = _markdown_to_html(f"# {title}\n\n{body}", title).split("<body>", 1)[-1].rsplit("</body>", 1)[0]
    target = ""
    if folder:
        target = (f"if not (exists folder {_as_string(folder)}) then make new folder with properties "
                  f"{{name:{_as_string(folder)}}}\n            set dest to folder {_as_string(folder)}")
    else:
        target = "set dest to default account"
    script = f"""
    tell application "Notes"
        {target}
        set n to make new note at dest with properties {{body:{_as_string(html_body)}}}
        return id of n
    end tell"""
    r = _script(script, timeout=45)
    if not r.get("ok"):
        return r
    return {"ok": True, "id": r.get("result"), "title": title}


def notes_append(text: str, title: str = "", note_id: str = "") -> dict:
    """Appends text to the end of an existing Apple Note.

    Args:
        text: What to add (simple Markdown allowed).
        title: The note's exact title.
        note_id: The note's id (preferred when known).
    """
    ref = f"note id {_as_string(note_id)}" if note_id else f"first note whose name is {_as_string(title)}"
    addition = _markdown_to_html(text, "").split("<body>", 1)[-1].rsplit("</body>", 1)[0]
    r = _script(f'tell application "Notes"\nset n to {ref}\nset body of n to (body of n) & {_as_string(addition)}\n'
                f'return name of n\nend tell', timeout=45)
    if not r.get("ok"):
        return r
    return {"ok": True, "title": r.get("result")}


# ---------------------------------------------------------------------------
# Apple Music, speech
# ---------------------------------------------------------------------------

def music_control(action: str, query: str = "") -> dict:
    """Controls Apple Music: "play", "pause", "next", "previous",
    "now_playing", "play_playlist" (query = playlist name) or "play_track"
    (query = song title from the library). For Spotify use spotify_control.

    Args:
        action: One of the actions above.
        query: Playlist name or track title, for play_playlist / play_track.
    """
    scripts = {
        "play": 'tell application "Music" to play',
        "pause": 'tell application "Music" to pause',
        "next": 'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
        "play_playlist": f'tell application "Music" to play playlist {_as_string(query)}',
        "play_track": f'tell application "Music" to play (first track of library playlist 1 whose name contains {_as_string(query)})',
    }
    if action not in scripts and action != "now_playing":
        return {"ok": False, "error": f"action must be one of {sorted(list(scripts) + ['now_playing'])}"}
    if action in scripts:
        r = _script(scripts[action], timeout=30)
        if not r.get("ok"):
            return r
    state = _script('tell application "Music"\nif player state is stopped then return "stopped"\n'
                    'return (player state as string) & "@@" & (name of current track) & "@@" & (artist of current track)\n'
                    'end tell', timeout=20)
    parts = str(state.get("result") or "").split("@@")
    return {"ok": True, "action": action, "state": parts[0] if parts else "unknown",
            "track": parts[1] if len(parts) > 1 else None, "artist": parts[2] if len(parts) > 2 else None}


def speak(text: str, voice: str = "", rate: int = 0) -> dict:
    """Says something out loud with macOS text-to-speech.

    Args:
        text: What to say (kept short -- long text takes a long time to speak).
        voice: Optional voice name, e.g. "Samantha". Empty uses the system voice.
        rate: Optional words per minute (e.g. 180). 0 uses the default.
    """
    text = text.strip()[:2000]
    if not text:
        return {"ok": False, "error": "nothing to say"}
    cmd = "say"
    if voice:
        cmd += f" -v {shlex.quote(voice)}"
    if rate:
        cmd += f" -r {int(rate)}"
    r = _shell(f"{cmd} {shlex.quote(text)}", timeout=120)
    return {"ok": r.get("exitCode") == 0, "spoken": text[:200], "error": (r.get("stderr") or "").strip() or None}


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------

_SYSTEM_COMMANDS = {
    "overview": "sw_vers; echo; uptime; echo; pmset -g batt | tail -n +2; echo; df -h / | tail -1",
    "storage": "df -h / /System/Volumes/Data 2>/dev/null; echo; du -sh ~/Downloads ~/Documents ~/Desktop ~/Library/Caches 2>/dev/null",
    "memory": "memory_pressure | tail -n 1; echo; vm_stat | head -n 10; echo; sysctl -n hw.memsize",
    "cpu": "sysctl -n machdep.cpu.brand_string; sysctl -n hw.ncpu; echo; ps -Ao pid,pcpu,pmem,comm -r | head -n 12",
    "processes": "ps -Ao pid,pcpu,pmem,rss,comm -r | head -n 20",
    "network": "networksetup -getairportnetwork en0 2>/dev/null; ipconfig getifaddr en0 2>/dev/null; "
               "scutil --nwi | head -n 12; ping -c 2 -t 3 1.1.1.1 | tail -n 2",
    "battery": "pmset -g batt; echo; system_profiler SPPowerDataType 2>/dev/null | grep -E 'Cycle Count|Condition|Maximum Capacity'",
}


def system_info(section: str = "overview") -> dict:
    """Reports system status. Read-only.

    Args:
        section: "overview", "storage", "memory", "cpu", "processes", "network" or "battery".
    """
    if section not in _SYSTEM_COMMANDS:
        return {"ok": False, "error": f"section must be one of {sorted(_SYSTEM_COMMANDS)}"}
    r = _shell(_SYSTEM_COMMANDS[section], timeout=40)
    return {"ok": r.get("ok", True), "section": section, "output": (r.get("stdout") or "").strip()[:6000]}


def permissions_check() -> dict:
    """Checks which macOS permissions MacBridge has (Accessibility, Screen
    Recording, Calendar, Reminders, Contacts, Notifications, Automation per
    app, Full Disk Access) and explains how to enable any that are missing.
    Read-only; never triggers a permission prompt.
    """
    r = call("/permissions/status", {}, timeout=30)
    if not r.get("ok"):
        return r
    steps = []
    for name in r.get("missing", []):
        entry = r["permissions"][name]
        steps.append(f"{name.replace('_', ' ').title()}: open {entry['where']} and enable MacBridge "
                     f"(needed for {entry['needed_for']}).")
    for app, status in (r["permissions"].get("automation", {}).get("targets") or {}).items():
        if status == "denied":
            steps.append(f"Automation for {app}: {r['permissions']['automation']['where']} -> turn on {app}.")
    r["how_to_fix"] = steps
    return r


def calendar_calendars() -> dict:
    """Lists the calendars available for events and which are writable. Read-only."""
    return call("/calendar/calendars", {}, timeout=20)


def safari_read_page(max_chars: int = 40000) -> dict:
    """Reads the visible text of the page open in Safari's front tab --
    for reading an article, docs page or search result the user has open.
    Read-only.

    Args:
        max_chars: Upper bound on returned characters.
    """
    return call("/safari/page-text", {"maxChars": int(max_chars)}, timeout=30)


# ---------------------------------------------------------------------------
# Git and code
# ---------------------------------------------------------------------------

def _git(repo: str, args: str, timeout: float = 60) -> dict:
    r = _shell(f"git {args}", cwd=repo, timeout=timeout)
    out = (r.get("stdout") or "").rstrip()
    err = (r.get("stderr") or "").strip()
    return {"ok": r.get("exitCode") == 0, "output": out[:12000], "error": err[:2000] or None,
            "exitCode": r.get("exitCode")}


def git_status(repo: str) -> dict:
    """Shows a git repository's branch, ahead/behind and changed files. Read-only.

    Args:
        repo: Path to the repository, e.g. "~/Desktop/Jugnu_test".
    """
    r = _git(repo, "status --short --branch")
    if r["ok"]:
        r["recent_commits"] = _git(repo, "log --oneline -5")["output"]
    return r


def git_diff(repo: str, staged: bool = False, path: str = "", stat_only: bool = False) -> dict:
    """Shows uncommitted changes in a git repository. Read-only.

    Args:
        repo: Path to the repository.
        staged: True shows staged changes instead of unstaged ones.
        path: Optional file or folder to limit the diff to.
        stat_only: True shows only a per-file summary.
    """
    args = "diff" + (" --cached" if staged else "") + (" --stat" if stat_only else "")
    if path:
        args += " -- " + shlex.quote(path)
    return _git(repo, args)


def git_log(repo: str, count: int = 15, path: str = "") -> dict:
    """Shows recent commits. Read-only.

    Args:
        repo: Path to the repository.
        count: How many commits.
        path: Optional file/folder to show history for.
    """
    args = f"log --oneline --decorate -n {max(1, min(int(count), 200))}"
    if path:
        args += " -- " + shlex.quote(path)
    return _git(repo, args)


def git_commit(repo: str, message: str, add_all: bool = False, paths: Optional[list] = None) -> dict:
    """Creates a LOCAL commit (never pushes). Stages the given paths, or
    everything when add_all is true, then commits and reports the result.

    Args:
        repo: Path to the repository.
        message: The commit message.
        add_all: Stage all changes (git add -A) before committing.
        paths: Specific files to stage before committing.
    """
    if not message.strip():
        return {"ok": False, "error": "commit message is required"}
    if add_all:
        staged = _git(repo, "add -A")
    elif paths:
        staged = _git(repo, "add -- " + " ".join(shlex.quote(p) for p in paths))
    else:
        staged = {"ok": True}
    if not staged["ok"]:
        return staged
    r = _git(repo, "commit -m " + shlex.quote(message))
    if r["ok"]:
        r["commit"] = _git(repo, "log --oneline -1")["output"]
    return r


def code_search(pattern: str, path: str, glob: str = "", max_results: int = 80, regex: bool = False) -> dict:
    """Searches code/text files for a pattern (ripgrep when installed, grep
    otherwise), returning file:line:match. Read-only.

    Args:
        pattern: Text (or regex when regex=true) to find.
        path: Folder or file to search, e.g. "~/Desktop/Jugnu_test".
        glob: Optional file filter, e.g. "*.py" or "*.swift".
        max_results: Maximum matching lines.
        regex: Treat pattern as a regular expression.
    """
    n = max(1, min(int(max_results), 500))
    fixed = "" if regex else "-F "
    rg = f"rg --no-heading -n {fixed}--max-columns 300 " + (f"-g {shlex.quote(glob)} " if glob else "") + \
        f"-e {shlex.quote(pattern)} ."
    grep_fixed = "" if regex else "-F "
    grep = f"grep -rIn {grep_fixed}" + (f"--include={shlex.quote(glob)} " if glob else "") + \
        f"--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.build -e {shlex.quote(pattern)} ."
    r = _shell(f"(command -v rg >/dev/null && {rg} || {grep}) | head -n {n}", cwd=path, timeout=60)
    lines = [l for l in (r.get("stdout") or "").splitlines() if l.strip()]
    return {"ok": r.get("exitCode") in (0, 1), "count": len(lines), "matches": lines,
            "error": (r.get("stderr") or "").strip()[:500] or None}


def _detect_test_command(listing: str) -> Optional[str]:
    files = set(listing.split())
    if "Package.swift" in files:
        return "swift test"
    if any(f.endswith(".xcodeproj") for f in files):
        return None
    if "package.json" in files:
        return "npm test --silent"
    if "Cargo.toml" in files:
        return "cargo test"
    if "go.mod" in files:
        return "go test ./..."
    if files & {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"} or "tests" in files:
        return "python3 -m pytest -q"
    if "Makefile" in files:
        return "make test"
    return None


def run_tests(path: str, command: str = "", timeout: int = 900) -> dict:
    """Runs a project's tests and reports pass/fail with the relevant output.
    Detects the test command (swift test, npm test, pytest, cargo test, go
    test, make test) unless one is given.

    Args:
        path: The project folder.
        command: Explicit test command, e.g. "python3 -m unittest discover -s tests".
        timeout: Seconds before the run is stopped (max 1800).
    """
    if not command:
        listing = _shell("ls -a", cwd=path, timeout=15)
        command = _detect_test_command(listing.get("stdout") or "") or ""
        if not command:
            return {"ok": False, "error": "Couldn't tell how to run this project's tests -- pass command explicitly."}
    r = _shell(command + " 2>&1", cwd=path, timeout=min(int(timeout), 1800))
    out = (r.get("stdout") or "")
    tail = out[-8000:]
    summary = next((l for l in reversed(out.splitlines()) if re.search(
        r"(passed|failed|error|OK|FAILED|Executed \d+ tests?|test result:)", l)), "")
    return {"ok": r.get("exitCode") == 0, "passed": r.get("exitCode") == 0, "command": command,
            "exitCode": r.get("exitCode"), "summary": summary.strip()[:300], "output": tail}


# ---------------------------------------------------------------------------
# Daily briefing
# ---------------------------------------------------------------------------

def daily_briefing(location: str = "") -> dict:
    """Gathers everything for a "prepare me for today/tomorrow" briefing in
    one call: today's and tomorrow's calendar, due and overdue reminders,
    upcoming scheduled tasks, unread mail count, and the weather. Read-only.

    Args:
        location: City for the weather, e.g. "Mississauga". Empty uses the
            remembered home city if Jugnu knows it, else skips weather.
    """
    now = dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=2)
    brief: dict = {"ok": True, "now": now.strftime("%A %d %B %Y, %H:%M")}
    cal = call("/calendar/list", {"start": start.isoformat(), "end": end.isoformat()}, timeout=30)
    brief["calendar"] = cal.get("events") if cal.get("ok") else {"error": cal.get("error")}
    rem = call("/reminders/list", {"includeCompleted": False}, timeout=30)
    if rem.get("ok"):
        due = []
        for r in rem.get("reminders", []):
            d = r.get("dueDate")
            if d and d[:10] <= end.date().isoformat():
                due.append(r)
        brief["reminders_due"] = due
        brief["reminders_open"] = len(rem.get("reminders", []))
    else:
        brief["reminders_due"] = {"error": rem.get("error")}
    try:
        import scheduler
        upcoming = [s for s in scheduler.list_schedules(include_disabled=False)
                    if s.get("next_run") and s["next_run"] < end.timestamp()]
        brief["scheduled"] = [{"title": s["title"], "when": s["next_run_text"]} for s in upcoming]
    except Exception:
        pass
    mail = call("/mail/list", {"limit": 25}, timeout=60)
    if mail.get("ok"):
        msgs = mail.get("messages") or []
        brief["unread_mail"] = sum(1 for m in msgs if not m.get("read", True))
        brief["recent_unread_subjects"] = [m.get("subject") for m in msgs if not m.get("read", True)][:8]
    if not location:
        try:
            import memory_store
            hits = memory_store.recall("home city where the user lives", kinds=("user",), k=3, touch=False)
            for h in hits:
                m = re.search(r"(?:city|live[sd]? in|located in|from)[:\s]+([A-Z][A-Za-z .'-]{2,40})", h["content"])
                if m:
                    location = m.group(1).strip()
                    break
        except Exception:
            pass
    if location:
        try:
            from .extra_tools import geocode_location, get_weather
            geo = geocode_location(location, count=1)
            m = re.search(r"^(.*?) — lat=(-?[\d.]+), lon=(-?[\d.]+)", geo, re.M)
            if m:
                brief["weather"] = {"place": m.group(1),
                                    "forecast": get_weather(float(m.group(2)), float(m.group(3)), forecast_days=2)}
            else:
                brief["weather"] = {"error": geo[:200]}
        except Exception as e:
            brief["weather"] = {"error": str(e)[:200]}
    return brief
