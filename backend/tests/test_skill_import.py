from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from fastapi import HTTPException

from access_layer import main


class SkillImportTests(unittest.TestCase):
    def test_imports_valid_skill_zip(self) -> None:
        with TemporaryDirectory() as tmp:
            archive = _zip_bytes({
                "demo-skill/SKILL.md": "---\nname: Demo Skill\ndescription: Useful demo\n---\n\nDo the work.\n",
                "demo-skill/references/guide.md": "Guide",
            })
            with patch.object(main, "PROJECT_SKILLS_DIR", Path(tmp)):
                skill_id, skill_name, skill_dir = main._validate_and_install_skill_zip(archive, "demo.zip")

            self.assertEqual(skill_id, "demo-skill")
            self.assertEqual(skill_name, "Demo Skill")
            self.assertTrue((skill_dir / "SKILL.md").is_file())
            self.assertTrue((skill_dir / "references" / "guide.md").is_file())

    def test_rejects_missing_required_frontmatter(self) -> None:
        archive = _zip_bytes({
            "bad/SKILL.md": "---\nname: Bad Skill\n---\n\nMissing description.\n",
        })

        with TemporaryDirectory() as tmp, patch.object(main, "PROJECT_SKILLS_DIR", Path(tmp)):
            with self.assertRaises(HTTPException) as raised:
                main._validate_and_install_skill_zip(archive, "bad.zip")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("description", str(raised.exception.detail))

    def test_rejects_zip_slip_paths(self) -> None:
        archive = _zip_bytes({
            "bad/SKILL.md": "---\nname: Bad\ndescription: Bad\n---\n",
            "../escape.txt": "nope",
        })

        with TemporaryDirectory() as tmp, patch.object(main, "PROJECT_SKILLS_DIR", Path(tmp)):
            with self.assertRaises(HTTPException) as raised:
                main._validate_and_install_skill_zip(archive, "bad.zip")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("非法路径", str(raised.exception.detail))

    def test_rejects_files_outside_skill_root(self) -> None:
        archive = _zip_bytes({
            "good/SKILL.md": "---\nname: Good\ndescription: Good\n---\n",
            "extra.txt": "unexpected",
        })

        with TemporaryDirectory() as tmp, patch.object(main, "PROJECT_SKILLS_DIR", Path(tmp)):
            with self.assertRaises(HTTPException) as raised:
                main._validate_and_install_skill_zip(archive, "good.zip")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("目录外", str(raised.exception.detail))


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
