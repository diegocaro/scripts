# Philosophy

- **Self-contained**: Each script is a single file with everything it needs. Drop it anywhere and it works.
- **`uv` for dependencies**: Dependencies are declared inline using `uv` script metadata — no `requirements.txt`, no virtual env setup.
- **Tests in the same file**: Tests live alongside the code they test, no separate files or directories.
- **Vibe code is fine, but a human reviews it**: AI-generated code is welcome, but a person must read, understand, and own every line before it ships.

# Conventions

- **`--unit-tests` flag**: Scripts should support a `--unit-tests` flag that runs their built-in self-tests. When present, it will be automatically run on every PR and merge to `main`.
