"""Project（项目制）阶段一核心行为：ProjectStore 校验/CAS + SessionStore 归属存储布局。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from access_layer.projects.models import ProjectCreateInput, ProjectUpdateInput
from access_layer.projects.store import ProjectStore, ProjectValidationError
from access_layer.sessions.store import SessionBusyError, SessionStore
from access_layer.storage.file import FileStorage


class ProjectStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_rejects_relative_and_system_root_paths(self) -> None:
        with TemporaryDirectory() as home:
            storage = FileStorage(Path(home) / "state")
            store = ProjectStore(storage)

            with self.assertRaises(ProjectValidationError):
                await store.create(ProjectCreateInput(rootPath="relative/dir"))
            with self.assertRaises(ProjectValidationError):
                await store.create(ProjectCreateInput(rootPath="/"))

    async def test_create_dedupes_by_root_path_and_defaults_name(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            store = ProjectStore(storage)

            first = await store.create(ProjectCreateInput(rootPath=project_dir))
            second = await store.create(ProjectCreateInput(rootPath=project_dir, name="ignored on dedupe"))

            self.assertEqual(first.id, second.id)
            self.assertEqual(first.name, Path(project_dir).resolve().name)
            self.assertEqual(len(await store.list()), 1)

    async def test_update_rename_and_delete_only_removes_index(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            store = ProjectStore(storage)
            project = await store.create(ProjectCreateInput(rootPath=project_dir))

            renamed = await store.update(project.id, ProjectUpdateInput(name="新名字"))
            assert renamed is not None
            self.assertEqual(renamed.name, "新名字")
            # 改名不触碰磁盘：root_path 指向的目录依然存在且未被重命名。
            self.assertTrue(Path(project_dir).is_dir())

            deleted = await store.delete(project.id)
            self.assertTrue(deleted)
            self.assertIsNone(await store.get(project.id))
            # 删除只影响索引：磁盘目录必须还在。
            self.assertTrue(Path(project_dir).is_dir())

    async def test_index_survives_restart(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            store = ProjectStore(storage)
            created = await store.create(ProjectCreateInput(rootPath=project_dir, name="备份"))

            reloaded_store = ProjectStore(storage)
            reloaded = await reloaded_store.get(created.id)
            assert reloaded is not None
            self.assertEqual(reloaded.name, "备份")
            self.assertEqual(reloaded.root_path, created.root_path)


class ProjectBoundSessionStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_session_bundle_lives_under_projects_tree_without_workspace_dir(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            project_store = ProjectStore(storage)
            project = await project_store.create(ProjectCreateInput(rootPath=project_dir))

            session_store = SessionStore(storage, project_store=project_store)
            session = await session_store.create_session(project_id=project.id)

            bundle = Path(home) / "state" / "projects" / project.id / "sessions" / session.id
            self.assertTrue((bundle / "session.json").is_file())
            self.assertFalse((bundle / "workspace").exists())
            # 平铺布局里不应该再出现这个会话。
            self.assertFalse((Path(home) / "state" / "sessions" / session.id).exists())

            refreshed = await project_store.get(project.id)
            assert refreshed is not None
            self.assertEqual(refreshed.session_count, 1)

    async def test_restart_discovers_sessions_from_both_trees(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            project_store = ProjectStore(storage)
            project = await project_store.create(ProjectCreateInput(rootPath=project_dir))

            store = SessionStore(storage, project_store=project_store)
            bound = await store.create_session(project_id=project.id)
            plain = await store.create_session()

            fresh_store = SessionStore(storage, project_store=project_store)
            summaries = {summary.id: summary for summary in await fresh_store.list_summaries()}
            self.assertEqual(summaries[bound.id].project_id, project.id)
            self.assertIsNone(summaries[plain.id].project_id)

    async def test_delete_project_session_removes_only_its_own_bundle(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            project_store = ProjectStore(storage)
            project = await project_store.create(ProjectCreateInput(rootPath=project_dir))
            store = SessionStore(storage, project_store=project_store)
            session = await store.create_session(project_id=project.id)

            deleted = await store.delete_session(session.id)
            self.assertTrue(deleted)
            bundle = Path(home) / "state" / "projects" / project.id / "sessions" / session.id
            self.assertFalse(bundle.exists())
            refreshed = await project_store.get(project.id)
            assert refreshed is not None
            self.assertEqual(refreshed.session_count, 0)
            # 删除会话绝不能碰 Project 的 root_path。
            self.assertTrue(Path(project_dir).is_dir())


class BindProjectMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bind_project_copies_workspace_into_root_and_drops_private_workspace(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            project_store = ProjectStore(storage)
            project = await project_store.create(ProjectCreateInput(rootPath=project_dir))

            store = SessionStore(storage, project_store=project_store)
            session = await store.create_session()
            old_workspace = Path(home) / "state" / "sessions" / session.id / "workspace"
            old_workspace.mkdir(parents=True, exist_ok=True)
            (old_workspace / "notes.md").write_text("hello", encoding="utf-8")

            migrated = await store.bind_project(session.id, project.id, project.root_path)
            assert migrated is not None
            self.assertEqual(migrated.project_id, project.id)

            # 文件已经并入 Project 根目录。
            self.assertEqual((Path(project_dir) / "notes.md").read_text(encoding="utf-8"), "hello")
            # 旧的平铺簿记目录已经不存在；新的簿记目录里没有 workspace/。
            self.assertFalse((Path(home) / "state" / "sessions" / session.id).exists())
            new_bundle = Path(home) / "state" / "projects" / project.id / "sessions" / session.id
            self.assertTrue((new_bundle / "session.json").is_file())
            self.assertFalse((new_bundle / "workspace").exists())

    async def test_bind_project_rejects_active_session(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as project_dir:
            storage = FileStorage(Path(home) / "state")
            project_store = ProjectStore(storage)
            project = await project_store.create(ProjectCreateInput(rootPath=project_dir))
            store = SessionStore(storage, project_store=project_store)
            session = await store.create_session()
            store._active_run_ids[session.id] = "run-1"  # noqa: SLF001 模拟正在运行

            with self.assertRaises(SessionBusyError):
                await store.bind_project(session.id, project.id, project.root_path)


if __name__ == "__main__":
    unittest.main()
