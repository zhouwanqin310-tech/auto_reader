#!/bin/bash
# 论文阅读助手 Flask 启动脚本
# 使用方法: ./start_server.sh
# 点击执行时自动关闭旧服务并完整重启

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
WEB_DIR="$PROJECT_ROOT/web"
PID_FILE="$PROJECT_ROOT/.paper_assistant.pid"
LOG_FILE="$PROJECT_ROOT/server.log"
PORT=5001

mkdir -p "$PROJECT_ROOT/papers/notes" "$PROJECT_ROOT/pdfs" "$PROJECT_ROOT/chat_history"

if [ ! -d "$WEB_DIR" ]; then
    echo "❌ 未找到 web 目录: $WEB_DIR"
    exit 1
fi

echo "=================================================="
echo "📚 论文阅读助手"
echo "=================================================="

# 检查 Python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未安装 python3"
    exit 1
fi

# 检查依赖
if ! python3 -c "import flask, yaml, requests, schedule" 2>/dev/null; then
    echo "📦 正在安装运行依赖..."
    python3 -m pip install -r "$PROJECT_ROOT/requirements.txt"
fi

echo ""
echo "♻️ 正在关闭旧服务..."

# 先按 PID 文件关闭旧进程
if [ -f "$PID_FILE" ]; then
    EXISTING_PID=$(<"$PID_FILE")
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        kill "$EXISTING_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$EXISTING_PID" 2>/dev/null; then
            kill -9 "$EXISTING_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

# 再按端口清理占用进程，确保完整重启
PORT_PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null)
if [ -n "$PORT_PIDS" ]; then
    echo "$PORT_PIDS" | xargs kill 2>/dev/null || true
    sleep 1
    PORT_PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null)
    if [ -n "$PORT_PIDS" ]; then
        echo "$PORT_PIDS" | xargs kill -9 2>/dev/null || true
    fi
fi

echo "✅ 旧服务已关闭"
echo ""
echo "🌐 正在后台启动服务器..."
echo ""

nohup python3 "$WEB_DIR/app.py" > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

sleep 1

if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "✅ 服务器已启动 (PID: $SERVER_PID)"
    echo ""
    echo "=================================================="
    echo "🌐 请访问: http://localhost:$PORT"
    echo "=================================================="
    echo ""
    echo "📝 日志文件: $LOG_FILE"
    echo "🛑 停止服务器: kill $SERVER_PID && rm -f '$PID_FILE'"
    echo ""
else
    echo "❌ 启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi

