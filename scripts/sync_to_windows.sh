#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/sync_to_windows.sh <windows-target-path> [--apply] [--delete]

Examples:
  scripts/sync_to_windows.sh /mnt/c/Users/chuan/projects/lka_backend
  scripts/sync_to_windows.sh /mnt/c/Users/chuan/projects/lka_backend --apply
  scripts/sync_to_windows.sh /mnt/c/Users/chuan/projects/lka_backend --apply --delete

Behavior:
  - Default mode is dry-run. It prints what would change.
  - On the WSL-primary development workstation, use the destination only for Windows compatibility tests.
  - This script does not start services or migrate the everyday backend/data to Windows.
  - Copies the application and lockfile; excludes tests, evaluations, local data and environments.
  - --apply writes files to the target path.
  - --delete mirrors removals into the target. Use it only when WSL is the source of truth.
EOF
}

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required. Install it in WSL, for example: sudo apt install rsync" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
target_path=""
apply=false
delete=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      apply=true
      ;;
    --delete)
      delete=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      if [[ -n "$target_path" ]]; then
        echo "Only one target path is supported." >&2
        usage
        exit 1
      fi
      target_path="$1"
      ;;
  esac
  shift
done

if [[ -z "$target_path" ]]; then
  echo "Missing target path." >&2
  usage
  exit 1
fi

target_path="${target_path%/}"
if [[ "$target_path" == "/" || "$target_path" == "$repo_root" ]]; then
  echo "Refusing unsafe target path: $target_path" >&2
  exit 1
fi

rsync_args=(
  -a
  --itemize-changes
  --human-readable
  --exclude .git/
  --exclude .agents/
  --exclude .codex/
  --exclude .aws/
  --exclude .venv/
  --exclude .bootstrap/
  --exclude .uv-cache/
  --exclude scratch/
  --exclude /docs/_engineering/
  --exclude /temp.md
  --exclude /tmp_*.md
  --exclude /tests/
  --exclude /evals/
  --exclude /scripts/eval_\*
  --exclude /scripts/benchmark_\*
  --exclude /scripts/probe_\*
  --exclude /scripts/study_\*
  --exclude /scripts/experiment_\*
  --exclude __pycache__/
  --exclude .pytest_cache/
  --exclude .ruff_cache/
  --exclude .mypy_cache/
  --exclude data/
  --exclude dist/
  --exclude build/
  --exclude "*.egg-info/"
  --exclude "*.pyc"
  --exclude "*.sqlite3"
  --exclude ".coverage"
  --exclude "coverage.xml"
  --exclude htmlcov/
  --exclude .env
  --exclude ':memory:*'
  --exclude /tmp_\*/
  --exclude config/local.toml
  --exclude "config/secrets*.toml"
  --exclude "config/tokens*.json"
  --exclude config/cache/
)

if [[ "$delete" == true ]]; then
  rsync_args+=(--delete)
fi

if [[ "$apply" != true ]]; then
  rsync_args+=(--dry-run)
else
  mkdir -p "$target_path"
fi

echo "Source: $repo_root/"
echo "Target: $target_path/"
echo "Mode:   $([[ "$apply" == true ]] && echo apply || echo dry-run)"
echo "Delete: $delete"
echo

rsync "${rsync_args[@]}" "$repo_root/" "$target_path/"
