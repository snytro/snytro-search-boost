# Host-agent analysis

After extraction, use `list` and `fetch` to read indexed original units. Page/section
compression should preserve operative conditions, exceptions, parties, amounts,
currencies, dates, IDs, obligations, table rows and useful headings. Do not invent
missing values. Write markdown, not HTML. Use bounded fetches and follow offsets
until the required original unit is complete.

Preserve whether a property is requested, allowed, excluded, optional, or quoted.
Keep field/section meaning when compressing text or JSON: summarize `exclude_styles`
as exclusions rather than merging its terms into a positive description. Preserve
conflicting settings and language distinctions. A neutral reusable summary describes
the source; relevance to a particular question is decided at retrieval time under
[relevance.md](relevance.md), not stored as a permanent document label.

Save analysis using a JSON file or stdin (`--input -`):

```json
{
  "checksum": "<checksum returned by list/fetch>",
  "tagline": "Contract for supplier support services",
  "summary": "Document-wide factual markdown summary, assembled from all unit summaries.",
  "keywords": ["support", "supplier", "termination"],
  "units": [
    {"number": 1, "tagline": "Parties, scope and fees", "summary": "Factual markdown compression of unit 1."}
  ]
}
```

Multiple saves may add unit summaries incrementally. All unit summaries must be
completed before `analysis_status` becomes ready. Keep the document summary factual
and update it as new units become available. If it still covers only part of the
document, explicitly label that limitation in its text. Saved analysis does not
overwrite original content or invalidate source citations.

For invoices, contracts and other recognizable forms, save stable snake_case fields
from original evidence. Use numeric amounts with currency fields, ISO-like dates
when inferable, arrays for repeated items, and null for unavailable values:

```json
{
  "checksum": "<indexed checksum>",
  "schema": "invoice",
  "source_units": [1],
  "fields": {"invoice_number": "INV-123", "total": 1250.0, "currency": "AUD", "due_date": null}
}
```

`save-extraction` stores provenance with the checksum and validates source units.
Both save commands reject stale checksums. Changes to source content discard old
summaries, fields and embeddings on successful reindexing. Extracted fields are
agent-produced interpretations; verify exact values against originals when needed.
