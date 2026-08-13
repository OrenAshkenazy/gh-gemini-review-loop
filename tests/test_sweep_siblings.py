"""Tests for the sibling sweep.

The sweep proposes edits to code the reviewer never mentioned, so the tests
that matter most are the ones asserting it stays quiet: too few sites, a
too-generic pattern, a file outside the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sweep_siblings as sweeper


MIRROR_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "mirror_corpus.json").read_text()
)


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

    @pytest.mark.parametrize("token", ["name", "snake_case", "x1", "A"])
    def test_letter_tokens_are_significant(self, token):
        assert sweeper.is_significant_token(token)

    @pytest.mark.parametrize("token", ["!", "&&", "=>", ");"])
    def test_punctuation_tokens_are_not_significant(self, token):
        assert not sweeper.is_significant_token(token)


class TestNormalizeBlock:
    def test_strips_comments_and_collapses_whitespace(self):
        assert sweeper.normalize_block([
            "  const   options = values; // kept in sync",
            "/* block comment",
            "   continued */  return   options;",
        ]) == ("const options = values;", "return options;")

    def test_preserves_comment_markers_in_strings_and_decrement_operators(self):
        assert sweeper.normalize_block([
            'const url = "https://example.test/a";',
            "remaining--;",
        ]) == ('const url = "https://example.test/a";', "remaining--;")

    def test_preserves_hash_syntax_unless_the_file_uses_hash_comments(self):
        lines = ["#ifdef WINDOWS", "color: #fff;"]

        assert sweeper.normalize_block(lines) == tuple(lines)
        assert sweeper.normalize_block(
            ["# explanation", "value = 1"], hash_comments=True
        ) == ("value = 1",)

    def test_preserves_python_floor_division(self):
        assert sweeper.normalize_block(
            ["result = total // width"],
            hash_comments=True,
            slash_comments=False,
        ) == ("result = total // width",)

    def test_preserves_whitespace_inside_string_literals(self):
        assert sweeper.normalize_block(
            ['message = "access  denied"'],
        ) != sweeper.normalize_block(
            ['message = "access denied"'],
        )

    @pytest.mark.parametrize("delimiter", ["`", '"""'])
    def test_preserves_whitespace_inside_multiline_literals(self, delimiter):
        first = [f"message = {delimiter}hello", f"  world{delimiter}"]
        second = [f"message = {delimiter}hello", f" world{delimiter}"]

        assert sweeper.normalize_block(first) != sweeper.normalize_block(second)

    def test_drops_indented_comment_only_and_blank_lines(self):
        assert sweeper.normalize_block(
            ["    # note", "    ", "    run_task()"],
            hash_comments=True,
            slash_comments=False,
            preserve_indentation=True,
        ) == ("    run_task()",)

    def test_strips_inline_hash_and_dash_comments(self):
        assert sweeper.normalize_block(
            ["value=compute()# note"],
            hash_comments=True,
            slash_comments=False,
        ) == ("value=compute()",)
        assert sweeper.normalize_block(
            ["SELECT compute()-- note"],
            slash_comments=False,
            dash_comments=True,
        ) == ("SELECT compute()",)

    @pytest.mark.parametrize("path", ["script.sh", "config.yaml", "settings.ini"])
    def test_hash_literals_are_not_treated_as_comments_in_other_languages(self, path):
        options = sweeper._comment_options(path)

        assert sweeper.normalize_block(
            ["target=host/#blue"], **options
        ) != sweeper.normalize_block(
            ["target=host/#green"], **options
        )

    def test_ruby_hash_comments_can_start_without_whitespace(self):
        options = sweeper._comment_options("parser.rb")

        assert sweeper.normalize_block(
            ["value=compute()# blue"], **options
        ) == sweeper.normalize_block(
            ["value=compute()# green"], **options
        )

    def test_preserves_python_indentation_when_requested(self):
        assert sweeper.normalize_block(
            ["    if ready:", "        run_task()"],
            hash_comments=True,
            slash_comments=False,
            preserve_indentation=True,
        ) != sweeper.normalize_block(
            ["    if ready:", "      run_task()"],
            hash_comments=True,
            slash_comments=False,
            preserve_indentation=True,
        )


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

    def test_parses_a_flagged_range(self):
        site = sweeper.parse_site("lib/options.ts:5-8")

        assert site.path == "lib/options.ts"
        assert site.start_line == 5
        assert site.line == 8
        assert str(site) == "lib/options.ts:5-8"


@pytest.fixture
def mirror_repo(tmp_path):
    for fixture in MIRROR_CORPUS:
        target = tmp_path / fixture["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        padding = [""] * (fixture["startLine"] - 1)
        target.write_text("\n".join(padding + fixture["lines"]) + "\n")
    return tmp_path


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

    def test_token_sweep_does_not_report_lines_inside_flagged_ranges(self, tmp_path):
        (tmp_path / "ranges.py").write_text(
            "one = path.read_text().encode()\n"
            "two = path.read_text().encode()\n"
            "unrelated()\n"
            "three = path.read_text().encode()\n"
            "four = path.read_text().encode()\n"
        )

        result = sweeper.sweep(
            signature="range-shape",
            label="range shape",
            sites=["ranges.py:1-2", "ranges.py:4-5"],
            changed_files=["ranges.py"],
            root=tmp_path,
        )

        assert result.candidates == []

    def test_never_leaves_the_changed_file_set(self, repo):
        """vendor.py contains the identical pattern but is not in the diff."""
        result = self._sweep(repo)
        assert all(not c["path"].startswith("vendor") for c in result.candidates)

        widened = self._sweep(repo, changed_files=["app/config.py", "vendor.py"])
        assert any(c["path"] == "vendor.py" for c in widened.candidates)

    def test_does_not_read_a_flagged_site_outside_changed_files(self, repo, monkeypatch):
        original = sweeper._read_lines
        reads = []

        def recording_reader(path):
            reads.append(path)
            return original(path)

        monkeypatch.setattr(sweeper, "_read_lines", recording_reader)
        result = self._sweep(
            repo,
            sites=["vendor.py:2", "vendor.py:6"],
            changed_files=["app/config.py"],
        )

        assert result.status == "too_few_sites"
        assert all(path.name != "vendor.py" for path in reads)

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

    def test_punctuation_only_intersection_is_too_thin(self, tmp_path):
        (tmp_path / "conditions.js").write_text(
            "!alpha && beta\n"
            "!gamma && delta\n"
        )
        result = sweeper.sweep(
            signature="conditional-gate",
            label="independent conditional gate",
            sites=[
                "conditions.js:1",
                "conditions.js:2",
            ],
            changed_files=["conditions.js"],
            root=tmp_path,
        )

        assert result.status == "pattern_too_thin"
        assert result.invariant_tokens == ("!", "&&")
        assert result.candidates == []

    def test_flagged_range_finds_the_twin_implementation(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["lib/options.ts:5-8"],
            changed_files=[fixture["path"] for fixture in MIRROR_CORPUS],
            root=mirror_repo,
        )

        assert result.status == "ok"
        assert len(result.candidates) == 1
        assert result.candidates[0]["candidateClass"] == "mirror"
        assert result.candidates[0]["site"] == (
            "src/options.js:12-15"
        )
        assert result.candidates[0]["fingerprint"]

    def test_mirror_rejects_a_structural_only_seed(self, tmp_path):
        (tmp_path / "generic.js").write_text(
            "return;\n}\n\nreturn;\n}\n"
        )

        result = sweeper.sweep(
            signature="generic-ending",
            label="generic ending",
            sites=["generic.js:1-2"],
            changed_files=["generic.js"],
            root=tmp_path,
        )

        assert result.status == "too_few_sites"
        assert result.candidates == []

    def test_mirror_never_searches_outside_changed_files(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["lib/options.ts:5-8"],
            changed_files=["lib/options.ts"],
            root=mirror_repo,
        )

        assert result.status == "too_few_sites"
        assert result.candidates == []

    def test_mirror_does_not_re_report_another_flagged_range(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=[
                "lib/options.ts:5-8",
                "src/options.js:12-15",
            ],
            changed_files=[fixture["path"] for fixture in MIRROR_CORPUS],
            root=mirror_repo,
        )

        assert all(
            candidate["candidateClass"] != "mirror"
            for candidate in result.candidates
        )

    def test_mirror_does_not_re_report_a_flagged_single_line(self, tmp_path):
        block = (
            "const normalized = rawOptions\n"
            "  .filter((option) => option.enabled)\n"
            "  .map((option) => option.name.trim())\n"
            "  .sort();\n"
        )
        (tmp_path / "source.js").write_text(block)
        (tmp_path / "twin.js").write_text(block)

        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["source.js:1-4", "twin.js:2"],
            changed_files=["source.js", "twin.js"],
            root=tmp_path,
        )

        assert all(candidate["candidateClass"] != "mirror" for candidate in result.candidates)

    @pytest.mark.parametrize(
        ("path", "source"),
        [
            (
                "template.js",
                "message = `header\n  // keep this\n  value`;\n",
            ),
            (
                "template.py",
                'message = """header\n  # keep this\n  value"""\n',
            ),
        ],
    )
    def test_mirror_seed_inside_multiline_literal_uses_file_context(
        self, tmp_path, path, source
    ):
        (tmp_path / path).write_text(source)
        twin = tmp_path / f"twin{Path(path).suffix}"
        twin.write_text(source)

        result = sweeper.sweep(
            signature="literal-body",
            label="literal body",
            sites=[f"{path}:2-3"],
            changed_files=[path, twin.name],
            root=tmp_path,
        )

        assert [candidate["site"] for candidate in result.candidates] == [
            f"{twin.name}:2-3"
        ]

    def test_mirror_does_not_re_report_seed_after_comment_stripping(self, tmp_path):
        (tmp_path / "query.js").write_text(
            "// flagged range includes this comment\n"
            "const normalizedOptions = rawOptions\n"
            "  .filter((option) => option.enabled)\n"
            "  .map((option) => option.name.trim())\n"
            "  .sort();\n"
            "\n"
            "const normalizedOptions = rawOptions\n"
            "  .filter((option) => option.enabled)\n"
            "  .map((option) => option.name.trim())\n"
            "  .sort();\n"
        )

        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["query.js:1-5"],
            changed_files=["query.js"],
            root=tmp_path,
        )

        assert [candidate["site"] for candidate in result.candidates] == [
            "query.js:7-10"
        ]

    def test_mirror_does_not_report_windows_overlapping_the_seed(self, tmp_path):
        (tmp_path / "repeat.js").write_text("x();\n" * 5)

        result = sweeper.sweep(
            signature="repeated-block",
            label="repeated block",
            sites=["repeat.js:2-4"],
            changed_files=["repeat.js"],
            root=tmp_path,
        )

        assert result.candidates == []

    def test_token_candidates_are_distinct_from_mirrors(self, repo):
        result = self._sweep(repo)

        assert {candidate["candidateClass"] for candidate in result.candidates} == {"token"}

    def test_token_hit_inside_a_mirror_is_not_double_counted(self, tmp_path):
        block = (
            "prepare();\n"
            "value = path.read_text().encode();\n"
        )
        (tmp_path / "copies.js").write_text(
            block + "unrelated();\n" + block + "unrelated();\n" + block
        )

        result = sweeper.sweep(
            signature="copied-block",
            label="copied block",
            sites=["copies.js:1-2", "copies.js:4-5"],
            changed_files=["copies.js"],
            root=tmp_path,
        )

        assert [(candidate["candidateClass"], candidate["site"])
                for candidate in result.candidates] == [
            ("mirror", "copies.js:7-8")
        ]

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
        result = self._sweep(
            repo,
            sites=["app/missing.py:2", "app/gone.py:4"],
            changed_files=["app/missing.py", "app/gone.py"],
        )
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


class TestMirrorNormalizationGuards:
    """A mirror asserts two blocks are the same code, so every normalization
    step is a chance to claim that falsely. Each guard below must lose the
    mirror rather than report one."""

    @staticmethod
    def _sweep(tmp_path, sites, changed_files):
        return sweeper.sweep(
            signature="guard", label="guard",
            sites=sites, changed_files=changed_files, root=tmp_path,
        )

    HEREDOC_SCRIPT = (
        "write_config() {\n"
        "  cat <<EOF\n"
        "# PAYLOAD setting\n"
        "value=1\n"
        "EOF\n"
        "}\n"
    )

    @classmethod
    def _heredoc(cls, payload):
        return cls.HEREDOC_SCRIPT.replace("PAYLOAD", payload)

    def test_a_hash_line_inside_a_heredoc_is_payload_not_a_comment(self, tmp_path):
        (tmp_path / "script.sh").write_text(
            self._heredoc("alpha") + self._heredoc("beta")
        )

        result = self._sweep(tmp_path, ["script.sh:2-5"], ["script.sh"])

        assert result.candidates == []

    def test_heredoc_payload_spacing_is_significant(self, tmp_path):
        (tmp_path / "spacing.sh").write_text(
            "cat <<EOF\n"
            "indent_one   two\n"
            "EOF\n"
            "cat <<EOF\n"
            "indent_one two\n"
            "EOF\n"
        )

        result = self._sweep(tmp_path, ["spacing.sh:1-3"], ["spacing.sh"])

        assert result.candidates == []

    def test_identical_heredocs_still_mirror(self, tmp_path):
        """The guard must not silence every shell mirror."""
        (tmp_path / "same.sh").write_text(self._heredoc("alpha") * 2)

        result = self._sweep(tmp_path, ["same.sh:2-5"], ["same.sh"])

        assert [c["site"] for c in result.candidates] == ["same.sh:8-11"]

    def test_yaml_nesting_depth_is_not_normalized_away(self, tmp_path):
        (tmp_path / "compose.yaml").write_text(
            "services:\n"
            "  web:\n"
            "    image: alpine_base\n"
            "    command: serve_http\n"
            "overrides:\n"
            "      image: alpine_base\n"
            "      command: serve_http\n"
        )

        result = self._sweep(tmp_path, ["compose.yaml:3-4"], ["compose.yaml"])

        assert result.candidates == []

    def test_yaml_blocks_at_the_same_depth_still_mirror(self, tmp_path):
        (tmp_path / "same.yaml").write_text(
            "web:\n"
            "    image: alpine_base\n"
            "    command: serve_http\n"
            "api:\n"
            "    image: alpine_base\n"
            "    command: serve_http\n"
        )

        result = self._sweep(tmp_path, ["same.yaml:2-3"], ["same.yaml"])

        assert [c["site"] for c in result.candidates] == ["same.yaml:5-6"]

    def test_an_overlong_seed_is_refused_rather_than_scanned(self, tmp_path, monkeypatch):
        """Cost is O(file lines x range lines) per seed; the cap is the bound."""
        monkeypatch.setattr(sweeper, "MAX_MIRROR_BLOCK_LINES", 3)
        block = "alpha_one();\nbeta_two();\ngamma_three();\ndelta_four();\n"
        (tmp_path / "long.js").write_text(block * 2)

        result = self._sweep(tmp_path, ["long.js:1-4"], ["long.js"])

        assert result.candidates == []

    def test_a_seed_within_the_cap_still_mirrors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sweeper, "MAX_MIRROR_BLOCK_LINES", 4)
        block = "alpha_one();\nbeta_two();\ngamma_three();\ndelta_four();\n"
        (tmp_path / "long.js").write_text(block * 2)

        result = self._sweep(tmp_path, ["long.js:1-4"], ["long.js"])

        assert [c["site"] for c in result.candidates] == ["long.js:5-8"]

    def test_differing_non_utf8_bytes_are_not_reported_as_a_mirror(self, tmp_path):
        """Both blocks decode to U+FFFD under errors='replace' and would
        otherwise fingerprint alike despite differing on disk."""
        (tmp_path / "legacy.js").write_bytes(
            b'const label = "caf\xe9";\nconst other = compute_value();\n'
            b'const label = "caf\xe8";\nconst other = compute_value();\n'
        )

        result = self._sweep(tmp_path, ["legacy.js:1-2"], ["legacy.js"])

        assert result.candidates == []

    def test_the_same_block_in_valid_utf8_does_mirror(self, tmp_path):
        (tmp_path / "clean.js").write_bytes(
            'const label = "café";\nconst other = compute_value();\n'.encode() * 2
        )

        result = self._sweep(tmp_path, ["clean.js:1-2"], ["clean.js"])

        assert [c["site"] for c in result.candidates] == ["clean.js:3-4"]

    def test_lossy_files_are_still_readable_for_token_sweeps(self, tmp_path):
        """Only exact matching refuses lossy text; the token sweep tolerates it."""
        path = tmp_path / "legacy.py"
        path.write_bytes(b'value = "caf\xe9".read_text().encode()\n')

        assert sweeper._is_lossy_text(path) is True
        assert sweeper._read_lines(path) == ['value = "caf�".read_text().encode()']

    def test_a_heredoc_terminator_with_trailing_blanks_is_still_payload(self, tmp_path):
        """Bash ends a heredoc only on a line that is exactly the delimiter, so
        ``EOF   `` must not expose the lines after it to comment stripping."""
        block = (
            "cat <<EOF\n"
            "alpha_data\n"
            "EOF   \n"
            "# PAYLOAD\n"
            "EOF\n"
        )
        (tmp_path / "fake_end.sh").write_text(
            block.replace("PAYLOAD", "FIRST_DATA")
            + block.replace("PAYLOAD", "SECOND_DATA")
        )

        result = self._sweep(tmp_path, ["fake_end.sh:1-5"], ["fake_end.sh"])

        assert result.candidates == []

    def test_heredoc_payload_trailing_whitespace_is_significant(self, tmp_path):
        (tmp_path / "trailing.sh").write_text(
            "cat <<EOF\n"
            "alpha_one \n"
            "EOF\n"
            "cat <<EOF\n"
            "alpha_one  \n"
            "EOF\n"
        )

        result = self._sweep(tmp_path, ["trailing.sh:1-3"], ["trailing.sh"])

        assert result.candidates == []

    def test_removing_a_block_comment_does_not_weld_two_tokens(self, tmp_path):
        """``account/**/display_name`` is two tokens; ``accountdisplay_name``
        is one. Dropping the comment without a separator conflates them."""
        (tmp_path / "report.sql").write_text(
            "SELECT account/**/display_name FROM records\n"
            "WHERE archived_at IS NULL\n"
            "SELECT accountdisplay_name FROM records\n"
            "WHERE archived_at IS NULL\n"
        )

        result = self._sweep(tmp_path, ["report.sql:1-2"], ["report.sql"])

        assert result.candidates == []

    def test_a_block_comment_between_lines_still_mirrors(self, tmp_path):
        """The separator must not stop genuinely identical SQL from matching."""
        (tmp_path / "same.sql").write_text(
            "SELECT account/**/display_name FROM records\n"
            "WHERE archived_at IS NULL\n"
            "SELECT account /* note */ display_name FROM records\n"
            "WHERE archived_at IS NULL\n"
        )

        result = self._sweep(tmp_path, ["same.sql:1-2"], ["same.sql"])

        assert [c["site"] for c in result.candidates] == ["same.sql:3-4"]

    def test_a_hash_inside_a_yaml_block_scalar_is_scalar_data(self, tmp_path):
        (tmp_path / "messages.yaml").write_text(
            "first:\n"
            "  message: |\n"
            "    # alpha_payload\n"
            "    body_line\n"
            "second:\n"
            "  message: |\n"
            "    # beta_payload\n"
            "    body_line\n"
        )

        result = self._sweep(tmp_path, ["messages.yaml:2-4"], ["messages.yaml"])

        assert result.candidates == []

    def test_a_real_yaml_comment_is_still_stripped(self, tmp_path):
        """The scalar guard is scoped to block-scalar bodies, not all of YAML."""
        (tmp_path / "commented.yaml").write_text(
            "first:\n"
            "  # alpha note\n"
            "  image: alpine_base\n"
            "  command: serve_http\n"
            "second:\n"
            "  # beta note\n"
            "  image: alpine_base\n"
            "  command: serve_http\n"
        )

        result = self._sweep(tmp_path, ["commented.yaml:2-4"], ["commented.yaml"])

        assert [c["site"] for c in result.candidates] == ["commented.yaml:7-8"]

    def test_markdown_urls_are_not_truncated_as_slash_comments(self, tmp_path):
        """``//`` in ``https://`` is not a comment marker in prose."""
        (tmp_path / "docs.md").write_text(
            "Docs at https://alpha.example/path\n"
            "Shared trailing sentence here\n"
            "Docs at https://beta.example/path\n"
            "Shared trailing sentence here\n"
        )

        result = self._sweep(tmp_path, ["docs.md:1-2"], ["docs.md"])

        assert result.candidates == []

    def test_slash_comments_still_apply_to_c_family_files(self, tmp_path):
        (tmp_path / "handler.ts").write_text(
            "const handler = buildHandler(); // alpha note\n"
            "registerHandler(handler);\n"
            "const handler = buildHandler(); // beta note\n"
            "registerHandler(handler);\n"
        )

        result = self._sweep(tmp_path, ["handler.ts:1-2"], ["handler.ts"])

        assert [c["site"] for c in result.candidates] == ["handler.ts:3-4"]

    def test_lines_inside_a_literal_do_not_mirror_executable_code(self, tmp_path):
        """Same text, different lexical state: fixing the call site cannot
        imply editing the template that merely spells it out."""
        (tmp_path / "runner.js").write_text(
            "const template = `\n"
            "alpha_task()\n"
            "beta_task()\n"
            "`;\n"
            "alpha_task()\n"
            "beta_task()\n"
        )

        result = self._sweep(tmp_path, ["runner.js:2-3"], ["runner.js"])

        assert result.candidates == []

    def test_executable_code_still_mirrors_executable_code(self, tmp_path):
        (tmp_path / "calls.js").write_text(
            "alpha_task();\n"
            "beta_task();\n"
            "alpha_task();\n"
            "beta_task();\n"
        )

        result = self._sweep(tmp_path, ["calls.js:1-2"], ["calls.js"])

        assert [c["site"] for c in result.candidates] == ["calls.js:3-4"]

    @pytest.mark.parametrize("name", ["Makefile", "build.mk", "Makefile.common"])
    def test_makefile_recipe_tabs_are_significant(self, tmp_path, name):
        """A leading tab is what makes a line a recipe rather than a directive."""
        (tmp_path / name).write_text(
            "target_one:\n"
            "\texport build_mode=enabled\n"
            "\trun_step_two\n"
            "export build_mode=enabled\n"
            "run_step_two\n"
        )

        result = self._sweep(tmp_path, [f"{name}:2-3"], [name])

        assert result.candidates == []

    def test_a_non_identifier_heredoc_delimiter_is_recognized(self, tmp_path):
        """Bash applies quote removal but no expansion, so ``<<$EOF`` ends on a
        line reading ``$EOF``."""
        block = (
            "cat <<$EOF\n"
            "# PAYLOAD\n"
            "shared_body\n"
            "$EOF\n"
        )
        (tmp_path / "dollar.sh").write_text(
            block.replace("PAYLOAD", "alpha_data")
            + block.replace("PAYLOAD", "beta_data")
        )

        result = self._sweep(tmp_path, ["dollar.sh:1-4"], ["dollar.sh"])

        assert result.candidates == []

    @pytest.mark.parametrize("opener,terminator", [
        ("cat <<'EOF'", "EOF"),
        ("cat <<\\EOF", "EOF"),
        ("cat <<-EOF", "\tEOF"),
    ])
    def test_quoted_and_escaped_heredoc_delimiters_terminate(
        self, tmp_path, opener, terminator
    ):
        """Quote removal means all these forms close on a plain ``EOF``."""
        block = f"{opener}\n# PAYLOAD\nshared_body\n{terminator}\n"
        (tmp_path / "forms.sh").write_text(
            block.replace("PAYLOAD", "alpha_data")
            + block.replace("PAYLOAD", "beta_data")
        )

        result = self._sweep(tmp_path, ["forms.sh:1-4"], ["forms.sh"])

        assert result.candidates == []

    def test_an_unclassifiable_heredoc_opener_declines_the_rest_of_the_file(
        self, tmp_path
    ):
        """If we cannot find where the payload ends, nothing after it is code."""
        flags = sweeper._heredoc_body_flags([
            "cat <<;",
            "# alpha_data",
            "EOF",
        ])

        assert flags == [False, True, True]

    def test_a_postgres_dollar_quoted_body_is_string_data(self, tmp_path):
        """``--`` inside ``$body$ ... $body$`` is content, not a SQL comment."""
        block = (
            "SELECT $body$\n"
            "-- PAYLOAD\n"
            "shared_statement\n"
            "$body$ FROM records\n"
        )
        (tmp_path / "fn.sql").write_text(
            block.replace("PAYLOAD", "alpha_payload")
            + block.replace("PAYLOAD", "beta_payload")
        )

        result = self._sweep(tmp_path, ["fn.sql:1-4"], ["fn.sql"])

        assert result.candidates == []

    def test_a_real_sql_comment_is_still_stripped(self, tmp_path):
        (tmp_path / "plain.sql").write_text(
            "SELECT account_id FROM records -- alpha note\n"
            "WHERE archived_at IS NULL\n"
            "SELECT account_id FROM records -- beta note\n"
            "WHERE archived_at IS NULL\n"
        )

        result = self._sweep(tmp_path, ["plain.sql:1-2"], ["plain.sql"])

        assert [c["site"] for c in result.candidates] == ["plain.sql:3-4"]


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

    def test_report_labels_a_mirror_candidate(self, mirror_repo):
        result = sweeper.sweep(
            signature="option-normalization",
            label="option normalization",
            sites=["lib/options.ts:5-8"],
            changed_files=[fixture["path"] for fixture in MIRROR_CORPUS],
            root=mirror_repo,
        )

        assert "[mirror]" in sweeper.render_report(result)


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


class TestCommentLines:
    """A reviewer finding anchors to code, so a comment is never a sibling.

    Prose shares ordinary words with code, which is the one false-positive
    class a token intersection cannot rule out on its own. Found by fuzzing
    random line pairs: a docstring mentioning `state.json` matched a
    `json.dumps` call and proposed comment lines as siblings.
    """

    @pytest.mark.parametrize("line,is_code", [
        ("    data = json.loads(x)", True),
        ("# state.json must be a JSON object", False),
        ("    # trailing thought", False),
        ("// javascript comment", False),
        (" * javadoc continuation", False),
        ("-- sql comment", False),
        ("; lisp comment", False),
        ("", False),
        ("    ", False),
        ("value = 1  # inline comments do not disqualify the line", True),
    ])
    def test_classifies_code_versus_comment(self, line, is_code):
        assert sweeper.is_code_line(line) is is_code

    def test_a_comment_is_never_reported_as_a_sibling(self, tmp_path):
        (tmp_path / "m.py").write_text(
            "data = json.loads(path.read_text())\n"
            "more = json.loads(path.read_text())\n"
            "# json.loads(path.read_text()) is what this used to do\n"
            "other = json.loads(path.read_text())\n"
        )
        result = sweeper.sweep(
            signature="s", label="json.loads", sites=["m.py:1", "m.py:2"],
            changed_files=["m.py"], root=tmp_path,
        )
        assert [c["line"] for c in result.candidates] == [4]

    def test_flagged_sites_pointing_at_comments_do_not_sweep(self, tmp_path):
        (tmp_path / "m.py").write_text(
            "# the config is loaded from state.json here\n"
            "# and state.json is written back on exit\n"
            "data = json.loads(path.read_text())\n"
        )
        result = sweeper.sweep(
            signature="s", label="prose", sites=["m.py:1", "m.py:2"],
            changed_files=["m.py"], root=tmp_path,
        )
        assert result.status == "no_source"
        assert result.candidates == []
