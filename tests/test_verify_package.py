"""`scripts/verify_package.py`: a package either matches the release's checksums or it does not.

The three failures this has to tell apart are the three a person would act on differently: a file
that is not there, a file the release never listed (a different release, or a browser's `(1)` suffix
on the name), and a file whose bytes are not the published ones. So the tests below are about the
*sentences* as much as the exit codes — a check that reports "mismatch" for a name it never matched
sends the reader to re-download a package that was fine.

The one path not exercised here is the fetch: it reaches GitHub, so what is pinned is the address it
would reach and the tag it refuses to build one from.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts.verify_package import (
    LATEST_SUMS,
    downloads_dir,
    digest_of,
    fetch_sums,
    main,
    packages_in,
    parse_sums,
    sums_url,
    verify,
)

DEB = "Codify_1.0.0_amd64.deb"
APPIMAGE = "Codify_1.0.0_amd64.AppImage"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestReadingAChecksumsFile(unittest.TestCase):
    def test_sha256sum_output_is_read_as_names_and_digests(self) -> None:
        digest = sha256(b"a deb")
        sums = parse_sums(f"{digest}  {DEB}\n{sha256(b'x')}  {APPIMAGE}\n")
        self.assertEqual({DEB: digest, APPIMAGE: sha256(b"x")}, sums)

    def test_the_binary_mode_marker_is_the_same_thing(self) -> None:
        # `sha256sum -b` writes `hash *name`; it is the same digest of the same file.
        digest = sha256(b"a deb")
        self.assertEqual({DEB: digest}, parse_sums(f"{digest} *{DEB}\n"))

    def test_digests_are_compared_case_insensitively(self) -> None:
        # A checksums file is hex; a person pasting one in from somewhere may have uppercased it.
        self.assertEqual({DEB: sha256(b"a deb")}, parse_sums(f"{sha256(b'a deb').upper()}  {DEB}\n"))

    def test_blank_lines_are_not_a_malformed_file(self) -> None:
        sums = parse_sums(f"\n{sha256(b'a deb')}  {DEB}\n\n")
        self.assertEqual([DEB], list(sums))

    def test_a_line_that_is_not_sha256sum_output_is_named_by_its_number(self) -> None:
        # Skipping it would read as "this file is not listed", and the reader would go looking for
        # the wrong release rather than at a mangled checksums file.
        with self.assertRaises(ValueError) as caught:
            parse_sums(f"{sha256(b'a deb')}  {DEB}\n# Codify 1.0.0\n")
        self.assertIn("line 2", str(caught.exception))
        self.assertIn("# Codify", str(caught.exception))

    def test_a_checksums_file_with_no_entries_is_refused(self) -> None:
        # Comparing against nothing would report every package as unlisted, which is not a
        # checksum failure and would send the reader to the wrong fix.
        with self.assertRaises(ValueError) as caught:
            parse_sums("\n\n")
        self.assertIn("list no files", str(caught.exception))


class TestAddressesAndFolders(unittest.TestCase):
    def test_the_newest_release_is_the_address_the_app_itself_reads(self) -> None:
        self.assertEqual(LATEST_SUMS, sums_url(None))
        self.assertTrue(sums_url(None).startswith("https://github.com/GeneticxCln/Codify/releases/"))

    def test_a_tag_becomes_that_release_address(self) -> None:
        self.assertEqual(
            "https://github.com/GeneticxCln/Codify/releases/download/v1.0.0/SHA256SUMS",
            sums_url("v1.0.0"),
        )
        self.assertEqual(sums_url("1.0.0"), sums_url("v1.0.0"), "the `v` is the tag, not the version")

    def test_a_tag_that_is_not_one_never_reaches_an_address(self) -> None:
        for tag in ["../../etc/passwd", "v1.0.0/../..", "nightly", "", "v", "1.0.0 ", "v1.0.0\n"]:
            with self.assertRaises(ValueError, msg=f"{tag!r} was put in a URL"):
                sums_url(tag)

    def test_unreadable_checksums_are_a_sentence_not_a_traceback(self) -> None:
        with self.assertRaises(ValueError) as caught:
            fetch_sums("https://127.0.0.1:9/SHA256SUMS", timeout=1)
        self.assertIn("could not read the checksums from", str(caught.exception))

    def test_no_named_folder_means_the_downloads_folder(self) -> None:
        self.assertEqual(Path.home() / "Downloads", downloads_dir(None))
        self.assertEqual(Path("/somewhere/else"), downloads_dir("/somewhere/else"))


class TestHashingAFile(unittest.TestCase):
    def test_a_files_digest_is_its_bytes_digest(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / DEB
            package.write_bytes(b"a deb")
            self.assertEqual(sha256(b"a deb"), digest_of(package))

    def test_a_file_that_cannot_be_read_is_a_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(ValueError) as caught:
                digest_of(Path(scratch) / "nothing-here.deb")
            self.assertIn("could not be read", str(caught.exception))
            # A directory is not a package either, and open() says so rather than raising something
            # a caller has to guess about.
            with self.assertRaises(ValueError):
                digest_of(Path(scratch))


class TestCheckingOneFile(unittest.TestCase):
    def test_a_file_that_matches_is_reported_with_its_digest(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / DEB
            package.write_bytes(b"a deb")
            (result,) = verify([package], {DEB: sha256(b"a deb")})
            self.assertIsNone(result.problem)
            self.assertEqual(sha256(b"a deb"), result.digest)
            self.assertEqual(package, result.path)

    def test_bytes_that_are_not_the_published_ones_name_both_digests(self) -> None:
        # A truncated download is the case this exists for, and the reader needs both numbers to
        # see that it is the file on disk and not the checksums that are wrong.
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / DEB
            package.write_bytes(b"a truncated de")
            published = sha256(b"a deb")
            (result,) = verify([package], {DEB: published})
            problem = result.problem or ""
            self.assertIn(published, problem)
            self.assertIn(sha256(b"a truncated de"), problem)
            self.assertIn("not the published file", problem)

    def test_a_file_the_release_never_listed_is_not_called_a_mismatch(self) -> None:
        # A browser's `(1)` suffix, or a package from a different release: neither is a corrupt
        # download, and saying "mismatch" would send the reader to the wrong fix.
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / "Codify_1.0.0_amd64(1).deb"
            package.write_bytes(b"a deb")
            (result,) = verify([package], {DEB: sha256(b"a deb")})
            self.assertIsNone(result.digest, "nothing was compared, so no digest is claimed")
            self.assertIn("does not list", result.problem or "")
            self.assertIn("Codify_1.0.0_amd64(1).deb", result.problem or "")

    def test_a_file_that_is_not_there_is_its_own_sentence(self) -> None:
        missing = Path(tempfile.gettempdir()) / "codify-verify-package-not-here.deb"
        (result,) = verify([missing], {DEB: sha256(b"a deb")})
        self.assertIn("there is no file at", result.problem or "")

    def test_a_file_is_matched_by_its_name_wherever_it_is(self) -> None:
        # The release lists names, not paths, and a package is checked wherever a person left it.
        with tempfile.TemporaryDirectory() as scratch:
            nested = Path(scratch) / "some" / "folder"
            nested.mkdir(parents=True)
            package = nested / DEB
            package.write_bytes(b"a deb")
            (result,) = verify([package], {DEB: sha256(b"a deb")})
            self.assertIsNone(result.problem)


class TestTheCommandItself(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_matching_package_and_a_checksums_file_you_have(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / DEB
            package.write_bytes(b"a deb")
            sums = Path(scratch) / "SHA256SUMS"
            sums.write_text(f"{sha256(b'a deb')}  {DEB}\n", encoding="utf-8")
            code, out, err = self.run_main(str(package), "--sums", str(sums))
            self.assertEqual(0, code, err)
            self.assertIn("ok    Codify_1.0.0_amd64.deb", out)
            # What a pass claims and what it does not, said where the person reads it.
            self.assertIn("not a signature", out)

    def test_a_package_that_does_not_match_fails_and_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / DEB
            package.write_bytes(b"not the deb")
            sums = Path(scratch) / "SHA256SUMS"
            sums.write_text(f"{sha256(b'a deb')}  {DEB}\n", encoding="utf-8")
            code, out, err = self.run_main(str(package), "--sums", str(sums))
            self.assertEqual(1, code)
            self.assertIn("FAIL", out)
            self.assertIn("did not match", err)
            self.assertNotIn("not a signature", out, "a pass was claimed for a mismatch")

    def test_a_mangled_checksums_file_is_refused_before_anything_is_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / DEB
            package.write_bytes(b"a deb")
            sums = Path(scratch) / "SHA256SUMS"
            sums.write_text("this is not a checksums file\n", encoding="utf-8")
            code, out, err = self.run_main(str(package), "--sums", str(sums))
            self.assertEqual(1, code)
            self.assertEqual("", out, "a half-answer was printed before the refusal")
            self.assertIn("line 1", err)

    def test_no_package_named_and_none_to_find_says_where_it_looked(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            sums = Path(scratch) / "SHA256SUMS"
            sums.write_text(f"{sha256(b'a deb')}  {DEB}\n", encoding="utf-8")
            code, _out, err = self.run_main("--sums", str(sums), "--downloads", scratch)
            self.assertEqual(2, code)
            self.assertIn(scratch, err)
            self.assertIn("--downloads", err)

    def test_with_no_package_named_it_checks_what_is_in_the_downloads_folder(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / DEB).write_bytes(b"a deb")
            (Path(scratch) / APPIMAGE).write_bytes(b"an appimage")
            # A checksums file and a source tarball sitting beside them are not packages this app
            # published, and checking them would report two failures that are not failures.
            (Path(scratch) / "SHA256SUMS").write_text("x  y\n", encoding="utf-8")
            (Path(scratch) / "source.tar.gz").write_bytes(b"a tarball")
            sums = Path(scratch) / "sums"
            sums.write_text(
                f"{sha256(b'a deb')}  {DEB}\n{sha256(b'an appimage')}  {APPIMAGE}\n", encoding="utf-8"
            )
            code, out, err = self.run_main("--sums", str(sums), "--downloads", scratch)
            self.assertEqual(0, code, err)
            self.assertIn(DEB, out)
            self.assertIn(APPIMAGE, out)
            self.assertNotIn("source.tar.gz", out)

    def test_the_packages_it_looks_for_are_this_apps_two_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            for name in (DEB, APPIMAGE, "Codify.deb.sig", "SHA256SUMS", "other.deb"):
                (Path(scratch) / name).write_bytes(b"x")
            self.assertEqual(
                sorted([Path(scratch) / DEB, Path(scratch) / APPIMAGE]),
                sorted(packages_in(Path(scratch))),
            )
            self.assertEqual([], packages_in(Path(scratch) / "nowhere-in-particular"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
