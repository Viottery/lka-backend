# Historical badcase manifests

Badcase manifests are manually curated, privacy-reviewed JSON regression data.
They are intentionally separate from executable benchmark suites: loading or
selecting a badcase never launches an Agent, invokes a tool, resolves fixture
paths, or reads a production run log.

Copy only the minimum task description needed to reproduce a failure. Remove
names, addresses, message bodies, credentials, local paths, and other identifying
or secret values before marking `privacy_reviewed: true`. Do not add raw run-log,
trace, prompt-dump, or production-log paths. The schema rejects unknown fields
and known raw-data field names. Curation must happen explicitly; there is no
directory scanner or automatic production-log importer.

The version 1 schema is in `evals/schemas/badcase_manifest.schema.json`. A
manifest contains `manifest_version`, `manifest_id`, and cases. Cases include a
sanitized `task`, the target `agent_id` and `executor_type`, severity/tags,
optional identifiers for separately curated fixtures, and simple expected
metric checks. Checks are inert declarations; a future runner must map metric
names to an allowlisted evaluator before applying them.

Validate and select from Python:

```python
from pathlib import Path
from evals.lka_evals.badcases import load_badcase_manifest, select_badcases

manifest = load_badcase_manifest(Path("evals/badcases/regressions.json"))
selected = select_badcases(manifest, tags={"approval"}, executor_type="general_react")
```

Selection is stable: cases are returned in lexical `case_id` order. Unknown
explicit IDs and unsupported schema versions fail closed. This first slice does
not execute cases or define online shadow evaluation.
