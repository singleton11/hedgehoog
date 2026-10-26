# Classifier comment identity

The Hub trigger loads the live global skill at
`/home/singleton/.config/opencode/skills/paseo-classify-issue/SKILL.md`.
The repository copy is `.opencode/skills/paseo-classify-issue/SKILL.md`.
Editing the repository copy alone does not update the installed skill. After
review/approval, compare the live file first, preserve concurrent changes, and
copy the approved version into the global location. No trigger redeploy or
GitHub login change is needed. Issue #39 explicitly authorized updating both.

The classifier must send its rationale through Hub's reply tool with the
arrival's execution ID. GitHub CLI inspection remains read-only for comments;
its active login must not determine comment authorship. Label writes retain the
existing command and readiness/locking checks.

Acceptance checks:

1. Deliver an eligible issue through the classifier trigger.
2. Verify the persisted rationale marker and route on that exact issue, with
   REST author `demik-paseo-bot[bot]` of type `Bot` (GraphQL normalizes the
   login to `demik-paseo-bot`; verify its `Bot` typename), before a routing label
   is added.
3. Retry without adding duplicate bot rationales. A historical personal rationale
   does not count as bot evidence and must not be edited or deleted.
4. With Hub reply unavailable or delivery/authorship uncertain, stop without
   adding the routing label; never fall back to a personal comment client.

Run static policy checks with:

```sh
python3 -m unittest discover -s scripts/tests -p 'test_classifier_skill.py' -v
```

These checks protect instruction wording, not agent compliance or live trigger
execution. A Hub reply from the issue #39 implementation execution separately
verifies the bot delivery channel; it is not an end-to-end classifier run. Do not
remove/re-add routing labels on a real issue just to test the classifier, since
that can launch downstream work.
