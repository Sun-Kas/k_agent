"""access_layer.fs_browse：给"新建 Project"弹窗用的服务端目录浏览/新建接口。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from access_layer.fs_browse import create_directory, list_directories


class ListDirectoriesTests(unittest.TestCase):
    def test_lists_only_direct_subdirectories_sorted_and_hides_noise(self) -> None:
        with TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / "zeta").mkdir()
            (root_path / "Alpha").mkdir()
            (root_path / ".git").mkdir()
            (root_path / "node_modules").mkdir()
            (root_path / "not_a_dir.txt").write_text("x")

            listing = list_directories(str(root_path))

            names = [entry.name for entry in listing.entries]
            self.assertEqual(names, ["Alpha", "zeta"])
            self.assertEqual(listing.path, str(root_path.resolve()))

    def test_parent_is_none_at_filesystem_root(self) -> None:
        listing = list_directories("/")
        self.assertIsNone(listing.parent)

    def test_missing_path_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            list_directories("/this/path/should/not/exist/anywhere")

    def test_file_path_raises_not_a_directory(self) -> None:
        with TemporaryDirectory() as root:
            file_path = Path(root) / "a.txt"
            file_path.write_text("x")
            with self.assertRaises(NotADirectoryError):
                list_directories(str(file_path))

    def test_default_root_is_home_when_path_omitted(self) -> None:
        listing = list_directories(None)
        self.assertEqual(listing.path, str(Path.home().resolve()))


class CreateDirectoryTests(unittest.TestCase):
    def test_creates_subdirectory_and_returns_entry(self) -> None:
        with TemporaryDirectory() as root:
            entry = create_directory(root, "my-new-project")
            # 用 resolve() 对齐：macOS 上 TemporaryDirectory 常落在 /var/..，
            # 而 /var 本身是指向 /private/var 的 symlink，create_directory 内部
            # 会 resolve() 掉这层间接，这里断言也要 resolve() 才能比对上。
            created = (Path(root) / "my-new-project").resolve()
            self.assertTrue(created.is_dir())
            self.assertEqual(entry.path, str(created))
            self.assertEqual(entry.name, "my-new-project")

    def test_rejects_name_with_path_separators(self) -> None:
        with TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                create_directory(root, "a/b")
            with self.assertRaises(ValueError):
                create_directory(root, "..")

    def test_rejects_existing_name(self) -> None:
        with TemporaryDirectory() as root:
            (Path(root) / "dup").mkdir()
            with self.assertRaises(FileExistsError):
                create_directory(root, "dup")

    def test_parent_must_be_a_directory(self) -> None:
        with TemporaryDirectory() as root:
            file_path = Path(root) / "a.txt"
            file_path.write_text("x")
            with self.assertRaises(NotADirectoryError):
                create_directory(str(file_path), "sub")


if __name__ == "__main__":
    unittest.main()
