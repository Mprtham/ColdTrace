# Contributing

## Git attribution (hard rules, no exceptions)

- All commits must be authored and committed as: `Prathamesh Mishra <mprathamesh2023@gmail.com>`
- `Co-authored-by` trailers are forbidden in all commit messages.
- No AI tool name (Claude, Copilot, ChatGPT, or any other) may appear in commit author, committer, message body, or PR description.
- AI-assisted tooling must have automatic attribution disabled. Claude Code reads `.claude/settings.json` (`"includeCoAuthoredBy": false`).
- Enable the enforcing hook once per clone:

  ```bash
  git config core.hooksPath .githooks
  ```

- Sole-author check before every push — every line must be the author above:

  ```bash
  git log --format="%an <%ae> | %cn <%ce>" | sort -u
  ```

Full rationale: `docs/DESIGN.md` §12.1.
