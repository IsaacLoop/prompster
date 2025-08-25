#!/usr/bin/env python3

"""
Prompster: a vibe-coded Flask app to browse repos, select files or folders,
and copy a Markdown preview for LLMs. Read-only, non-critical.
"""

import os
import re
import fnmatch
import mimetypes
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# -------------------------------------------------------
# Configuration / constants
# -------------------------------------------------------
ROOT = Path(os.getenv("PROMPSTER_ROOT", Path.cwd())).resolve()

IGNORE_DEFAULTS = [
    ".git/",
    ".hg/",
    ".svn/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".tox/",
    "node_modules/",
    "venv/",
    ".venv/",
    "env/",
    ".ipynb_checkpoints/",
    "build/",
    "dist/",
    "*.pyc",
    ".DS_Store",
]

PROMPSTER_IGNORE_FILE = ROOT / ".prompsterignore"
IGNORE_PATTERNS = IGNORE_DEFAULTS[:]
if PROMPSTER_IGNORE_FILE.exists():
    for line in PROMPSTER_IGNORE_FILE.read_text(
        encoding="utf-8", errors="ignore"
    ).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            IGNORE_PATTERNS.append(line)

MAX_FILE_BYTES = int(os.getenv("PROMPSTER_MAX_FILE_BYTES", "1048576"))  # 1 MiB
BINARY_SNIFF_BYTES = 4096

EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".json": "json",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "bash",
    ".zsh": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".rs": "rust",
    ".swift": "swift",
}


def detect_language(file_path: str) -> str:
    _, ext = os.path.splitext(file_path)
    return EXT_TO_LANG.get(ext.lower(), "")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except Exception:
        return False


def _ignored(rel_posix: str, is_dir: bool) -> bool:
    """Gitignore-like light matcher with directory-aware patterns."""
    for pat in IGNORE_PATTERNS:
        if pat.endswith("/"):
            if is_dir and fnmatch.fnmatch(rel_posix + "/", pat):
                return True
        else:
            if fnmatch.fnmatch(rel_posix, pat):
                return True
    return False


def _children_of(dir_path: Path, offset: int, limit: int):
    """Return immediate children (no recursion). Paginated."""
    assert dir_path.is_dir()
    items = []
    entries = []

    try:
        with os.scandir(dir_path) as it:
            for e in it:
                try:
                    isdir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue

                abs_child = Path(e.path).resolve()
                if not _is_relative_to(abs_child, ROOT):
                    # Out-of-root symlink or traversal — skip
                    continue

                rel = abs_child.relative_to(ROOT).as_posix()
                if _ignored(rel, isdir):
                    continue

                entries.append((e, isdir, rel, abs_child))
    except PermissionError:
        entries = []

    # Sort: directories first, then case-insensitive filename
    entries.sort(key=lambda t: (not t[1], t[0].name.lower()))
    total = len(entries)
    for e, isdir, rel, abs_child in entries[offset : offset + limit]:
        node = {
            "name": e.name,
            "path": rel,
            "fullPath": str(abs_child),
            "is_dir": isdir,
        }
        if isdir:
            # Quick probe to check if it has any (non-ignored) children
            has_children = False
            try:
                with os.scandir(e.path) as it2:
                    for c in it2:
                        try:
                            c_isdir = c.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        abs_c = Path(c.path).resolve()
                        if not _is_relative_to(abs_c, ROOT):
                            continue
                        rel_c = abs_c.relative_to(ROOT).as_posix()
                        if _ignored(rel_c, c_isdir):
                            continue
                        has_children = True
                        break
            except (PermissionError, FileNotFoundError):
                has_children = False
            node["hasChildren"] = has_children
        else:
            try:
                node["size"] = e.stat(follow_symlinks=False).st_size
            except OSError:
                node["size"] = 0
        items.append(node)

    return {
        "children": items,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": (offset + limit) < total,
    }


def _dynamic_fence(text: str, lang: str) -> str:
    """Use a backtick fence longer than any run inside content."""
    longest = 0
    for m in re.finditer(r"`+", text):
        longest = max(longest, len(m.group(0)))
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}\n"


def _read_text_sampled(p: Path) -> tuple[str, bool, str | None]:
    """
    Returns (text, truncated, note).
    - Detects binary quickly.
    - Caps maximum bytes.
    """
    try:
        size = p.stat().st_size
    except OSError as e:
        return (f"Error: stat failed: {e}", False, "error")

    # sniff for binary
    try:
        with open(p, "rb") as f:
            head = f.read(BINARY_SNIFF_BYTES)
    except OSError as e:
        return (f"Error: open failed: {e}", False, "error")

    if b"\x00" in head:
        return ("<Binary file omitted>", False, "binary")

    # Bounded read
    to_read = min(size, MAX_FILE_BYTES)
    try:
        with open(p, "rb") as f:
            data = f.read(to_read)
    except OSError as e:
        return (f"Error: read failed: {e}", False, "error")

    truncated = size > MAX_FILE_BYTES
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("utf-8", errors="replace")

    if truncated:
        note = f"<Truncated: {size} bytes > {MAX_FILE_BYTES} byte preview>"
        text = f"{note}\n{text}"
    return (text, truncated, None)


# 3) FRONT-END HTML
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Prompster</title>
  <style>
    body {
      margin: 0;
      padding: 0;
      background-color: #1e1e1e;
      color: #ffffff;
      font-family: Arial, sans-serif;
    }
    .container {
      max-width: 900px;
      margin: auto;
      padding: 1rem;
    }
    h1 {
      margin: 0 0 1rem;
    }
    .file-tree {
      background: #2d2d2d;
      padding: 0.5rem;
      border-radius: 4px;
    }
    ul {
      list-style-type: none;
      margin: 0.5em 0;
      padding-left: 1.5em;
    }
    li {
      margin: 0.4em 0;
      position: relative;
    }
    .folder-label, .file-label {
      cursor: pointer;
    }
    .folder-arrow {
      display: inline-block;
      width: 1.2em;
      color: #ccc;
      text-align: center;
      cursor: pointer;
    }
    .folder-count, .file-size {
      color: #999; 
      font-size: 0.85em;
      margin-left: 0.3em;
    }
    #btnBar1, #btnBar2 {
      display: flex;
      gap: 1rem;
      align-items: center;
      margin-top: 1rem;
    }
    button {
      padding: 0.6rem 1.2rem;
      background: #007acc;
      color: #ffffff;
      border: none;
      font-size: 1rem;
      cursor: pointer;
      border-radius: 4px;
    }
    button:hover {
      background: #005fa3;
    }
    #statsLine {
      margin: 1rem 0 0 0;
      font-size: 0.9rem;
      color: #ccc;
    }
    #result {
      margin-top: 1rem;
      white-space: pre-wrap;
      background: #2d2d2d;
      padding: 1rem;
      border-radius: 4px;
    }
    #result:empty {
      display: none;
    }

    /* MULTI-LEVEL "STACKED" STICKY HEADERS */
    .folder {
      position: relative;
      border-left: 1px dashed #444;
      margin-left: 0.5em;
      padding-left: 0.5em;
    }
    .folder-header {
      position: sticky;
      top: 0;
      background-color: #2d2d2d;
      display: flex;
      align-items: center;
      padding: 0.2rem 0;
    }
    .folder-content {
      padding-left: 1.5em;
      margin-bottom: 1rem;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>Prompster</h1>
    <p>
      A tiny Flask server for multi-level folder browsing with stacked sticky headers,
      tri-state checkboxes, on-demand expansion, and z-index by depth 
      so no child appears above its ancestors.
    </p>

    <div class="file-tree" id="tree"></div>

    <div id="btnBar1">
      <button id="selectAllBtn">Select All ⭐</button>
      <button id="unselectAllBtn">Unselect All ♻</button>
      <button id="expandAllBtn">Expand All ⬇</button>
      <button id="collapseAllBtn">Collapse All ⬆</button>
    </div>
    <div id="btnBar2">
      <button id="copyBtn">Copy 📋</button>
      <button id="refreshBtn">Refresh 🔄</button>
    </div>

    <div id="statsLine"></div>
    <h3>Previsualisation</h3>
    <div id="result"></div>
  </div>

  <script>
  const PAGE_SIZE = 500;
  let ROOT_FULL_PATH = "";

  // Default names to exclude from bulk selection. Users can override per item.
  const BLACKLIST_NAMES = [
    '.env', '.env.local', '.env.development', '.env.production', '.env.test',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ipynb_checkpoints',
    'node_modules', '.venv', 'venv', '.tox', '.cache', 'build', 'dist'
  ];
  function isBlacklistedPath(fp) {
    if (!fp) return false;
    const segs = String(fp).split(/[\\/]+/);
    return segs.some(s => BLACKLIST_NAMES.includes(s));
  }

  let checkMap = {};      // fullPath -> boolean | 'dir'
  let expandMap = {};     // fullPath -> boolean (expanded)
  let allowMap = {};      // fullPath -> boolean (user allowed blacklist override)
  let treeCache = new Map(); // fullPath -> { children: [...], total, offset }

  window.onload = async function() {
    loadLocalMaps();
    await renderRoot();
    await updatePreview();
  };

  function loadLocalMaps() {
    try {
      checkMap = JSON.parse(localStorage.getItem("prompster-checked") || "{}");
      expandMap = JSON.parse(localStorage.getItem("prompster-expanded") || "{}");
      allowMap = JSON.parse(localStorage.getItem("prompster-allow-blacklist") || "{}");
    } catch { checkMap = {}; expandMap = {}; allowMap = {}; }
  }
  function saveCheckMap(){ localStorage.setItem("prompster-checked", JSON.stringify(checkMap)); }
  function saveExpandMap(){ localStorage.setItem("prompster-expanded", JSON.stringify(expandMap)); }
  function saveAllowMap(){ localStorage.setItem("prompster-allow-blacklist", JSON.stringify(allowMap)); }

  function shouldAllowSelection(fp) {
    if (!isBlacklistedPath(fp)) return true;
    if (allowMap[fp]) return true;
    const ok = window.confirm('This item is blacklisted by default (e.g., .env, __pycache__). Include it anyway?');
    if (ok) { allowMap[fp] = true; saveAllowMap(); return true; }
    return false;
  }

  // ---------- API ----------
  async function fetchChildren(fullPath = "", offset = 0, limit = PAGE_SIZE) {
    const q = new URLSearchParams({ path: fullPath, offset: String(offset), limit: String(limit) });
    const res = await fetch(`/api/tree?${q.toString()}`);
    return await res.json(); // shape: { parent, children, offset, limit, total, has_more }
  }

  // ---------- Rendering ----------
  async function renderRoot() {
    const treeDiv = document.getElementById('tree');
    treeDiv.innerHTML = "";
    const data = await fetchChildren("");
    const ul = document.createElement('ul');
    await renderChildrenInto(ul, data.children, 0);
    treeDiv.appendChild(ul);
    // cache root
    ROOT_FULL_PATH = data.parent.fullPath;
    treeCache.set(data.parent.fullPath, { children: data.children, total: data.total, offset: data.children.length });
    if (data.has_more) {
      ul.appendChild(makeLoadMoreRow(data.parent.fullPath, 0));
    }
    fixStickyHeaders();
    // Visual sync on initial render
    syncDomFromCheckMap();
  }

  async function renderChildrenInto(containerUL, nodeList, depth) {
    for (const node of nodeList) {
      const li = document.createElement('li');
      if (node.is_dir) {
        li.appendChild(await renderFolder(node, depth));
      } else {
        li.appendChild(renderFile(node));
      }
      containerUL.appendChild(li);
      // Visual sync: if a folder/file is already selected via checkMap, reflect it
      if (node.is_dir) {
        const checkbox = li.querySelector(':scope > .folder > .folder-header input[type="checkbox"]');
        if (checkbox && checkMap[node.fullPath] === 'dir') { checkbox.checked = true; checkbox.indeterminate = false; }
      } else {
        const checkbox = li.querySelector(':scope > input[type="checkbox"]');
        if (checkbox && checkMap[node.fullPath] === true) { checkbox.checked = true; }
      }
    }
    // ensure parents reflect children
    document.querySelectorAll('li').forEach(li => updateParentFolders(li));
  }

  function renderFile(node) {
    const frag = document.createDocumentFragment();

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.fullPath = node.fullPath;
    cb.dataset.isDir = 'false';
    cb.checked = !!checkMap[node.fullPath];
    cb.onchange = async () => {
      if (cb.checked && !shouldAllowSelection(node.fullPath)) { cb.checked = false; return; }
      checkMap[node.fullPath] = cb.checked ? true : false;
      saveCheckMap();
      updateParentFolders(cb.closest('li'));
      await updatePreview();
    };
    frag.appendChild(cb);

    const label = document.createElement('span');
    label.classList.add('file-label');
    label.textContent = " " + node.name;
    frag.appendChild(label);

    const sizeSpan = document.createElement('span');
    sizeSpan.classList.add('file-size');
    if (typeof node.size === "number") sizeSpan.textContent = formatSize(node.size);
    frag.appendChild(sizeSpan);

    return frag;
  }

  async function renderFolder(node, depth) {
    const folderDiv = document.createElement('div');
    folderDiv.classList.add('folder');

    const header = document.createElement('div');
    header.classList.add('folder-header');
    header.dataset.depth = String(depth);

    const arrow = document.createElement('span');
    arrow.classList.add('folder-arrow');
    const expanded = expandMap[node.fullPath] === true;
    arrow.textContent = expanded ? "▼" : "►";

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.fullPath = node.fullPath;
    cb.dataset.isDir = 'true';
    cb.checked = !!checkMap[node.fullPath];
    cb.onchange = async () => {
      if (cb.checked && !shouldAllowSelection(node.fullPath)) { cb.checked = false; return; }
      toggleChildren(folderDiv, cb.checked);
      updateParentFolders(folderDiv.closest('li'));
      await setFolderSelectionRecursive(node.fullPath, cb.checked);
      await updatePreview();
    };

    const label = document.createElement('span');
    label.classList.add('folder-label');
    label.textContent = " " + node.name;

    const meta = document.createElement('span');
    meta.classList.add('folder-count');
    meta.textContent = node.hasChildren ? "" : "(Empty folder)";

    header.appendChild(arrow);
    header.appendChild(cb);
    header.appendChild(label);
    header.appendChild(meta);

    const content = document.createElement('div');
    content.classList.add('folder-content');
    content.style.display = expanded ? "block" : "none";

    arrow.onclick = async (e) => {
      e.stopPropagation();
      const now = content.style.display !== "none";
      expandMap[node.fullPath] = !now;
      saveExpandMap();
      arrow.textContent = !now ? "▼" : "►";
      content.style.display = !now ? "block" : "none";

      if (!now) { // expanding
        if (!treeCache.has(node.fullPath)) {
          const data = await fetchChildren(node.fullPath);
          const ul = document.createElement('ul');
          await renderChildrenInto(ul, data.children, depth + 1);
          content.appendChild(ul);
          treeCache.set(node.fullPath, { children: data.children, total: data.total, offset: data.children.length });
          if (data.has_more) {
            ul.appendChild(makeLoadMoreRow(node.fullPath, depth + 1));
          }
          fixStickyHeaders();
        }
      } else {
        // collapsing - ensure it's visible if offscreen after collapse
        const rect = header.getBoundingClientRect();
        if (rect.top < 0) header.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    };

    folderDiv.appendChild(header);
    folderDiv.appendChild(content);

    // If initially expanded, fetch children immediately
    if (expanded) {
      const data = await fetchChildren(node.fullPath);
      const ul = document.createElement('ul');
      await renderChildrenInto(ul, data.children, depth + 1);
      content.appendChild(ul);
      treeCache.set(node.fullPath, { children: data.children, total: data.total, offset: data.children.length });
      if (data.has_more) {
        ul.appendChild(makeLoadMoreRow(node.fullPath, depth + 1));
      }
    }

    return folderDiv;
  }

  function makeLoadMoreRow(folderFullPath, depth) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.textContent = "Load more…";
    btn.style.marginLeft = "1.6rem";
    btn.onclick = async () => {
      const cache = treeCache.get(folderFullPath);
      const nextOffset = cache?.offset || 0;
      const data = await fetchChildren(folderFullPath, nextOffset, PAGE_SIZE);
      const parentUL = li.parentElement;
      const insertionPoint = li; // append before the button row
      const fragUL = document.createElement('ul');
      await renderChildrenInto(fragUL, data.children, depth);
      // Move child lis into the real UL
      while (fragUL.firstChild) parentUL.insertBefore(fragUL.firstChild, insertionPoint);
      const newOffset = nextOffset + data.children.length;
      treeCache.set(folderFullPath, { children: (cache.children || []).concat(data.children), total: data.total, offset: newOffset });
      if (!data.has_more) li.remove();
      fixStickyHeaders();
    };
    li.appendChild(btn);
    return li;
  }

  // ---------- Utilities (mostly yours, slightly adapted) ----------
  function formatSize(bytes) {
    const units = ['B','KB','MB','GB','TB'];
    let size = bytes, i = 0;
    while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
    return `(${i === 0 ? Math.round(size) : size.toFixed(1)} ${units[i]})`;
  }

  function fixStickyHeaders() {
    const all = Array.from(document.querySelectorAll(".folder-header"));
    all.sort((a, b) => (parseInt(a.dataset.depth||"0",10) - parseInt(b.dataset.depth||"0",10)));
    for (const h of all) {
      const d = parseInt(h.dataset.depth || "0", 10);
      h.style.zIndex = String(1000 - d);
      const parentHeader = h.closest(".folder")?.parentElement?.closest(".folder")?.querySelector(":scope > .folder-header");
      if (!parentHeader) h.style.top = "0px";
      else {
        const parentTop = parseFloat(parentHeader.style.top) || 0;
        const parentRect = parentHeader.getBoundingClientRect();
        h.style.top = (parentTop + (parentRect.height || 0)) + "px";
      }
    }
  }

  // ---------- Async helpers & deep operations ----------
  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  async function waitFor(conditionFn, timeoutMs = 20000, intervalMs = 50) {
    const start = Date.now();
    while (true) {
      try { if (conditionFn()) return; } catch {}
      if (Date.now() - start > timeoutMs) throw new Error('Timeout waiting for condition');
      await sleep(intervalMs);
    }
  }

  async function fetchAllChildrenBfs(folderFullPath, onChild, shouldDescendDir) {
    const queue = [folderFullPath];
    while (queue.length) {
      const current = queue.shift();
      let offset = 0;
      let total = 0;
      do {
        const data = await fetchChildren(current, offset, PAGE_SIZE);
        total = data.total;
        offset += data.children.length;
        for (const child of data.children) {
          await onChild(child);
          if (child.is_dir) {
            const allowDescend = typeof shouldDescendDir === 'function' ? shouldDescendDir(child.fullPath) : true;
            if (allowDescend) queue.push(child.fullPath);
          }
        }
      } while (offset < total);
    }
  }

  async function collectAllFilesUnder(folderFullPath) {
    const results = [];
    await fetchAllChildrenBfs(folderFullPath, async (node) => {
      if (!node.is_dir && node.fullPath) results.push(node.fullPath);
    }, (dirPath) => (!isBlacklistedPath(dirPath) || allowMap[dirPath]));
    return results;
  }

  async function collectAllUnder(folderFullPath) {
    const files = [], dirs = [];
    await fetchAllChildrenBfs(folderFullPath, async (node) => {
      if (node.is_dir && node.fullPath) dirs.push(node.fullPath);
      else if (!node.is_dir && node.fullPath) files.push(node.fullPath);
    }, (dirPath) => (!isBlacklistedPath(dirPath) || allowMap[dirPath]));
    return { files, dirs };
  }

  async function setFolderSelectionRecursive(folderFullPath, checked) {
    checkMap[folderFullPath] = checked ? 'dir' : false;
    const { files, dirs } = await collectAllUnder(folderFullPath);
    for (const d of dirs) {
      if (checked) {
        if (isBlacklistedPath(d) && !allowMap[d]) continue;
        checkMap[d] = 'dir';
      } else {
        checkMap[d] = false;
      }
    }
    for (const f of files) {
      if (checked) {
        if (isBlacklistedPath(f) && !allowMap[f]) continue;
        checkMap[f] = true;
      } else {
        checkMap[f] = false;
      }
    }
    saveCheckMap();
  }

  async function expandFolderFully(header) {
    const content = header.parentElement.querySelector('.folder-content');
    const arrow = header.querySelector('.folder-arrow');
    if (content && content.style.display === 'none') {
      arrow.click();
      await waitFor(() => content.style.display !== 'none');
    }
    // Load all pages if there is a Load more… button
    while (true) {
      const btn = content?.querySelector(':scope > ul > li > button');
      if (!btn || btn.textContent.trim() !== 'Load more…') break;
      const beforeCount = content.querySelectorAll(':scope > ul > li').length;
      btn.click();
      await waitFor(() => {
        const afterCount = content.querySelectorAll(':scope > ul > li').length;
        return afterCount > beforeCount || !content.contains(btn);
      });
    }
    // Recursively expand direct child folders
    const childHeaders = content?.querySelectorAll(':scope > ul > li > .folder > .folder-header') || [];
    for (const ch of childHeaders) {
      await expandFolderFully(ch);
    }
  }

  async function expandAllRecursively() {
    // Ensure root is fully loaded first
    const rootUL = document.querySelector('#tree > ul');
    if (rootUL) {
      while (true) {
        const btn = rootUL.querySelector(':scope > li > button');
        if (!btn || btn.textContent.trim() !== 'Load more…') break;
        const beforeCount = rootUL.querySelectorAll(':scope > li').length;
        btn.click();
        await waitFor(() => {
          const afterCount = rootUL.querySelectorAll(':scope > li').length;
          return afterCount > beforeCount || !rootUL.contains(btn);
        });
      }
    }
    // Expand all top-level folders recursively
    const headers = Array.from(document.querySelectorAll('#tree > ul > li > .folder > .folder-header'));
    for (const h of headers) {
      // Skip blacklisted top-level dirs unless explicitly allowed
      const fp = h.parentElement?.querySelector(':scope > .folder-header > input[type="checkbox"]')?.dataset.fullPath;
      if (fp && isBlacklistedPath(fp) && !allowMap[fp]) continue;
      await expandFolderFully(h);
    }
  }

  // Tri-state helpers (kept from your implementation)
  function updateParentFolders(childLi) {
    const parentLi = childLi?.parentElement?.closest('li');
    if (!parentLi) return;
    const parentCheckbox = parentLi.querySelector(':scope > .folder > .folder-header input[type="checkbox"]');
    if (!parentCheckbox) return;

    const direct = childLi.parentElement.querySelectorAll(':scope > li > input[type="checkbox"], :scope > li .folder-header > input[type="checkbox"]');
    let checkedCount = 0;
    direct.forEach(cb => { if (cb.checked) checkedCount++; });

    if (checkedCount === 0) {
      parentCheckbox.checked = false; parentCheckbox.indeterminate = false;
    } else if (checkedCount === direct.length) {
      parentCheckbox.checked = true; parentCheckbox.indeterminate = false;
    } else {
      parentCheckbox.checked = false; parentCheckbox.indeterminate = true;
    }
    updateParentFolders(parentLi);
  }

  function toggleChildren(folderDiv, checked) {
    const inputs = folderDiv.querySelectorAll('input[type="checkbox"]');
    inputs.forEach(inp => { inp.checked = checked; inp.indeterminate = false; });
  }

  function rebuildCheckMapFromDOM() {
    checkMap = {};
    document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      const fp = cb.dataset.fullPath;
      if (!fp) return;
      if (cb.dataset.isDir === 'true') {
        // Do not record blacklisted dirs unless explicitly allowed or checked
        if (isBlacklistedPath(fp) && !allowMap[fp] && !cb.checked) return;
        checkMap[fp] = cb.checked ? 'dir' : false;
      } else {
        if (isBlacklistedPath(fp) && !allowMap[fp] && !cb.checked) return;
        checkMap[fp] = cb.checked ? true : false;
      }
    });
  }

  function syncDomFromCheckMap() {
    // Set checkbox states from checkMap for currently rendered nodes
    document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      const fp = cb.dataset.fullPath;
      if (!fp) return;
      const isDir = cb.dataset.isDir === 'true';
      const val = checkMap[fp];
      if (isDir) {
        if (val === 'dir') { cb.checked = true; cb.indeterminate = false; }
        else if (val === false) { cb.checked = false; cb.indeterminate = false; }
      } else {
        if (val === true) cb.checked = true;
        else if (val === false) cb.checked = false;
      }
    });
    // Recompute tri-state up the tree
    document.querySelectorAll('li').forEach(li => updateParentFolders(li));
  }

  // ---------- Preview / controls ----------
  async function updatePreview() {
    const filesSet = new Set();
    const excluded = new Set(Object.keys(checkMap).filter(fp => checkMap[fp] === false));
    // Add explicitly checked files
    for (const [fp, val] of Object.entries(checkMap)) {
      if (val === true && !excluded.has(fp)) {
        if (!isBlacklistedPath(fp) || allowMap[fp]) filesSet.add(fp);
      }
    }
    // Add files under checked directories, minus explicit exclusions
    const selectedDirs = Object.keys(checkMap).filter(fp => checkMap[fp] === 'dir');
    for (const dir of selectedDirs) {
      const files = await collectAllFilesUnder(dir);
      for (const f of files) {
        if (excluded.has(f)) continue;
        if (isBlacklistedPath(f) && !allowMap[f]) continue;
        filesSet.add(f);
      }
    }
    // Ensure explicit unchecks are removed (defensive)
    for (const f of excluded) filesSet.delete(f);
    const checkedFiles = Array.from(filesSet);

    if (checkedFiles.length === 0) {
      document.getElementById('result').textContent = "";
      document.getElementById('statsLine').textContent = "0 files | 0 lines | 0 words | 0 characters selected";
      return;
    }
    const res = await fetch('/api/copy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: checkedFiles }) });
    const untrimmed = await res.text();
    const resultText = untrimmed.trimEnd();
    document.getElementById('result').textContent = resultText;
    const stats = calculateStats(resultText, checkedFiles.length);
    document.getElementById('statsLine').textContent = stats;
  }

  function formatNum(n) { try { return Number(n).toLocaleString(); } catch { return String(n); } }
  function calculateStats(text, fileCount) {
    const lines = text ? text.split("\n").length : 0;
    const words = text.trim() ? text.trim().split(/\s+/).filter(Boolean).length : 0;
    const chars = text.length;
    return `${formatNum(fileCount)} file${fileCount>1?"s":""} | ${formatNum(lines)} line${lines>1?"s":""} | ${formatNum(words)} word${words>1?"s":""} | ${formatNum(chars)} character${chars>1?"s":""} selected`;
  }

  document.addEventListener('click', async (e) => {
    const id = e.target.id;
    if (!id) return;
    switch(id) {
      case 'copyBtn': {
        const content = document.getElementById('result').textContent;
        if (!content) return;
        try { await navigator.clipboard.writeText(content); } catch (err) { console.error("Clipboard error:", err); }
        break;
      }
      case 'refreshBtn': {
        treeCache.clear();
        await renderRoot();
        await updatePreview();
        break;
      }
      case 'collapseAllBtn': {
        expandMap = {}; saveExpandMap();
        document.querySelectorAll('.folder-content').forEach(c => c.style.display = "none");
        document.querySelectorAll('.folder .folder-header .folder-arrow').forEach(a => a.textContent = "►");
        break;
      }
      case 'expandAllBtn': {
        const btn = e.target;
        btn.disabled = true;
        try {
          await expandAllRecursively();
          fixStickyHeaders();
        } finally {
          btn.disabled = false;
        }
        break;
      }
      case 'selectAllBtn': {
        // Select from entire root recursively via API, skipping blacklist
        const filesSet = new Set();
        await fetchAllChildrenBfs(ROOT_FULL_PATH, async (node) => {
          const fp = node.fullPath;
          if (!fp) return;
          if (node.is_dir) {
            if (!isBlacklistedPath(fp) || allowMap[fp]) checkMap[fp] = 'dir';
          } else {
            if (!isBlacklistedPath(fp) || allowMap[fp]) filesSet.add(fp);
          }
        }, (dirPath) => (!isBlacklistedPath(dirPath) || allowMap[dirPath]));
        for (const f of filesSet) checkMap[f] = true;
        saveCheckMap();
        syncDomFromCheckMap();
        await updatePreview();
        break;
      }
      case 'unselectAllBtn': {
        document.querySelectorAll('input[type="checkbox"]').forEach(cb => cb.checked = false);
        rebuildCheckMapFromDOM(); saveCheckMap(); await updatePreview();
        break;
      }
    }
  });
  </script>
</body>
</html>
"""


# -------------------------------------------------------
# Flask Routes
# -------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/tree")
def api_tree():
    """
    Lazy endpoint: returns immediate children of the requested folder.
    Query params:
      - path: relative or absolute folder path (defaults to ROOT)
      - limit: max items (default 500)
      - offset: pagination offset (default 0)
    """
    rel = request.args.get("path", "")
    limit = int(request.args.get("limit", 500))
    offset = int(request.args.get("offset", 0))

    base = (ROOT / rel).resolve() if rel else ROOT
    if (not base.exists()) or (not base.is_dir()) or (not _is_relative_to(base, ROOT)):
        return jsonify({"error": "Invalid path"}), 400

    payload = _children_of(base, offset, limit)
    # Return a shape that's easy for the client: just list of children + paging
    return jsonify(
        {
            "parent": {
                "name": base.name or str(ROOT),
                "path": (
                    base.relative_to(ROOT).as_posix()
                    if _is_relative_to(base, ROOT)
                    else ""
                ),
                "fullPath": str(base),
            },
            **payload,
        }
    )


@app.route("/api/copy", methods=["POST"])
def api_copy():
    """
    Receives JSON: { "files": ["absOrRelPath1", "absOrRelPath2", ...] }
    Returns a single Markdown snippet for the selected files. Safer & bounded.
    """
    data = request.get_json(force=True, silent=True) or {}
    selected = data.get("files", [])
    if not isinstance(selected, list):
        return jsonify({"error": "files must be a list"}), 400

    md_pieces = []
    for raw in selected:
        p = Path(raw).resolve()
        # Ensure the file is inside ROOT
        if not _is_relative_to(p, ROOT) or (not p.exists()) or (not p.is_file()):
            continue

        rel = p.relative_to(ROOT).as_posix()
        lang = detect_language(rel)

        text, truncated, note = _read_text_sampled(p)
        if note == "binary":
            snippet = f"**{rel}**\n```\n<Binary file omitted>\n```\n\n"
        elif note == "error":
            snippet = f"**{rel}**\n```\n{text}\n```\n\n"
        else:
            fenced = _dynamic_fence(text, lang)
            snippet = f"**{rel}**\n{fenced}\n"
        md_pieces.append(snippet)

    return "".join(md_pieces)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)

# Less than 1,000 lines!