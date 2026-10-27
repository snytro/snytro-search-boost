# Setup and runtime

1. Bootstrap base dependencies with `bash "<skill-dir>/scripts/bootstrap.sh"` or
   `& "<skill-dir>/scripts/bootstrap.ps1"`. This launcher needs no Python. It uses
   an existing uv or downloads a versioned uv archive and verifies its release checksum.
   Python 3.12, dependencies and caches are private to `SNYTRO_DATA_DIR`.
2. Run `scripts/snytro doctor` (PowerShell: `scripts/snytro.ps1 doctor`). Diagnostics
   do not scan user documents or download embedding models.
3. Before enabling embeddings, rerun bootstrap with `--embeddings` (`-Embeddings`).
   Before Postgres, also add `--postgres` (`-Postgres`). Install both flags together
   when needed. Dependency installation does not download model weights.
   On Intel macOS, `--embeddings` installs FastEmbed and ONNX Runtime for BGE-small
   instead of the PyTorch extra. On other platforms, use `--bge-small` to install
   that backend explicitly. The existing PyTorch extra remains for UltraBERT and
   Nemotron on supported machines.
4. Configure: `scripts/snytro setup --storage sqlite --model bge-small`, `ultrabert`,
   or `nemotron`. Intel Mac `--model auto` recommends BGE-small for English documents.
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
The BGE-small backend uses 384-dimensional vectors in the same SQLite database and
collection; changing models does not create another database. BGE-small is English
only and supports at most 512 tokens per chunk. Multilingual material needs another model.
Changing storage requires a separate data directory; automatic database migration is
not available. Do not point the CLI at the file manager's existing database by default.

`--model none` is an explicit keyword-only/offline diagnostic mode, not a third
recommended embedding model. `index --lexical-only` defers embeddings while preserving
the selected model. Models are never silently downloaded during ordinary search.

CPU defaults: batch 1, 512 tokens, 2 threads. These are conservative starting values,
not measured hardware guarantees. Use setup flags to tune. Long 32K inputs are not
used by default on laptops. Runtime libraries add disk/RAM beyond weight file sizes.
Windows scripts require validation on Windows before advertising Windows support.
