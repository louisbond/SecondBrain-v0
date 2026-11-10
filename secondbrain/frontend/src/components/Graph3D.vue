<script setup>
/**
 * 3D 力导向图谱（自研轻量引擎，零外部依赖）。
 *
 * - 节点在三维空间里做力导向布局：斥力 + 向心力 + 边弹簧
 * - 透视投影到 2D 画布：绕 Y/X 轴旋转 → 画家算法排序 → 由远及近绘制
 * - 交互：拖拽旋转 / 滚轮缩放 / 点击选中 / 双击跳转
 */
import { ref, onMounted, onUnmounted, watch } from 'vue'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  // height=0 表示「撑满父容器」（父容器需要有确定高度）
  height: { type: Number, default: 520 },
  // 外部指定的聚焦笔记路径（点文件树 → 图谱高亮对应节点）
  focusPath: { type: String, default: '' }
})
const emit = defineEmits(['select'])

const canvasRef = ref(null)
const hovered = ref(null)
const selected = ref(null)
const stats = ref({ fps: 0, nodes: 0, links: 0 })

const COLORS = {
  note: '#4f9cf9',
  manuscript: '#ffd166',
  partition: '#f5a524',
  tag: '#35c48e',
  link: '#4b5a6b'
}

let raf = null
let running = false
let sim = { pts: [], edges: [], links: [] }
let rotY = 0.6
let rotX = -0.25
let zoom = 1
let dragging = false
let lastX = 0
let lastY = 0
let dpr = 1

// ---------------- 布局 ----------------
let dataFingerprint = ''

function buildSim(force = false) {
  const nodes = props.nodes || []
  const edges = props.edges || []
  // 数据没变就不重建——重建会让所有节点重新随机分布（用户看到的"乱跳"）
  const fp = `${nodes.length}:${edges.length}:${nodes[0]?.id || ''}:${edges[0]?.source || ''}`
  if (!force && fp === dataFingerprint && sim.pts.length) return
  dataFingerprint = fp
  const n = nodes.length
  const pts = new Array(n)
  for (let i = 0; i < n; i++) {
    // 初始位置：球面随机分布，种子化（同一份数据每次一样）
    const t = (i * 0.6180339887) % 1
    const phi = Math.acos(1 - 2 * t)
    const theta = Math.PI * (1 + Math.sqrt(5)) * i
    const r = 120 + (i % 7) * 14
    pts[i] = {
      id: nodes[i].id,
      kind: nodes[i].kind,
      label:
        nodes[i].kind === 'manuscript'
          ? `手稿·${nodes[i].note_title || nodes[i].label || ''}`
          : nodes[i].label,
      path: nodes[i].path,
      size: nodes[i].size || 1,
      x: r * Math.sin(phi) * Math.cos(theta),
      y: r * Math.sin(phi) * Math.sin(theta) * 0.6,
      z: r * Math.cos(phi),
      vx: 0,
      vy: 0,
      vz: 0,
      deg: 0
    }
  }

  // 分区节点固定到圆周，作为聚类中心；笔记靠 note→partition 的 spring 被拉过去
  const partNodes = pts.filter((x) => x.kind === 'partition')
  const RING = 210
  partNodes.forEach((p, k) => {
    const ang = (k / Math.max(1, partNodes.length)) * Math.PI * 2
    p.x = Math.cos(ang) * RING
    p.y = ((k % 3) - 1) * 26
    p.z = Math.sin(ang) * RING
    p.fixed = true
    p.vx = p.vy = p.vz = 0
  })

  const index = new Map(pts.map((p, i) => [p.id, i]))
  const links = []
  for (const e of edges) {
    const a = index.get(e.source)
    const b = index.get(e.target)
    if (a === undefined || b === undefined) continue
    links.push({ a, b, kind: e.kind || 'link' })
  }
  sim = { pts, edges, links }
  settled = false
  startedAt = performance.now()
  stats.value = { ...stats.value, nodes: pts.length, links: links.length }
}

function step(dt) {
  const { pts, links } = sim
  const n = pts.length
  if (!n) return
  const REPULSION = 2600
  const CENTER = 0.0022
  const SPRING = 0.012
  const REST = 78
  const DAMP = 0.86

  for (let i = 0; i < n; i++) {
    const p = pts[i]
    if (p.fixed) continue
    let fx = -p.x * CENTER
    let fy = -p.y * CENTER
    let fz = -p.z * CENTER
    for (let j = i + 1; j < n; j++) {
      const q = pts[j]
      let dx = p.x - q.x
      let dy = p.y - q.y
      let dz = p.z - q.z
      let d2 = dx * dx + dy * dy + dz * dz
      if (d2 < 1) {
        // 完全重合时给个确定性的微小偏移，避免除零
        dx = ((i % 7) - 3) * 0.6 + 0.1
        dy = ((j % 5) - 2) * 0.6 + 0.1
        dz = 0.5
        d2 = dx * dx + dy * dy + dz * dz
      }
      if (d2 > 90000) continue
      const f = REPULSION / d2
      fx += dx * f
      fy += dy * f
      fz += dz * f
    }
    p.vx += fx * dt
    p.vy += fy * dt
    p.vz += fz * dt
  }

  for (const l of links) {
    const a = pts[l.a]
    const b = pts[l.b]
    const dx = b.x - a.x
    const dy = b.y - a.y
    const dz = b.z - a.z
    const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1
    // 连到分区节点的边：更短的距离 + 更硬的弹簧 → 笔记明显聚在分区周围
    const toPart = pts[l.a].fixed || pts[l.b].fixed
    const rest = toPart ? 44 : REST
    const spring = toPart ? SPRING * 2.2 : SPRING
    const f = (dist - rest) * spring
    const ux = (dx / dist) * f
    const uy = (dy / dist) * f
    const uz = (dz / dist) * f
    a.vx += ux
    a.vy += uy
    a.vz += uz
    b.vx -= ux
    b.vy -= uy
    b.vz -= uz
  }

  for (const p of pts) {
    if (p.fixed) continue
    p.vx *= DAMP
    p.vy *= DAMP
    p.vz *= DAMP
    p.x += p.vx * dt
    p.y += p.vy * dt
    p.z += p.vz * dt
  }
}

// ---------------- 渲染 ----------------
function project(p, w, h) {
  const cy = Math.cos(rotY)
  const sy = Math.sin(rotY)
  const x = p.x * cy - p.z * sy
  let z = p.x * sy + p.z * cy
  const cx = Math.cos(rotX)
  const sx = Math.sin(rotX)
  const y = p.y * cx - z * sx
  z = p.y * sx + z * cx
  const f = 900
  const scale = f / (f + z + 700)
  return {
    x: w / 2 + x * scale * zoom,
    y: h / 2 + y * scale * zoom,
    z,
    scale
  }
}

let lastFrame = 0
let frameCount = 0
let fpsTimer = 0
let startedAt = 0

function draw() {
  const canvas = canvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  const dprNow = window.devicePixelRatio || 1
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  if (canvas.width !== w * dprNow || canvas.height !== h * dprNow) {
    canvas.width = w * dprNow
    canvas.height = h * dprNow
    dpr = dprNow
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)

  // 背景
  const grad = ctx.createRadialGradient(w / 2, h / 2, 10, w / 2, h / 2, Math.max(w, h))
  grad.addColorStop(0, '#12161d')
  grad.addColorStop(1, '#0b0e13')
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, w, h)

  const { pts, links } = sim
  if (!pts.length) {
    ctx.fillStyle = 'rgba(200,210,225,0.5)'
    ctx.font = '13px system-ui'
    ctx.textAlign = 'center'
    ctx.fillText('暂无图谱数据', w / 2, h / 2)
    return
  }

  const proj = pts.map((p) => project(p, w, h))
  const order = pts.map((_, i) => i).sort((a, b) => proj[b].z - proj[a].z)

  // 边
  for (const l of links) {
    const pa = proj[l.a]
    const pb = proj[l.b]
    const alpha = Math.max(0.06, Math.min(0.5, 0.42 - Math.abs(pa.z - pb.z) / 2600))
    const edgeColor =
      l.kind === 'hierarchy'
        ? 'rgba(245,165,36,0.5)'
        : l.kind === 'manuscript'
          ? 'rgba(255,209,102,0.55)'
          : `rgba(120,140,165,${alpha})`
    ctx.strokeStyle = edgeColor
    ctx.lineWidth = l.kind === 'hierarchy' || l.kind === 'manuscript' ? 1.4 : 0.8
    ctx.beginPath()
    ctx.moveTo(pa.x, pa.y)
    ctx.lineTo(pb.x, pb.y)
    ctx.stroke()
  }

  // 节点
  for (const i of order) {
    const p = pts[i]
    const s = proj[i]
    const r = (3.2 + (p.size - 1) * 1.4) * Math.max(0.45, s.scale)
    const color = COLORS[p.kind] || COLORS.note
    const isSel = selected.value && selected.value.id === p.id
    const isHov = hovered.value && hovered.value.id === p.id

    if (isSel || isHov) {
      ctx.beginPath()
      ctx.arc(s.x, s.y, r + 6, 0, Math.PI * 2)
      ctx.fillStyle = color + '33'
      ctx.fill()
    }
    ctx.beginPath()
    ctx.arc(s.x, s.y, r, 0, Math.PI * 2)
    ctx.fillStyle = color
    ctx.fill()
    ctx.lineWidth = 1
    ctx.strokeStyle = 'rgba(255,255,255,0.35)'
    ctx.stroke()

    // 标签：距离近或被选中时才画，避免糊成一片
    if (isSel || isHov || s.scale > 1.15) {
      ctx.fillStyle = isSel ? '#ffffff' : 'rgba(225,232,242,0.85)'
      ctx.font = `${isSel ? '12.5' : '11'}px system-ui`
      ctx.textAlign = 'left'
      ctx.fillText(p.label, s.x + r + 4, s.y + 4)
    }
  }
}

let settled = false

/** 总动能：衡量布局是否已稳定。 */
function kinetic() {
  let e = 0
  for (const p of sim.pts) e += p.vx * p.vx + p.vy * p.vy + p.vz * p.vz
  return e
}

function loop(ts) {
  if (!running) return
  const dt = Math.min(32, ts - (lastFrame || ts)) / 16.67
  lastFrame = ts
  // 收敛即冻结：布局稳定后不再模拟，节点纹丝不动（用户转视角/缩放不影响布局）
  if (!settled) {
    const iterations = sim.pts.length > 120 ? 1 : 2
    for (let i = 0; i < iterations; i++) step(dt)
    // 动能低于阈值 → 冻结；同时给一个最长收敛时间（8 秒）兜底
    if (kinetic() < 0.35 || ts - startedAt > 8000) settled = true
    frameCount++
    if (ts - fpsTimer > 1000) {
      stats.value = { ...stats.value, fps: frameCount }
      frameCount = 0
      fpsTimer = ts
    }
  }
  draw()
  raf = requestAnimationFrame(loop)
}

// ---------------- 交互 ----------------
function onPointerDown(e) {
  dragging = true
  lastX = e.clientX
  lastY = e.clientY
}
function onPointerMove(e) {
  const canvas = canvasRef.value
  if (!canvas) return
  const rect = canvas.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top
  if (dragging) {
    rotY += (e.clientX - lastX) * 0.008
    rotX -= (e.clientY - lastY) * 0.006
    rotX = Math.max(-1.35, Math.min(1.35, rotX))
    lastX = e.clientX
    lastY = e.clientY
    return
  }
  // 命中检测
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  const proj = sim.pts.map((p) => project(p, w, h))
  let best = null
  let bestD = 18
  proj.forEach((s, i) => {
    const d = (s.x - mx) ** 2 + (s.y - my) ** 2
    if (d < bestD) {
      bestD = d
      best = sim.pts[i]
    }
  })
  hovered.value = best
  canvas.style.cursor = best ? 'pointer' : 'grab'
}
function onPointerUp() {
  dragging = false
}
function onClick() {
  if (!hovered.value) return
  selected.value = hovered.value
  emit('select', hovered.value)
}
function onWheel(e) {
  e.preventDefault()
  zoom *= e.deltaY > 0 ? 0.92 : 1.08
  zoom = Math.max(0.25, Math.min(3.2, zoom))
}
function resetView() {
  rotY = 0.6
  rotX = -0.25
  zoom = 1
}

// 文件树里点了某篇笔记 → 图谱把对应节点设为选中（高亮）
watch(
  () => props.focusPath,
  (p) => {
    if (!p || !sim || !sim.pts || !sim.pts.length) return
    const hit = sim.pts.find((x) => x.path === p)
    if (hit) selected.value = hit
  }
)

onMounted(() => {
  buildSim()
  running = true
  raf = requestAnimationFrame(loop)
})
onUnmounted(() => {
  running = false
  if (raf) cancelAnimationFrame(raf)
})
watch(
  () => props.nodes,
  () => buildSim()
)
watch(
  () => props.edges,
  () => buildSim()
)

defineExpose({ resetView })
</script>

<template>
  <div class="graph3d" :style="height > 0 ? { height: height + 'px' } : { height: '100%' }">
    <canvas
      ref="canvasRef"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointerleave="onPointerUp"
      @click="onClick"
      @wheel="onWheel"
    ></canvas>
    <div class="graph-hint dim">
      <span class="mono">{{ stats.nodes }} 节点 / {{ stats.links }} 连线 / {{ stats.fps }}fps</span>
    </div>
    <div class="graph-legend">
      <span><i style="background: #ffd166"></i>个人手稿</span>
      <span><i style="background: #4f9cf9"></i>笔记</span>
      <span><i style="background: #f5a524"></i>分区</span>
      <span><i style="background: #35c48e"></i>标签</span>
    </div>
    <button class="btn ghost sm graph-reset" @click="resetView">复位视角</button>
  </div>
</template>

<style scoped>
.graph3d {
  position: relative;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--line);
  background: #0b0e13;
}
.graph3d canvas {
  width: 100%;
  height: 100%;
  display: block;
  cursor: grab;
}
.graph-hint {
  position: absolute;
  left: 10px;
  bottom: 8px;
  font-size: 11px;
  color: rgba(200, 210, 225, 0.55);
  display: flex;
  gap: 12px;
  align-items: center;
}
.graph-legend {
  position: absolute;
  right: 10px;
  top: 8px;
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: rgba(200, 210, 225, 0.7);
}
.graph-legend i {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 4px;
}
.graph-reset {
  position: absolute;
  right: 10px;
  bottom: 8px;
}

/* 窄屏下左下角提示与右下角「复位视角」会叠在一起：提示上移一行 */
@media (max-width: 720px) {
  .graph-hint {
    left: 8px;
    right: 8px;
    bottom: 40px;
    flex-wrap: wrap;
    gap: 2px 8px;
    font-size: 10px;
  }

  .graph-legend {
    left: 8px;
    right: auto;
    top: 6px;
    gap: 8px;
    font-size: 10px;
  }

  .graph-reset {
    right: 8px;
    bottom: 6px;
  }
}
</style>
