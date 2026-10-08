# Remaining work

## Current source: VS Code 0.3.0

The local Windows extension now provides terminal routing, core/job/usage binding,
durable interactive history, input/summary submission, selected-copy review/
promotion/recovery, role/test/archive management and explicit session recovery/
runtime checks. See the [walkthrough](VSCODE_GUIDE.md).

Next extension work includes external-process handoff, remote/other-OS validation
and additional adapters. Charge enforcement, whole-flow deletion and the separate
desktop candidate's remaining native/UI/distribution checks are independent work.
Older milestone notes below describe their original release context.

## Project onboarding

The setup wizard supports local project selection, CLI discovery, input review, and explicit connection installation. Next work includes runner lifecycle management, an installation/management interface, and first-use validation with separately authorized real provider calls. See [setup](SETUP.md) and [validation](VALIDATION.md).

## Flow cleanup

Archive readers, flow-lifetime locks, read-only planning, archive-only writing, and explicit continuation exist in 0.1.0a2. Whole-flow deletion, archive expiry, and interrupted deletion remain future work. Preserve private queue history and recovery data throughout. The first alpha release does not depend on these deletion features.

## Usage budgets

Usage snapshots and cumulative claim limits exist. Token/cost budget warnings and dispatch limits still need policies for missing reports, estimates, concurrent reservations, and interrupted work. An unreported cost must not become zero.

## Context and sessions

Full/omit selection and frozen input checks exist. Tokenizer-based estimates, validated summaries, and session reuse remain separate work. Do not silently summarize or retry tasks as part of input selection.

## Portability

Keep platform results in [VALIDATION.md](VALIDATION.md). Expand direct macOS testing and independent Linux-host testing. WSL results do not establish every Linux environment. Keep provider authentication, permissions, and real-call validation separate from unit tests.
