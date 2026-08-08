import { useEffect, useRef } from 'react'
import cytoscape from 'cytoscape'
import coseBilkent from 'cytoscape-cose-bilkent'
import { colorFor } from './palette'

cytoscape.use(coseBilkent)

const LAYOUT = {
  name: 'cose-bilkent',
  animate: false,
  nodeDimensionsIncludeLabels: false,
  idealEdgeLength: 110,
  nodeRepulsion: 9000,
  gravityRange: 2.5,
  randomize: true,
  fit: true,
  padding: 50,
}

// Above this many nodes sharing a label, individual names become noise —
// 180 project titles rendered at 9px is not information, it's texture.
const LABEL_CARDINALITY_LIMIT = 25
const EDGE_LABEL_LIMIT = 90

function displayName(node) {
  const props = node.props || {}
  // Prefer a human-readable property over the raw key.
  const preferred = Object.keys(props).find((k) =>
    /name|title|label|description/i.test(k) && props[k] != null
  )
  const value = preferred
    ? props[preferred]
    : Object.entries(props).find(([k, v]) => k !== '_ds' && v != null)?.[1]
  const text = String(value ?? node.label)
  return text.length > 24 ? `${text.slice(0, 23)}…` : text
}

export default function GraphCanvas({ data, labels, keyByLabel = {}, highlight, onSelect }) {
  const containerRef = useRef(null)
  const cyRef = useRef(null)

  // Build the instance once, then patch elements on updates so the layout
  // does not restart from scratch on every expand.
  useEffect(() => {
    const cy = cytoscape({
      container: containerRef.current,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': 'data(color)',
            // Size carries degree, so hubs read as hubs at a glance instead
            // of every node looking equally important.
            width: 'data(size)',
            height: 'data(size)',
            label: 'data(display)',
            color: '#e6edf3',
            'font-size': 'data(fontSize)',
            'font-weight': 'data(fontWeight)',
            'text-valign': 'center',
            'text-halign': 'right',
            'text-margin-x': 5,
            'text-outline-color': '#0d1117',
            'text-outline-width': 2.5,
            'border-width': 0,
            'z-index': 'data(zIndex)',
          },
        },
        {
          selector: 'node:selected',
          style: {
            'border-width': 3,
            'border-color': '#ffffff',
            label: 'data(name)',
            'font-size': 12,
            'z-index': 999,
          },
        },
        {
          selector: 'node.highlighted',
          style: {
            'border-width': 3,
            'border-color': '#ffd866',
            label: 'data(name)',
            'font-size': 11,
            'z-index': 900,
          },
        },
        { selector: 'node.dimmed', style: { opacity: 0.15 } },
        {
          selector: 'edge',
          style: {
            width: 0.8,
            'line-color': '#2f3743',
            'target-arrow-color': '#2f3743',
            'target-arrow-shape': 'triangle',
            'arrow-scale': 0.6,
            'curve-style': 'bezier',
            label: 'data(display)',
            'font-size': 7,
            color: '#6b7482',
            'text-rotation': 'autorotate',
            'text-outline-color': '#0d1117',
            'text-outline-width': 2,
            opacity: 0.55,
          },
        },
        {
          selector: 'edge.highlighted',
          style: {
            'line-color': '#ffd866',
            'target-arrow-color': '#ffd866',
            width: 1.8,
            opacity: 1,
            'z-index': 900,
          },
        },
        { selector: 'edge.dimmed', style: { opacity: 0.06 } },
      ],
      minZoom: 0.1,
      maxZoom: 4,
      wheelSensitivity: 0.25,
    })

    cy.on('tap', 'node', (event) => onSelect?.(event.target.data()))
    cy.on('tap', (event) => { if (event.target === cy) onSelect?.(null) })

    cyRef.current = cy
    return () => cy.destroy()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !data) return

    const present = new Set(data.nodes.map((n) => n.id))
    const edges = data.edges.filter((e) => present.has(e.source) && present.has(e.target))

    // Degree drives both size and whether a node is worth labelling.
    const degree = new Map()
    for (const edge of edges) {
      degree.set(edge.source, (degree.get(edge.source) || 0) + 1)
      degree.set(edge.target, (degree.get(edge.target) || 0) + 1)
    }

    // How many nodes share each label in this view. A label with hundreds of
    // instances is a leaf type; one with a handful is a hub type.
    const perLabel = new Map()
    for (const node of data.nodes) {
      perLabel.set(node.label, (perLabel.get(node.label) || 0) + 1)
    }

    const maxDegree = Math.max(1, ...degree.values())

    const nodeElements = data.nodes
      // A node whose edges all fell outside the sample would otherwise be
      // parked in an orphan grid off to one side, which reads as a bug.
      .filter((n) => degree.has(n.id) || data.edges.length === 0)
      .map((n) => {
        const d = degree.get(n.id) || 0
        const isHub = (perLabel.get(n.label) || 0) <= LABEL_CARDINALITY_LIMIT
        const name = displayName(n)
        return {
          data: {
            id: n.id,
            name,
            display: isHub ? name : '',
            label: n.label,
            color: colorFor(n.label, labels),
            props: n.props,
            // Square-root scaling: linear makes a degree-180 hub swamp the canvas.
            size: 14 + 34 * Math.sqrt(d / maxDegree),
            fontSize: isHub ? 12 : 9,
            fontWeight: isHub ? 700 : 400,
            zIndex: isHub ? 100 : 1,
          },
        }
      })

    const kept = new Set(nodeElements.map((n) => n.data.id))
    const showEdgeLabels = edges.length <= EDGE_LABEL_LIMIT

    const edgeElements = edges
      .filter((e) => kept.has(e.source) && kept.has(e.target))
      .map((e) => ({
        data: {
          id: `${e.source}-${e.type}-${e.target}`,
          source: e.source,
          target: e.target,
          type: e.type,
          display: showEdgeLabels ? e.type : '',
        },
      }))

    cy.batch(() => {
      cy.elements().remove()
      cy.add([...nodeElements, ...edgeElements])
    })
    cy.layout(LAYOUT).run()
  }, [data, labels])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return

    cy.elements().removeClass('highlighted dimmed')
    if (!highlight?.length) return

    const wanted = new Set(highlight)
    const matched = cy.nodes().filter((node) => {
      const props = node.data('props') || {}
      // Match on identity only — the node's key, or the label shown on screen.
      // Matching any property lights up every node that merely shares an
      // attribute with the answer: ask about two distributors and every node
      // in the same city joins in, which reads as the answer being wrong.
      const keyProp = keyByLabel[node.data('label')]
      const identity = [
        keyProp ? props[keyProp] : null,
        node.data('name'),
      ]
      return identity.some((v) => typeof v === 'string' && wanted.has(v.trim()))
    })
    if (!matched.length) return

    // Dimming the rest is what makes an answer legible on a dense graph —
    // highlighting alone just adds more colour to an already busy canvas.
    const neighbourhood = matched.union(matched.connectedEdges())
    cy.elements().difference(neighbourhood).addClass('dimmed')
    matched.addClass('highlighted')
    matched.connectedEdges().addClass('highlighted')

    cy.animate({ fit: { eles: neighbourhood, padding: 90 } }, { duration: 450 })
  }, [highlight])

  return <div id="cy" ref={containerRef} />
}
