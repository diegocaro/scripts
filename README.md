# Diego's scripts repo



Scripts for fun stuff, probably vibe-coded but supervised by me. One of my three cats approved the PRs.

All scripts use `uv` inline dependencies (I love them!).

## Fun stuff

| Script | Description |
|--------|-------------|
| [`ikea_stock_monitor.py`](ikea_stock_monitor.py) | Monitors IKEA Chile product availability and sends Telegram notifications when items are back in stock. Supports continuous monitoring and single-check mode (for cron). |

## How to run the scripts

1. Install [uv](https://docs.astral.sh/uv/)
2. Run a script:

   ```bash
   uv run ikea_stock_monitor.py 30623912
   ```

   `uv` will automatically install the required dependencies on the first run.


## Philosophy

- **Self-contained**: Each script is a single file with everything it needs. Drop it anywhere and it works.
- **`uv` for dependencies**: Dependencies are declared inline using `uv` script metadata — no `requirements.txt`, no virtual env setup.
- **Tests in the same file**: Tests live alongside the code they test, no separate files or directories.
- **Vibe code is fine, but a human reviews it**: AI-generated code is welcome, but a person must read, understand, and own every line before it ships.


## Conventions

- **`--unit-tests` flag**: Scripts should support a `--unit-tests` flag that runs their built-in self-tests. When present, it will be automatically run on every PR and merge to `main`.


## Team

![One of the PR reviewers](assets/july-ascii.png)