"""access_layer.projects.scaffold：新建 Project 时的最小骨架（git init + .gitignore + README）。"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from access_layer.projects.scaffold import scaffold_new_project

_HAS_GIT = shutil.which("git") is not None


class ScaffoldNewProjectTests(unittest.TestCase):
    @unittest.skipUnless(_HAS_GIT, "git not installed in this environment")
    def test_initializes_git_and_writes_gitignore_and_readme_in_empty_dir(self) -> None:
        with TemporaryDirectory() as root:
            root_path = Path(root)
            scaffold_new_project(root_path, "my-app")

            self.assertTrue((root_path / ".git").is_dir())
            self.assertTrue((root_path / ".gitignore").is_file())
            self.assertTrue((root_path / "README.md").is_file())
            self.assertEqual((root_path / "README.md").read_text(encoding="utf-8"), "# my-app\n")
            # 不自动打 initial commit：unborn branch 没有 HEAD。
            git_dir = root_path / ".git"
            self.assertFalse((git_dir / "refs" / "heads" / "main").exists())
            self.assertFalse((git_dir / "refs" / "heads" / "master").exists())

    def test_never_overwrites_existing_git_repo_or_files(self) -> None:
        with TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / ".git").mkdir()  # 模拟"用户选中了一个已有的真实 git 仓库"
            (root_path / ".gitignore").write_text("custom\n", encoding="utf-8")
            (root_path / "README.md").write_text("# 原有内容\n", encoding="utf-8")

            scaffold_new_project(root_path, "ignored-name")

            self.assertEqual((root_path / ".gitignore").read_text(encoding="utf-8"), "custom\n")
            self.assertEqual((root_path / "README.md").read_text(encoding="utf-8"), "# 原有内容\n")

    @unittest.skipUnless(_HAS_GIT, "git not installed in this environment")
    def test_only_fills_in_missing_pieces_when_git_already_initialized(self) -> None:
        with TemporaryDirectory() as root:
            root_path = Path(root)
            scaffold_new_project(root_path, "first-pass")
            # 第二次调用（模拟 root_path 被复用/重复创建请求）不应该报错，
            # 也不应该改动已经写好的内容——整个函数应该是幂等的。
            scaffold_new_project(root_path, "second-pass")

            self.assertEqual((root_path / "README.md").read_text(encoding="utf-8"), "# first-pass\n")

    def test_missing_git_binary_does_not_raise(self) -> None:
        with TemporaryDirectory() as root:
            root_path = Path(root)
            # 模拟本机没装 git（subprocess.run 找不到可执行文件时就是 OSError）：
            # 验证 git init 失败被内部 try/except 吞掉并仅记日志，.gitignore/
            # README 仍然照常写出来，整个函数本身绝不向调用方抛异常。
            with patch("access_layer.projects.scaffold.subprocess.run", side_effect=OSError("no git")):
                scaffold_new_project(root_path, "no-git-case")
            self.assertFalse((root_path / ".git").exists())
            self.assertTrue((root_path / ".gitignore").is_file())
            self.assertTrue((root_path / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
