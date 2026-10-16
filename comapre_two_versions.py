"""
Compare two directories and show only files with differences,
including which copy is more recent.
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path

DIR1 = r"C:\Users\dahab\AppData\Local\JetBrains\PyCharm2026.1\projects\music_studio-webapp.3ebfb088"
DIR2 = r"C:\Users\dahab\AppData\Local\JetBrains\PyCharm2026.2\projects\music_studio-webapp.3ebfb088"


def file_hash(path: Path, chunk_size: int = 65536) -> str:
    """Return MD5 hash of a file's content."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(root: str) -> dict:
    """Walk a directory and return {relative_path: info}."""
    root_path = Path(root)
    files = {}
    if not root_path.exists():
        print(f"Warning: {root} does not exist.")
        return files
    for p in root_path.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(root_path))
            try:
                stat = p.stat()
                files[rel] = {
                    "path": p,
                    "mtime": datetime.fromtimestamp(stat.st_mtime),
                    "size": stat.st_size,
                    "hash": file_hash(p),
                }
            except (OSError, PermissionError) as e:
                print(f"Skipping {p}: {e}")
    return files


def compare(dir1: str, dir2: str):
    files1 = collect_files(dir1)
    files2 = collect_files(dir2)

    all_paths = sorted(set(files1) | set(files2))
    results = []

    for rel in all_paths:
        f1 = files1.get(rel)
        f2 = files2.get(rel)

        if f1 and not f2:
            status, newer = "Only in 2026.1", "2026.1"
        elif f2 and not f1:
            status, newer = "Only in 2026.2", "2026.2"
        elif f1["hash"] != f2["hash"]:
            status = "Content differs"
            if f1["mtime"] > f2["mtime"]:
                newer = "2026.1"
            elif f2["mtime"] > f1["mtime"]:
                newer = "2026.2"
            else:
                newer = "Same time"
        else:
            # Identical content — skip
            continue

        results.append({
            "file": rel,
            "status": status,
            "newer": newer,
            "date1": f1["mtime"].strftime("%Y-%m-%d %H:%M:%S") if f1 else "-",
            "date2": f2["mtime"].strftime("%Y-%m-%d %H:%M:%S") if f2 else "-",
            "size1": f1["size"] if f1 else "-",
            "size2": f2["size"] if f2 else "-",
        })

    return results


def print_table(rows: list):
    if not rows:
        print("No differences found.")
        return

    headers = ["File", "Status", "Newer", "2026.1 Date", "2026.2 Date", "2026.1 Size", "2026.2 Size"]
    keys = ["file", "status", "newer", "date1", "date2", "size1", "size2"]

    # Compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, k in enumerate(keys):
            widths[i] = max(widths[i], len(str(row[k])))

    # Cap File column so the table stays readable
    max_file_width = 80
    file_idx = keys.index("file")
    if widths[file_idx] > max_file_width:
        widths[file_idx] = max_file_width

    def truncate(text, width):
        text = str(text)
        if len(text) <= width:
            return text
        return "..." + text[-(width - 3):]

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    print(sep)
    print("|" + "|".join(f" {h:<{widths[i]}} " for i, h in enumerate(headers)) + "|")
    print(sep)

    for row in rows:
        cells = []
        for i, k in enumerate(keys):
            val = truncate(row[k], widths[i])
            cells.append(f" {val:<{widths[i]}} ")
        print("|" + "|".join(cells) + "|")

    print(sep)
    print(f"\nTotal files with differences: {len(rows)}")


def main():
    print(f"Comparing:\n  2026.1: {DIR1}\n  2026.2: {DIR2}\n")
    rows = compare(DIR1, DIR2)
    print_table(rows)

    # Optional: export to CSV
    # import csv
    # with open("diff_report.csv", "w", newline="", encoding="utf-8") as f:
    #     writer = csv.DictWriter(f, fieldnames=["file","status","newer","date1","date2","size1","size2"])
    #     writer.writeheader()
    #     writer.writerows(rows)
    # print("\nReport saved to diff_report.csv")


if __name__ == "__main__":
    main()