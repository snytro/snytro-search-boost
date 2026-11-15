# Setup and runtime

1. Bootstrap base dependencies with `bash "<skill-dir>/scripts/bootstrap.sh"` or
   `& "<skill-dir>/scripts/bootstrap.ps1"`. This launcher needs no Python. It uses
   an existing uv or downloads a versioned uv archive and verifies its release checksum.
   Python 3.12, dependencies and caches are private to `SNYTRO_DATA_DIR`.
2. Run `scripts/snytro doctor` (PowerShell: `scripts/snytro.ps1 doctor`). Diagnostics
   do not scan user documents or download embedding models.
3. Before enabling local CPU embeddings, rerun bootstrap with `--embeddings` (`-Embeddings`).
   Before Postgres, also add `--postgres` (`-Postgres`). Install both flags together
   when needed. Dependency installation does not download model weights.
4. Configure: `scripts/snytro setup --storage sqlite --model ultrabert` or `nemotron`.
   `--model auto --language multilingual --priority accuracy` uses the diagnostic
   recommendation; if RAM is inadequate it stops rather than picking an unsuitable model.
5. `scripts/snytro model-download` downloads the selected snapshot, pins its immutable
   commit, and tests query/document CPU inference before marking it ready. Native model
   support is required; downloaded custom code is never implicitly executed.

SQLite uses FTS5 and sqlite-vec. No service is installed. Postgres supports:

- An explicitly configured, dedicated database via `SNYTRO_POSTGRES_URL`. Enable
  pgvector in that database beforehand. Connection strings remain in environment
  variables, never CLI arguments or public JSON.
- A private managed cluster using `setup --storage postgres --pg-bin-dir "<bin>"`.
  The binaries need a matching pgvector extension. Credentials are generated and
  stored privately. Port 54387 is the default; choose `--pg-port` if occupied.
- On macOS with Homebrew, `--install-postgres` installs PostgreSQL 17 and pgvector,
  then starts the private cluster. It does not install Homebrew or start a shared
  Homebrew service. Other OSes currently need supplied binaries or a configured server.

Reuse setup to change the model; then re-run `index` for the selected paths to rebuild
embeddings. Original extraction and agent analysis are retained when content is unchanged.
Changing storage requires a separate data directory. Transfer prepared collections
using `export` and `restore`. Do not point the CLI at the file manager's existing
database by default.

`--model none` is an explicit keyword-only/offline diagnostic mode, not a third
recommended embedding model. `index --lexical-only` defers embeddings while preserving
the selected model. Models are never silently downloaded during ordinary search.

CPU defaults: batch 1, 512 tokens, 2 threads. These are conservative starting values,
not measured hardware guarantees. Use setup flags to tune. Long 32K inputs are not
used by default on laptops. Runtime libraries add disk/RAM beyond weight file sizes.
Windows scripts require validation on Windows before advertising Windows support.

## API providers

Choose the embedding and summary services independently. The host still judges
relevance and writes answers. It can also write summaries without an API key.
Set API keys in the environment inherited by the shell or desktop host. Never
request secrets in chat or put them in CLI arguments, profiles, or archives.
Settings store provider endpoints, model IDs and **environment variable names**.

For Chandima-style OpenAI indexing, set `OPENAI_API_KEY`, then:

```bash
snytro setup --storage sqlite --embedding-provider openai --summary-provider openai
snytro model-download
snytro --collection policies index --full --rich "/selected/files"
```

The defaults are `text-embedding-3-small` at 768 dimensions and `gpt-5.4-mini`
for summaries. `--embedding-model` and `--summary-model` override the model IDs.
In API mode, `model-download` sends a synthetic query to test the connection and
dimensions; no weights or PyTorch install are required.

To use a different service, substitute its documented endpoint and models:

```bash
snytro setup \
  --embedding-provider compatible --embedding-base-url https://provider.example/v1 \
  --embedding-model vendor-embedding --embedding-dimensions 1024 --embedding-key-env VENDOR_API_KEY \
  --summary-provider compatible --summary-base-url https://provider.example/v1 \
  --summary-model vendor-chat --summary-key-env VENDOR_API_KEY
```

Embedding services need an OpenAI-compatible `POST /embeddings` route returning
batched float vectors of the configured size. The optional `dimensions` field is
omitted for a compatible endpoint unless `--embedding-send-dimensions` is set.
Summary services need `POST /chat/completions` with a model that can return JSON.
A chat-only service cannot produce vectors. It can be paired with local embeddings
or a different API. Providers with incompatible native routes require a gateway.

API summaries send source content to the summary service. API embeddings send
chunks, summaries, taglines and queries to the embedding service. Review the
selected corpus first: a saved profile excludes unlisted and excluded files before
API indexing. Explicit-only files are still indexed; the restriction is on retrieval.
Hosted Codex or Claude may also process the evidence you show it.

For rich extraction, put `soffice` (DOCX page rendition), `pdftoppm` (PDF
rasterization), and `tesseract` (OCR) on PATH before `index --rich`. Without the
layout tool, DOCX uses a text unit. Scanned PDFs without OCR tools may have missing
text; images require Tesseract. Slides and draw.io labels use built-in parsers.
