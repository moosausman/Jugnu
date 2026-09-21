#!/usr/bin/env python3
"""Adds Swift source files to Jugnu.xcodeproj (explicit file references).

usage: scripts/add_swift_files.py Views/Foo.swift Models/Bar.swift ...
       scripts/add_swift_files.py --remove Views/Old.swift ...

The project lists every file explicitly rather than syncing folders, so a
new .swift file on disk isn't compiled until it has a PBXFileReference, a
PBXBuildFile, a place in its folder's PBXGroup, and an entry in the Sources
build phase. Idempotent: files already in the project are skipped.
"""

import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBX = os.path.join(ROOT, "Jugnu.xcodeproj", "project.pbxproj")


def _id(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()[:24].upper()


def add(paths: list[str]) -> None:
    text = open(PBX).read()
    for rel in paths:
        folder, name = os.path.split(rel)
        if not os.path.exists(os.path.join(ROOT, rel)):
            sys.exit(f"missing on disk: {rel}")
        if re.search(rf"/\* {re.escape(name)} \*/ = {{isa = PBXFileReference", text):
            print(f"already in project: {rel}")
            continue
        ref, build = _id("ref:" + rel), _id("build:" + rel)
        text = text.replace(
            "/* End PBXFileReference section */",
            f"\t\t{ref} /* {name} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; "
            f"path = {name}; sourceTree = \"<group>\"; }};\n/* End PBXFileReference section */", 1)
        text = text.replace(
            "/* End PBXBuildFile section */",
            f"\t\t{build} /* {name} in Sources */ = {{isa = PBXBuildFile; fileRef = {ref} /* {name} */; }};\n"
            "/* End PBXBuildFile section */", 1)
        group = re.search(r"(isa = PBXGroup;\s*children = \()([^()]*)(\);\s*path = " + re.escape(folder) + r";)",
                          text, re.S)
        if not group:
            sys.exit(f"no PBXGroup with path = {folder}")
        text = text[:group.end(2)] + f"\t\t\t\t{ref} /* {name} */,\n\t\t\t" + text[group.end(2):]
        phase = re.search(r"(isa = PBXSourcesBuildPhase;.*?files = \()(.*?)(\);)", text, re.S)
        text = text[:phase.end(2)] + f"\t\t\t\t{build} /* {name} in Sources */,\n\t\t\t" + text[phase.end(2):]
        print(f"added: {rel}")
    open(PBX, "w").write(text)


def remove(paths: list[str]) -> None:
    """Drops files from the project (all four places). The files on disk are left alone."""
    text = open(PBX).read()
    for rel in paths:
        name = os.path.basename(rel)
        ref = re.search(rf"(\w{{24}}) /\* {re.escape(name)} \*/ = {{isa = PBXFileReference", text)
        build = re.search(rf"(\w{{24}}) /\* {re.escape(name)} in Sources \*/ = {{isa = PBXBuildFile", text)
        if not ref:
            print(f"not in project: {rel}")
            continue
        ids = [ref.group(1)] + ([build.group(1)] if build else [])
        text = "".join(line for line in text.splitlines(keepends=True) if not any(i in line for i in ids))
        print(f"removed: {rel}")
    open(PBX, "w").write(text)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--remove"]:
        remove(sys.argv[2:])
    else:
        add(sys.argv[1:])
