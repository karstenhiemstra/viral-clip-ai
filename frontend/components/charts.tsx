"use client";

/**
 * Minimal, dependency-free SVG charts for the dark dashboard.
 * Data-viz rules applied: one series = one colour (validated slot-1 blue on the dark surface),
 * thin marks (<=24px columns, 4px rounded data-end, square baseline), hairline solid grid,
 * text in ink tokens (never the series colour), per-mark hover/focus tooltips with a hit area
 * larger than the mark, and 2px surface rings on scatter dots.
 */

import { type ReactNode, useEffect, useRef, useState } from "react";

const SERIES = "#3987e5"; // slot 1 (dark), validated against #111118
const SERIES_HOVER = "#6da7ec";
const SURFACE = "#111118";
const GRID = "#262633";
const TICK = "#8a8aa0";

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}

function Tooltip({ x, y, children, width }: { x: number; y: number; children: ReactNode; width: number }) {
  const left = Math.min(Math.max(8, x - 90), Math.max(8, width - 188));
  return (
    <div
      className="pointer-events-none absolute z-10 w-[180px] rounded-lg border border-line-2 bg-panel-3 px-3 py-2 text-xs shadow-xl"
      style={{ left, top: Math.max(0, y - 70) }}
    >
      {children}
    </div>
  );
}

export interface ColumnDatum {
  label: string;
  value: number | null;
  detail?: string;
}

export function ColumnChart({
  data,
  format,
  height = 220,
  capLabels = true,
  ariaLabel,
}: {
  data: ColumnDatum[];
  format: (v: number) => string;
  height?: number;
  capLabels?: boolean;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const padL = 44, padR = 8, padT = 22, padB = 28;
  const innerW = Math.max(0, width - padL - padR);
  const innerH = height - padT - padB;
  const max = niceMax(Math.max(0, ...data.map((d) => d.value ?? 0)));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
  const band = data.length ? innerW / data.length : 0;
  const barW = Math.max(4, Math.min(24, band * 0.55));
  const y = (v: number) => padT + innerH - (v / max) * innerH;
  const every = Math.max(1, Math.ceil(data.length / Math.max(1, Math.floor(innerW / 56))));

  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={ariaLabel}>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={padL} x2={width - padR} y1={y(t)} y2={y(t)} stroke={GRID} strokeWidth={1} />
              <text x={padL - 8} y={y(t)} dy="0.32em" textAnchor="end" fontSize={10} fill={TICK} className="tabular-nums">
                {format(t)}
              </text>
            </g>
          ))}
          {data.map((d, i) => {
            const cx = padL + band * i + band / 2;
            const v = d.value ?? 0;
            const top = y(v);
            const h = padT + innerH - top;
            const r = Math.min(4, h);
            const x0 = cx - barW / 2;
            const x1 = cx + barW / 2;
            const base = padT + innerH;
            const path = h > 0 ? `M${x0},${base} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x1 - r} Q${x1},${top} ${x1},${top + r} V${base} Z` : "";
            return (
              <g key={d.label}>
                {path && <path d={path} fill={hover === i ? SERIES_HOVER : SERIES} />}
                {capLabels && d.value != null && (
                  <text x={cx} y={top - 6} textAnchor="middle" fontSize={11} fill="#c9c9d6" className="tabular-nums">
                    {format(d.value)}
                  </text>
                )}
                {i % every === 0 && (
                  <text x={cx} y={height - 8} textAnchor="middle" fontSize={10} fill={TICK}>
                    {d.label}
                  </text>
                )}
                <rect
                  x={padL + band * i}
                  y={padT}
                  width={band}
                  height={innerH}
                  fill="transparent"
                  tabIndex={0}
                  aria-label={`${d.label}: ${d.value == null ? "geen data" : format(d.value)}`}
                  onPointerEnter={() => setHover(i)}
                  onPointerLeave={() => setHover(null)}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                  className="outline-none"
                />
              </g>
            );
          })}
        </svg>
      )}
      {hover !== null && data[hover] && (
        <Tooltip x={padL + band * hover + band / 2} y={y(data[hover].value ?? 0)} width={width}>
          <p className="text-sm font-semibold text-ink tabular-nums">{data[hover].value == null ? "—" : format(data[hover].value!)}</p>
          <p className="text-muted">{data[hover].label}</p>
          {data[hover].detail && <p className="mt-0.5 text-muted">{data[hover].detail}</p>}
        </Tooltip>
      )}
    </div>
  );
}

export interface ScatterPoint {
  x: number;
  y: number;
  title: string;
  sub?: string;
  href?: string;
}

export function ScatterChart({
  points,
  xLabel,
  yLabel,
  formatY,
  height = 280,
  ariaLabel,
}: {
  points: ScatterPoint[];
  xLabel: string;
  yLabel: string;
  formatY: (v: number) => string;
  height?: number;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const padL = 52, padR = 12, padT = 12, padB = 36;
  const innerW = Math.max(0, width - padL - padR);
  const innerH = height - padT - padB;
  const ys = points.map((p) => Math.max(1, p.y));
  const lo = Math.floor(Math.log10(Math.min(...ys, 10)));
  const hi = Math.max(lo + 1, Math.ceil(Math.log10(Math.max(...ys, 10))));
  const sx = (v: number) => padL + (v / 100) * innerW;
  const sy = (v: number) => padT + innerH - ((Math.log10(Math.max(1, v)) - lo) / (hi - lo)) * innerH;
  const yTicks = Array.from({ length: hi - lo + 1 }, (_, i) => Math.pow(10, lo + i));

  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={ariaLabel}>
          {yTicks.map((t) => (
            <g key={t}>
              <line x1={padL} x2={width - padR} y1={sy(t)} y2={sy(t)} stroke={GRID} />
              <text x={padL - 8} y={sy(t)} dy="0.32em" textAnchor="end" fontSize={10} fill={TICK}>{formatY(t)}</text>
            </g>
          ))}
          {[0, 25, 50, 75, 100].map((t) => (
            <g key={t}>
              <line x1={sx(t)} x2={sx(t)} y1={padT} y2={padT + innerH} stroke={GRID} />
              <text x={sx(t)} y={height - 18} textAnchor="middle" fontSize={10} fill={TICK}>{t}</text>
            </g>
          ))}
          <text x={padL + innerW / 2} y={height - 3} textAnchor="middle" fontSize={10} fill={TICK}>{xLabel}</text>
          <text transform={`translate(11 ${padT + innerH / 2}) rotate(-90)`} textAnchor="middle" fontSize={10} fill={TICK}>{yLabel}</text>
          {points.map((p, i) => (
            <g key={i}>
              <circle cx={sx(p.x)} cy={sy(p.y)} r={hover === i ? 6 : 4.5} fill={hover === i ? SERIES_HOVER : SERIES} stroke={SURFACE} strokeWidth={2} />
              <circle
                cx={sx(p.x)}
                cy={sy(p.y)}
                r={12}
                fill="transparent"
                tabIndex={0}
                aria-label={`${p.title}: score ${Math.round(p.x)}, ${formatY(p.y)}`}
                onPointerEnter={() => setHover(i)}
                onPointerLeave={() => setHover(null)}
                onFocus={() => setHover(i)}
                onBlur={() => setHover(null)}
                onClick={() => p.href && (window.location.href = p.href)}
                className="cursor-pointer outline-none"
              />
            </g>
          ))}
        </svg>
      )}
      {hover !== null && points[hover] && (
        <Tooltip x={sx(points[hover].x)} y={sy(points[hover].y)} width={width}>
          <p className="text-sm font-semibold text-ink tabular-nums">{formatY(points[hover].y)}</p>
          <p className="line-clamp-2 text-ink-2">{points[hover].title}</p>
          <p className="text-muted">Viral Score {Math.round(points[hover].x)}{points[hover].sub ? ` · ${points[hover].sub}` : ""}</p>
        </Tooltip>
      )}
    </div>
  );
}
