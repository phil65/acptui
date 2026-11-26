"""Parsing git patches into files, hunks and lines."""

from acptui.protocol.diff import parse_patch, side_by_side


GIT_PATCH = """\
diff --git a/src/app.py b/src/app.py
index 83db48f..bf269f4 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,4 @@ def main():
 import sys
--- removed line that looks like a header
-old = 1
+new = 1
+extra = 2
 print(sys.argv)
\\ No newline at end of file
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
"""

ABSOLUTE = """\
diff --git /work/a.txt /work/a.txt
--- /work/a.txt
+++ /work/a.txt
@@ -1 +1,2 @@
 hello
+world
"""


def test_parse_git_patch() -> None:
    app, new = parse_patch(GIT_PATCH)

    assert app.path == "src/app.py"
    assert app.old_path == "src/app.py"
    (hunk,) = app.hunks
    assert hunk.header == "def main():"
    assert [line.kind for line in hunk.lines] == [
        "context",
        "remove",
        "remove",
        "add",
        "add",
        "context",
    ]
    assert hunk.lines[1].text == "-- removed line that looks like a header"
    assert (hunk.lines[3].old_no, hunk.lines[3].new_no) == (None, 2)
    assert (hunk.lines[-1].old_no, hunk.lines[-1].new_no) == (4, 4)
    assert (app.added, app.removed) == (2, 2)

    assert new.old_path is None
    assert new.path == "new.txt"
    assert new.added == 1


def test_parse_absolute_paths_without_prefixes() -> None:
    (diff,) = parse_patch(ABSOLUTE)

    assert diff.path == "/work/a.txt"
    assert [line.text for line in diff.hunks[0].lines] == ["hello", "world"]


def test_plain_unified_diff_without_git_header() -> None:
    (diff,) = parse_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")

    assert diff.path == "x"
    assert (diff.added, diff.removed) == (1, 1)


def test_side_by_side_pairs_changes() -> None:
    (diff,) = parse_patch(GIT_PATCH)[:1]
    rows = side_by_side(diff.hunks[0])

    pairs = [(left.text if left else None, right.text if right else None) for left, right in rows]
    assert pairs == [
        ("import sys", "import sys"),
        ("-- removed line that looks like a header", "new = 1"),
        ("old = 1", "extra = 2"),
        ("print(sys.argv)", "print(sys.argv)"),
    ]


def test_wrong_hunk_counts_do_not_swallow_the_next_file() -> None:
    patch = "--- a/x\n+++ b/x\n@@ -1 +1,3 @@\n-a\n+b\ndiff --git a/y b/y\n@@ -1 +1 @@\n-c\n+d\n"
    x, y = parse_patch(patch)

    assert (x.added, x.removed) == (1, 1)
    assert y.path == "y"
    assert (y.added, y.removed) == (1, 1)


def test_garbage_is_ignored() -> None:
    assert parse_patch("not a patch\n@@ broken\n") == []
