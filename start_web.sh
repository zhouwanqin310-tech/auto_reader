#!/bin/bash
# 论文阅读助手 Web 版前台启动脚本

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
WEB_DIR="$PROJECT_ROOT/web"

if [ ! -d "$WEB_DIR" ]; then
    echo "❌ 未找到 web 目录: $WEB_DIR"
    exit 1
fi

echo "=================================================="
echo "📚 论文阅读助手 Web 版"
echo "=================================================="

# 检查依赖
if ! python3 -c "import flask, yaml, requests, schedule" 2>/dev/null; then
    echo "❌ 依赖未安装，正在安装..."
    python3 -m pip install -r "$PROJECT_ROOT/requirements.txt"
    echo "✅ 安装完成"
fi

echo ""
echo "🌐 启动中..."
echo ""

python3 "$WEB_DIR/app.py"
