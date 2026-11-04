#!/usr/bin/env bash
# Ollama 内存调优（Mac 上的 Ollama 应用）：开启 Flash Attention，并把 KV 缓存从 16 bit 改为 8 bit。
# 8 GB 机器上可再省约一半的 KV 缓存（4K 上下文时约 0.3 GB）。
#
# 这会修改当前登录会话的环境变量（launchctl setenv），重启电脑后失效，需要时再运行一次。
# 运行后会重启 Ollama 应用让设置生效。
set -euo pipefail

show() {
  for k in OLLAMA_FLASH_ATTENTION OLLAMA_KV_CACHE_TYPE; do
    printf "  %-24s %s\n" "$k" "$(launchctl getenv "$k" || true)"
  done
}

echo "当前设置："
show
if [[ "${1:-}" == "--check" ]]; then
  exit 0
fi

read -r -p "设置 OLLAMA_FLASH_ATTENTION=1、OLLAMA_KV_CACHE_TYPE=q8_0 并重启 Ollama？[y/N] " ans
[[ "$ans" =~ ^[Yy]$ ]] || { echo "未修改。"; exit 0; }

launchctl setenv OLLAMA_FLASH_ATTENTION 1
launchctl setenv OLLAMA_KV_CACHE_TYPE q8_0
echo "已设置："
show

echo "重启 Ollama…"
osascript -e 'quit app "Ollama"' 2>/dev/null || true
sleep 3
open -a Ollama
for _ in $(seq 1 30); do
  curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null && { echo "Ollama 已重新启动。"; exit 0; }
  sleep 1
done
echo "Ollama 没有自动启动，请手动打开 Ollama 应用。"
