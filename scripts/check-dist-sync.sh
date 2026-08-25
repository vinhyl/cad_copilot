#!/usr/bin/env bash
# 校验 frontend/dist 是否由 frontend/src 经 `npm run build` 重建、且与 HEAD 一致。
# 用于 CI（frontend-dist job）和本机 pre-commit hook，防止提交"与 src 不同步的 dist"。
# 根因：曾提交过与 src 对不上的 dist（dist 重做 DOM、src 仍按旧 id 找元素），导致编辑页
#       功能静默失效。详见历史反面案例（提交 1820116）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"

echo "==> [dist-sync] installing frontend deps (npm ci)"
npm ci

echo "==> [dist-sync] rebuilding frontend/dist (npm run build)"
rm -rf dist
npm run build

cd "$ROOT"
# 把所有 dist 变动（含新增/删除的 chunk）暂存，再与 HEAD 比对
git add -A frontend/dist
if [ -n "$(git diff --cached --name-only -- frontend/dist)" ]; then
  echo "::error::frontend/dist is OUT OF SYNC with frontend/src"
  echo "  提交里包含的 dist 不是由当前 src 重建出来的（可能手改过 dist，或忘了重建）。"
  echo "  修复：cd frontend && npm ci && npm run build && git add frontend/dist，再提交。"
  git reset -q frontend/dist
  exit 1
fi
git reset -q frontend/dist
echo "==> [dist-sync] OK: frontend/dist matches frontend/src"
