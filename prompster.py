#!/usr/bin/env python3

"""
prompster.py
------------------
A tiny Flask server for multi-level folder browsing to
easily copy code from a code base into a LLM prompt,
for instance on chatgpt.com.

Usage:
  1) pip install flask
  2) python prompster.py
  3) Open http://127.0.0.1:5000
"""

import os
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# 1) EXTENSION -> LANGUAGE MAPPING
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
    # Add more if needed...
}

def detect_language(file_path: str) -> str:
    _, ext = os.path.splitext(file_path)
    return EXT_TO_LANG.get(ext.lower(), "")

# 2) BUILD THE FILE TREE (recursive)
def build_file_tree(path: Path):
    """
    Return a dict: {
      name, path, fullPath, is_dir,
      children[], directCount, totalCount
    }
    """
    if not path.is_dir():
        return {
            "name": path.name,
            "path": path.relative_to(Path.cwd()).as_posix(),
            "fullPath": str(path.resolve()),
            "is_dir": False,
            "children": [],
            "directCount": 0,
            "totalCount": 1,
        }

    items = []
    try:
        for entry in path.iterdir():
            node = build_file_tree(entry)
            items.append(node)
    except PermissionError:
        pass

    # Sort dirs first, then files
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    direct_count = len(items)
    total_count = 1
    for child in items:
        total_count += child["totalCount"]

    return {
        "name": path.name,
        "path": path.relative_to(Path.cwd()).as_posix(),
        "fullPath": str(path.resolve()),
        "is_dir": True,
        "children": items,
        "directCount": direct_count,
        "totalCount": total_count,
    }

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
    .folder-count {
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
    let checkMap = {};   
    let expandMap = {};  
    let fileTreeData = [];
    let nodeMap = {};    // fullPath -> node object

    // On load
    window.onload = async function() {
      loadLocalMaps();
      await loadTree();
      await updatePreview();
    };

    function loadLocalMaps() {
      const c = localStorage.getItem("prompster-checked");
      const e = localStorage.getItem("prompster-expanded");
      checkMap = c ? JSON.parse(c) : {};
      expandMap = e ? JSON.parse(e) : {};
    }
    function saveCheckMap() {
      localStorage.setItem("prompster-checked", JSON.stringify(checkMap));
    }
    function saveExpandMap() {
      localStorage.setItem("prompster-expanded", JSON.stringify(expandMap));
    }

    async function loadTree() {
      const res = await fetch('/api/tree');
      fileTreeData = await res.json();

      // Build nodeMap for on-demand child rendering
      nodeMap = {};
      buildNodeMap(fileTreeData);

      const treeDiv = document.getElementById('tree');
      treeDiv.innerHTML = "";
      renderTree(treeDiv, fileTreeData, 0);
      fixStickyHeaders();
    }

    function buildNodeMap(list) {
      for (const node of list) {
        nodeMap[node.fullPath] = node;
        if (node.is_dir && node.children && node.children.length > 0) {
          buildNodeMap(node.children);
        }
      }
    }

    function renderTree(container, nodeList, depth) {
      const ul = document.createElement('ul');

      for (const node of nodeList) {
        const li = document.createElement('li');

        if (node.is_dir) {
          // FOLDER
          const folderDiv = document.createElement('div');
          folderDiv.classList.add('folder');

          // Header
          const folderHeader = document.createElement('div');
          folderHeader.classList.add('folder-header');
          folderHeader.dataset.depth = depth.toString(); // so fixStickyHeaders can set z-index

          // Arrow
          const arrowSpan = document.createElement('span');
          arrowSpan.classList.add('folder-arrow');
          const isExpanded = expandMap[node.fullPath] !== false;
          arrowSpan.textContent = isExpanded ? "▼" : "►";
          arrowSpan.onclick = (e) => {
            e.stopPropagation();
            const currently = expandMap[node.fullPath] !== false;
            expandMap[node.fullPath] = !currently;
            saveExpandMap();
            arrowSpan.textContent = expandMap[node.fullPath] ? "▼" : "►";

            if (!expandMap[node.fullPath]) {
              // Just collapsed
              folderContent.style.display = "none";
              // If offscreen
              const rect = folderHeader.getBoundingClientRect();
              if (rect.top < 0) {
                folderHeader.scrollIntoView({ behavior: "smooth", block: "start" });
              }
            } else {
              // Just expanded
              folderContent.style.display = "block";
              if (!folderContent.hasChildNodes()) {
                // on-demand child rendering
                const realNode = nodeMap[node.fullPath];
                if (realNode && realNode.children && realNode.children.length > 0) {
                  renderTree(folderContent, realNode.children, depth+1);
                  fixStickyHeaders();
                }
              }
            }
          };

          // Checkbox
          const folderCb = document.createElement('input');
          folderCb.type = 'checkbox';
          folderCb.dataset.fullPath = node.fullPath;
          folderCb.dataset.isDir = "true";
          folderCb.checked = !!checkMap[node.fullPath];
          folderCb.onchange = async function() {
            toggleChildren(folderDiv, folderCb.checked);
            updateParentFolders(li);
            rebuildCheckMapFromDOM();
            saveCheckMap();
            await updatePreview();
          };

          // Label
          const label = document.createElement('span');
          label.classList.add('folder-label');
          label.textContent = " " + node.name;

          // Child count
          const countSpan = document.createElement('span');
          countSpan.classList.add('folder-count');
          if (node.directCount === 0) {
            countSpan.textContent = "(Empty folder)";
          } else {
            const dc = node.directCount;
            const tc = node.totalCount - 1;
            const dcWord = dc === 1 ? "child" : "children";
            const tcWord = tc === 1 ? "child" : "children";
            countSpan.textContent = `(${dc} direct ${dcWord} | ${tc} total ${tcWord})`;
          }

          folderHeader.appendChild(arrowSpan);
          folderHeader.appendChild(folderCb);
          folderHeader.appendChild(label);
          folderHeader.appendChild(countSpan);

          const folderContent = document.createElement('div');
          folderContent.classList.add('folder-content');
          folderContent.style.display = isExpanded ? "block" : "none";

          if (isExpanded && node.children && node.children.length > 0) {
            // render children right away
            renderTree(folderContent, node.children, depth+1);
          }

          folderDiv.appendChild(folderHeader);
          folderDiv.appendChild(folderContent);
          li.appendChild(folderDiv);

        } else {
          // FILE
          const fileCb = document.createElement('input');
          fileCb.type = 'checkbox';
          fileCb.dataset.fullPath = node.fullPath;
          fileCb.dataset.isDir = 'false';
          fileCb.checked = !!checkMap[node.fullPath];
          fileCb.onchange = async function() {
            updateParentFolders(li);
            rebuildCheckMapFromDOM();
            saveCheckMap();
            await updatePreview();
          };

          const fileLabel = document.createElement('span');
          fileLabel.classList.add('file-label');
          fileLabel.textContent = " " + node.name;

          li.appendChild(fileCb);
          li.appendChild(fileLabel);
        }
        ul.appendChild(li);
      }
      container.appendChild(ul);
    }

    /**
     * fixStickyHeaders:
     *  - Single pass. We find all .folder-header,
     *    sort them by depth ascending,
     *    then for each, place it below parent. 
     *  - Also set zIndex = 1000 - depth
     */
    function fixStickyHeaders() {
      const allHeaders = Array.from(document.querySelectorAll(".folder-header"));

      // sort by ascending depth
      allHeaders.sort((a,b) => {
        const da = parseInt(a.dataset.depth||"0",10);
        const db = parseInt(b.dataset.depth||"0",10);
        return da - db;
      });

      for (const header of allHeaders) {
        const depth = parseInt(header.dataset.depth || "0", 10);
        // z-index => 1000 - depth
        header.style.zIndex = (1000 - depth).toString();

        const parentHeader = header
          .closest(".folder")
          ?.parentElement
          ?.closest(".folder")
          ?.querySelector(":scope > .folder-header");

        if (!parentHeader) {
          header.style.top = "0px";
        } else {
          const parentTop = parseFloat(parentHeader.style.top) || 0;
          const parentRect = parentHeader.getBoundingClientRect();
          const parentHeight = parentRect.height || 0;
          header.style.top = (parentTop + parentHeight) + "px";
        }
      }
    }

    // Tri-state logic
    function updateParentFolders(childLi) {
      const parentLi = childLi.parentElement.closest('li');
      if (!parentLi) return;
      const parentCheckbox = parentLi.querySelector('input[type="checkbox"]');
      if (!parentCheckbox) return;

      const directChildren = childLi.parentElement.querySelectorAll(
        ':scope > li > input[type="checkbox"], :scope > li .folder-header > input[type="checkbox"]'
      );
      let checkedCount = 0;
      directChildren.forEach(cb => {
        if (cb.checked) checkedCount++;
      });

      if (checkedCount === 0) {
        parentCheckbox.checked = false;
        parentCheckbox.indeterminate = false;
      } else if (checkedCount === directChildren.length) {
        parentCheckbox.checked = true;
        parentCheckbox.indeterminate = false;
      } else {
        parentCheckbox.checked = false;
        parentCheckbox.indeterminate = true;
      }
      updateParentFolders(parentLi);
    }

    function toggleChildren(folderDiv, checked) {
      const inputs = folderDiv.querySelectorAll('input[type="checkbox"]');
      inputs.forEach(inp => {
        inp.checked = checked;
        inp.indeterminate = false;
      });
    }

    function rebuildCheckMapFromDOM() {
      checkMap = {};
      const allCbs = document.querySelectorAll('input[type="checkbox"]');
      allCbs.forEach(cb => {
        checkMap[cb.dataset.fullPath] = cb.checked;
      });
    }

    // Expand/Collapse All
    async function collapseAll() {
      flattenFolders(fileTreeData).forEach(path => {
        expandMap[path] = false;
      });
      saveExpandMap();
      await loadTree();
      await updatePreview();
    }
    async function expandAll() {
      flattenFolders(fileTreeData).forEach(path => {
        expandMap[path] = true;
      });
      saveExpandMap();
      await loadTree();
      await updatePreview();
    }
    function flattenFolders(nodes) {
      let result = [];
      for (const node of nodes) {
        if (node.is_dir) {
          result.push(node.fullPath);
          result = result.concat(flattenFolders(node.children));
        }
      }
      return result;
    }

    // Select All / Unselect All
    async function selectAll() {
      flattenAllNodes(fileTreeData).forEach(path => {
        checkMap[path] = true;
      });
      saveCheckMap();
      flattenFolders(fileTreeData).forEach(path => {
        expandMap[path] = true;
      });
      saveExpandMap();
      await loadTree();
      await updatePreview();
    }
    async function unselectAll() {
      flattenAllNodes(fileTreeData).forEach(path => {
        checkMap[path] = false;
      });
      saveCheckMap();
      await loadTree();
      await updatePreview();
    }
    function flattenAllNodes(nodes) {
      let result = [];
      for (const node of nodes) {
        result.push(node.fullPath);
        if (node.children) {
          result = result.concat(flattenAllNodes(node.children));
        }
      }
      return result;
    }

    // Dynamic Preview
    async function updatePreview() {
      const checkedFiles = [];
      const allCbs = document.querySelectorAll('input[type="checkbox"]:checked');
      allCbs.forEach(cb => {
        if (cb.dataset.isDir === 'false') {
          checkedFiles.push(cb.dataset.fullPath);
        }
      });
      if (checkedFiles.length === 0) {
        document.getElementById('result').textContent = "";
        document.getElementById('statsLine').textContent 
          = "0 files | 0 lines | 0 words | 0 characters selected";
        return;
      }

      const res = await fetch('/api/copy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ files: checkedFiles })
      });
      const untrimmed = await res.text();
      const resultText = untrimmed.trimEnd();

      document.getElementById('result').textContent = resultText;

      const stats = calculateStats(resultText, checkedFiles.length);
      document.getElementById('statsLine').textContent = stats;
    }

    function formatNumber(num) {
      return new Intl.NumberFormat("en-US").format(num);
    }
    function calculateStats(text, fileCount) {
      const lines = text.split("\n").length;
      const words = text.trim().split(/\s+/).filter(Boolean).length;
      const chars = text.length;

      const fFiles = formatNumber(fileCount);
      const fLines = formatNumber(lines);
      const fWords = formatNumber(words);
      const fChars = formatNumber(chars);

      return `${fFiles} file${fileCount>1?"s":""} | ${fLines} line${lines>1?"s":""} | ${fWords} word${words>1?"s":""} | ${fChars} character${chars>1?"s":""} selected`;
    }

    // Button listeners
    document.addEventListener('click', async (e) => {
      const id = e.target.id;
      if (!id) return;
      switch(id) {
        case 'copyBtn': {
          const content = document.getElementById('result').textContent;
          if (!content) return;
          try {
            await navigator.clipboard.writeText(content);
            console.log("Copied to clipboard!");
          } catch (err) {
            console.error("Could not copy to clipboard:", err);
          }
          break;
        }
        case 'refreshBtn': {
          await loadTree();
          await updatePreview();
          break;
        }
        case 'collapseAllBtn': {
          await collapseAll();
          break;
        }
        case 'expandAllBtn': {
          await expandAll();
          break;
        }
        case 'selectAllBtn': {
          await selectAll();
          break;
        }
        case 'unselectAllBtn': {
          await unselectAll();
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
    """Return top-level items (dirs/files) from the current directory."""
    root_path = Path.cwd()
    data = []
    for entry in root_path.iterdir():
        node = build_file_tree(entry)
        data.append(node)
    data.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return jsonify(data)

@app.route("/api/copy", methods=["POST"])
def api_copy():
    """
    Receives JSON: { "files": ["fullPath1", "fullPath2", ...] }
    Returns a single Markdown snippet for the selected files.
    """
    data = request.get_json()
    selected_paths = data.get("files", [])

    md_pieces = []
    for abs_path_str in selected_paths:
        abs_path = Path(abs_path_str)
        if not abs_path.is_file():
            continue

        rel_path = abs_path.relative_to(Path.cwd()).as_posix()
        lang = detect_language(rel_path)
        size = abs_path.stat().st_size
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            if size == 0:
                snippet = f"**{rel_path}**\n```{lang}\n<File is empty>\n```\n\n"
            else:
                snippet = f"**{rel_path}**\n```{lang}\n{content}\n```\n\n"
            md_pieces.append(snippet)
        except Exception as e:
            md_pieces.append(f"**{rel_path}**\n```\nError reading file: {e}\n```\n\n")

    return "".join(md_pieces)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
