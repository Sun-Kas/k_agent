import unittest

from backend.tools.file_hunk import hunks_for_edit, unified_hunks


class FileHunkTests(unittest.TestCase):
    def test_edit_uses_match_offset_and_file_context(self) -> None:
        before = "\n".join(f"line {index}" for index in range(1, 11)) + "\n"
        hunks = hunks_for_edit(before, "line 5", "LINE 5")
        self.assertEqual(len(hunks), 1)
        hunk = hunks[0]
        self.assertEqual(hunk["oldStart"], 2)
        self.assertEqual(hunk["newStart"], 2)
        kinds = [
            (line["kind"], line.get("oldLine"), line.get("newLine"), line["text"])
            for line in hunk["lines"]
        ]
        self.assertEqual(kinds[0], ("ctx", 2, 2, "line 2"))
        self.assertIn(("del", 5, None, "line 5"), kinds)
        self.assertIn(("add", None, 5, "LINE 5"), kinds)
        self.assertEqual(kinds[-1], ("ctx", 8, 8, "line 8"))

    def test_edit_collapses_unique_match_padding_to_nearby_context(self) -> None:
        body = [
            "keep1",
            "keep2",
            "keep3",
            "keep4",
            "CHANGE",
            "keep5",
            "keep6",
            "keep7",
            "keep8",
        ]
        before = "\n".join(["a", "b", "c", *body, "x", "y", "z"]) + "\n"
        old = "\n".join(body)
        new = old.replace("CHANGE", "CHANGED")
        hunks = hunks_for_edit(before, old, new)
        texts = [line["text"] for line in hunks[0]["lines"]]
        self.assertNotIn("a", texts)
        self.assertNotIn("keep1", texts)
        self.assertIn("keep4", texts)
        self.assertIn("CHANGE", texts)
        self.assertIn("CHANGED", texts)
        self.assertIn("keep5", texts)
        self.assertNotIn("keep8", texts)

    def test_replace_all_emits_one_hunk_per_match(self) -> None:
        before = "foo\nkeep\nfoo\n"
        hunks = hunks_for_edit(before, "foo", "bar", replace_all=True)
        self.assertEqual(len(hunks), 2)
        deleted = [
            line.get("oldLine")
            for hunk in hunks
            for line in hunk["lines"]
            if line["kind"] == "del"
        ]
        self.assertEqual(deleted, [1, 3])

    def test_new_file_write_is_numbered_additions(self) -> None:
        hunks = unified_hunks("", "alpha\nbeta\n")
        self.assertEqual(len(hunks), 1)
        self.assertEqual(
            [(line["kind"], line["newLine"], line["text"]) for line in hunks[0]["lines"]],
            [("add", 1, "alpha"), ("add", 2, "beta")],
        )


if __name__ == "__main__":
    unittest.main()
