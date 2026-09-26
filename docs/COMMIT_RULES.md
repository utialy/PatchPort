# Commit rules

## Message

Use an English subject in this form:

```text
type(scope): imperative summary
```

The scope is optional. Allowed types are feat, fix, docs, test, refactor, perf, build, ci, chore, and revert. Keep the subject at most 72 characters. Examples:

```text
fix(archive): reject mismatched evidence hashes
docs: explain role cleanup limits
test: cover missing private queues
```

For a change that needs explanation, use a blank line after the subject and describe the problem, resulting behavior, and relevant validation. Do not paste a task transcript, claim tests that were not run, or add repetitive generated prose. Preserve any required attribution; do not invent sign-offs or authorship.

## Content

- Commit one coherent change. Separate behavior changes from broad formatting or translation work.
- Use English for documentation, comments, docstrings, and help text. Repository text uses ASCII spelling and punctuation; Unicode fixtures use escapes to retain their runtime values. Automated checks enforce encoding and excluded-content rules, while review determines whether the prose is clear English.
- Never commit credentials, `.env` files, operational queues, provider logs, request transcripts, virtual environments, build output, task copies, or private handoff history.
- Use generic paths in examples. Do not include local usernames, home-directory paths, email addresses, or machine-specific project paths in documentation.
- Review the staged diff and add explicit paths. Do not force-add an ignored directory to make a commit pass.
- Keep applicable license notices and third-party attribution. A style cleanup must not remove them.

## Checks

```sh
git diff --check
python scripts/check_public_release.py --tree
python -m unittest discover -s tests -v
git add PATHS
git diff --cached --check
python scripts/check_public_release.py --staged
git diff --cached
git commit -m "type: summary"
```

Run tests appropriate to the change. Documentation-only work does not require repeating the full suite, but local links and release policy must pass. Behavior changes require regression coverage. Report platform-specific skips explicitly.

The pre-commit hook checks all indexed files, including staged bytes rather than working-tree bytes. The commit-msg hook checks the subject format and ASCII message text. These checks do not prove that content is safe to publish or that its claims are correct. Do not bypass a failed check to complete an automated task; fix the issue or explain the blocker.

Commits are local unless publication is requested. Never configure a remote, push, rewrite shared history, select a license, or add fabricated author identities as part of an ordinary commit.

## Maintainer imports

Follow [the maintainer update workflow](MAINTAINER_UPDATES.md) for reviewed patches from another checkout. Several development commits can form one coherent public commit. Do not copy private history or planning notes, and do not advance an import baseline past outstanding changes.
