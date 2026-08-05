"""Tests for the sibling sweep.

The sweep proposes edits to code the reviewer never mentioned, so the tests
that matter most are the ones asserting it stays quiet: too few sites, a
too-generic pattern, a file outside the diff.
"""

from __future__ import annotations

import json

import pytest

import sweep_siblings as sweeper


UTF8 = '''\
def load_config(path):
    return path.read_text().encode()


def load_manifest(path):
    return path.read_text().encode()


def load_lockfile(path):
    return path.read_text().encode()


def unrelated(value):
    return value * 2
'''

SALT = '''\
def hash_password(password, salt):
    return digest(password, salt.encode())
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text(UTF8)
    (tmp_path / "app" / "auth.py").write_text(SALT)
    (tmp_path / "vendor.py").write_text(UTF8)
    return tmp_path


class TestTokenize:
    def test_a_line_tokenizes_the_same_whatever_its_literals_are(self):
        """Literals vary site to site; the pattern is what is left."""
        assert (sweeper.tokenize("write_file('a.json', 42)")
                == sweeper.tokenize("write_file('totally-different.yaml', 9999)"))

    def test_literals_do_not_leak_into_tokens(self):
        tokens = sweeper.tokenize("greet('hello world', 1234)")
        assert "hello" not in tokens
        assert "1234" not in tokens
        assert "greet" in tokens

    def test_drops_stopwords_and_single_characters(self):
        tokens = sweeper.tokenize("if self.x == y: return z")
        assert "self" not in tokens
        assert "if" not in tokens
        assert "return" not in tokens
        assert "x" not in tokens

    def test_is_case_insensitive(self):
        assert sweeper.tokenize("Path.read_text()") == sweeper.tokenize("path.READ_TEXT()")


class TestInvariantTokens:
    def test_keeps_only_what_every_site_shares(self):
        common = sweeper.invariant_tokens([
            "value = payload.read_text().encode()",
            "other = handle.read_text().encode()",
        ])
        assert "read_text" in common
        assert "encode" in common
        # Present at one site only, so it describes that site, not the pattern.
        assert "payload" not in common
        assert "handle" not in common

    def test_empty_input_yields_nothing(self):
        assert sweeper.invariant_tokens([]) == set()
        assert sweeper.invariant_tokens(["", "   "]) == set()


class TestParseSite:
    @pytest.mark.parametrize("raw,path,line", [
        ("app/config.py:12", "app/config.py", 12),
        ("app/config.py", "app/config.py", None),
        ("C:/src/app.py:3", "C:/src/app.py", 3),
        ("weird:name.py", "weird:name.py", None),
        ("", "", None),
    ])
    def test_splits_on_a_trailing_numeric_line(self, raw, path, line):
        site = sweeper.parse_site(raw)
        assert (site.path, site.line) == (path, line)


class TestSweep:
    def _sweep(self, repo, **overrides):
        kwargs = dict(
            signature="utf8",
            label="missing decode guard",
            sites=["app/config.py:2", "app/config.py:6"],
            changed_files=["app/config.py", "app/auth.py"],
            root=repo,
        )
        kwargs.update(overrides)
        return sweeper.sweep(**kwargs)

    def test_finds_the_sibling_the_reviewer_missed(self, repo):
        result = self._sweep(repo)
        assert result.status == "ok"
        assert [c["site"] for c in result.candidates] == ["app/config.py:10"]
        assert "encode" in result.invariant_tokens

    def test_does_not_re_report_the_flagged_sites(self, repo):
        result = self._sweep(repo)
        flagged = {"app/config.py:2", "app/config.py:6"}
        assert flagged.isdisjoint({c["site"] for c in result.candidates})

    def test_never_leaves_the_changed_file_set(self, repo):
        """vendor.py contains the identical pattern but is not in the diff."""
        result = self._sweep(repo)
        assert all(not c["path"].startswith("vendor") for c in result.candidates)

        widened = self._sweep(repo, changed_files=["app/config.py", "vendor.py"])
        assert any(c["path"] == "vendor.py" for c in widened.candidates)

    def test_a_single_flagged_site_is_not_a_pattern(self, repo):
        result = self._sweep(repo, sites=["app/config.py:2"])
        assert result.status == "too_few_sites"
        assert result.candidates == []

    def test_refuses_when_the_shared_shape_is_too_generic(self, repo):
        """Two sites sharing almost nothing must not sweep the whole diff."""
        (repo / "app" / "thin.py").write_text("alpha(x)\nbravo(y)\ncharlie(z)\n")
        result = self._sweep(
            repo,
            sites=["app/thin.py:1", "app/thin.py:2"],
            changed_files=["app/thin.py"],
        )
        assert result.status == "pattern_too_thin"
        assert result.candidates == []
        assert "Too broad to sweep safely" in result.reason

    def test_a_line_matching_only_some_invariant_tokens_is_not_a_sibling(self, repo):
        """Containment is all-or-nothing; partial overlap is not a match."""
        (repo / "app" / "partial.py").write_text(
            "a = path.read_text().encode()\n"
            "b = other.read_text().encode()\n"
            "c = thing.read_text()\n"          # read_text but no encode
            "d = thing.encode()\n"             # encode but no read_text
        )
        result = self._sweep(
            repo,
            sites=["app/partial.py:1", "app/partial.py:2"],
            changed_files=["app/partial.py"],
        )
        assert result.status == "ok"
        assert result.candidates == []

    def test_stops_when_the_flagged_lines_cannot_be_read(self, repo):
        result = self._sweep(repo, sites=["app/missing.py:2", "app/gone.py:4"])
        assert result.status == "no_source"
        assert result.candidates == []

    def test_out_of_range_line_numbers_do_not_crash(self, repo):
        result = self._sweep(repo, sites=["app/config.py:9999", "app/config.py:2"])
        assert result.status in {"no_source", "too_few_sites", "pattern_too_thin", "ok"}

    def test_a_missing_changed_file_is_skipped_not_fatal(self, repo):
        result = self._sweep(repo, changed_files=["app/config.py", "app/deleted.py"])
        assert result.status == "ok"

    def test_candidates_are_capped_and_the_cap_is_reported(self, repo):
        result = self._sweep(repo, max_candidates=0)
        assert result.truncated is True
        assert result.candidates == []
        assert "refactor, not a sweep" in result.reason

    def test_candidates_are_ordered_by_file_then_line(self, repo):
        (repo / "app" / "extra.py").write_text(UTF8)
        result = self._sweep(
            repo, changed_files=["app/extra.py", "app/config.py"]
        )
        sites = [(c["path"], c["line"]) for c in result.candidates]
        assert sites == sorted(sites)

    def test_every_candidate_carries_its_evidence(self, repo):
        result = self._sweep(repo)
        for candidate in result.candidates:
            assert candidate["matchedTokens"]
            assert candidate["text"]
            assert set(candidate["matchedTokens"]) == set(result.invariant_tokens)

    def test_an_oversized_file_is_skipped(self, repo, monkeypatch):
        monkeypatch.setattr(sweeper, "MAX_FILE_BYTES", 1)
        result = self._sweep(repo)
        assert result.status == "no_source"


class TestReport:
    def test_report_names_the_sites_and_the_shared_shape(self, repo):
        result = sweeper.sweep(
            signature="utf8", label="missing decode guard",
            sites=["app/config.py:2", "app/config.py:6"],
            changed_files=["app/config.py"], root=repo,
        )
        report = sweeper.render_report(result)
        assert "app/config.py:10" in report
        assert "encode" in report
        assert "flagged:" in report

    def test_report_says_so_when_there_is_nothing_to_sweep(self, repo):
        result = sweeper.sweep(
            signature="salt", label="salt encode",
            sites=["app/auth.py:2", "app/auth.py:2"],
            changed_files=["app/auth.py"], root=repo,
        )
        report = sweeper.render_report(result)
        assert "skipped:" in report or "none" in report

    def test_skipped_report_explains_why(self, repo):
        result = sweeper.sweep(
            signature="utf8", label="one site",
            sites=["app/config.py:2"], changed_files=["app/config.py"], root=repo,
        )
        assert "skipped:" in sweeper.render_report(result)


class TestCli:
    def test_json_stdout_is_json_only(self, repo, capsys):
        rc = sweeper.main([
            "--signature", "utf8", "--label", "missing decode guard",
            "--site", "app/config.py:2", "--site", "app/config.py:6",
            "--changed-file", "app/config.py",
            "--root", str(repo), "--json",
        ])
        out = capsys.readouterr()
        payload = json.loads(out.out)
        assert rc == 0
        assert out.err == ""
        assert payload["status"] == "ok"
        assert payload["candidates"][0]["site"] == "app/config.py:10"

    def test_human_output_is_the_report(self, repo, capsys):
        rc = sweeper.main([
            "--signature", "utf8",
            "--site", "app/config.py:2", "--site", "app/config.py:6",
            "--changed-file", "app/config.py", "--root", str(repo),
        ])
        assert rc == 0
        assert "[sweep]" in capsys.readouterr().out

    def test_at_least_two_sites_are_required_end_to_end(self, repo, capsys):
        rc = sweeper.main([
            "--signature", "utf8", "--site", "app/config.py:2",
            "--changed-file", "app/config.py", "--root", str(repo), "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["status"] == "too_few_sites"
        assert payload["candidates"] == []
