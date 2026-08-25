#!/usr/bin/env bash
# 校验 frontend/dist 是否由 frontend/src 经 `npm run build` 重建、且与 HEAD 一致。
# 用于 CI（frontend-dist job）和本机 pre-commit hook，防止提交"与 src 不同步的 dist"。
# 根因：曾提交过与 src 对不上的 dist（dist 重做 DOM、src 仍按旧 id 找元素），导致编辑页
#       功能静默失效。详见历史反面案例（提交 1820116）。
#
# 两种模式：
#   - 默认（pre-commit 调用）：重建 dist 并自动暂存，仅在 dist 与 HEAD 不同时警告、
#     不阻断提交。解决“同时提交 src+dist 重建”的鸡生蛋问题，同时保证提交物始终与
#     src 一致（1820116 防护）。
#   - --strict（CI 调用）：重建后与 HEAD 比对，若 dist 不同步直接失败（exit 1），
#     作为推送前的权威守门人，拦下任何“手改 dist / 忘了重建”的提交。
set -euo pipefail

STRICT=0
if [ "${1:-}" = "--strict" ] || [ "${CI:-}" = "true" ]; then
  STRICT=1
fi

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
  if [ "$STRICT" = "1" ]; then
    echo "::error::frontend/dist is OUT OF SYNC with frontend/src"
    echo "  提交里包含的 dist 不是由当前 src 重建出来的（可能手改过 dist，或忘了重建）。"
    echo "  本地修复：cd frontend && npm ci && npm run build && git add frontend/dist，再提交/推送。"
    git reset -q frontend/dist
    exit 1
  fi
  # 非严格模式：dist 与 HEAD 不同说明 src 已变动，本次提交的 dist 就是刚重建出的正确
  # 版本。保留暂存的最新 dist（不阻断提交），仅提示。
  echo "::warning::frontend/dist 已由当前 src 重建并暂存，提交将包含最新 dist（与 src 一致）。"
else
  # dist 已与 src 一致：取消暂存，保持工作树清晰
  git reset -q frontend/dist
fi
echo "==> [dist-sync] OK: frontend/dist matches frontend/src"
