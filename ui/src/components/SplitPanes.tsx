import React, { useRef, useState } from "react";
import { X } from "lucide-react";
import { DEFAULT_RATIO, MIN_PANE_REM, clampRatio, type PaneSide } from "../panes";
import type { TabKind } from "../tabs";
import { useUiScale } from "../uiScale";
import { IconButton } from "./ui/IconButton";
import { KIND_ICON, KIND_NAME } from "./TabBar";

/**
 * Two panes side by side, and the divider between them (`docs/09` §12).
 *
 * A view, like the drawers: what each pane holds, the ratio and which pane has the focus come in, and every press goes
 * out as a call. What goes in a pane is the App's business, and which two tabs may share the column is
 * `panes.ts`'s.
 *
 * A **grid**, not two widths: `minmax(22rem, Nfr)` on each side lets the browser hold both panes at their minimum
 * however the ratio and the window disagree, so there is no state in which a pane is drawn too narrow to read.
 *
 * The divider is a DOM element and may be, because no native view is ever in the centre while a split shows: a browser
 * page cannot be in one (`panes.ts`), which is what makes a drag safe. It holds the pointer for the drag, so the moves
 * reach it wherever the pointer is, and is a separator a keyboard can drive (arrows, Home, End; a double-click puts it
 * back in the middle), as the splitter pattern asks. The position is reported as it moves and again, as final, when
 * it is let go, so the caller can keep it in state while dragging and remember it once.
 */

/** One arrow press, as a share of the row. */
const STEP = 0.05;

/** The width of the divider's grab area, in rem. */
const DIVIDER_REM = 0.375;

export interface SplitPanesProps {
  left: React.ReactNode;
  right: React.ReactNode;
  leftTitle: string;
  rightTitle: string;
  leftKind: TabKind;
  rightKind: TabKind;
  /** The pane the person is working in. */
  focused: PaneSide;
  /** The left pane's share of the row. Kept inside what both panes need before it is used. */
  ratio: number;
  /** A pane was used (pressed in, or focus arrived in it). Called each time; the caller makes it idempotent. */
  onFocusPane: (side: PaneSide) => void;
  /** The divider moved. `commit` is false while it is being dragged and true when the position is final. */
  onRatioChange: (ratio: number, commit: boolean) => void;
  onCloseSplit: () => void;
}

const PaneFrame: React.FC<{
  side: PaneSide;
  kind: TabKind;
  title: string;
  focused: boolean;
  onFocusPane: (side: PaneSide) => void;
  onCloseSplit: () => void;
  children: React.ReactNode;
}> = ({ side, kind, title, focused, onFocusPane, onCloseSplit, children }) => {
  const Icon = KIND_ICON[kind];
  return (
    <div
      role="group"
      aria-label={`${KIND_NAME[kind]}: ${title}`}
      data-pane={side}
      data-focused={String(focused)}
      onPointerDownCapture={() => onFocusPane(side)}
      onFocusCapture={() => onFocusPane(side)}
      className={
        "flex min-h-0 min-w-0 flex-col border-t-2 " + (focused ? "border-codify-accent" : "border-transparent")
      }
    >
      <div className="flex items-center gap-1.5 border-b border-codify-border bg-codify-surface px-2 py-1 text-xs">
        <Icon className={"h-3 w-3 flex-shrink-0 " + (focused ? "text-codify-accent" : "text-codify-muted")} />
        <span
          title={title}
          className={"min-w-0 flex-1 truncate " + (focused ? "text-codify-primary" : "text-codify-muted")}
        >
          {title}
        </span>
        <IconButton label="Close split" title="Close split (Ctrl+.)" onClick={onCloseSplit} className="!h-5 !w-5">
          <X className="h-3 w-3" />
        </IconButton>
      </div>
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </div>
  );
};

export const SplitPanes: React.FC<SplitPanesProps> = ({
  left,
  right,
  leftTitle,
  rightTitle,
  leftKind,
  rightKind,
  focused,
  ratio,
  onFocusPane,
  onRatioChange,
  onCloseSplit,
}) => {
  const rowRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const lastRatio = useRef(ratio);
  const [engaged, setEngaged] = useState(false);
  const scale = useUiScale();
  const rootPx = (16 * scale) / 100;
  const rowPx = (): number => rowRef.current?.getBoundingClientRect().width ?? 0;
  const clamp = (r: number): number => clampRatio(r, rowPx(), rootPx);

  const move = (r: number, commit: boolean): void => {
    lastRatio.current = r;
    onRatioChange(r, commit);
  };
  const end = (): void => {
    if (!dragging.current) return;
    dragging.current = false;
    setEngaged(false);
    onRatioChange(lastRatio.current, true);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const target =
      event.key === "ArrowLeft"
        ? ratio - STEP
        : event.key === "ArrowRight"
          ? ratio + STEP
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? 1
              : null;
    if (target === null) return;
    event.preventDefault();
    move(clamp(target), true);
  };

  return (
    <div
      ref={rowRef}
      className="grid min-h-0 min-w-0 flex-1"
      style={{
        gridTemplateColumns: `minmax(${MIN_PANE_REM}rem, ${ratio}fr) ${DIVIDER_REM}rem minmax(${MIN_PANE_REM}rem, ${1 - ratio}fr)`,
        gridTemplateRows: "minmax(0, 1fr)",
      }}
    >
      <PaneFrame side={0} kind={leftKind} title={leftTitle} focused={focused === 0} onFocusPane={onFocusPane} onCloseSplit={onCloseSplit}>
        {left}
      </PaneFrame>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize panes"
        aria-valuenow={Math.round(ratio * 100)}
        aria-valuemin={Math.round(clamp(0) * 100)}
        aria-valuemax={Math.round(clamp(1) * 100)}
        tabIndex={0}
        title="Drag to resize; double-click to even out"
        onPointerDown={(event) => {
          dragging.current = true;
          lastRatio.current = ratio;
          setEngaged(true);
          // Hold the pointer, so the moves reach this element wherever it goes (a terminal's canvas under it
          // would otherwise take them). Absent where the platform has no pointer capture.
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (!dragging.current) return;
          const box = rowRef.current?.getBoundingClientRect();
          if (!box || !(box.width > 0)) return;
          move(clamp((event.clientX - box.left) / box.width), false);
        }}
        onPointerUp={end}
        onPointerCancel={end}
        onLostPointerCapture={end}
        onDoubleClick={() => move(DEFAULT_RATIO, true)}
        onKeyDown={onKeyDown}
        onFocus={() => setEngaged(true)}
        onBlur={() => setEngaged(dragging.current)}
        className={
          "touch-none select-none cursor-col-resize transition-colors " +
          (engaged ? "bg-codify-accent" : "bg-codify-border")
        }
      />
      <PaneFrame side={1} kind={rightKind} title={rightTitle} focused={focused === 1} onFocusPane={onFocusPane} onCloseSplit={onCloseSplit}>
        {right}
      </PaneFrame>
    </div>
  );
};
