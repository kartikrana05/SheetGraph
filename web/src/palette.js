// Deterministic colour assignment per node label, so a label keeps the same
// colour between the schema preview, the legend and the graph canvas.
const PALETTE = [
  '#4c8dff', // blue
  '#3fb950', // green
  '#d29922', // amber
  '#c77dff', // violet
  '#f85149', // red
  '#2dd4bf', // teal
  '#f472b6', // pink
  '#a3e635', // lime
  '#fb923c', // orange
  '#818cf8', // indigo
]

export function colorFor(label, allLabels) {
  const index = allLabels.indexOf(label)
  if (index >= 0) return PALETTE[index % PALETTE.length]

  // Stable hash fallback for labels not in the known list
  let hash = 0
  for (let i = 0; i < label.length; i += 1) {
    hash = (hash * 31 + label.charCodeAt(i)) >>> 0
  }
  return PALETTE[hash % PALETTE.length]
}

export { PALETTE }
