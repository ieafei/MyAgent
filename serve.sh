#!/usr/bin/env bash
# 启动服务，日志按启动时间落到 log/ 下：log/2026-11-25_17-45-30.log
#
# 为什么用脚本而不是每次手敲 nohup：时间戳文件名必须和"这一次启动"绑定，
# 手打容易忘、也容易复制昨天的命令把日志覆盖掉。参数透传给 main.py，
# 例如 ./serve.sh --port 8011 --reload。

set -uo pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-.venv/bin/python}
mkdir -p log

log_file="log/$(date +%Y-%m-%d_%H-%M-%S).log"

# > 而不是 >>：文件名已经按时间唯一，追加只会把不该混的东西混进来
nohup "$PYTHON" main.py "$@" >"$log_file" 2>&1 &
pid=$!

sleep 2
if kill -0 "$pid" 2>/dev/null; then
    echo "服务已启动 pid=$pid"
    echo "日志: $log_file"
    echo "查看: tail -f $log_file"
else
    echo "启动失败（进程已退出），日志末尾：" >&2
    tail -n 20 "$log_file" >&2
    exit 1
fi
