"""检查产品检索文档标题编号；--write 清理，默认只检查并返回非零退出码。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sleepagent.document_text import normalize_markdown_headings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "product_docs"
    changed = []
    for folder in (root, root / "user", root / "internal", root / "examples"):
        for path in sorted(folder.glob("*.md")):
            text = path.read_bytes().decode("utf-8")
            clean = normalize_markdown_headings(text)
            if clean != text:
                changed.append(path)
                print(path.relative_to(root))
                if args.write:
                    path.write_bytes(clean.encode("utf-8"))
    print("changed=" + str(len(changed)))
    return 1 if changed and not args.write else 0


if __name__ == "__main__":
    raise SystemExit(main())
