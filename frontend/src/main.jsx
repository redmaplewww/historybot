import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import cytoscape from 'cytoscape'
import {
  BookOpen,
  CalendarRange,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  FileCheck2,
  Focus,
  GitBranch,
  History,
  MapPinned,
  Network,
  PanelRightClose,
  RotateCcw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  UserRound,
} from 'lucide-react'
import './styles.css'

const NAV_ITEMS = [
  { id: 'graph', label: '人物图', icon: Network },
  { id: 'timeline', label: '时间线', icon: CalendarRange },
  { id: 'places', label: '地理线', icon: MapPinned },
  { id: 'claims', label: '证据主张', icon: FileCheck2 },
  { id: 'sources', label: '史料库', icon: BookOpen },
]

const STATUS_LABELS = {
  machine_reviewed: '机器审查',
  disputed: '存疑',
  promoted: '已采信',
  refuted: '已证伪',
  extracted: '已抽取',
  evidence_linked: '已挂证据',
  human_reviewed: '人工审查',
}

const KIND_LABELS = {
  compilation: '正史编纂',
  folk: '文学民间',
  primary: '一手史料',
  contemporary: '同时代材料',
  modern_study: '现代研究',
  unknown: '未分类',
}

const GRAPH_MODES = [
  { id: 'people', label: '人物关系' },
  { id: 'all', label: '全部节点' },
  { id: 'claims', label: '主张关系' },
  { id: 'events', label: '事件共现' },
]

function formatPercent(value) {
  return `${Math.round((value || 0) * 100)}%`
}

function locatorText(locator) {
  if (!locator || !Object.keys(locator).length) return '原文段落'
  if (locator.ordinal !== undefined) return `段落 ${locator.ordinal + 1}`
  return Object.values(locator).join(' · ')
}

function StatusBadge({ status }) {
  return <span className={`status status-${status}`}>{STATUS_LABELS[status] || status}</span>
}

function Confidence({ value, compact = false }) {
  const score = Math.round((value || 0) * 100)
  return (
    <div className={`confidence ${compact ? 'confidence-compact' : ''}`} title={`置信度 ${score}%`}>
      <span className="confidence-track"><span style={{ width: `${score}%` }} /></span>
      <strong>{score}</strong>
    </div>
  )
}

function App() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [view, setView] = useState('graph')
  const [selectedNode, setSelectedNode] = useState(null)
  const [selectedClaim, setSelectedClaim] = useState(null)
  const dimensionsLoadedRef = useRef(false)
  const dataVersionRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    const loadWorkspace = () => fetch('/api/workspace?include_graph=false&include_dimensions=false')
      .then((response) => {
        if (!response.ok) throw new Error(`数据接口返回 ${response.status}`)
        return response.json()
      })
      .then((payload) => {
        if (cancelled) return
        const nextVersion = payload.overview?.data_version
        if (nextVersion && dataVersionRef.current === nextVersion) return
        dataVersionRef.current = nextVersion || null
        dimensionsLoadedRef.current = false
        setError('')
        setData({ ...payload, graph: { nodes: [], edges: [] } })
      })
      .catch((reason) => setError(reason.message))
    loadWorkspace()
    const timer = window.setInterval(loadWorkspace, 15000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    if (!data || view === 'graph' || dimensionsLoadedRef.current) return undefined
    let cancelled = false
    fetch('/api/dimensions')
      .then((response) => {
        if (!response.ok) throw new Error(`维度接口返回 ${response.status}`)
        return response.json()
      })
      .then((dimensions) => {
        if (cancelled) return
        dimensionsLoadedRef.current = true
        setData((current) => ({ ...current, ...dimensions }))
      })
      .catch((reason) => {
        if (!cancelled) setError(reason.message)
      })
    return () => { cancelled = true }
  }, [data, view])

  if (error) return <SystemState icon={CircleAlert} title="无法载入知识库" detail={error} />
  if (!data) return <SystemState icon={History} title="正在装载史料" detail="" />

  const current = NAV_ITEMS.find((item) => item.id === view)
  const openClaim = (claim) => {
    setSelectedClaim(claim)
    setView('claims')
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark"><History size={19} /><span>史证图谱</span></div>
        <nav aria-label="数据维度">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}>
                <Icon size={18} /><span>{item.label}</span>
              </button>
            )
          })}
        </nav>
        <div className="system-foot">
          <span className="pulse-dot" />
          <div><strong>知识库在线</strong><small>{data.overview.counts.passages.toLocaleString()} 条史料段落</small></div>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div><span className="eyebrow">历史史料研究空间</span><h1>{current.label}</h1></div>
          <div className="topbar-status"><ExtractionStatus extraction={data.overview.extraction} /></div>
          <div className="top-metrics">
            <Metric value={data.overview.counts.sources} label="来源" />
            <Metric value={data.overview.counts.passages.toLocaleString()} label="段落" />
            <Metric value={data.overview.counts.person_nodes.toLocaleString()} label="人物" />
            <Metric value={data.overview.counts.claims} label="主张" />
            <Metric value={data.overview.counts.evidence} label="证据链" />
          </div>
        </header>

        <section className="view-stage">
          {view === 'graph' && <GraphView data={data} selectedNode={selectedNode} onSelectNode={setSelectedNode} onSelectClaim={openClaim} />}
          {view === 'timeline' && <TimelineView items={data.timeline} />}
          {view === 'places' && <PlacesView items={data.places} />}
          {view === 'claims' && <ClaimsView claims={data.claims} selected={selectedClaim} onSelect={setSelectedClaim} />}
          {view === 'sources' && <SourcesView sources={data.sources} overview={data.overview} />}
        </section>
      </main>
    </div>
  )
}

function SystemState({ icon: Icon, title, detail }) {
  return <div className="system-state"><Icon size={28} /><strong>{title}</strong>{detail && <span>{detail}</span>}</div>
}

function Metric({ value, label }) {
  return <div className="metric"><strong>{value}</strong><span>{label}</span></div>
}

function ExtractionStatus({ extraction }) {
  if (!extraction) return null
  const phase = extraction.active_scope || 'people'
  const current = extraction[phase] || extraction
  const complete = current.completed_passages || 0
  const total = current.total_passages || 0
  const finished = total > 0 && complete >= total
  const label = phase === 'evidence' ? '事件与证据抽取' : phase === 'complete' ? '全库抽取完成' : '人物抽取'
  return (
    <div className="extraction-status" title="抽取账本会随数据库批次提交自动刷新">
      <span className={`pulse-dot ${finished ? 'pulse-dot-done' : ''}`} />
      <div>
        <strong>{finished ? `${label}完成` : `${label}进行中`}</strong>
        <small>{complete.toLocaleString()} / {total.toLocaleString()} 段落 · {formatPercent(current.coverage)}</small>
      </div>
    </div>
  )
}

function GraphView({ data, selectedNode, onSelectNode, onSelectClaim }) {
  const [mode, setMode] = useState('people')
  const [query, setQuery] = useState('')
  const [minConfidence, setMinConfidence] = useState(0)
  const [showInspector, setShowInspector] = useState(true)
  const [graph, setGraph] = useState({ nodes: [], edges: [] })
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const cyRef = useRef(null)
  const containerRef = useRef(null)
  const requestRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    const requestId = requestRef.current + 1
    requestRef.current = requestId
    setLoading(true)
    setLoadError('')
    const kind = mode === 'people' ? '&kind=person' : ''
    fetch(`/api/graph?limit=120${kind}`)
      .then((response) => {
        if (!response.ok) throw new Error(`图谱接口返回 ${response.status}`)
        return response.json()
      })
      .then((payload) => {
        if (cancelled || requestRef.current !== requestId) return
        setGraph(payload)
        setLoading(false)
        const preferred = payload.nodes.find((node) => node.kind === 'person') || payload.nodes[0] || null
        onSelectNode((current) => current && payload.nodes.some((node) => node.id === current.id) ? current : preferred)
      })
      .catch((reason) => {
        if (cancelled) return
        setLoadError(reason.message)
        setLoading(false)
      })
    return () => { cancelled = true }
  }, [mode, data.overview.data_version, onSelectNode])

  useEffect(() => {
    if (!selectedNode?.id) return undefined
    let cancelled = false
    fetch(`/api/nodes/${encodeURIComponent(selectedNode.id)}`)
      .then((response) => {
        if (!response.ok) throw new Error(`节点接口返回 ${response.status}`)
        return response.json()
      })
      .then((payload) => {
        if (cancelled) return
        setGraph((current) => {
          const nodes = new Map(current.nodes.map((node) => [node.id, node]))
          ;(payload.nodes || [payload.node]).forEach((node) => nodes.set(node.id, node))
          const edges = new Map(current.edges.map((edge) => [edge.id, edge]))
          payload.edges.forEach((edge) => edges.set(edge.id, edge))
          return { nodes: [...nodes.values()], edges: [...edges.values()] }
        })
      })
      .catch((reason) => {
        if (!cancelled) setLoadError(reason.message)
      })
    return () => { cancelled = true }
  }, [selectedNode?.id])

  useEffect(() => {
    const needle = query.trim()
    if (!needle) return undefined
    const handle = window.setTimeout(() => {
      const kind = mode === 'people' ? '&kind=person' : ''
      fetch(`/api/graph?q=${encodeURIComponent(needle)}&limit=120${kind}`)
        .then((response) => response.ok ? response.json() : Promise.reject(new Error(`搜索接口返回 ${response.status}`)))
        .then((payload) => {
          setGraph(payload)
          const match = payload.nodes[0]
          if (match) onSelectNode(match)
        })
        .catch((reason) => setLoadError(reason.message))
    }, 240)
    return () => window.clearTimeout(handle)
  }, [query, mode, onSelectNode])

  const filtered = useMemo(() => {
    let edges = graph.edges.filter((edge) => edge.confidence >= minConfidence)
    if (mode === 'claims') edges = edges.filter((edge) => edge.edge_type === 'claim')
    if (mode === 'events') edges = edges.filter((edge) => edge.edge_type === 'event')
    let allowedNodes = graph.nodes
    if (mode === 'people') {
      const people = new Set(graph.nodes.filter((node) => node.kind === 'person').map((node) => node.id))
      const focused = selectedNode?.id
      edges = edges.filter((edge) => (people.has(edge.source) && people.has(edge.target)) || edge.source === focused || edge.target === focused)
      const focusedNeighbors = new Set(edges.flatMap((edge) => edge.source === focused ? [edge.target] : edge.target === focused ? [edge.source] : []))
      allowedNodes = graph.nodes.filter((node) => people.has(node.id) || node.id === focused || focusedNeighbors.has(node.id))
    }
    const connected = new Set(edges.flatMap((edge) => [edge.source, edge.target]))
    const visibleNodes = mode === 'people' || mode === 'all'
      ? allowedNodes
      : allowedNodes.filter((node) => connected.has(node.id))
    return { nodes: visibleNodes, edges }
  }, [graph, mode, minConfidence, selectedNode?.id])

  useEffect(() => {
    if (!containerRef.current) return undefined
    const elements = [
      ...filtered.nodes.map((node) => ({ data: node })),
      ...filtered.edges.map((edge) => ({ data: edge })),
    ]
    const cy = cytoscape({
      container: containerRef.current,
      elements,
      minZoom: 0.35,
      maxZoom: 2.4,
      wheelSensitivity: 0.18,
      style: [
        { selector: 'node', style: { 'background-color': '#d8ddd8', 'border-color': '#6d7772', 'border-width': 1.5, label: 'data(label)', color: '#202823', 'font-size': 11, 'font-family': 'Microsoft YaHei, sans-serif', 'text-valign': 'bottom', 'text-margin-y': 8, width: 34, height: 34, 'overlay-opacity': 0 } },
        { selector: 'node[kind = "person"]', style: { 'background-color': '#f7f8f5', 'border-color': '#176b67', 'border-width': 3, width: 46, height: 46, 'font-size': 13, 'font-weight': 600 } },
        { selector: 'node[kind = "place"]', style: { 'background-color': '#edf1f6', 'border-color': '#41658a', shape: 'diamond' } },
        { selector: 'node[kind = "office"]', style: { 'background-color': '#f5e9cf', 'border-color': '#a66d18', shape: 'round-rectangle' } },
        { selector: 'edge', style: { width: 1.6, 'line-color': '#aab2ad', 'target-arrow-color': '#aab2ad', 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', label: 'data(label)', color: '#626b66', 'font-size': 9, 'text-background-color': '#f4f5f2', 'text-background-opacity': 0.9, 'text-background-padding': 3, 'text-rotation': 'autorotate', 'arrow-scale': 0.75, 'overlay-opacity': 0 } },
        { selector: 'edge[edge_type = "event"]', style: { 'line-color': '#5e7f91', 'target-arrow-shape': 'none', 'line-style': 'dashed' } },
        { selector: 'edge[status = "disputed"]', style: { 'line-color': '#b54a3a', 'target-arrow-color': '#b54a3a' } },
        { selector: ':selected', style: { 'border-color': '#b54a3a', 'border-width': 4, 'line-color': '#b54a3a', 'target-arrow-color': '#b54a3a' } },
      ],
      layout: { name: 'cose', animate: false, fit: true, padding: 48, nodeRepulsion: 9000, idealEdgeLength: 120, edgeElasticity: 80 },
    })
    cy.on('tap', 'node', (event) => {
      onSelectNode(event.target.data())
      setShowInspector(true)
    })
    cyRef.current = cy
    return () => cy.destroy()
  }, [filtered, onSelectNode])

  const resetView = () => cyRef.current?.animate({ fit: { eles: cyRef.current.elements(), padding: 48 } }, { duration: 250 })
  return (
    <div className="graph-layout">
      <div className="graph-main">
        <div className="graph-toolbar">
          <div className="search-box"><Search size={16} /><input aria-label="搜索人物或关系" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索人物或关系" /></div>
          <div className="segmented" aria-label="图谱模式">
            {GRAPH_MODES.map((item) => <button key={item.id} className={mode === item.id ? 'active' : ''} onClick={() => setMode(item.id)}>{item.label}</button>)}
          </div>
          <label className="range-control" title="最低置信度"><SlidersHorizontal size={15} /><input type="range" min="0" max="0.8" step="0.1" value={minConfidence} onChange={(event) => setMinConfidence(Number(event.target.value))} /><span>{Math.round(minConfidence * 100)}</span></label>
          <button className="icon-button" onClick={resetView} title="适合视图" aria-label="适合视图"><Focus size={17} /></button>
          <button className="icon-button desktop-only" onClick={() => setShowInspector((value) => !value)} title="切换详情面板" aria-label="切换详情面板"><PanelRightClose size={17} /></button>
        </div>
        <div ref={containerRef} className="graph-canvas" data-testid="graph-canvas" />
        <div className="graph-legend">
          <span><i className="legend-node person" />人物</span><span><i className="legend-line claim" />主张</span><span><i className="legend-line event" />事件共现</span><span><i className="legend-line disputed" />存疑</span>
        </div>
        <div className="graph-count">已载入 {filtered.nodes.length} 节点 · {filtered.edges.length} 关系 · 全库 {data.overview.counts.person_nodes.toLocaleString()} 人物</div>
        {(loading || loadError) && <div className="graph-loading">{loading ? '正在按需载入图谱…' : loadError}</div>}
      </div>
      {showInspector && <NodeInspector node={selectedNode} data={{ ...data, graph }} onClose={() => setShowInspector(false)} onSelectClaim={onSelectClaim} />}
    </div>
  )
}

function NodeInspector({ node, data, onClose, onSelectClaim }) {
  if (!node) return <aside className="inspector empty"><Network size={24} /><span>未选中节点</span></aside>
  const edges = data.graph.edges.filter((edge) => node.id === edge.source || node.id === edge.target)
  const claimIds = new Set(edges.map((edge) => edge.claim_id).filter(Boolean))
  const claims = data.claims.filter((claim) => claimIds.has(claim.id))
  return (
    <aside className="inspector">
      <div className="inspector-head">
        <div className={`node-avatar ${node.kind}`}><UserRound size={22} /></div>
        <div><span>{node.entity_types?.join(' · ') || '关系节点'}</span><h2>{node.label}</h2>{node.aliases?.length > 0 && <small className="node-aliases">别名：{node.aliases.join('、')}</small>}</div>
        <button className="icon-button" onClick={onClose} title="关闭详情" aria-label="关闭详情"><PanelRightClose size={17} /></button>
      </div>
      <div className="inspector-stats"><Metric value={edges.length} label="关联" /><Metric value={claims.length} label="主张" /><Metric value={node.event_ids?.length || 0} label="事件" /></div>
      <section className="inspector-section">
        <h3>关系索引</h3>
        <div className="relation-list">
          {edges.length === 0 && <span className="muted">暂无关系</span>}
          {edges.map((edge) => {
            const peer = edge.source === node.id ? edge.target : edge.source
            return <div className="relation-row" key={edge.id}><span className={`edge-mark ${edge.edge_type} ${edge.status}`} /><div><strong>{peer}</strong><small>{edge.label}</small></div><Confidence compact value={edge.confidence} /></div>
          })}
        </div>
      </section>
      <section className="inspector-section">
        <h3>证据主张</h3>
        <div className="mini-claims">
          {claims.map((claim) => <button key={claim.id} onClick={() => onSelectClaim(claim)}><span>{claim.statement}</span><StatusBadge status={claim.status} /><ChevronRight size={15} /></button>)}
          {claims.length === 0 && <span className="muted">当前关系来自事件参与记录</span>}
        </div>
      </section>
    </aside>
  )
}

function TimelineView({ items }) {
  const dated = items.filter((item) => item.date_start)
  const undated = items.filter((item) => !item.date_start)
  return (
    <div className="content-view timeline-view">
      <div className="section-heading"><div><span className="eyebrow">事件维度</span><h2>年代与事件序列</h2></div><div className="summary-number"><strong>{dated.length}</strong><span>明确时间</span></div></div>
      <div className="timeline-band">
        {dated.map((item) => <TimelineItem key={item.id} item={item} />)}
      </div>
      <div className="subheading"><h3>待考年代</h3><span>{undated.length} 个事件</span></div>
      <div className="event-grid">{undated.map((item) => <EventItem key={item.id} item={item} />)}</div>
    </div>
  )
}

function TimelineItem({ item }) {
  return <article className="timeline-item"><div className="time-label">{item.date_start}{item.date_end && ` — ${item.date_end}`}</div><div className="time-rule"><i /></div><div className="time-content"><span>{item.event_type}</span><h3>{item.label}</h3><p>{item.participant_surfaces.join('、')}</p><small>{item.source.title} · {formatPercent(item.confidence)}</small></div></article>
}

function EventItem({ item }) {
  return <article className="event-item"><div className="event-icon"><GitBranch size={17} /></div><div><span>{item.event_type}</span><h3>{item.label}</h3><p>{item.participant_surfaces.join('、') || '参与者未识别'}</p></div><Confidence value={item.confidence} compact /></article>
}

function PlacesView({ items }) {
  return (
    <div className="content-view places-view">
      <div className="section-heading"><div><span className="eyebrow">空间维度</span><h2>地点与事件链</h2></div><div className="summary-number"><strong>{items.length}</strong><span>已定位地点</span></div></div>
      <div className="place-lines">
        {items.map((place, index) => <article className="place-row" key={place.name}><div className="place-index">{String(index + 1).padStart(2, '0')}</div><div className="place-name"><MapPinned size={19} /><div><h3>{place.name}</h3><span>{place.event_count} 个事件 · {place.participants.length} 个参与者</span></div></div><div className="place-events">{place.events.map((event) => <div key={event.id}><span>{event.date_start || '年代待考'}</span><strong>{event.label}</strong><small>{event.participant_surfaces.join('、')}</small></div>)}</div></article>)}
        {!items.length && <EmptyData icon={MapPinned} text="尚无带地点证据的事件" />}
      </div>
    </div>
  )
}

function ClaimsView({ claims, selected, onSelect }) {
  const [status, setStatus] = useState('all')
  const [query, setQuery] = useState('')
  const visible = claims.filter((claim) => (status === 'all' || claim.status === status) && (!query || `${claim.subject}${claim.predicate}${claim.object}${claim.statement}`.includes(query)))
  const active = selected && visible.some((claim) => claim.id === selected.id) ? selected : visible[0]
  return (
    <div className="claims-layout">
      <section className="claims-table-wrap">
        <div className="claims-tools"><div className="search-box"><Search size={16} /><input aria-label="搜索主张" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索主张、人物或事件" /></div><div className="segmented"><button className={status === 'all' ? 'active' : ''} onClick={() => setStatus('all')}>全部</button><button className={status === 'machine_reviewed' ? 'active' : ''} onClick={() => setStatus('machine_reviewed')}>机器审查</button><button className={status === 'disputed' ? 'active' : ''} onClick={() => setStatus('disputed')}>存疑</button></div></div>
        <div className="table-head"><span>主张</span><span>状态</span><span>置信度</span></div>
        <div className="claims-list">{visible.map((claim) => <button key={claim.id} className={active?.id === claim.id ? 'active' : ''} onClick={() => onSelect(claim)}><div><strong>{claim.subject} <i>{claim.predicate}</i> {claim.object}</strong><span>{claim.statement}</span></div><StatusBadge status={claim.status} /><Confidence compact value={claim.confidence.score} /></button>)}</div>
      </section>
      <ClaimInspector claim={active} />
    </div>
  )
}

function ClaimInspector({ claim }) {
  if (!claim) return <aside className="claim-detail"><EmptyData icon={FileCheck2} text="没有符合条件的主张" /></aside>
  const dimensions = [
    ['来源可靠度', claim.confidence.source_reliability],
    ['抽取质量', claim.confidence.extraction_quality],
    ['消歧质量', claim.confidence.resolution_quality],
    ['交叉互证', claim.confidence.corroboration],
  ]
  return <aside className="claim-detail"><div className="claim-detail-head"><StatusBadge status={claim.status} /><Confidence value={claim.confidence.score} /></div><h2>{claim.statement}</h2><p className="rationale">{claim.confidence.rationale}</p><section><h3>置信度构成</h3>{dimensions.map(([label, value]) => <div className="dimension-row" key={label}><span>{label}</span><Confidence value={value} /></div>)}</section><section><h3>证据链</h3>{claim.evidence.map((item) => <article className="evidence-block" key={item.id}><div><span className={`polarity ${item.polarity}`}>{item.polarity === 'supports' ? '支持' : item.polarity === 'refutes' ? '反驳' : '部分支持'}</span><strong>{item.source.title}</strong><small>{locatorText(item.locator)}</small>{item.source.url && <a href={item.source.url} target="_blank" rel="noreferrer" title="打开史料来源"><ExternalLink size={14} /></a>}</div><blockquote>{item.quote}</blockquote><p>{item.reasoning}</p><footer><span>{KIND_LABELS[item.source.kind] || item.source.kind}</span><span>证据置信度 {formatPercent(item.confidence)}</span></footer></article>)}</section></aside>
}

function SourcesView({ sources, overview }) {
  return <div className="content-view source-view"><div className="section-heading"><div><span className="eyebrow">来源维度</span><h2>史料来源与评级</h2></div><div className="source-summary"><ShieldCheck size={20} /><div><strong>{formatPercent(overview.evidence_coverage)}</strong><span>主张已挂接证据</span></div></div></div><div className="source-table"><div className="source-table-head"><span>史料</span><span>类型</span><span>段落</span><span>主张</span><span>存疑</span><span>基线</span></div>{sources.map((source) => { const assessment = source.metadata?.assessment || {}; return <article key={source.id}><div className="source-title"><BookOpen size={18} /><div><strong>{source.title}</strong><span>{source.author || '作者待考'}</span></div>{source.url && <a href={source.url} target="_blank" rel="noreferrer" title="打开原始来源"><ExternalLink size={15} /></a>}</div><span>{KIND_LABELS[source.kind] || source.kind}</span><strong>{source.passage_count}</strong><strong>{source.claim_count}</strong><strong className={source.disputed_count ? 'danger-text' : ''}>{source.disputed_count}</strong><Confidence compact value={assessment.baseline_score || 0} /><p>{assessment.rationale}</p></article>})}</div></div>
}

function EmptyData({ icon: Icon, text }) {
  return <div className="empty-data"><Icon size={22} /><span>{text}</span></div>
}

createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)
