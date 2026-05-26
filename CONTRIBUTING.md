# Contributing

Thanks for opening a PR.

## Release labels (required-ish)

Every PR merge to `main` triggers `.github/workflows/release.yml`, which bumps the version in `plugins/gh-gemini-review-loop/.claude-plugin/plugin.json`, tags the commit, and publishes a GitHub Release whose body is the PR description.

The **bump level** is controlled by a label on the PR:

| Label | When to use | Effect |
|---|---|---|
| `release:major` | Breaking changes (CLI flag removed, behavior reversed, hard requirement bump) | `1.2.3 → 2.0.0` |
| `release:minor` | New features (new flag, new exported helper, new SKILL.md section that changes agent behavior) | `1.2.3 → 1.3.0` |
| `release:patch` | Bug fixes, doc updates, refactors, tests, internal cleanup | `1.2.3 → 1.2.4` |
| `release:skip` | Chores you don't want to release (CI tweaks, README typos, .github/* only) | No bump, no tag, no Release. |

If no `release:*` label is present, the workflow defaults to `release:patch`. To bootstrap the labels in a fresh fork, run the **Setup release labels** workflow once (Actions tab → run workflow). Precedence when multiple are set: `skip > major > minor > patch`.

## Release notes

The GitHub Release body is the merged PR's body verbatim. Structure PR descriptions accordingly:

- Lead with a one-paragraph summary.
- Use `### Subsections` to group changes (Added / Changed / Fixed).
- The first heading after the summary becomes the highlight in the Release page card.

Keep PR titles concise and prefixed with the change type (`fix:`, `feat:`, `chore:`, `docs:`, `refactor:`, `test:`). The Release title is built as `vX.Y.Z: <PR title>`.

## CHANGELOG

`CHANGELOG.md` is human-maintained under `[Unreleased]`. The release workflow does not modify it. When you open a PR, add an entry there too — it pairs with the auto-Release for users who prefer a single file.

## Tests and linting

Local quick check before opening a PR:

```bash
python3 -m pip install --quiet pytest ruff   # or use a venv
pytest tests/
ruff check plugins/ tests/
```

CI runs the same on Python 3.10 / 3.11 / 3.12 (see `.github/workflows/ci.yml`).

## Stacked PRs

Don't. The merge logic of squash-and-delete-branch with stacked PRs has bitten us before — child PRs end up merging into deleted parent branches. If you must stack, either rebase each child onto `main` before merging the parent, or change the child's base to `main` before merging.
