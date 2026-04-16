#!/usr/bin/env python3
"""检查论文 Markdown 结构：必须只有一个一级标题，且至少包含多个二级标题。"""
import re
import sys
from pathlib import Path


def validate_markdown(path: Path) -> int:
    content = path.read_text(encoding='utf-8')
    lines = content.splitlines()

    h1_lines = [line for line in lines if re.match(r'^#(?!#)\s+', line)]
    h2_lines = [line for line in lines if re.match(r'^##(?!#)\s+', line)]

    errors = []
    if len(h1_lines) != 1:
        errors.append(f'一级标题数量错误：期望 1，实际 {len(h1_lines)}')
    if len(h2_lines) < 2:
        errors.append(f'二级标题数量不足：至少需要 2 个，实际 {len(h2_lines)}')

    if errors:
        print(f'❌ {path}')
        for error in errors:
            print(f'  - {error}')
        return 1

    print(f'✅ {path} | H1={len(h1_lines)} H2={len(h2_lines)}')
    return 0


def collect_targets(args):
    if args:
        return [Path(arg).expanduser().resolve() for arg in args]

    project_root = Path(__file__).resolve().parents[1]
    papers_dir = project_root / 'papers'
    return sorted(papers_dir.glob('*.md'))


def main():
    targets = collect_targets(sys.argv[1:])
    if not targets:
        print('未找到要检查的 Markdown 文件')
        return 1

    exit_code = 0
    for target in targets:
        if not target.exists():
            print(f'❌ 文件不存在: {target}')
            exit_code = 1
            continue
        exit_code = max(exit_code, validate_markdown(target))

    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
