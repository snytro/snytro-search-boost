# Curated indexing, retrieval routes and handover

Use a named collection and create its profile from an explicitly selected source
root. Inspect only that inventory and enough material to classify source authority
and version. Choose a canonical copy; exclude duplicates, superseded material and
answer keys from the main corpus. Keep scenarios or lab evidence explicit-only if
the user wants to query them separately. Unlisted paths are excluded; do not
expand the allowlist based on hits from an unrelated collection.

Example profile (paths are relative to the selected source root):

```json
{
  "sources": {
    "rules/current.pdf": {"rank": 1, "visibility": "default"},
    "guidance/reference.xlsx": {"rank": 2, "visibility": "default"},
    "case-study/evidence.docx": {"rank": 6, "visibility": "explicit_only"},
    "training/answer-key.pdf": {"rank": 8, "visibility": "excluded"}
  },
  "routes": {
    "requirements": ["rules/current.pdf", "guidance/reference.xlsx"],
    "case-study": ["case-study/evidence.docx"]
  },
  "verify_original": true,
  "web_verification": "required",
  "official_domains": ["example.gov"]
}
```

Lower rank means higher authority. Rank adjusts semantic cosine distance and breaks
keyword score ties. Exact lookup ranks matching originals by authority. Visibility
and route filter candidate IDs **before** ranking. Explicit-only sources may be
indexed for future scenario work, but search, list, structured results and fetch
hide them absent the explicit flag. Even a known ID cannot bypass it.

```bash
snytro --collection policy profile --source-root "/path/to/selected" --input profile.json
snytro --collection policy index --full --rich "/path/to/selected"
snytro --collection policy status JOB_ID
snytro --collection policy verify --check-originals
snytro --collection policy search "authentication policy" --route requirements --stage summaries --dense-only
snytro --collection policy search "privileged accounts" --stage pages --semantic --document-ids DOCUMENT_ID
snytro --collection policy search "CONTROL-123" --stage chunks --exact --document-ids DOCUMENT_ID
snytro --collection policy fetch DOCUMENT_ID --unit UNIT_NUMBER
```

The host chooses the route by interpreting the current question; route names are
not a set of automatic keyword triggers. Read `profile` with no flags for the saved
rules. If a route is insufficient, search the broader **default-eligible sources**
of the same collection when allowed by the task. Check scope and exact identifiers
before relying on snippets. For high-precision questions, fetch and paginate
complete original units; when `verify_original` is true, do this before making
factual claims. For cross-framework answers, corroborate with another authoritative
local source when applicable. Historical/scenario evidence does not establish the
current rule. Keep original quotations separate from model summaries.

Set `web_verification` to `off`, `when-needed`, or `required`. The skill uses the
host's web tools, not the embedding provider. Start with the listed public hosts,
compare editions, scope and applicability, and cite web and local evidence separately.

## Transfer a prepared collection

```bash
snytro --collection policy export policy.zip --with-originals
snytro --collection policy-copy restore policy.zip
snytro --collection policy-copy verify --check-originals
```

The ZIP contains readable source text and is unencrypted. Share it privately with
authorized recipients. `--with-originals` includes
original files only if they match indexed checksums; otherwise export contains the
stored extracted originals (source-unit text), summaries, vectors and routing policy.
The command prints a SHA-256; share it via a trusted channel for integrity checking.
Restore uses an empty target collection, validates individual entries, and restores
all records without re-indexing or copying credentials. It refuses new content in
an occupied collection or overwriting a different original. `--source-root` selects
the restored source location; otherwise a private data subdirectory is used.

Use SQLite or PostgreSQL on the receiving side. For immediate semantic search, set
the same embedding model, dimensions, chunk configuration, and provider endpoint
(or the same downloaded local revision and model settings) as the sender. Never
reuse a vector with a different model just because its dimensions match. If a
different model is wanted, restore originals and run `index` to regenerate vectors.
The ZIP is Search Boost's portable format; existing Chandima PostgreSQL dumps use
a different schema and are not directly importable.
