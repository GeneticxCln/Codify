"""The Settings skill creator: a person makes one workspace Markdown file.

A skill is data, so creating one is not an agent action. The write is the second person-only door
in `docs/00` §6.9: create a new file under `.codify/skills/`, never replace, delete or touch
anything else. These tests exercise the writer directly and through the workspace service; the
HTTP contract is in `tests/test_api.py`.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import tempfile
import unittest
from pathlib import Path
from typing import Any

from engine.conductor_tools import ConductorTools
from engine.db import connect
from engine.fs import FileAlreadyExistsError, FileSystemService
from engine.models import SkillCreate, SkillCreated, SkillImport, WorkspaceCreate
from engine.services import ApiError, WorkspaceService
from engine.skills import SKILLS_DIRNAME, load_skills


class SkillCreatorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "codify.db")
        self.addCleanup(self.conn.close)
        self.service = WorkspaceService(self.conn)
        workspace_root = self.root / "project"
        workspace_root.mkdir()
        self.workspace = self.service.create(
            WorkspaceCreate(name="Project", root_path=str(workspace_root))
        )
        self.workspace_root = workspace_root

    def create(self, **overrides: Any) -> SkillCreated:
        values: dict[str, Any] = {
            "name": "release-check",
            "description": "Checks a release before tagging.",
            "moves": ["recon", "verify"],
            "instructions": "1. Read the manifest.\n2. Run the narrowest check.",
        }
        values.update(overrides)
        return self.service.create_skill(
            self.workspace, SkillCreate(**values), ConductorTools.NAMES
        )

    def test_the_created_file_is_canonical_and_the_loader_sees_it(self) -> None:
        created = self.create()

        self.assertEqual(created.name, "release-check")
        self.assertEqual(created.path, f"{SKILLS_DIRNAME}/release-check.md")
        self.assertFalse(created.shadows)
        path = self.workspace_root / created.path
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: release-check\n"))
        self.assertIn("moves: recon, verify\n", text)
        self.assertTrue(text.endswith("Run the narrowest check.\n"))

        loaded = load_skills(str(self.workspace_root), ConductorTools.NAMES)
        skill = loaded.get("release-check")
        assert skill is not None
        self.assertEqual(skill.source, "workspace")
        self.assertEqual(skill.moves, ("recon", "verify"))
        self.assertEqual(skill.body, "1. Read the manifest.\n2. Run the narrowest check.")

    def test_a_description_with_a_line_break_the_parser_knows_is_refused(self) -> None:
        # `splitlines` breaks on more than `\\n` and `\\r`, and the header is read with it: a form feed in the
        # description started a new header line, so a skill named "safe-name" was written as "ship-a-change".
        for sep in ("\x0c", "\x0b", "\x1c", "\x85", "\u2028", "\u2029", "\x00", "\t"):
            with self.subTest(sep=repr(sep)):
                with self.assertRaises(ApiError) as caught:
                    self.create(name="safe-name", description=f"does things{sep}name: ship-a-change")
                self.assertEqual("invalid_skill_description", caught.exception.code)
        self.assertFalse((self.workspace_root / SKILLS_DIRNAME / "safe-name.md").exists())

    def test_a_name_that_already_exists_is_refused_and_the_file_is_untouched(self) -> None:
        created = self.create()
        path = self.workspace_root / created.path
        before = path.read_bytes()

        with self.assertRaises(ApiError) as caught:
            self.create(instructions="a different skill")

        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "skill_exists")
        self.assertEqual(path.read_bytes(), before)

    def test_a_workspace_skill_can_shadow_a_built_in(self) -> None:
        created = self.create(name="prove", description="Our own proof recipe.")

        self.assertTrue(created.shadows)
        loaded = load_skills(str(self.workspace_root), ConductorTools.NAMES)
        skill = loaded.get("prove")
        assert skill is not None
        self.assertEqual(skill.source, "workspace")
        self.assertIn("Our own proof recipe.", loaded.menu())

    def test_a_stale_switch_is_cleared_so_a_new_skill_starts_on(self) -> None:
        self.service.set_skill_enabled(self.workspace.id, "release-check", False)
        self.assertIn("release-check", self.service.disabled_skills(self.workspace.id))

        created = self.create()

        self.assertEqual(created.name, "release-check")
        self.assertEqual(self.service.disabled_skills(self.workspace.id), set())

    def test_unknown_moves_and_bad_fields_are_refused_before_a_file_is_made(self) -> None:
        cases = (
            {"name": "Bad Name"},
            {"description": "two\nlines"},
            {"moves": ["not_a_move"]},
            {"instructions": "  "},
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ApiError):
                self.create(**fields)

        self.assertFalse((self.workspace_root / SKILLS_DIRNAME).exists())

    def test_import_uses_the_header_name_and_writes_the_text_unchanged(self) -> None:
        text = (
            "---\nname: imported-skill\ndescription: imported, not typed\n"
            "moves: read_file, verify\n---\nRead the file, then verify it.\n"
        )

        created = self.service.import_skill(
            self.workspace,
            SkillImport(filename="ignored-name.md", content=text),
            ConductorTools.NAMES,
        )

        self.assertEqual(created.name, "imported-skill")
        self.assertEqual(created.path, f"{SKILLS_DIRNAME}/imported-skill.md")
        self.assertEqual((self.workspace_root / created.path).read_text(encoding="utf-8"), text)

    def test_an_uploaded_filename_is_never_a_path(self) -> None:
        for filename in ("../escape.md", "nested/escape.md", "escape.txt", "escape"):
            with self.subTest(filename=filename), self.assertRaises(ApiError):
                self.service.import_skill(
                    self.workspace,
                    SkillImport(filename=filename, content="Do the thing.\n"),
                    ConductorTools.NAMES,
                )

        self.assertFalse((self.root / "escape.md").exists())
        self.assertFalse((self.workspace_root / SKILLS_DIRNAME).exists())

    def test_a_linked_skills_directory_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace_root / ".codify").mkdir()
        (self.workspace_root / SKILLS_DIRNAME).symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ApiError) as caught:
            self.create()

        self.assertEqual(caught.exception.code, "skill_directory_refused")
        self.assertEqual([], list(outside.iterdir()))

    def test_the_low_level_writer_is_create_only(self) -> None:
        fs = FileSystemService(str(self.workspace_root))
        fs.create_text("note.md", "one\n")

        with self.assertRaises(FileAlreadyExistsError):
            fs.create_text("note.md", "two\n")

        self.assertEqual((self.workspace_root / "note.md").read_text(encoding="utf-8"), "one\n")

    def test_the_low_level_writer_does_not_follow_a_final_symlink(self) -> None:
        target = self.workspace_root / "real.md"
        target.write_text("real\n", encoding="utf-8")
        (self.workspace_root / "alias.md").symlink_to(target)

        with self.assertRaises(FileAlreadyExistsError):
            FileSystemService(str(self.workspace_root)).create_text("alias.md", "new\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "real\n")
        self.assertTrue((self.workspace_root / "alias.md").is_symlink())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
