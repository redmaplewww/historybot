"""Build a standalone HTML report from local OCR evaluation JSON files."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import shutil
from pathlib import Path

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OCR 识别核验报告</title>
  <style>
    :root { --ink:#1f2924; --muted:#66736c; --line:#dce3dd; --paper:#f5f7f3; --card:#fff; --teal:#176b67; --blue:#41658a; --amber:#a66d18; --red:#a33f32; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--paper); color:var(--ink); font-family: "Microsoft YaHei", "Noto Sans SC", sans-serif; }
    .page { max-width:1440px; margin:0 auto; padding:32px clamp(16px, 4vw, 56px) 56px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; padding-bottom:24px; border-bottom:1px solid var(--line); }
    .eyebrow { color:var(--teal); font-size:11px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
    h1 { margin:7px 0 0; font-family: Georgia, "Songti SC", serif; font-size:clamp(28px, 4vw, 44px); font-weight:600; letter-spacing:0; }
    .subtitle { max-width:620px; margin:12px 0 0; color:var(--muted); font-size:13px; line-height:1.7; }
    .summary { display:flex; gap:18px; flex-wrap:wrap; justify-content:flex-end; }
    .summary-item { min-width:92px; padding-left:14px; border-left:2px solid var(--teal); }
    .summary-item strong { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
    .summary-item span { color:var(--muted); font-size:11px; }
    .toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:22px 0 14px; }
    .filters { display:flex; flex-wrap:wrap; gap:7px; }
    .filter { border:1px solid var(--line); border-radius:4px; background:var(--card); color:var(--muted); padding:7px 11px; font-size:11px; cursor:pointer; }
    .filter.active { border-color:#8db9ab; background:#eaf4ef; color:var(--teal); font-weight:700; }
    .hint { color:var(--muted); font-size:11px; }
    .source { margin-top:22px; border:1px solid var(--line); background:var(--card); }
    .source-head { display:flex; align-items:center; justify-content:space-between; gap:14px; padding:16px 18px; border-bottom:1px solid var(--line); }
    .source-head h2 { margin:0; font-size:15px; overflow-wrap:anywhere; }
    .source-head p { margin:5px 0 0; color:var(--muted); font-size:11px; }
    .source-kind { border:1px solid #aebdca; color:var(--blue); padding:5px 8px; border-radius:3px; font-size:10px; white-space:nowrap; }
    .source-body { display:grid; grid-template-columns:minmax(260px, .85fr) minmax(0, 1.4fr); gap:18px; padding:18px; }
    .original { min-width:0; }
    .label { margin:0 0 9px; color:var(--muted); font-size:10px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
    .preview { display:grid; place-items:center; min-height:250px; padding:12px; background:#edf0ec; border:1px solid var(--line); }
    .preview img { display:block; max-width:100%; max-height:420px; object-fit:contain; }
    .file-link { display:inline-flex; margin-top:9px; color:var(--blue); font-size:11px; text-decoration:none; overflow-wrap:anywhere; }
    .file-link:hover { text-decoration:underline; }
    .models { display:grid; gap:10px; min-width:0; }
    .model-card { border:1px solid var(--line); padding:13px 14px; background:#fcfdfb; }
    .model-top { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
    .model-name { min-width:0; }
    .model-name strong { display:block; font-size:13px; overflow-wrap:anywhere; }
    .model-name span { display:block; margin-top:4px; color:var(--muted); font-size:10px; overflow-wrap:anywhere; }
    .status { flex:0 0 auto; border-radius:3px; padding:4px 7px; font-size:10px; font-weight:700; }
    .status-ok { color:var(--teal); background:#e6f3ed; }
    .status-review { color:var(--red); background:#fff0ed; }
    .status-error { color:var(--red); background:#fff0ed; }
    .confidence { display:flex; align-items:center; gap:9px; margin:13px 0 10px; }
    .track { height:7px; flex:1; background:#e2e8e3; overflow:hidden; }
    .track i { display:block; height:100%; background:var(--teal); }
    .confidence strong { min-width:42px; text-align:right; font-size:12px; font-variant-numeric:tabular-nums; }
    .recognized { margin:0; padding:11px; min-height:48px; white-space:pre-wrap; overflow-wrap:anywhere; background:#fff; border-left:3px solid var(--teal); font-family: "Microsoft YaHei", sans-serif; font-size:13px; line-height:1.65; }
    .error { border-left-color:var(--red); color:var(--red); }
    .meta { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:10px; color:var(--muted); font-size:10px; }
    .meta b { color:var(--ink); font-weight:600; }
    .copy { margin-top:10px; border:1px solid var(--line); border-radius:3px; background:#fff; color:var(--blue); padding:5px 8px; font-size:10px; cursor:pointer; }
    .copy:hover { border-color:#8aa39a; }
    footer { margin-top:28px; padding-top:14px; border-top:1px solid var(--line); color:var(--muted); font-size:10px; line-height:1.6; }
    .empty { padding:38px; text-align:center; color:var(--muted); }
    @media (max-width: 820px) { header { display:block; } .summary { justify-content:flex-start; margin-top:20px; } .source-body { grid-template-columns:1fr; } .preview { min-height:180px; } .toolbar { display:block; } .hint { margin-top:8px; } }
  </style>
</head>
<body>
  <main class="page">
    <header>
      <div><div class="eyebrow">HistoryBot / OCR Review</div><h1>OCR 识别核验</h1><p class="subtitle">在同一视图中对照原始文件、不同识别模型的文本、置信度与复核状态。原始文件未被改写，视觉模型结果默认进入人工复核。</p></div>
      <div class="summary" id="summary"></div>
    </header>
    <div class="toolbar"><div class="filters" id="filters"></div><span class="hint">点击模型标签可筛选结果</span></div>
    <section id="reports"></section>
    <footer>报告由本地 OCR 评测产物生成。置信度是模型输出的质量信号，不等于历史事实可信度；最终入库仍需通过证据完整性检查。</footer>
  </main>
  <script>
    const reports = __REPORTS__;
    const imagePaths = __IMAGE_PATHS__;
    const state = { model: 'all' };
    const modelLabels = { rapidocr: 'RapidOCR', 'trocr-printed': 'TrOCR 印刷体', 'trocr-handwritten': 'TrOCR 手写体' };
    const percent = value => `${Math.round((value || 0) * 100)}%`;
    const esc = value => String(value ?? '').replace(/[&<>\"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[char]));
    function render() {
      const models = [...new Set(reports.flatMap(item => item.models.map(model => model.model)))];
      document.querySelector('#filters').innerHTML = [`<button class="filter ${state.model === 'all' ? 'active' : ''}" data-model="all">全部模型</button>`, ...models.map(model => `<button class="filter ${state.model === model ? 'active' : ''}" data-model="${esc(model)}">${esc(modelLabels[model] || model)}</button>`)].join('');
      document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => { state.model = button.dataset.model; render(); }));
      const shownModels = reports.flatMap(item => item.models).filter(item => state.model === 'all' || item.model === state.model);
      document.querySelector('#summary').innerHTML = `<div class="summary-item"><strong>${reports.length}</strong><span>原始文件</span></div><div class="summary-item"><strong>${shownModels.length}</strong><span>识别结果</span></div><div class="summary-item"><strong>${shownModels.filter(item => !item.error && !item.needs_review).length}</strong><span>无需复核</span></div>`;
      document.querySelector('#reports').innerHTML = reports.map(report => {
        const models = report.models.filter(item => state.model === 'all' || item.model === state.model);
        const sourceLink = report.source_page ? `<a class="file-link" href="${esc(report.source_page)}" target="_blank" rel="noreferrer">查看 Wikimedia Commons 来源页</a>` : '';
        const sourceMeta = [report.source_institution, report.license, report.width && report.height ? `${report.width} × ${report.height}px` : ''].filter(Boolean).map(value => `<span>${esc(value)}</span>`).join('');
        return `<article class="source"><div class="source-head"><div><h2>${esc(report.image)}</h2><p>${esc(report.description || '真实历史资料原图')} · ${models.length} 个识别结果</p>${sourceLink}<div class="meta source-meta">${sourceMeta}</div></div><span class="source-kind">${esc((report.image || '').split('.').pop().toUpperCase())}</span></div><div class="source-body"><div class="original"><p class="label">原始文件</p><div class="preview"><img src="${esc(imagePaths[report.image] || report.image)}" alt="${esc(report.image)}"></div><a class="file-link" href="${esc(imagePaths[report.image] || report.image)}" target="_blank" rel="noreferrer">在新窗口打开原始文件</a></div><div class="models"><p class="label">识别输出与置信度</p>${models.length ? models.map(modelCard).join('') : '<div class="empty">当前筛选没有结果</div>'}</div></div></article>`;
      }).join('');
      document.querySelectorAll('.copy').forEach(button => button.addEventListener('click', async () => { await navigator.clipboard.writeText(button.dataset.text); const old = button.textContent; button.textContent = '已复制'; setTimeout(() => { button.textContent = old; }, 1200); }));
    }
    function modelCard(item) {
      if (item.error) return `<article class="model-card"><div class="model-top"><div class="model-name"><strong>${esc(modelLabels[item.model] || item.model)}</strong><span>${esc(item.model)}</span></div><span class="status status-error">运行失败</span></div><p class="recognized error">${esc(item.error)}</p><div class="meta"><span>错误类型 <b>${esc(item.error_type)}</b></span><span>耗时 <b>${esc(item.elapsed_seconds)}s</b></span></div></article>`;
      const review = item.needs_review;
      return `<article class="model-card"><div class="model-top"><div class="model-name"><strong>${esc(modelLabels[item.model] || item.model)}</strong><span>${esc(item.processor)} · ${esc(item.metadata?.device || '本地')}</span></div><span class="status ${review ? 'status-review' : 'status-ok'}">${review ? '需复核' : '可继续处理'}</span></div><div class="confidence"><span class="track"><i style="width:${percent(item.confidence)}"></i></span><strong>${percent(item.confidence)}</strong></div><p class="recognized">${esc(item.text || '未识别到文本')}</p><div class="meta"><span>耗时 <b>${esc(item.elapsed_seconds)}s</b></span><span>文本块 <b>${item.spans?.length || 0}</b></span><span>处理器 <b>${esc(item.processor_version)}</b></span></div><button class="copy" data-text="${esc(item.text || '')}">复制识别文本</button></article>`;
    }
    render();
  </script>
</body>
</html>
"""


def build(reports: list[Path], output: Path, *, embed_images: bool = True) -> None:
    payload = []
    for path in reports:
        value = json.loads(path.read_text(encoding="utf-8"))
        payload.extend(value if isinstance(value, list) else [value])
    image_paths = {}
    for report in payload:
        image = Path(report["image"])
        if image.exists() and embed_images:
            media_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            image_paths[report["image"]] = f"data:{media_type};base64,{encoded}"
        elif image.exists():
            asset_dir = output.parent / "ocr_real_assets"
            asset_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image, asset_dir / image.name)
            image_paths[report["image"]] = f"ocr_real_assets/{image.name}"
        else:
            image_paths[report["image"]] = report["image"]
    rendered = PAGE.replace("__REPORTS__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    rendered = rendered.replace("__IMAGE_PATHS__", json.dumps(image_paths, ensure_ascii=False).replace("</", "<\\/"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build standalone OCR HTML report")
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/ocr_results.html"))
    parser.add_argument("--external-images", action="store_true", help="copy originals beside the HTML instead of embedding them")
    args = parser.parse_args()
    build(args.reports, args.output, embed_images=not args.external_images)
    print(args.output)


if __name__ == "__main__":
    main()
