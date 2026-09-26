# Remaining work

## Flow cleanup

Archive readers and their role-review/result/overview integrations exist. Next steps are a flow-lifetime lock and a read-only cleanup preview, followed by the archive writer, explicit deletion, and interrupted-operation handling. Preserve private queue history and recovery data throughout.

## Usage budgets

Usage snapshots and cumulative claim limits exist. Token/cost budget warnings and dispatch limits still need policies for missing reports, estimates, concurrent reservations, and interrupted work. An unreported cost must not become zero.

## Context and sessions

Full/omit selection and frozen input checks exist. Tokenizer-based estimates, validated summaries, and session reuse remain separate work. Do not silently summarize or retry tasks as part of input selection.

## Portability

Keep platform results in [VALIDATION.md](VALIDATION.md). Expand direct macOS testing and independent Linux-host testing. WSL results do not establish every Linux environment. Keep provider authentication, permissions, and real-call validation separate from unit tests.
