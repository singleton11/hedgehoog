# Reviewable external attention runtime candidates

These are complete candidate replacements, not installed runtime files. Nothing in
this directory is auto-deployed. `base-hashes.json` records the SHA-256 of each
external source as read; each `.patch` is against that exact source. The patches
use zero-context unified diffs so blank context lines do not introduce trailing
whitespace in the repository. After verifying the exact baseline SHA-256, review
and check them with `git apply --unidiff-zero --check` in an isolated staging
copy, never against the installed runtime without separate authorization. The
round-trip regression test reconstructs the recorded baseline and checks that
applying each patch reproduces its complete candidate byte-for-byte.

The skill candidate preserves issue-only scope, live approval checks, workspace
ownership, nonblocking locks, continuation journals, delivery receipts and
no-deployment rules.

Before any separately authorized installation, compare the live source hashes to
the manifest, review all patches, and resolve drift rather than overwriting newer
operator changes. This change does not authorize installation, service restarts,
Telegram polling, or a live acceptance test.

The verifiers read SQLite in read-only mode. Legacy unversioned text events retain
the installed identity contract. Callback events require schema version 1, exact
canonical outbox equality, the original answered question/context, the persisted
raw inbox update and digest, private-chat human sender and bot-authored exact
question message, and agreement between token, prompt option and answer metadata.
The displayed prompt URL must match the verified issue context exactly; another
issue or a PR prompt cannot authorize a continuation for this issue.
The `__other__` button is never an answer. Callback text is `answer.text`, never a
synthetic `reply.text`. Approval gates remain separate from identity validation.
The receiver sends the complete event as compact JSON-string manual-run input:
original Hub `7cbdd52` exposes only string input as the successor's `paseo.prompt`.
The durable outbox retains its original JSON object and delivery identity.

The historical accepted-event `:input-fix-v1` exception permits exactly one
additional delivery-key suffix and no other event changes. The by-key loader
supports that same exception only for an accepted canonical row; the original
outbox event and receipt remain untouched.

Offline validation (uses temporary SQLite databases and the repository receiver,
not deployed state, Telegram or Hub):

```sh
python3 -m unittest discover -s scripts/tests -p test_attention_verifier.py -v
```
