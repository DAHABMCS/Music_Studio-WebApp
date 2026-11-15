#!/usr/bin/env python3
"""DiskScope - free TreeSize Pro-style disk space analyzer.

GUI:       python DiskScope.py [folder]
Headless:  python DiskScope.py D:\\Data --csv report.csv --html report.html
           (use with Windows Task Scheduler for scheduled reports)
Optional:  pip install send2trash   (delete -> Recycle Bin instead of permanent)
"""
import os, sys, csv, json, time, heapq, hashlib, zlib, colorsys, string, shutil
import threading, subprocess, html, datetime, queue

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    tk = None
try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


# ---------------------------------------------------------------- helpers
def fmt(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def dt(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
    except (OSError, ValueError, OverflowError):
        return ""


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def color_for(name, is_dir=False):
    key = name if is_dir else (os.path.splitext(name)[1].lower() or "none")
    h = (zlib.crc32(key.encode()) % 360) / 360
    r, g, b = colorsys.hsv_to_rgb(h, .25 if is_dir else .5, .72 if is_dir else .88)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


class Node:
    __slots__ = ("name", "path", "parent", "size", "nfiles", "ndirs", "mtime", "dirs", "files")

    def __init__(s, name, path, parent=None):
        s.name, s.path, s.parent = name, path, parent
        s.size = s.nfiles = s.ndirs = 0
        s.mtime = 0
        s.dirs, s.files = [], []          # files: (name, size, mtime)


class Scanner:
    def __init__(s):
        s.stop, s.count, s.cur = False, 0, ""

    def scan(s, node):
        s.cur = node.path
        try:
            it = os.scandir(node.path)
        except OSError:
            return
        with it:
            for e in it:
                if s.stop:
                    return
                try:
                    if e.is_symlink() or getattr(e, "is_junction", lambda: False)():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        c = Node(e.name, e.path, node)
                        node.dirs.append(c)
                        s.scan(c)
                        node.size += c.size
                        node.nfiles += c.nfiles
                        node.ndirs += c.ndirs + 1
                        node.mtime = max(node.mtime, c.mtime)
                    else:
                        st = e.stat(follow_symlinks=False)
                        node.files.append((e.name, st.st_size, st.st_mtime))
                        node.size += st.st_size
                        node.nfiles += 1
                        node.mtime = max(node.mtime, st.st_mtime)
                        s.count += 1
                except OSError:
                    pass


def walk_dirs(n):
    st = [n]
    while st:
        d = st.pop()
        yield d
        st.extend(d.dirs)


def all_files(root):
    for d in walk_dirs(root):
        for f in d.files:
            yield d, f


def squarify(items, x, y, w, h):
    """items: [(size, obj)] sorted desc -> [(x, y, w, h, obj)]"""
    out, total = [], sum(a for a, _ in items)
    if total <= 0 or w < 1 or h < 1:
        return out
    k = w * h / total
    items = [(a * k, o) for a, o in items]

    def worst(r, short):
        t, mx, mn = sum(a for a, _ in r), max(a for a, _ in r), min(a for a, _ in r)
        return max(short * short * mx / (t * t), t * t / (short * short * mn))

    while items:
        short = min(w, h)
        row, i = [items[0]], 1
        while i < len(items) and worst(row + [items[i]], short) <= worst(row, short):
            row.append(items[i])
            i += 1
        items = items[i:]
        t = sum(a for a, _ in row)
        if w >= h:
            cw, yy = t / h, y
            for a, o in row:
                out.append((x, yy, cw, a / cw, o))
                yy += a / cw
            x += cw
            w -= cw
        else:
            rh, xx = t / w, x
            for a, o in row:
                out.append((xx, y, a / rh, rh, o))
                xx += a / rh
            y += rh
            h -= rh
    return out


def write_csv(top, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Path", "Size (bytes)", "Size", "Files", "Folders", "Last modified"])
        for d in sorted(walk_dirs(top), key=lambda d: d.path.lower()):
            w.writerow([d.path, d.size, fmt(d.size), d.nfiles, d.ndirs, dt(d.mtime)])


def write_html(top, path):
    rows = []

    def rec(n, depth):
        p = n.size / top.size * 100 if top.size else 0
        rows.append(f'<tr><td style="padding-left:{depth * 18}px">{html.escape(n.name or n.path)}</td>'
                    f'<td>{fmt(n.size)}</td><td><div style="background:#4a90d9;height:10px;'
                    f'width:{p:.1f}%"></div></td><td>{p:.1f}%</td><td>{n.nfiles:,}</td></tr>')
        if depth < 3:
            for c in sorted(n.dirs, key=lambda c: -c.size)[:25]:
                rec(c, depth + 1)

    rec(top, 0)
    big = heapq.nlargest(50, all_files(top), key=lambda x: x[1][1])
    ft = {}
    for _, f in all_files(top):
        r = ft.setdefault(os.path.splitext(f[0])[1].lower() or "(none)", [0, 0])
        r[0] += 1
        r[1] += f[1]
    types = "".join(f"<tr><td>{html.escape(e)}</td><td>{c:,}</td><td>{fmt(sz)}</td></tr>"
                    for e, (c, sz) in sorted(ft.items(), key=lambda x: -x[1][1])[:30])
    files = "".join(f"<tr><td>{html.escape(os.path.join(d.path, f[0]))}</td><td>{fmt(f[1])}</td>"
                    f"<td>{dt(f[2])}</td></tr>" for d, f in big)
    doc = f"""<!doctype html><meta charset=utf-8><title>DiskScope report</title>
<style>body{{font:14px Segoe UI,sans-serif;margin:30px}}table{{border-collapse:collapse;width:100%;margin-bottom:30px}}
td,th{{padding:3px 8px;border-bottom:1px solid #ddd;text-align:left}}</style>
<h1>DiskScope report</h1><p>{html.escape(top.path)} &ndash; {fmt(top.size)} in {top.nfiles:,} files, {top.ndirs:,} folders
&ndash; {datetime.datetime.now():%Y-%m-%d %H:%M}</p>
<h2>Folders</h2><table><tr><th>Name<th>Size<th>Share<th>%<th>Files</tr>{"".join(rows)}</table>
<h2>File types</h2><table><tr><th>Type<th>Files<th>Size</tr>{types}</table>
<h2>Largest files</h2><table><tr><th>Path<th>Size<th>Modified</tr>{files}</table>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


# ---------------------------------------------------------------- GUI
class App:
    def __init__(s, root, start=None):
        s.root, s.top, s.nodes, s.cur = root, None, {}, None
        s.rects, s.q, s.scanner, s.trees, s.ctx_t = [], queue.Queue(), None, [], None
        root.title("DiskScope - free disk space analyzer")
        root.geometry("1250x760")

        drives = ([f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
                  if os.name == "nt" else ["/", os.path.expanduser("~")])
        s.path = tk.StringVar(value=start or drives[0])
        bar = ttk.Frame(root, padding=4)
        bar.pack(fill="x")
        ttk.Combobox(bar, textvariable=s.path, values=drives, width=48).pack(side="left", padx=2)
        for text, cmd in (("Browse...", s.browse), ("Scan", s.scan), ("Stop", s.stop),
                          ("Export CSV", s.export_csv), ("Export HTML", s.export_html),
                          ("Save snapshot", s.save_snap), ("Compare...", s.compare),
                          ("Run as admin", s.admin)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)

        s.status = ttk.Label(root, anchor="w", relief="sunken", padding=3)
        s.status.pack(side="bottom", fill="x")
        s.nb = ttk.Notebook(root)
        s.nb.pack(fill="both", expand=True)

        # context menu shared by every list
        s.menu = tk.Menu(root, tearoff=0)
        s.menu.add_command(label="Show in Explorer", command=s.reveal_sel)
        s.menu.add_command(label="Copy path", command=s.copy_sel)
        s.menu.add_separator()
        s.menu.add_command(label="Delete (Recycle Bin)", command=lambda: s.delete_sel(s.ctx_t))

        # Folders tab
        f = s.tab("Folders")
        s.tree = s.mk_tree(f, [("#0", "Name", 420), ("size", "Size", 90), ("pct", "% of parent", 150),
                               ("files", "Files", 80), ("mod", "Last modified", 130)], tree=True)
        s.tree.bind("<<TreeviewOpen>>", s.on_open)
        s.tree.bind("<<TreeviewSelect>>", s.on_select)

        # Treemap tab
        f = s.tab("Treemap")
        s.cv_title = ttk.Label(f, text="Click a folder to drill in, right-click to go up")
        s.cv_title.pack(anchor="w")
        s.cv = tk.Canvas(f, bg="#222", highlightthickness=0)
        s.cv.pack(fill="both", expand=True)
        s.cv.bind("<Configure>", lambda e: s.draw())
        s.cv.bind("<Button-1>", s.cv_click)
        s.cv.bind("<Button-3>", s.cv_up)
        s.cv.bind("<Motion>", s.cv_move)

        # File types
        f = s.tab("File types")
        s.t_types = s.mk_tree(f, [("ext", "Type", 160), ("count", "Files", 100),
                                  ("size", "Size", 110), ("pct", "% of total", 100)])
        # Largest files
        f = s.tab("Largest files")
        s.t_top = s.mk_tree(f, s.file_cols())
        # Old files
        f = s.tab("Old files")
        c = ttk.Frame(f)
        c.pack(fill="x", pady=3)
        s.old_days, s.old_mb = tk.StringVar(value="365"), tk.StringVar(value="1")
        ttk.Label(c, text="Not modified for (days):").pack(side="left")
        ttk.Entry(c, textvariable=s.old_days, width=6).pack(side="left", padx=4)
        ttk.Label(c, text="Min size (MB):").pack(side="left")
        ttk.Entry(c, textvariable=s.old_mb, width=6).pack(side="left", padx=4)
        ttk.Button(c, text="Show", command=s.show_old).pack(side="left")
        s.t_old = s.mk_tree(f, s.file_cols())
        # Duplicates
        f = s.tab("Duplicates")
        c = ttk.Frame(f)
        c.pack(fill="x", pady=3)
        s.dup_mb = tk.StringVar(value="1")
        ttk.Label(c, text="Min size (MB):").pack(side="left")
        ttk.Entry(c, textvariable=s.dup_mb, width=6).pack(side="left", padx=4)
        ttk.Button(c, text="Find duplicates", command=s.find_dups).pack(side="left")
        ttk.Button(c, text="Select all but first in each group", command=s.dup_select).pack(side="left", padx=6)
        ttk.Button(c, text="Delete selected", command=lambda: s.delete_sel(s.t_dup)).pack(side="left")
        s.t_dup = s.mk_tree(f, [("#0", "Group / file", 800), ("size", "Size", 100), ("mod", "Modified", 130)],
                            tree=True)
        # Search
        f = s.tab("Search")
        c = ttk.Frame(f)
        c.pack(fill="x", pady=3)
        s.sq, s.smin = tk.StringVar(), tk.StringVar(value="0")
        ttk.Label(c, text="Name contains:").pack(side="left")
        e = ttk.Entry(c, textvariable=s.sq, width=30)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _: s.search())
        ttk.Label(c, text="Min size (MB):").pack(side="left")
        ttk.Entry(c, textvariable=s.smin, width=6).pack(side="left", padx=4)
        ttk.Button(c, text="Search", command=s.search).pack(side="left")
        s.t_search = s.mk_tree(f, s.file_cols())

        s.st("Pick a drive or folder and press Scan. Tip: 'Run as admin' shows protected folders too.")
        if start:
            s.scan()

    # ---- widget builders
    def tab(s, title):
        f = ttk.Frame(s.nb, padding=3)
        s.nb.add(f, text=title)
        return f

    @staticmethod
    def file_cols():
        return [("name", "Name", 260), ("size", "Size", 90), ("mod", "Modified", 130), ("path", "Folder", 600)]

    def mk_tree(s, parent, cols, tree=False):
        fr = ttk.Frame(parent)
        fr.pack(fill="both", expand=True)
        data = [c[0] for c in cols if c[0] != "#0"]
        t = ttk.Treeview(fr, columns=data, show="tree headings" if tree else "headings", selectmode="extended")
        for cid, title, w in cols:
            t.heading(cid, text=title)
            t.column(cid, width=w, anchor="w" if cid in ("#0", "name", "path", "ext") else "e",
                     stretch=cid in ("#0", "path"))
        sb = ttk.Scrollbar(fr, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.bind("<Button-3>", lambda e, t=t: s.ctx(e, t))
        t.bind("<Delete>", lambda e, t=t: s.delete_sel(t))
        s.trees.append(t)
        return t

    def st(s, text):
        s.status.config(text=text)

    # ---- scanning
    def browse(s):
        d = filedialog.askdirectory()
        if d:
            s.path.set(os.path.normpath(d))

    def scan(s):
        p = s.path.get().strip()
        if not os.path.isdir(p):
            return messagebox.showerror("DiskScope", "That folder does not exist.")
        s.stop()
        sc, top = Scanner(), Node(p, p)
        s.scanner, s.t0 = sc, time.time()

        def work():
            sc.scan(top)
            s.q.put((sc, top))

        threading.Thread(target=work, daemon=True).start()
        s.poll()

    def stop(s):
        if s.scanner:
            s.scanner.stop = True

    def poll(s):
        if s.scanner is None:
            return
        try:
            sc, top = s.q.get_nowait()
        except queue.Empty:
            s.st(f"Scanning... {s.scanner.count:,} files   {s.scanner.cur[-90:]}")
            return s.root.after(150, s.poll)
        if sc is not s.scanner:
            return s.root.after(0, s.poll)
        s.scanner = None
        s.top = top
        s.after_scan()
        s.st(f"{'Stopped (partial)' if sc.stop else 'Done'}: {fmt(top.size)} in {top.nfiles:,} files, "
             f"{top.ndirs:,} folders - {time.time() - s.t0:.1f}s")

    def after_scan(s):
        t = s.top
        s.nodes = {d.path: d for d in walk_dirs(t)}
        s.tree.delete(*s.tree.get_children())
        s.tree.insert("", "end", iid=t.path, text="[D] " + t.path,
                      values=(fmt(t.size), "100%", f"{t.nfiles:,}", dt(t.mtime)))
        s.tree.insert(t.path, "end", iid="ph:" + t.path)
        s.tree.focus(t.path)
        s.on_open()
        s.tree.item(t.path, open=True)
        s.cur = t
        s.draw()
        ft = {}
        for _, f in all_files(t):
            r = ft.setdefault(os.path.splitext(f[0])[1].lower() or "(none)", [0, 0])
            r[0] += 1
            r[1] += f[1]
        s.t_types.delete(*s.t_types.get_children())
        for e, (c, sz) in sorted(ft.items(), key=lambda x: -x[1][1])[:1000]:
            s.t_types.insert("", "end", values=(e, f"{c:,}", fmt(sz), f"{sz / t.size * 100 if t.size else 0:.1f}%"))
        s.fill_files(s.t_top, heapq.nlargest(500, all_files(t), key=lambda x: x[1][1]))
        s.show_old()

    # ---- folder tree
    @staticmethod
    def pct(a, tot):
        p = a / tot if tot else 0
        return "#" * round(p * 12) + f" {p * 100:.1f}%"

    def on_open(s, _=None):
        iid = s.tree.focus()
        kids = s.tree.get_children(iid)
        if len(kids) == 1 and kids[0].startswith("ph:"):
            s.tree.delete(kids[0])
            n = s.nodes[iid]
            rows = [(d.size, 0, d) for d in n.dirs] + [(f[1], 1, f) for f in n.files]
            rows.sort(key=lambda r: -r[0])
            for size, kind, o in rows[:3000]:
                if kind == 0:
                    s.tree.insert(iid, "end", iid=o.path, text="[D] " + o.name,
                                  values=(fmt(size), s.pct(size, n.size), f"{o.nfiles:,}", dt(o.mtime)))
                    if o.dirs or o.files:
                        s.tree.insert(o.path, "end", iid="ph:" + o.path)
                else:
                    s.tree.insert(iid, "end", iid=os.path.join(n.path, o[0]), text="    " + o[0],
                                  values=(fmt(size), s.pct(size, n.size), "", dt(o[2])))

    def on_select(s, _=None):
        iid = s.tree.focus()
        if iid in s.nodes:
            s.cur = s.nodes[iid]
            s.draw()

    # ---- treemap
    def draw(s):
        c, n = s.cv, s.cur
        c.delete("all")
        s.rects = []
        if not n:
            return
        s.cv_title.config(text=f"{n.path}   ({fmt(n.size)})  - click folder to enter, right-click = up")
        items = [(d.size, d) for d in n.dirs if d.size > 0]
        items += [(f[1], (n, f)) for f in heapq.nlargest(400, n.files, key=lambda f: f[1]) if f[1] > 0]
        items.sort(key=lambda t: -t[0])
        for x, y, w, h, o in squarify(items, 2, 2, c.winfo_width() - 4, c.winfo_height() - 4):
            isdir = isinstance(o, Node)
            name = o.name if isdir else o[1][0]
            c.create_rectangle(x, y, x + w, y + h, fill=color_for(name, isdir), outline="#222")
            if w > 60 and h > 16:
                c.create_text(x + 4, y + 3, anchor="nw", text=name, width=w - 8, font=("Segoe UI", 8))
            s.rects.append((x, y, x + w, y + h, o))

    def hit(s, e):
        for x0, y0, x1, y1, o in s.rects:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                return o

    def cv_click(s, e):
        o = s.hit(e)
        if isinstance(o, Node):
            s.cur = o
            s.draw()

    def cv_up(s, _):
        if s.cur and s.cur.parent and s.cur is not s.top:
            s.cur = s.cur.parent
            s.draw()

    def cv_move(s, e):
        o = s.hit(e)
        if isinstance(o, Node):
            s.st(f"{o.path}  -  {fmt(o.size)}  -  {o.nfiles:,} files")
        elif o:
            s.st(f"{os.path.join(o[0].path, o[1][0])}  -  {fmt(o[1][1])}")

    # ---- file lists
    def fill_files(s, t, pairs):
        t.delete(*t.get_children())
        for d, f in pairs:
            try:
                t.insert("", "end", iid=os.path.join(d.path, f[0]),
                         values=(f[0], fmt(f[1]), dt(f[2]), d.path))
            except tk.TclError:
                pass

    def show_old(s):
        if not s.top:
            return
        cutoff = time.time() - num(s.old_days.get(), 365) * 86400
        mn = num(s.old_mb.get(), 1) * 1048576
        pairs = [(d, f) for d, f in all_files(s.top) if f[2] < cutoff and f[1] >= mn]
        s.fill_files(s.t_old, heapq.nlargest(3000, pairs, key=lambda x: x[1][1]))

    def search(s):
        if not s.top:
            return
        q, mn = s.sq.get().lower(), num(s.smin.get()) * 1048576
        pairs = [(d, f) for d, f in all_files(s.top) if q in f[0].lower() and f[1] >= mn]
        s.fill_files(s.t_search, heapq.nlargest(5000, pairs, key=lambda x: x[1][1]))
        s.st(f"{len(pairs):,} matches")

    # ---- duplicates
    @staticmethod
    def _hash(p, limit=None):
        try:
            h = hashlib.md5()
            with open(p, "rb") as fh:
                if limit:
                    h.update(fh.read(limit))
                else:
                    for b in iter(lambda: fh.read(1 << 20), b""):
                        h.update(b)
            return h.hexdigest()
        except OSError:
            return None

    def find_dups(s):
        if not s.top:
            return
        mn = max(1, num(s.dup_mb.get(), 1) * 1048576)
        s.st("Finding duplicates (size -> quick hash -> full hash)...")

        def work():
            by = {}
            for d, f in all_files(s.top):
                if f[1] >= mn:
                    by.setdefault(f[1], []).append(os.path.join(d.path, f[0]))
            groups = []
            for size, ps in by.items():
                if len(ps) < 2:
                    continue
                quick = {}
                for p in ps:
                    h = s._hash(p, 65536)
                    if h:
                        quick.setdefault(h, []).append(p)
                for ps2 in quick.values():
                    full = {}
                    for p in (ps2 if len(ps2) > 1 else []):
                        h = s._hash(p)
                        if h:
                            full.setdefault(h, []).append(p)
                    groups += [(size, g) for g in full.values() if len(g) > 1]
            groups.sort(key=lambda g: -g[0] * (len(g[1]) - 1))
            s.root.after(0, lambda: s.show_dups(groups))

        threading.Thread(target=work, daemon=True).start()

    def show_dups(s, groups):
        t = s.t_dup
        t.delete(*t.get_children())
        for i, (size, g) in enumerate(groups[:2000]):
            gid = f"grp:{i}"
            t.insert("", "end", iid=gid, open=True,
                     text=f"{len(g)} copies x {fmt(size)} - wasted {fmt(size * (len(g) - 1))}")
            for p in g:
                try:
                    t.insert(gid, "end", iid=p, text=p, values=(fmt(size), dt(os.path.getmtime(p))))
                except (OSError, tk.TclError):
                    pass
        waste = sum(sz * (len(g) - 1) for sz, g in groups)
        s.st(f"{len(groups):,} duplicate groups - {fmt(waste)} could be freed")

    def dup_select(s):
        sel = []
        for g in s.t_dup.get_children():
            sel += s.t_dup.get_children(g)[1:]
        s.t_dup.selection_set(sel)

    # ---- file actions
    def ctx(s, e, t):
        r = t.identify_row(e.y)
        if r and r not in t.selection():
            t.selection_set(r)
        s.ctx_t = t
        s.menu.tk_popup(e.x_root, e.y_root)

    @staticmethod
    def sel_paths(t):
        return [i for i in t.selection() if os.path.exists(i)]

    def reveal_sel(s):
        ps = s.sel_paths(s.ctx_t)
        if not ps:
            return
        p = os.path.normpath(ps[0])
        if os.name == "nt":
            subprocess.Popen(f'explorer /select,"{p}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(p)])

    def copy_sel(s):
        ps = s.sel_paths(s.ctx_t)
        s.root.clipboard_clear()
        s.root.clipboard_append("\n".join(ps))

    def delete_sel(s, t):
        ps = s.sel_paths(t)
        if not ps:
            return
        how = "to the Recycle Bin" if send2trash else "PERMANENTLY (install send2trash for Recycle Bin)"
        if not messagebox.askyesno("Delete", f"Delete {len(ps)} item(s) {how}?\n\n" + "\n".join(ps[:8])):
            return
        for p in ps:
            try:
                if send2trash:
                    send2trash(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            except Exception as ex:
                messagebox.showerror("Delete failed", f"{p}\n{ex}")
                continue
            s.remove_path(p)
            for tr in s.trees:
                if tr.exists(p):
                    tr.delete(p)
        s.st(f"Deleted {len(ps)} item(s).")

    def remove_path(s, p):
        n = s.nodes.get(p)
        if n:
            par, size, nf, nd = n.parent, n.size, n.nfiles, n.ndirs + 1
            if par:
                par.dirs.remove(n)
            for k in [k for k in s.nodes if k == p or k.startswith(p + os.sep)]:
                del s.nodes[k]
        else:
            par = s.nodes.get(os.path.dirname(p))
            f = next((f for f in par.files if f[0] == os.path.basename(p)), None) if par else None
            if not f:
                return
            par.files.remove(f)
            size, nf, nd = f[1], 1, 0
        while par:
            par.size -= size
            par.nfiles -= nf
            par.ndirs -= nd
            if s.tree.exists(par.path):
                s.tree.set(par.path, "size", fmt(par.size))
            par = par.parent

    # ---- export / snapshots / admin
    def export_csv(s):
        if s.top:
            p = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")])
            if p:
                write_csv(s.top, p)
                s.st("Saved " + p)

    def export_html(s):
        if s.top:
            p = filedialog.asksaveasfilename(defaultextension=".html", filetypes=[("HTML report", "*.html")])
            if p:
                write_html(s.top, p)
                s.st("Saved " + p)

    def save_snap(s):
        if not s.top:
            return
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"snapshot_{datetime.date.today()}.json",
                                         filetypes=[("Snapshot", "*.json")])
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"root": s.top.path, "time": time.time(),
                           "dirs": {d.path: d.size for d in walk_dirs(s.top)}}, fh)
            s.st("Snapshot saved. Scan again later and use Compare to see what grew.")

    def compare(s):
        if not s.top:
            return messagebox.showinfo("Compare", "Scan a folder first, then compare it with an older snapshot.")
        p = filedialog.askopenfilename(filetypes=[("Snapshot", "*.json")])
        if not p:
            return
        with open(p, encoding="utf-8") as fh:
            old = json.load(fh)
        new = {d.path: d.size for d in walk_dirs(s.top)}
        diff = sorted(((new.get(k, 0) - old["dirs"].get(k, 0), k) for k in set(new) | set(old["dirs"])),
                      key=lambda x: -abs(x[0]))
        w = tk.Toplevel(s.root)
        w.title(f"Changes since {dt(old['time'])}")
        w.geometry("900x500")
        t = s.mk_tree(w, [("path", "Folder", 560), ("old", "Before", 100), ("new", "Now", 100), ("chg", "Change", 110)])
        for ch, k in [x for x in diff if x[0]][:2000]:
            t.insert("", "end", values=(k, fmt(old["dirs"].get(k, 0)), fmt(new.get(k, 0)),
                                        ("+" if ch > 0 else "-") + fmt(abs(ch))))

    def admin(s):
        if os.name != "nt":
            return messagebox.showinfo("DiskScope", "Run with sudo to read protected folders.")
        import ctypes
        args = " ".join(f'"{a}"' for a in sys.argv[1:])
        if not getattr(sys, "frozen", False):
            args = f'"{os.path.abspath(sys.argv[0])}" ' + args
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)
        s.root.destroy()


def main():
    args, opts, pos, i = sys.argv[1:], {}, [], 0
    while i < len(args):
        if args[i] in ("--csv", "--html") and i + 1 < len(args):
            opts[args[i]] = args[i + 1]
            i += 2
        else:
            pos.append(args[i])
            i += 1
    folder = pos[0] if pos else None
    if opts and folder:                       # headless / scheduled mode
        top = Node(folder, folder)
        Scanner().scan(top)
        if "--csv" in opts:
            write_csv(top, opts["--csv"])
        if "--html" in opts:
            write_html(top, opts["--html"])
        print(f"{folder}: {fmt(top.size)} in {top.nfiles:,} files")
        return
    if tk is None:
        sys.exit("tkinter is not available in this Python install.")
    root = tk.Tk()
    App(root, folder)
    root.mainloop()


if __name__ == "__main__":
    main()
