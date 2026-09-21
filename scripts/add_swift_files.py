#!/usr/bin/env python3
"""Adds Swift source files to Jugnu.xcodeproj (explicit file references).

usage: scripts/add_swift_files.py Views/Foo.swift Models/Bar.swift ...

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


if __name__ == "__main__":
    add(sys.argv[1:])
