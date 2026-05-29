# Privacy Policy

**Last updated: 2026-05-29**

## What this plugin is

gh-gemini-review-loop is a Claude Code plugin that runs locally on your machine. It has no backend, no telemetry, and no data collection of any kind.

## Data flows

### GitHub API (always active)
The plugin calls the GitHub GraphQL and REST APIs using your existing `gh` CLI authentication token. Review thread content and PR metadata are fetched from GitHub and displayed in your Claude Code session. Nothing is stored beyond your local session.

### OpenAI judge eval (opt-in, off by default)
If you explicitly enable judge eval, Gemini finding bodies and associated diff hunks from your PR are sent to the OpenAI API for classification. This feature is:

- **Off by default.** Nothing is sent to OpenAI until you opt in.
- **Read-only.** The judge never posts comments, resolves threads, or pushes code.
- **Disclosed at opt-in.** The privacy boundary is stated before you enable it.

Data sent to OpenAI is subject to [OpenAI's privacy policy](https://openai.com/policies/privacy-policy). You use your own OpenAI API key; the plugin author has no access to it or to any data sent through it.

### Preferences file
If you use judge eval, your chosen mode (`on_cycle` / `on_complete` / `off`) is saved to `~/.config/gh-gemini-review-loop/preferences.json` on your local machine. No preference data leaves your machine.

## No analytics or tracking

The plugin collects no usage data, crash reports, or analytics. No information is sent to the plugin author.

## Contact

Privacy questions: oren.as@gmail.com
