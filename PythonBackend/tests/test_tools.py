"""Phase 7: extended tool logic and policies (no MacBridge needed)."""

import _env  # noqa: F401

import unittest

import permissions
import policy
from policy import Risk
from tools import extended_tools as x
from tools.tools import FUNCTIONS, SCHEMAS


class ExtendedToolTests(unittest.TestCase):
    def test_markdown_to_html(self):
        html = x._markdown_to_html("# Title\n\nSome **bold** and *it* `code`.\n\n- a\n- b\n\n1. one\n\n[x](https://e.com)", "T")
        for fragment in ("<h1>Title</h1>", "<strong>bold</strong>", "<em>it</em>", "<code>code</code>",
                         "<ul><li>a</li><li>b</li></ul>", "<ol><li>one</li></ol>", '<a href="https://e.com">x</a>'):
            self.assertIn(fragment, html)
        self.assertIn("&lt;script&gt;", x._markdown_to_html("<script>alert(1)</script>", "T"), "HTML must be escaped")

    def test_test_command_detection(self):
        self.assertEqual(x._detect_test_command("Package.swift Sources Tests"), "swift test")
        self.assertEqual(x._detect_test_command("package.json src"), "npm test --silent")
        self.assertEqual(x._detect_test_command("pyproject.toml src tests"), "python3 -m pytest -q")
        self.assertIsNone(x._detect_test_command("README.md"))

    def test_every_tool_has_schema_and_policy(self):
        names = {s["function"]["name"] for s in SCHEMAS}
        self.assertEqual(names, set(FUNCTIONS))
        unclassified = [n for n in FUNCTIONS if n not in policy.TOOL_POLICIES]
        self.assertEqual(unclassified, [], "every tool needs an explicit risk level")


class ExtendedPolicyTests(unittest.TestCase):
    def setUp(self):
        permissions.save({})

    def test_dev_tools_need_project_folder(self):
        inside = policy.assess("run_tests", {"path": "~/Desktop/Jugnu_test"})
        self.assertFalse(inside.out_of_scope)
        self.assertEqual(inside.risk, Risk.LOW)
        outside = policy.assess("git_commit", {"repo": "~/Library/Something", "message": "x"})
        self.assertTrue(outside.out_of_scope)

    def test_run_tests_custom_command_classified(self):
        a = policy.assess("run_tests", {"path": "~/Desktop/Jugnu_test", "command": "rm -rf build && pytest"})
        self.assertEqual(a.risk, Risk.HIGH)

    def test_document_scopes(self):
        self.assertTrue(policy.assess("document_read", {"path": "~/.ssh/id_rsa"}).protected)
        self.assertEqual(policy.assess("document_create", {"path": "~/Documents/x.pdf", "content": "c"}).risk, Risk.LOW)
        self.assertEqual(policy.assess("document_create", {"path": "~/Documents/x.pdf", "content": "c",
                                                           "overwrite": True}).risk, Risk.MEDIUM)
        self.assertTrue(policy.assess("document_create", {"path": "/Applications/x.pdf", "content": "c"}).out_of_scope
                        or policy.assess("document_create", {"path": "/Applications/x.pdf", "content": "c"}).protected)

    def test_window_close_confirmed_in_balanced(self):
        a = policy.assess("window_close", {"app": "TextEdit"})
        self.assertEqual(policy.decide(a, mode="balanced").action, "confirm")
        self.assertEqual(policy.decide(policy.assess("window_arrange", {"app": "Safari", "layout": "left-half"}),
                                       mode="balanced").action, "allow")


if __name__ == "__main__":
    unittest.main()
